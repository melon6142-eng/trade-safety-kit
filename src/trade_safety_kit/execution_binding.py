"""Pure Phase 1 fixture contracts; no broker authority or live policy defaults.

Per-stock ceilings and score-band caps are supplied policy inputs, not a
portfolio ceiling. ETF capacity is a separate supplied aggregate mandate.
Unknown account membership, valuation, FX, cost or capacity inputs block.
"""
from __future__ import annotations

import hashlib
import json
import re

FIXTURE_SCOPE = "isolated-fixture-only/v1"


class BindingError(ValueError):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, allow_nan=False)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def shape(value, keys, name):
    if type(value) is not dict or set(value) != set(keys.split()):
        raise BindingError(f"{name}_fields_unknown_or_missing")


def text(value, name):
    if type(value) is not str or not value or value != value.strip():
        raise BindingError(f"{name}_required")
    return value


def integer(value, name, minimum=0):
    if type(value) is not int or not minimum <= value <= 2**53 - 1:
        raise BindingError(f"{name}_invalid")
    return value


def sha(value, name, length=64):
    if type(value) is not str or re.fullmatch(f"[0-9a-f]{{{length}}}", value) is None:
        raise BindingError(f"{name}_invalid")


def fixture(value, schema):
    if value.get("schema") != schema or value.get("scope") != FIXTURE_SCOPE:
        raise BindingError("production_authority_unsupported")
    canonical(value)


def instrument(value):
    for key in ("account_id", "market", "instrument_id"):
        text(value[key], key)
    if value["market"] not in {"us", "kr"}:
        raise BindingError("market_unsupported")
    if value.get("asset_class") not in {"stock", "etf"}:
        raise BindingError("asset_class_unknown")


def profiles(value, policy):
    for key in ("valuation_profile", "fx_profile", "cost_profile"):
        if value[key] != policy[key]:
            raise BindingError(f"{key}_mismatch")


def validate_account(value):
    shape(value, "schema scope account_id endpoint_id account_evidence_sha256 version", "account")
    fixture(value, "execution-account/fixture-v1")
    text(value["account_id"], "account_id"); text(value["endpoint_id"], "endpoint_id")
    sha(value["account_evidence_sha256"], "account_evidence_sha256")
    integer(value["version"], "account_version", 1)
    return value


def validate_policy(value):
    shape(value, "schema scope policy_id revision accounts stock_ceiling_krw score_caps_krw etf_aggregate_ceiling_krw capacity_accounts capacity_asset_classes max_occupied valuation_profile fx_profile cost_profile cash_basis", "policy")
    fixture(value, "execution-policy/fixture-v1")
    text(value["policy_id"], "policy_id"); integer(value["revision"], "policy_revision", 1)
    for key in ("accounts", "capacity_accounts", "capacity_asset_classes"):
        items = value[key]
        if type(items) is not list or not items or len(set(items)) != len(items):
            raise BindingError(f"{key}_unknown")
        for item in items: text(item, key)
    if not set(value["capacity_accounts"]) <= set(value["accounts"]):
        raise BindingError("capacity_membership_unknown")
    if not set(value["capacity_asset_classes"]) <= {"stock", "etf"}:
        raise BindingError("capacity_asset_class_unknown")
    integer(value["max_occupied"], "capacity_unknown", 1)
    integer(value["stock_ceiling_krw"], "per_stock_ceiling", 1)
    integer(value["etf_aggregate_ceiling_krw"], "etf_aggregate_ceiling", 1)
    bands = value["score_caps_krw"]
    if type(bands) is not dict or not bands: raise BindingError("score_caps_unknown")
    for band, cap in bands.items():
        text(band, "score_band"); integer(cap, "score_reduced_cap", 1)
        if cap > value["stock_ceiling_krw"]: raise BindingError("score_cap_exceeds_stock_ceiling")
    for key in ("valuation_profile", "fx_profile", "cost_profile"): text(value[key], key)
    if value["cash_basis"] != "fixture-gross-settled/v1": raise BindingError("cash_basis_unsupported")
    return value


def validate_intent(value, policy, binding):
    shape(value, "schema scope intent_id opportunity_id account_id account_binding_sha256 policy_sha256 market instrument_id asset_class owner_id book_id allocation_id quantity max_unit_execution_krw fee_ceiling_krw max_execution_krw score_band valuation_profile fx_profile cost_profile expires_at source_fact_sha256 content_manifest_sha256 source_commit_sha1 approval_sha256 plan_sha256 plan_generation assessment_id assessment_revision", "intent")
    fixture(value, "execution-intent/fixture-v1"); instrument(value); profiles(value, policy)
    if value["account_id"] not in policy["accounts"] or value["account_id"] != binding["account_id"]:
        raise BindingError("account_membership_unknown")
    if value["account_binding_sha256"] != digest(binding) or value["policy_sha256"] != digest(policy):
        raise BindingError("binding_or_policy_hash_mismatch")
    for key in ("intent_id", "opportunity_id", "owner_id", "book_id", "allocation_id", "assessment_id"):
        text(value[key], key)
    for key in ("source_fact_sha256", "content_manifest_sha256", "approval_sha256", "plan_sha256"):
        sha(value[key], key)
    sha(value["source_commit_sha1"], "source_commit_sha1", 40)
    for key in ("quantity", "max_unit_execution_krw", "max_execution_krw", "assessment_revision", "plan_generation", "expires_at"):
        integer(value[key], key, 1)
    integer(value["fee_ceiling_krw"], "fee_ceiling_krw")
    if value["max_execution_krw"] != value["quantity"] * value["max_unit_execution_krw"] + value["fee_ceiling_krw"]:
        raise BindingError("maximum_execution_bound_mismatch")
    if value["asset_class"] == "stock" and value["score_band"] not in policy["score_caps_krw"]:
        raise BindingError("score_band_unknown")
    if value["asset_class"] == "etf" and value["score_band"] is not None:
        raise BindingError("etf_cannot_use_stock_score_band")
    return value


def validate_snapshot(value, policy, binding, now):
    shape(value, "schema scope account_id account_binding_sha256 snapshot_id revision captured_at valid_until complete valuation_profile fx_profile cost_profile settled_cash_krw holdings orders cash_watermarks", "snapshot")
    fixture(value, "execution-snapshot/fixture-v1"); profiles(value, policy)
    if value["account_id"] != binding["account_id"] or value["account_binding_sha256"] != digest(binding):
        raise BindingError("snapshot_account_mismatch")
    text(value["snapshot_id"], "snapshot_id")
    for key in ("revision", "captured_at", "valid_until"): integer(value[key], key, 1)
    if value["complete"] is not True or not value["captured_at"] <= now <= value["valid_until"]:
        raise BindingError("snapshot_incomplete_or_stale")
    integer(value["settled_cash_krw"], "settled_cash_krw")
    if type(value["holdings"]) is not list or type(value["orders"]) is not list or type(value["cash_watermarks"]) is not dict:
        raise BindingError("snapshot_collections_unknown")
    seen = set()
    for row in value["holdings"]:
        shape(row, "market instrument_id asset_class quantity exposure_krw allocation_quantities allocation_versions allocation_exposure_krw", "holding")
        instrument({**row, "account_id": value["account_id"]})
        key = (row["market"], row["instrument_id"])
        if key in seen: raise BindingError("duplicate_holding")
        seen.add(key)
        integer(row["quantity"], "holding_quantity"); integer(row["exposure_krw"], "holding_exposure")
        if row["quantity"] > 0 and row["exposure_krw"] <= 0: raise BindingError("holding_valuation_unknown")
        for name in ("allocation_quantities", "allocation_versions", "allocation_exposure_krw"):
            if type(row[name]) is not dict: raise BindingError("allocation_snapshot_unknown")
            for key, val in row[name].items(): text(key, "allocation_id"); integer(val, name)
            if any(val <= 0 for val in row[name].values()): raise BindingError("owned_snapshot_value_unknown")
        if not set(row["allocation_quantities"]) == set(row["allocation_versions"]) == set(row["allocation_exposure_krw"]):
            raise BindingError("allocation_snapshot_keys_conflict")
        if row["quantity"] == 0 and row["exposure_krw"] != 0: raise BindingError("flat_holding_has_exposure")
    for row in value["orders"]:
        shape(row, "order_id market instrument_id asset_class side known remaining_qty remaining_max_krw", "external_order")
        text(row["order_id"], "external_order_id"); text(row["market"], "external_order_market"); text(row["instrument_id"], "external_order_instrument")
        if type(row["known"]) is not bool: raise BindingError("external_order_known_invalid")
        if row["known"]:
            instrument({**row, "account_id": value["account_id"]})
            if row["side"] not in {"buy", "sell"}: raise BindingError("external_order_side_unknown")
            integer(row["remaining_qty"], "external_remaining_qty", 1)
            integer(row["remaining_max_krw"], "external_remaining_bound", 1)
    for key, row in value["cash_watermarks"].items():
        text(key, "watermark_attempt")
        shape(row, "filled_qty spent_krw receipt_revision", "cash_watermark")
        for name, val in row.items(): integer(val, name)
    return value


def validate_receipt(value, kind):
    keys = "schema scope attempt_id broker_order_id account_id market instrument_id allocation_id owner_id book_id request_sha256 revision filled_qty terminal closed_unfilled_qty evidence_sha256 valuation_profile fx_profile cost_profile "
    keys += "spent_krw fee_paid_krw holding_krw" if kind == "entry" else "expected_allocation_version remaining_holding_krw"
    shape(value, keys, "receipt"); fixture(value, f"execution-{kind}-receipt/fixture-v1")
    for key in ("attempt_id", "broker_order_id", "account_id", "market", "instrument_id", "allocation_id", "owner_id", "book_id"):
        text(value[key], key)
    for key in ("request_sha256", "evidence_sha256"): sha(value[key], key)
    integer(value["revision"], "receipt_revision", 1)
    for key in ("filled_qty", "closed_unfilled_qty"): integer(value[key], key)
    if type(value["terminal"]) is not bool: raise BindingError("terminal_status_unknown")
    for key in (("spent_krw", "fee_paid_krw", "holding_krw") if kind == "entry" else ("expected_allocation_version", "remaining_holding_krw")):
        integer(value[key], key)
    return value


def validate_exit(value):
    shape(value, "schema scope exit_id account_id market instrument_id allocation_id owner_id book_id expected_allocation_version position_snapshot_id position_snapshot_revision plan_sha256 plan_generation quantity expires_at", "exit")
    fixture(value, "execution-exit/fixture-v1")
    for key in ("exit_id", "account_id", "market", "instrument_id", "allocation_id", "owner_id", "book_id", "position_snapshot_id"):
        text(value[key], key)
    sha(value["plan_sha256"], "plan_sha256")
    for key in ("expected_allocation_version", "position_snapshot_revision", "plan_generation", "quantity", "expires_at"):
        integer(value[key], key, 1)
    return value


def validate_gateway_evidence(value, policy, account):
    shape(value, "schema scope account_id account_binding_sha256 account_version market instrument_id asset_class candidate_id strategy variant owner_id book_id allocation_id policy_id policy_revision policy_sha256 assessment_id assessment_revision expected_previous_assessment_id expected_previous_assessment_revision evidence_id evidence_revision evidence_sha256 result position_snapshot_id position_snapshot_revision plan_sha256 plan_generation", "gateway_evidence")
    fixture(value,"execution-gateway-evidence/fixture-v1"); instrument(value)
    for key in ("candidate_id","strategy","variant","owner_id","book_id","allocation_id","assessment_id","evidence_id","position_snapshot_id"):
        text(value[key],key)
    for key in ("account_version","policy_revision","assessment_revision","evidence_revision","position_snapshot_revision","plan_generation"):
        integer(value[key],key,1)
    for key in ("evidence_sha256","plan_sha256"):sha(value[key],key)
    integer(value["expected_previous_assessment_revision"],"expected_previous_assessment_revision")
    if value["expected_previous_assessment_id"] is None:
        if value["expected_previous_assessment_revision"] != 0:raise BindingError("assessment_predecessor_revision_conflict")
    else:text(value["expected_previous_assessment_id"],"expected_previous_assessment_id")
    if value["result"] not in {"PASS","VETO","UNKNOWN"}:raise BindingError("evidence_result_unknown")
    if (value["account_id"] != account["account_id"] or value["account_binding_sha256"] != digest(account)
            or value["account_version"] != account["version"] or value["policy_id"] != policy["policy_id"]
            or value["policy_revision"] != policy["revision"] or value["policy_sha256"] != digest(policy)):
        raise BindingError("gateway_account_or_policy_revision_conflict")
    return value


def gateway_evidence_key(value):
    return digest({k:value[k] for k in ("account_id","market","instrument_id","candidate_id","strategy","variant")})


def validate_gateway_payload(value, policy, account):
    shape(value,"schema scope route_id intent evidence snapshot_bindings attempt_id cas_profile","gateway_payload")
    fixture(value,"execution-gateway-request/fixture-v1")
    text(value["route_id"],"route_id");text(value["attempt_id"],"attempt_id")
    if value["cas_profile"] != "fixture-shared-evidence/v1":raise BindingError("durable_native_CAS_join_unavailable")
    intent=validate_intent(value["intent"],policy,account)
    evidence=validate_gateway_evidence(value["evidence"],policy,account)
    for key in ("account_id","account_binding_sha256","market","instrument_id","asset_class","owner_id","book_id","allocation_id","policy_sha256","assessment_id","assessment_revision","plan_sha256","plan_generation"):
        if intent[key] != evidence[key]:raise BindingError("gateway_intent_evidence_exact_identity_conflict")
    snapshots=value["snapshot_bindings"]
    if type(snapshots) is not dict or set(snapshots)!=set(policy["accounts"]):raise BindingError("gateway_snapshot_membership_unknown")
    for name,row in snapshots.items():
        text(name,"snapshot_account")
        shape(row,"snapshot_id revision snapshot_sha256","snapshot_binding")
        text(row["snapshot_id"],"snapshot_id");integer(row["revision"],"snapshot_revision",1);sha(row["snapshot_sha256"],"snapshot_sha256")
    return value
