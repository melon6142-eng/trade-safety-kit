"""Synthetic gateway inputs; no broker/account proof or live financial policy."""
import json

from trade_safety_kit import execution_binding as b
from tests import execution_phase1_fixtures as f


def payload(ledger, request, *, route="fixture-t1-B",variant="fixture-B"):
    snapshots={name:json.loads(ledger.db.execute("SELECT payload FROM snapshots WHERE account=?",(name,)).fetchone()[0]) for name in ledger.policy["accounts"]}
    selected=snapshots[request["account_id"]];account=ledger.accounts[request["account_id"]]
    evidence=dict(schema="execution-gateway-evidence/fixture-v1",scope=b.FIXTURE_SCOPE,
        **{k:request[k] for k in ("account_id","account_binding_sha256","market","instrument_id","asset_class","owner_id","book_id","allocation_id","policy_sha256","assessment_id","assessment_revision","plan_sha256","plan_generation")},
        account_version=account["version"],candidate_id="fixture-candidate:"+request["opportunity_id"],strategy=route,variant=variant,
        policy_id=ledger.policy["policy_id"],policy_revision=ledger.policy["revision"],
        expected_previous_assessment_id=None,expected_previous_assessment_revision=0,
        evidence_id="fixture-evidence:"+request["intent_id"],evidence_revision=1,evidence_sha256=b.digest({"fixture":request["intent_id"]}),result="PASS",
        position_snapshot_id=selected["snapshot_id"],position_snapshot_revision=selected["revision"])
    return dict(schema="execution-gateway-request/fixture-v1",scope=b.FIXTURE_SCOPE,route_id=route,intent=request,evidence=evidence,
        snapshot_bindings={name:{"snapshot_id":s["snapshot_id"],"revision":s["revision"],"snapshot_sha256":b.digest(s)} for name,s in snapshots.items()},
        attempt_id="fixture-attempt:"+request["intent_id"],cas_profile="fixture-shared-evidence/v1")
