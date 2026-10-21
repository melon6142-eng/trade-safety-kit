"""Synthetic policy values are test parameters, never live financial policy."""
from copy import deepcopy
from trade_safety_kit import execution_binding as b

SCOPE = b.FIXTURE_SCOPE
NOW = 1000


def account(name="A"):
    return dict(schema="execution-account/fixture-v1", scope=SCOPE, account_id=name,
                endpoint_id="fixture-endpoint", account_evidence_sha256=b.digest({"fixture_account": name}), version=1)


def policy(accounts=("A",), *, stock_cap=5_000_000, reduced_cap=2_000_000, etf_cap=5_000_000, capacity=10):
    return dict(schema="execution-policy/fixture-v1", scope=SCOPE, policy_id="fixture-policy", revision=1,
                accounts=list(accounts), stock_ceiling_krw=stock_cap, score_caps_krw={"fixture-full":stock_cap,"fixture-reduced":reduced_cap},
                etf_aggregate_ceiling_krw=etf_cap, capacity_accounts=list(accounts), capacity_asset_classes=["stock"], max_occupied=capacity,
                valuation_profile="fixture-valuation/v1", fx_profile="fixture-fx-bound/v1", cost_profile="fixture-limit-cost/v1", cash_basis="fixture-gross-settled/v1")


def profiles(p):
    return {k:p[k] for k in ("valuation_profile", "fx_profile", "cost_profile")}


def snapshot(p, name="A", *, revision=1, holdings=None, orders=None, cash=50_000_000, watermarks=None):
    return dict(schema="execution-snapshot/fixture-v1", scope=SCOPE, account_id=name, account_binding_sha256=b.digest(account(name)),
                snapshot_id=f"fixture-snapshot:{name}:{revision}", revision=revision, captured_at=900, valid_until=5000, complete=True,
                **profiles(p), settled_cash_krw=cash, holdings=deepcopy(holdings or []), orders=deepcopy(orders or []), cash_watermarks=deepcopy(watermarks or {}))


def intent(p, identity="one", *, name="A", instrument="MSFT", asset="stock", owner="t1", qty=10, unit=100_000, fee=0, band="fixture-full", opportunity=None):
    return dict(schema="execution-intent/fixture-v1", scope=SCOPE, intent_id=identity, opportunity_id=opportunity or identity,
                account_id=name, account_binding_sha256=b.digest(account(name)), policy_sha256=b.digest(p), market="us", instrument_id=instrument,
                asset_class=asset, owner_id=owner, book_id=owner, allocation_id=f"allocation:{identity}", quantity=qty,
                max_unit_execution_krw=unit, fee_ceiling_krw=fee, max_execution_krw=qty*unit+fee,
                score_band=band if asset=="stock" else None, **profiles(p), expires_at=4000,
                source_fact_sha256=b.digest({"fixture_fact":opportunity or identity}), content_manifest_sha256="c"*64,
                source_commit_sha1="d"*40, approval_sha256=b.digest({"fixture_approval":identity}), plan_sha256=b.digest({"fixture_plan":identity}),
                plan_generation=1, assessment_id=f"fixture-assessment:{identity}", assessment_revision=1)


def entry_receipt(p, request, attempt, *, qty, revision=1, terminal=False, closed=0, spent=None, fee=0, holding=None):
    spent = qty*request["max_unit_execution_krw"]+fee if spent is None else spent
    return dict(schema="execution-entry-receipt/fixture-v1", scope=SCOPE, attempt_id=attempt, broker_order_id=f"fixture-order:{attempt}",
                **{k:request[k] for k in ("account_id","market","instrument_id","allocation_id","owner_id","book_id")},
                request_sha256=b.digest(request), revision=revision, filled_qty=qty, terminal=terminal, closed_unfilled_qty=closed,
                evidence_sha256=b.digest({"fixture_receipt":attempt,"revision":revision}), **profiles(p), spent_krw=spent, fee_paid_krw=fee,
                holding_krw=spent if holding is None else holding)


def holding(instrument, *, qty, exposure, asset="stock", allocations=()):
    return dict(market="us", instrument_id=instrument, asset_class=asset, quantity=qty, exposure_krw=exposure,
                allocation_quantities={a["allocation_id"]:a["quantity"] for a in allocations},
                allocation_versions={a["allocation_id"]:a["version"] for a in allocations},
                allocation_exposure_krw={a["allocation_id"]:a["exposure"] for a in allocations})


def exit_request(request, allocation, snap, identity="exit-one", *, qty):
    return dict(schema="execution-exit/fixture-v1", scope=SCOPE, exit_id=identity,
                **{k:request[k] for k in ("account_id","market","instrument_id","allocation_id","owner_id","book_id")},
                expected_allocation_version=allocation["version"], position_snapshot_id=snap["snapshot_id"], position_snapshot_revision=snap["revision"],
                plan_sha256=request["plan_sha256"], plan_generation=allocation["plan_generation"], quantity=qty, expires_at=4000)


def exit_receipt(p, request, attempt, *, qty, allocation_version, remaining, terminal=True, closed=0, revision=1):
    return dict(schema="execution-exit-receipt/fixture-v1", scope=SCOPE, attempt_id=attempt, broker_order_id=f"fixture-sell:{attempt}",
                **{k:request[k] for k in ("account_id","market","instrument_id","allocation_id","owner_id","book_id")},
                request_sha256=b.digest(request), revision=revision, filled_qty=qty, terminal=terminal, closed_unfilled_qty=closed,
                evidence_sha256=b.digest({"fixture_exit":attempt,"revision":revision}), **profiles(p),
                expected_allocation_version=allocation_version, remaining_holding_krw=remaining)
