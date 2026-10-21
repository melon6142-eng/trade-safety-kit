"""OFFLINE permit preparation only. No dispatch, broker imports or live authority.

Only the explicit fixture CAS profile is supported. Native T1 database helpers
cannot join this transaction yet; native permit requests are blocked. All claims
use one Ledger.atomic transaction, never a second database pseudo-transaction.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
import json
import sqlite3
import threading

from trade_safety_kit import execution_binding as binding
from trade_safety_kit.execution_ledger import Ledger, LedgerError, LedgerTransaction

EVIDENCE_NAMESPACE="execution-gateway-fixture-evidence/v1"
CLAIM_NAMESPACE="execution-gateway-fixture-CAS-claims/v1"
CAS_PROTOCOL="execution-gateway-fixture-CAS/v1"


class GatewayError(ValueError):pass


@dataclass(frozen=True)
class FixturePermit:
    payload_json: str

    def to_dict(self):
        value=json.loads(self.payload_json)
        if (value.get("schema")!="execution-gateway-permit/fixture-v1" or value.get("scope")!=binding.FIXTURE_SCOPE
                or value.get("live_authority") is not False or value.get("dispatch_allowed") is not False):
            raise GatewayError("permit_fixture_scope_required")
        return value

    def assert_live_authority(self):
        raise GatewayError("fixture_permit_never_live_authority")


@dataclass(frozen=True)
class PendingFixturePermit:
    transaction: LedgerTransaction
    payload_json: str

    request_json: str
    prepared_now: int

    def to_permit(self, *, now=None):
        try:
            if (self.transaction.state!="COMMITTED" or threading.get_ident()!=self.transaction._thread
                    or self.transaction._ledger._active_transaction is not None or self.transaction._ledger.db.in_transaction):
                raise GatewayError("permit_not_committed_by_owner")
            gateway=Gateway(self.transaction._ledger,enabled=True)
            return gateway._finalize_fixture(self,now=self.prepared_now if now is None else now)
        except sqlite3.Error as exc:
            raise GatewayError("committed_fixture_ledger_unavailable") from exc


class FixtureEvidenceCAS:
    """Actual shared-connection SQLite CAS fixture, not native/broker proof."""
    protocol=CAS_PROTOCOL
    scope=binding.FIXTURE_SCOPE

    def claim(self, transaction, payload, payload_hash):
        evidence=payload["evidence"];key=binding.gateway_evidence_key(evidence)
        head=transaction.read_head(EVIDENCE_NAMESPACE,key)
        if (head is None or head["head_id"]!=evidence["assessment_id"]
                or head["revision"]!=evidence["assessment_revision"] or head["payload"]!=evidence):
            raise GatewayError("latest_fixture_evidence_conflict")
        if evidence["result"]!="PASS":raise GatewayError("fixture_evidence_not_PASS")
        claim={"schema":"execution-gateway-CAS-claim/fixture-v1","scope":binding.FIXTURE_SCOPE,
               "request_payload_sha256":payload_hash,"assessment_scope_sha256":key,
               "assessment_id":head["head_id"],"assessment_revision":head["revision"],
               "evidence_revision":evidence["evidence_revision"],"evidence_sha256":evidence["evidence_sha256"]}
        return transaction.cas_head(namespace=CLAIM_NAMESPACE,head_key=payload["intent"]["intent_id"],
            expected_head_id=None,expected_revision=0,head_id=payload_hash,revision=1,
            payload=claim,expected_payload_hash=binding.digest(claim))


class Gateway:
    def __init__(self, ledger, *, enabled=False):
        if enabled is not True:raise GatewayError("execution_gateway_disabled")
        if type(ledger) is not Ledger:raise GatewayError("fixture_Ledger_required")
        self.ledger=ledger

    def record_fixture_evidence(self, evidence):
        with self.ledger.atomic() as transaction:
            return self.record_fixture_evidence_in_transaction(evidence, transaction=transaction)

    def record_fixture_evidence_in_transaction(self, evidence, *, transaction):
        with self.ledger.transaction(transaction=transaction):
            account=self.ledger._account(evidence["account_id"])
            binding.validate_gateway_evidence(evidence,self.ledger.policy,account)
            evidence=json.loads(binding.canonical(evidence))
            old=transaction.read_head(EVIDENCE_NAMESPACE,binding.gateway_evidence_key(evidence))
            if old:
                previous=old["payload"]
                if evidence["evidence_revision"]<previous["evidence_revision"]:
                    raise GatewayError("evidence_revision_rollback")
                if (evidence["evidence_revision"]==previous["evidence_revision"]
                        and (evidence["evidence_id"],evidence["evidence_sha256"])!=(previous["evidence_id"],previous["evidence_sha256"])):
                    raise GatewayError("evidence_revision_content_conflict")
            return transaction.cas_head(namespace=EVIDENCE_NAMESPACE,head_key=binding.gateway_evidence_key(evidence),
                expected_head_id=evidence["expected_previous_assessment_id"],
                expected_revision=evidence["expected_previous_assessment_revision"],
                head_id=evidence["assessment_id"],revision=evidence["assessment_revision"],
                payload=evidence,expected_payload_hash=binding.digest(evidence))

    def prepare(self, payload, *, expected_payload_hash, cas_join, now):
        with self.ledger.atomic() as transaction:
            pending=self.prepare_in_transaction(payload,expected_payload_hash=expected_payload_hash,cas_join=cas_join,now=now,transaction=transaction)
        return pending.to_permit()

    def _validate_context(self, transaction, payload, payload_hash, now):
        transaction.check();binding.integer(now,"now",1);binding.sha(payload_hash,"request_payload_hash")
        account=self.ledger._account(payload["intent"]["account_id"])
        binding.validate_gateway_payload(payload,self.ledger.policy,account)
        if binding.digest(payload)!=payload_hash:raise GatewayError("request_payload_hash_conflict")
        config=json.loads(self.ledger.db.execute("SELECT payload FROM config WHERE singleton=1").fetchone()[0])
        if config["policy"]!=self.ledger.policy or config["accounts"]!=self.ledger.accounts:
            raise GatewayError("frozen_ledger_policy_or_account_mutated")
        for name,expected in payload["snapshot_bindings"].items():
            current=self.ledger._snapshot(name,now)
            if expected!={"snapshot_id":current["snapshot_id"],"revision":current["revision"],"snapshot_sha256":binding.digest(current)}:
                raise GatewayError("exact_account_snapshot_revision_conflict")
        selected=payload["snapshot_bindings"][account["account_id"]];evidence=payload["evidence"]
        if evidence["position_snapshot_id"]!=selected["snapshot_id"] or evidence["position_snapshot_revision"]!=selected["revision"]:
            raise GatewayError("exact_position_evidence_revision_conflict")
        head=transaction.read_head(EVIDENCE_NAMESPACE,binding.gateway_evidence_key(evidence))
        if (head is None or head["head_id"]!=evidence["assessment_id"] or head["revision"]!=evidence["assessment_revision"]
                or head["payload"]!=evidence or head["payload_hash"]!=binding.digest(evidence)):
            raise GatewayError("latest_fixture_evidence_conflict")
        if evidence["result"]!="PASS":raise GatewayError("fixture_evidence_not_PASS")

    def _finalize_fixture(self, pending, *, now):
        """Recheck one consistent committed SQL view; never dispatch authority."""
        with self.ledger.atomic() as transaction:
            return self._finalize_fixture_in_transaction(pending, now=now, transaction=transaction)

    def _finalize_fixture_in_transaction(self, pending, *, now, transaction, native_request_hash=None):
        permit=FixturePermit(pending.payload_json);value=permit.to_dict()
        payload=json.loads(pending.request_json);intent=payload["intent"];evidence=payload["evidence"]
        # A fresh writer lease serializes this read with head/order changes.
        # It makes no positive claims and cannot join a separate native DB.
        with self.ledger.transaction(transaction=transaction):
            if "native_joint_request_sha256" in value and value["native_joint_request_sha256"] != native_request_hash:
                raise GatewayError("native_preparation_requires_native_finalization")
            self._validate_context(transaction,payload,value["request_payload_sha256"],now)
            if now>intent["expires_at"]:raise GatewayError("committed_fixture_intent_expired")
            stored=self.ledger.db.execute("SELECT * FROM gateway_preparations WHERE intent_id=?",(intent["intent_id"],)).fetchone()
            if (not stored or stored["permit_json"]!=pending.payload_json
                    or stored["payload_hash"]!=binding.digest(payload) or stored["attempt_id"]!=payload["attempt_id"]):
                raise GatewayError("permit_durable_payload_conflict")
            claim=transaction.read_head(value["CAS_namespace"],value["CAS_head_key"])
            expected_claim={"schema":"execution-gateway-CAS-claim/fixture-v1","scope":binding.FIXTURE_SCOPE,
                "request_payload_sha256":binding.digest(payload),"assessment_scope_sha256":binding.gateway_evidence_key(evidence),
                "assessment_id":evidence["assessment_id"],"assessment_revision":evidence["assessment_revision"],
                "evidence_revision":evidence["evidence_revision"],"evidence_sha256":evidence["evidence_sha256"]}
            if (value["CAS_namespace"]!=CLAIM_NAMESPACE or value["CAS_head_key"]!=intent["intent_id"]
                    or value["CAS_head_id"]!=binding.digest(payload) or value["CAS_revision"]!=1
                    or value["CAS_payload_sha256"]!=binding.digest(expected_claim)
                    or claim is None or claim["head_id"]!=value["CAS_head_id"] or claim["revision"]!=value["CAS_revision"]
                    or claim["payload_hash"]!=value["CAS_payload_sha256"] or claim["payload"]!=expected_claim
                    or binding.digest(claim["payload"])!=claim["payload_hash"]):
                raise GatewayError("committed_fixture_CAS_conflict")
            entry=self.ledger.db.execute("SELECT * FROM entries WHERE intent_id=?",(intent["intent_id"],)).fetchone()
            if (not entry or entry["request"]!=binding.canonical(intent) or entry["request_hash"]!=binding.digest(intent)
                    or entry["request_hash"]!=value["intent_sha256"] or entry["attempt_id"]!=payload["attempt_id"]
                    or entry["state"] not in {"SUBMITTING","SUBMISSION_UNKNOWN"} or entry["broker_order_id"] is not None
                    or entry["receipt_hash"] is not None
                    or any(entry[k]!=0 for k in ("filled","spent","fees","closed_unfilled","terminal","revision"))):
                raise GatewayError("committed_fixture_attempt_conflict")
            key=(intent["account_id"],intent["market"],intent["instrument_id"])
            active=self.ledger.db.execute("SELECT intent_id FROM active_instruments WHERE account=? AND market=? AND instrument=?",key).fetchone()
            opportunity=self.ledger.db.execute("SELECT intent_id,fact_hash FROM opportunities WHERE account=? AND market=? AND instrument=? AND opportunity=?",(*key,intent["opportunity_id"])).fetchone()
            allocation=self.ledger.db.execute("SELECT * FROM allocations WHERE allocation_id=?",(intent["allocation_id"],)).fetchone()
            expected_allocation={"allocation_id":intent["allocation_id"],"intent_id":intent["intent_id"],
                "account":key[0],"market":key[1],"instrument":key[2],"asset":intent["asset_class"],
                "owner":intent["owner_id"],"book":intent["book_id"],"quantity":0,"exposure":0,"version":0,
                "plan_hash":intent["plan_sha256"],"plan_generation":intent["plan_generation"]}
            if (not active or active["intent_id"]!=intent["intent_id"] or not opportunity
                    or opportunity["intent_id"]!=intent["intent_id"] or opportunity["fact_hash"]!=intent["source_fact_sha256"]
                    or not allocation or dict(allocation)!=expected_allocation):
                raise GatewayError("committed_fixture_reservation_conflict")
        return permit

    def prepare_native_in_transaction(self, request, *, expected_request_hash, now, transaction, native_observation=None):
        with self.ledger.transaction(transaction=transaction):
            from trade_safety_kit.t1_shared_fixture_join import NativeFixtureJoin
            return NativeFixtureJoin(self.ledger, enabled=True)._prepare(
                self, request, expected_request_hash=expected_request_hash,
                native_observation=native_observation, now=now, transaction=transaction)

    def mark_native_pending_in_transaction(self, pending, *, request_hash, transaction, existing):
        """Owner-only shared row update after actual native preparation."""
        with self.ledger.transaction(transaction=transaction):
            self.ledger._verify_native_fixture()
            bvalue = json.loads(pending.request_json)
            native_row = self.ledger.db.execute("SELECT request_hash,attempt_id FROM native_shared_preparations WHERE intent_id=?",
                (bvalue["intent"]["intent_id"],)).fetchone()
            if not native_row or native_row["request_hash"] != request_hash or native_row["attempt_id"] != bvalue["attempt_id"]:
                raise GatewayError("actual_native_preparation_required")
            value = json.loads(pending.payload_json)
            if existing and value.get("native_joint_request_sha256") != request_hash:
                raise GatewayError("native_shared_marker_conflict")
            value["native_joint_request_sha256"] = request_hash
            encoded = binding.canonical(value)
            changed = self.ledger.db.execute("UPDATE gateway_preparations SET permit_json=? WHERE intent_id=? AND permit_json=?",
                (encoded,bvalue["intent"]["intent_id"],pending.payload_json)).rowcount
            if changed != 1:
                raise GatewayError("native_shared_pending_CAS_conflict")
            return replace(pending,payload_json=encoded)

    def prepare_in_transaction(self, payload, *, expected_payload_hash, cas_join, now, transaction):
        with self.ledger.transaction(transaction=transaction):
            payload=json.loads(binding.canonical(payload))
            self._validate_context(transaction,payload,expected_payload_hash,now)
            if getattr(cas_join,"protocol",None)!=CAS_PROTOCOL or getattr(cas_join,"scope",None)!=binding.FIXTURE_SCOPE:
                raise GatewayError("durable_fixture_CAS_join_required")
            intent=payload["intent"];existing=self.ledger.db.execute("SELECT * FROM gateway_preparations WHERE intent_id=?",(intent["intent_id"],)).fetchone()
            if existing:
                if existing["payload_hash"]!=expected_payload_hash or existing["attempt_id"]!=payload["attempt_id"]:
                    raise GatewayError("prepared_payload_or_attempt_identity_conflict")
                return PendingFixturePermit(transaction,existing["permit_json"],binding.canonical(payload),now)
            receipt=cas_join.claim(transaction,payload,expected_payload_hash)
            transaction.require_cas_receipt(receipt,request_payload_hash=expected_payload_hash)
            self._validate_context(transaction,payload,expected_payload_hash,now)
            request_hash=self.ledger.reserve(intent,now=now,transaction=transaction)
            self.ledger.begin_entry(intent["intent_id"],attempt_id=payload["attempt_id"],now=now,transaction=transaction)
            evidence=payload["evidence"]
            value={"schema":"execution-gateway-permit/fixture-v1","scope":binding.FIXTURE_SCOPE,
                "live_authority":False,"dispatch_allowed":False,"intent_id":intent["intent_id"],
                "attempt_id":payload["attempt_id"],"route_id":payload["route_id"],
                "request_payload_sha256":expected_payload_hash,"intent_sha256":request_hash,
                "account_id":intent["account_id"],"account_version":evidence["account_version"],
                "market":intent["market"],"instrument_id":intent["instrument_id"],
                "policy_sha256":intent["policy_sha256"],"policy_revision":evidence["policy_revision"],
                "assessment_id":evidence["assessment_id"],"assessment_revision":evidence["assessment_revision"],
                "evidence_id":evidence["evidence_id"],"evidence_revision":evidence["evidence_revision"],
                "evidence_sha256":evidence["evidence_sha256"],"snapshot_bindings":payload["snapshot_bindings"],
                "CAS_transaction_id":receipt.transaction_id,"CAS_namespace":receipt.namespace,
                "CAS_head_key":receipt.head_key,"CAS_head_id":receipt.head_id,"CAS_revision":receipt.revision,
                "CAS_payload_sha256":receipt.payload_hash}
            encoded=binding.canonical(value)
            try:self.ledger.db.execute("INSERT INTO gateway_preparations VALUES (?,?,?,?)",(intent["intent_id"],expected_payload_hash,payload["attempt_id"],encoded))
            except sqlite3.IntegrityError as exc:raise GatewayError("prepared_identity_already_claimed") from exc
            return PendingFixturePermit(transaction,encoded,binding.canonical(payload),now)
