"""Real SQLite joint CAS/reserve/begin fixtures; never actual broker proof."""
from copy import deepcopy
from dataclasses import replace
import json
from pathlib import Path
import tempfile
import sqlite3
import threading
import unittest
from unittest.mock import patch

from trade_safety_kit import execution_binding as b
from trade_safety_kit.execution_ledger import Ledger, LedgerError
from trade_safety_kit.execution_gateway import Gateway, GatewayError, FixturePermit, FixtureEvidenceCAS, CAS_PROTOCOL, EVIDENCE_NAMESPACE, CLAIM_NAMESPACE
from tests import execution_phase1_fixtures as f
from tests import execution_gateway_fixtures as g


class GatewayTests(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/"gateway.sqlite";self.p=f.policy(("A","B"));self.accounts=[f.account("A"),f.account("B")]
        self.ledger=self.open();self.addCleanup(lambda:self.ledger.close())
        for name in self.p["accounts"]:self.ledger.observe(f.snapshot(self.p,name),now=f.NOW)
        self.gateway=Gateway(self.ledger,enabled=True);self.cas=FixtureEvidenceCAS()

    def open(self):return Ledger(self.path,policy=self.p,accounts=self.accounts,enabled=True)

    def request(self,identity="one",**kwargs):
        value=g.payload(self.ledger,f.intent(self.p,identity,**kwargs));self.gateway.record_fixture_evidence(value["evidence"]);return value

    def prepare(self,value,**kwargs):
        return self.gateway.prepare(value,expected_payload_hash=b.digest(value),cas_join=kwargs.pop("cas_join",self.cas),now=f.NOW,**kwargs)

    def counts(self):
        return tuple(self.ledger.db.execute("SELECT count(*) FROM "+table).fetchone()[0] for table in ("entries","gateway_preparations"))+ (self.ledger.db.execute("SELECT count(*) FROM shared_heads WHERE namespace=?",(CLAIM_NAMESPACE,)).fetchone()[0],)

    def test_default_off_and_native_profile_are_explicitly_blocked(self):
        with self.assertRaisesRegex(GatewayError,"disabled"):Gateway(self.ledger)
        value=self.request();value["cas_profile"]="native-assessment-join/v1"
        with self.assertRaisesRegex(b.BindingError,"durable_native_CAS_join_unavailable"):self.prepare(value)
        self.assertEqual(self.counts(),(0,0,0))

    def test_missing_CAS_and_assertion_only_CAS_cannot_prepare(self):
        value=self.request()
        with self.assertRaisesRegex(GatewayError,"CAS_join_required"):self.prepare(value,cas_join=None)
        class AssertionOnly:
            protocol=CAS_PROTOCOL;scope=b.FIXTURE_SCOPE
            def claim(self,*_args):return True
        with self.assertRaisesRegex(LedgerError,"CAS_join_missing"):self.prepare(value,cas_join=AssertionOnly())
        self.assertEqual(self.counts(),(0,0,0))

    def test_joint_connection_is_invisible_until_owner_commit(self):
        value=self.request();observer=self.open();self.addCleanup(observer.close)
        with self.ledger.atomic() as transaction:
            connection_id=transaction.connection_identity
            pending=self.gateway.prepare_in_transaction(value,expected_payload_hash=b.digest(value),cas_join=self.cas,now=f.NOW,transaction=transaction)
            self.assertEqual(connection_id,id(self.ledger.db));self.assertEqual(self.counts(),(1,1,1))
            self.assertEqual(observer.db.execute("SELECT count(*) FROM entries").fetchone()[0],0)
            with self.assertRaisesRegex(GatewayError,"not_committed"):pending.to_permit()
        permit=pending.to_permit();self.assertEqual(permit.to_dict()["CAS_transaction_id"],transaction.transaction_id)
        self.assertEqual(observer.db.execute("SELECT state FROM entries").fetchone()[0],"SUBMITTING")
        self.assertEqual(observer.db.execute("SELECT count(*) FROM gateway_preparations").fetchone()[0],1)

    def test_fixture_permit_is_never_live_or_dispatch_authority(self):
        permit=self.prepare(self.request());value=permit.to_dict()
        self.assertIs(value["live_authority"],False);self.assertIs(value["dispatch_allowed"],False)
        with self.assertRaisesRegex(GatewayError,"never_live_authority"):permit.assert_live_authority()
        with self.assertRaises(GatewayError):FixturePermit(b.canonical({**value,"live_authority":True})).to_dict()

    def test_exact_payload_hash_account_policy_and_evidence_revisions(self):
        value=self.request()
        with self.assertRaisesRegex(GatewayError,"payload_hash_conflict"):
            self.gateway.prepare(value,expected_payload_hash="0"*64,cas_join=self.cas,now=f.NOW)
        for field,changed in (("account_version",True),("policy_revision",True),("account_version",99),("policy_revision",99),("evidence_revision",2),("evidence_sha256","0"*64)):
            altered=deepcopy(value);altered["evidence"][field]=changed
            with self.subTest(field=field),self.assertRaises((GatewayError,b.BindingError)):self.prepare(altered)
        self.assertEqual(self.counts(),(0,0,0))

    def test_global_account_snapshot_and_position_revisions_are_exact(self):
        value=self.request();self.ledger.observe(f.snapshot(self.p,"B",revision=2),now=f.NOW)
        with self.assertRaisesRegex(GatewayError,"account_snapshot_revision_conflict"):self.prepare(value)
        value=g.payload(self.ledger,value["intent"]);value["evidence"]["position_snapshot_revision"]=99
        with self.assertRaisesRegex(GatewayError,"position_evidence_revision_conflict"):self.prepare(value)
        self.assertEqual(self.counts(),(0,0,0))

    def test_VETO_UNKNOWN_and_new_head_invalidate_old_fixture_request(self):
        value=self.request();old=value["evidence"]
        changed={**old,"assessment_id":"new-VETO","assessment_revision":2,"expected_previous_assessment_id":old["assessment_id"],"expected_previous_assessment_revision":1,"result":"VETO"}
        self.gateway.record_fixture_evidence(changed)
        with self.assertRaisesRegex(GatewayError,"latest_fixture_evidence_conflict"):self.prepare(value)
        for result in ("VETO","UNKNOWN"):
            request=deepcopy(value);request["intent"].update(assessment_id=changed["assessment_id"],assessment_revision=changed["assessment_revision"]);request["evidence"]=changed
            with self.assertRaisesRegex(GatewayError,"not_PASS"):self.prepare(request)
            if result=="VETO":
                changed={**changed,"assessment_id":"new-UNKNOWN","assessment_revision":3,"expected_previous_assessment_id":changed["assessment_id"],"expected_previous_assessment_revision":2,"result":"UNKNOWN"};self.gateway.record_fixture_evidence(changed)
        self.assertEqual(self.counts(),(0,0,0))

    def test_evidence_cannot_rollback_or_change_content_at_same_revision(self):
        value=self.request();old=value["evidence"]
        for revision,evidence_revision,hash_value in ((2,0,old["evidence_sha256"]),(2,1,"0"*64)):
            changed={**old,"assessment_id":"new","assessment_revision":revision,"expected_previous_assessment_id":old["assessment_id"],"expected_previous_assessment_revision":1,"evidence_revision":evidence_revision,"evidence_sha256":hash_value}
            with self.assertRaises((GatewayError,b.BindingError)):self.gateway.record_fixture_evidence(changed)

    def test_idempotent_prepare_never_mints_another_begin_or_CAS(self):
        value=self.request();first=self.prepare(value);second=self.prepare(value)
        self.assertEqual(first,second);self.assertEqual(self.counts(),(1,1,1))
        with self.assertRaises(LedgerError):self.ledger.begin_entry("one",attempt_id="second",now=f.NOW)

    def test_same_intent_changed_payload_or_attempt_requires_new_authority(self):
        value=self.request();self.prepare(value)
        for key,changed in (("route_id","fixture-user"),("attempt_id","different-attempt")):
            with self.assertRaisesRegex(GatewayError,"prepared_payload_or_attempt_identity_conflict"):self.prepare({**value,key:changed})
        changed=deepcopy(value);changed["intent"]["quantity"]+=1;changed["intent"]["max_execution_krw"]+=changed["intent"]["max_unit_execution_krw"]
        with self.assertRaisesRegex(GatewayError,"prepared_payload_or_attempt_identity_conflict"):self.prepare(changed)
        self.assertEqual(self.counts(),(1,1,1))

    def test_restart_timeout_unknown_and_expiry_retain_attempted_claim(self):
        value=self.request();permit=self.prepare(value);self.ledger.mark_unknown(value["attempt_id"])
        reopened=self.open();self.addCleanup(reopened.close);gateway=Gateway(reopened,enabled=True)
        again=gateway.prepare(value,expected_payload_hash=b.digest(value),cas_join=FixtureEvidenceCAS(),now=f.NOW)
        self.assertEqual(again,permit);self.assertEqual(reopened.db.execute("SELECT state FROM entries").fetchone()[0],"SUBMISSION_UNKNOWN")
        with self.assertRaises(LedgerError):reopened.cancel_unsent("one")
        with self.assertRaises((GatewayError,LedgerError,b.BindingError)):
            gateway.prepare(value,expected_payload_hash=b.digest(value),cas_join=FixtureEvidenceCAS(),now=6000)

    def test_CAS_callback_failure_rolls_back_real_SQL_state(self):
        value=self.request();base=self.cas
        class FailAfterCAS:
            protocol=CAS_PROTOCOL;scope=b.FIXTURE_SCOPE
            def claim(self,transaction,payload,payload_hash):base.claim(transaction,payload,payload_hash);raise RuntimeError("fixture injected after CAS")
        with self.assertRaises(RuntimeError):self.prepare(value,cas_join=FailAfterCAS())
        self.assertEqual(self.counts(),(0,0,0))

    def test_caught_join_failure_cannot_be_committed_by_outer_owner(self):
        value=self.request();base=self.cas
        class FailAfterCAS:
            protocol=CAS_PROTOCOL;scope=b.FIXTURE_SCOPE
            def claim(self,transaction,payload,payload_hash):base.claim(transaction,payload,payload_hash);raise RuntimeError("fixture after CAS")
        with self.assertRaisesRegex(LedgerError,"rollback_only"):
            with self.ledger.atomic() as transaction:
                try:
                    self.gateway.prepare_in_transaction(value,expected_payload_hash=b.digest(value),cas_join=FailAfterCAS(),now=f.NOW,transaction=transaction)
                except RuntimeError:pass
                with self.assertRaisesRegex(LedgerError,"rollback_only"):transaction.execute("SELECT 1")
        self.assertEqual(transaction.state,"ROLLED_BACK");self.assertEqual(self.counts(),(0,0,0))

    def test_failure_after_reservation_or_SUBMITTING_rolls_back_all_claims(self):
        value=self.request()
        with patch.object(self.ledger,"begin_entry",side_effect=RuntimeError("fixture injected before BEGIN state")):
            with self.assertRaises(RuntimeError):self.prepare(value)
        self.assertEqual(self.counts(),(0,0,0))
        canonical=b.canonical
        def fail_on_permit(data):
            if isinstance(data,dict) and data.get("schema")=="execution-gateway-permit/fixture-v1":raise RuntimeError("fixture injected after SUBMITTING")
            return canonical(data)
        with patch.object(b,"canonical",side_effect=fail_on_permit):
            with self.assertRaises(RuntimeError):self.prepare(value)
        self.assertEqual(self.counts(),(0,0,0))

    def test_foreign_database_receipt_is_not_joined_atomicity(self):
        value=self.request();other=Ledger(Path(self.tmp.name)/"foreign.sqlite",policy=self.p,accounts=self.accounts,enabled=True);self.addCleanup(other.close)
        class ForeignCAS:
            protocol=CAS_PROTOCOL;scope=b.FIXTURE_SCOPE
            def claim(self,transaction,payload,payload_hash):
                with other.atomic() as foreign:
                    content={"request_payload_sha256":payload_hash}
                    return foreign.cas_head(namespace="foreign-fixture",head_key="one",expected_head_id=None,expected_revision=0,head_id="foreign",revision=1,payload=content,expected_payload_hash=b.digest(content))
        with self.assertRaisesRegex(LedgerError,"foreign_transaction"):self.prepare(value,cas_join=ForeignCAS())
        self.assertEqual(self.counts(),(0,0,0))

    def test_foreign_expired_nested_and_wrong_thread_handles_are_rejected(self):
        value=self.request();other=self.open();self.addCleanup(other.close)
        with other.atomic() as foreign:
            with self.assertRaisesRegex(LedgerError,"foreign"):
                self.gateway.prepare_in_transaction(value,expected_payload_hash=b.digest(value),cas_join=self.cas,now=f.NOW,transaction=foreign)
        with self.ledger.atomic() as transaction:
            with self.assertRaisesRegex(LedgerError,"nested"):self.prepare(value)
            with self.assertRaises(AttributeError):transaction.state="COMMITTED"
            outcomes=[]
            def wrong_thread():
                try:transaction.check()
                except LedgerError:outcomes.append("blocked")
            worker=threading.Thread(target=wrong_thread);worker.start();worker.join(5);self.assertEqual(outcomes,["blocked"])
        with self.assertRaises(LedgerError):transaction.read_head("fixture","x")
        self.assertEqual(self.counts(),(0,0,0))

    def test_borrowed_SQL_cannot_commit_attach_or_modify_core_tables(self):
        with self.ledger.atomic() as transaction:
            for sql in ("COMMIT","ROLLBACK","SAVEPOINT other","ATTACH DATABASE ':memory:' AS other","DELETE FROM entries","CREATE INDEX wrong ON entries(intent_id)","PRAGMA writable_schema=ON"):
                with self.subTest(sql=sql),self.assertRaisesRegex(LedgerError,"borrowed_sql_rejected"):transaction.execute(sql)
            cursor=transaction.execute("SELECT count(*) FROM entries");self.assertEqual(cursor.fetchone()[0],0)
            with self.assertRaises(AttributeError):getattr(cursor,"connection")
        with self.assertRaises(LedgerError):cursor.fetchone()

    def test_CAS_head_versions_and_hashes_are_durable_exact_and_rollback_safe(self):
        with self.ledger.atomic() as transaction:
            payload={"fixture":1}
            receipt=transaction.cas_head(namespace="native-owner-fixture",head_key="scope",expected_head_id=None,expected_revision=0,head_id="first",revision=1,payload=payload,expected_payload_hash=b.digest(payload))
            self.assertEqual(receipt.connection_identity,id(self.ledger.db))
            for predecessor,revision,hash_value in (("wrong",2,b.digest(payload)),("first",1,b.digest(payload)),("first",2,"0"*64)):
                with self.assertRaises(LedgerError):transaction.cas_head(namespace="native-owner-fixture",head_key="scope",expected_head_id=predecessor,expected_revision=1,head_id="next",revision=revision,payload=payload,expected_payload_hash=hash_value)
        with self.ledger.atomic() as transaction:self.assertEqual(transaction.read_head("native-owner-fixture","scope")["head_id"],"first")

    def test_unknown_possible_orders_and_quarantines_survive_gateway_failure(self):
        value=self.request();order=dict(order_id="external",market="us",instrument_id="MSFT",asset_class=None,side=None,known=False,remaining_qty=None,remaining_max_krw=None)
        self.ledger.observe(f.snapshot(self.p,revision=2,orders=[order]),now=f.NOW)
        value=g.payload(self.ledger,value["intent"]);value["evidence"].update(assessment_id="fresh",assessment_revision=2,expected_previous_assessment_id="fixture-assessment:one",expected_previous_assessment_revision=1)
        value["intent"].update(assessment_id="fresh",assessment_revision=2);self.gateway.record_fixture_evidence(value["evidence"])
        with self.assertRaisesRegex(LedgerError,"capacity_unknown"):self.prepare(value)
        self.assertEqual(self.counts(),(0,0,0));self.assertEqual(self.ledger.db.execute("SELECT count(*) FROM possible_orders").fetchone()[0],1)

    def test_gateway_does_not_invent_policy_values_or_accept_mutated_config(self):
        value=self.request();self.ledger.policy["max_occupied"]=99
        with self.assertRaises((GatewayError,b.BindingError)):self.prepare(value)
        self.assertEqual(self.counts(),(0,0,0))

    def test_old_or_native_database_is_rejected_without_migration(self):
        for name,table in (("old-fixture","config"),("native","t1_proposals")):
            path=Path(self.tmp.name)/(name+".sqlite")
            connection=sqlite3.connect(path);connection.execute("CREATE TABLE "+table+"(fixture TEXT)");connection.commit();connection.close()
            before=path.read_bytes()
            with self.assertRaisesRegex(LedgerError,"existing_database_schema_unsupported"):
                Ledger(path,policy=self.p,accounts=self.accounts,enabled=True)
            self.assertEqual(path.read_bytes(),before)

    def test_account_scope_separates_owned_claims_and_cannot_be_rebound(self):
        first=self.request("first",name="A");second=self.request("second",name="B")
        self.prepare(first);self.prepare(second)
        self.assertEqual(self.counts(),(2,2,2))
        rebound=deepcopy(first);rebound["evidence"]["account_id"]="B"
        with self.assertRaises(b.BindingError):self.prepare(rebound)
        self.assertEqual(self.counts(),(2,2,2))


if __name__=="__main__":unittest.main()
