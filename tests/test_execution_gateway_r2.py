"""Public-API R2 regressions with real offline SQLite; no broker proof."""
from copy import deepcopy
from dataclasses import replace
import json
import unittest

from trade_safety_kit import execution_binding as b
from trade_safety_kit.execution_ledger import LedgerError
from trade_safety_kit.execution_gateway import Gateway, GatewayError, CLAIM_NAMESPACE, EVIDENCE_NAMESPACE
from tests import execution_phase1_fixtures as f
from tests import test_execution_gateway as original


class GatewayR2Tests(unittest.TestCase):
    setUp=original.GatewayTests.setUp
    open=original.GatewayTests.open
    request=original.GatewayTests.request
    prepare=original.GatewayTests.prepare
    counts=original.GatewayTests.counts

    def pending(self,value,transaction):
        return self.gateway.prepare_in_transaction(value,expected_payload_hash=b.digest(value),
            cas_join=self.cas,now=f.NOW,transaction=transaction)

    def assert_reopened_counts(self,expected):
        reopened=self.open()
        try:
            actual=tuple(reopened.db.execute("SELECT count(*) FROM "+table).fetchone()[0]
                for table in ("entries","gateway_preparations"))+(reopened.db.execute(
                    "SELECT count(*) FROM shared_heads WHERE namespace=?",(CLAIM_NAMESPACE,)).fetchone()[0],)
            self.assertEqual(actual,expected)
        finally:reopened.close()

    def test_caught_invalid_quantity_after_CAS_forces_owner_rollback(self):
        value=self.request();invalid={**value["intent"],"quantity":0}
        with self.assertRaisesRegex(LedgerError,"rollback_only"):
            with self.ledger.atomic() as transaction:
                self.cas.claim(transaction,value,b.digest(value))
                try:self.ledger.reserve(invalid,now=f.NOW,transaction=transaction)
                except b.BindingError:pass
        self.assertEqual(transaction.state,"ROLLED_BACK")
        self.assertEqual(self.counts(),(0,0,0));self.assert_reopened_counts((0,0,0))
        self.assertEqual(self.prepare(value).to_dict()["attempt_id"],value["attempt_id"])

    def test_caught_empty_attempt_after_reservation_forces_owner_rollback(self):
        value=self.request()
        with self.assertRaisesRegex(LedgerError,"rollback_only"):
            with self.ledger.atomic() as transaction:
                self.cas.claim(transaction,value,b.digest(value))
                self.ledger.reserve(value["intent"],now=f.NOW,transaction=transaction)
                try:self.ledger.begin_entry("one",attempt_id="",now=f.NOW,transaction=transaction)
                except b.BindingError:pass
        self.assertEqual(transaction.state,"ROLLED_BACK")
        self.assertEqual(self.counts(),(0,0,0));self.assert_reopened_counts((0,0,0))
        self.prepare(value);self.assert_reopened_counts((1,1,1))

    def test_deleted_CAS_after_prepare_cannot_finalize(self):
        value=self.request()
        with self.ledger.atomic() as transaction:
            pending=self.pending(value,transaction)
            transaction.execute("DELETE FROM shared_heads WHERE namespace=? AND head_key=?",
                (CLAIM_NAMESPACE,"one"))
        with self.assertRaisesRegex(GatewayError,"committed_fixture_CAS_conflict"):pending.to_permit()
        # Lost proof cannot free the already attempted order or mint a retry.
        self.assert_reopened_counts((1,1,0))
        with self.assertRaises(LedgerError):self.ledger.cancel_unsent("one")
        with self.assertRaises((GatewayError,LedgerError)):self.prepare(value)

    def test_same_lease_PASS_to_VETO_cannot_finalize_old_pending(self):
        value=self.request();previous=value["evidence"]
        changed={**previous,"assessment_id":"r2-VETO","assessment_revision":2,
            "expected_previous_assessment_id":previous["assessment_id"],
            "expected_previous_assessment_revision":1,"result":"VETO"}
        with self.ledger.atomic() as transaction:
            pending=self.pending(value,transaction)
            transaction.cas_head(namespace=EVIDENCE_NAMESPACE,head_key=b.gateway_evidence_key(previous),
                expected_head_id=previous["assessment_id"],expected_revision=1,head_id=changed["assessment_id"],
                revision=2,payload=changed,expected_payload_hash=b.digest(changed))
        with self.assertRaisesRegex(GatewayError,"latest_fixture_evidence_conflict"):pending.to_permit()
        with self.ledger.atomic() as transaction:
            self.assertEqual(transaction.read_head(EVIDENCE_NAMESPACE,b.gateway_evidence_key(previous))["payload"]["result"],"VETO")
        self.assert_reopened_counts((1,1,1))

    def test_other_prevalidation_errors_poison_prior_writes(self):
        for identity,kind in (("bad-now","now"),("bad-shape","shape"),("bad-entry-now","entry-now"),("bad-entry-id","entry-id")):
            value=self.request(identity)
            with self.subTest(kind=kind),self.assertRaisesRegex(LedgerError,"rollback_only"):
                with self.ledger.atomic() as transaction:
                    self.cas.claim(transaction,value,b.digest(value))
                    try:
                        if kind=="now":self.ledger.reserve(value["intent"],now=True,transaction=transaction)
                        elif kind=="shape":self.ledger.reserve({},now=f.NOW,transaction=transaction)
                        elif kind=="entry-now":self.ledger.begin_entry(identity,attempt_id="valid",now=None,transaction=transaction)
                        else:self.ledger.begin_entry("",attempt_id="valid",now=f.NOW,transaction=transaction)
                    except (b.BindingError,KeyError):pass
            self.assertEqual(self.counts(),(0,0,0))
        self.assert_reopened_counts((0,0,0))

    def test_valid_join_finalizes_and_exact_recovery_survives_reopen(self):
        value=self.request()
        with self.ledger.atomic() as transaction:
            pending=self.pending(value,transaction)
            with self.assertRaisesRegex(GatewayError,"not_committed"):pending.to_permit()
        permit=pending.to_permit();self.assertIs(permit.to_dict()["dispatch_allowed"],False)
        self.assert_reopened_counts((1,1,1));reopened=self.open()
        try:
            recovered=Gateway(reopened,enabled=True).prepare(value,
                expected_payload_hash=b.digest(value),cas_join=self.cas,now=f.NOW)
            self.assertEqual(recovered,permit)
        finally:reopened.close()

    def test_other_committed_lease_deleted_claim_cannot_finalize(self):
        value=self.request()
        with self.ledger.atomic() as transaction:pending=self.pending(value,transaction)
        other=self.open()
        try:
            with other.atomic() as transaction:
                transaction.execute("DELETE FROM shared_heads WHERE namespace=? AND head_key=?",(CLAIM_NAMESPACE,"one"))
        finally:other.close()
        with self.assertRaisesRegex(GatewayError,"committed_fixture_CAS_conflict"):pending.to_permit()

    def test_claim_hash_or_head_header_change_cannot_finalize(self):
        for index,field in enumerate(("payload_hash","head_id","revision")):
            value=self.request("tamper-"+field,instrument=("MSFT","AAPL","FAKEC")[index])
            with self.ledger.atomic() as transaction:
                pending=self.pending(value,transaction)
                changed=2 if field=="revision" else "0"*64
                transaction.execute("UPDATE shared_heads SET "+field+"=? WHERE namespace=? AND head_key=?",
                    (changed,CLAIM_NAMESPACE,value["intent"]["intent_id"]))
            with self.subTest(field=field),self.assertRaisesRegex(GatewayError,"committed_fixture_CAS_conflict"):pending.to_permit()

    def test_evidence_head_header_change_cannot_finalize(self):
        value=self.request()
        with self.ledger.atomic() as transaction:
            pending=self.pending(value,transaction)
            transaction.execute("UPDATE shared_heads SET head_id=? WHERE namespace=? AND head_key=?",
                ("different-head",EVIDENCE_NAMESPACE,b.gateway_evidence_key(value["evidence"])))
        with self.assertRaisesRegex(GatewayError,"latest_fixture_evidence_conflict"):pending.to_permit()

    def test_request_rebinding_and_expired_fixture_time_cannot_finalize(self):
        value=self.request()
        with self.ledger.atomic() as transaction:pending=self.pending(value,transaction)
        changed=deepcopy(value);changed["route_id"]="changed-route"
        with self.assertRaisesRegex(GatewayError,"request_payload_hash_conflict"):
            replace(pending,request_json=b.canonical(changed)).to_permit()
        with self.assertRaises((GatewayError,b.BindingError)):pending.to_permit(now=6000)
        self.assertIs(pending.to_permit().to_dict()["live_authority"],False)

    def test_actual_fixture_fill_invalidates_unfilled_pending_invariants(self):
        value=self.request()
        with self.ledger.atomic() as transaction:pending=self.pending(value,transaction)
        self.ledger.entry_receipt(f.entry_receipt(self.p,value["intent"],value["attempt_id"],qty=2))
        with self.assertRaisesRegex(GatewayError,"committed_fixture_attempt_conflict"):pending.to_permit()
        self.assertEqual(self.ledger.allocation(value["intent"]["allocation_id"])["quantity"],2)

    def test_unknown_attempt_retains_only_unchanged_non_dispatch_history(self):
        value=self.request()
        with self.ledger.atomic() as transaction:pending=self.pending(value,transaction)
        self.ledger.mark_unknown(value["attempt_id"])
        permit=pending.to_permit();self.assertIs(permit.to_dict()["dispatch_allowed"],False)
        with self.assertRaises(GatewayError):permit.assert_live_authority()
        with self.assertRaises(LedgerError):self.ledger.begin_entry("one",attempt_id="retry",now=f.NOW)
        self.assert_reopened_counts((1,1,1))


if __name__=="__main__":unittest.main()
