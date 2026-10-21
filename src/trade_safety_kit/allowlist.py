"""Single-use, expiring allowlist for exactly-scoped entry orders.

Deny-by-default: an entry order passes only when the caller presents the exact
symbol / quantity / limit price that a live allowlist document names. Another
symbol, quantity or price, a market order (no limit price), a missing,
malformed or expired document all keep the deny.

The document path comes from the caller or the ``TSK_ALLOWLIST_PATH``
environment variable. There is no built-in default location.
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime
from pathlib import Path
from typing import Any

PATH_ENV = "TSK_ALLOWLIST_PATH"
SCHEMA = "trade-safety-kit/one-time-entry-allowlist/v1"
ENTRY_KEYS = frozenset({"symbol", "quantity", "limit_price"})
OPTIONAL_ENTRY_KEYS = frozenset({"budget"})
TOP_KEYS = frozenset({"schema", "created_at", "expires_at", "max_total_notional",
                      "note", "entries", "reprice_at_send"})


class AllowlistDenied(RuntimeError):
    """Raised with a stable reason code; never echoes payload values."""


def _epoch(value: Any, field: str) -> float:
    if type(value) is not str:
        raise AllowlistDenied(f"allowlist_{field}_unknown")
    try:
        stamp = datetime.fromisoformat(value)
    except ValueError:
        raise AllowlistDenied(f"allowlist_{field}_unknown") from None
    if stamp.tzinfo is None:
        raise AllowlistDenied(f"allowlist_{field}_needs_timezone")
    return stamp.timestamp()


def _entry(row: Any, index: int) -> dict[str, Any]:
    if type(row) is not dict or set(row) - OPTIONAL_ENTRY_KEYS != ENTRY_KEYS:
        raise AllowlistDenied(f"allowlist_entry_shape_unknown:{index}")
    symbol, quantity, price = row["symbol"], row["quantity"], row["limit_price"]
    if type(symbol) is not str or not symbol or symbol != symbol.strip().upper():
        raise AllowlistDenied(f"allowlist_entry_symbol_unknown:{index}")
    if type(quantity) is not int or quantity < 1:
        raise AllowlistDenied(f"allowlist_entry_quantity_unknown:{index}")
    if type(price) is not float or not price > 0:
        raise AllowlistDenied(f"allowlist_entry_price_unknown:{index}")
    if round(price, 2) != price:
        raise AllowlistDenied(f"allowlist_entry_price_not_cent_precise:{index}")
    out = {"symbol": symbol, "quantity": quantity, "limit_price": price}
    if "budget" in row:
        budget = row["budget"]
        if type(budget) not in (int, float) or not budget > 0:
            raise AllowlistDenied(f"allowlist_entry_budget_unknown:{index}")
        out["budget"] = float(budget)
    return out


def notional(row: dict[str, Any]) -> float:
    return round(row["quantity"] * row["limit_price"], 2)


def _parse(value: Any) -> dict[str, Any]:
    if type(value) is not dict or set(value) - TOP_KEYS or not {"schema", "expires_at", "entries"} <= set(value):
        raise AllowlistDenied("allowlist_document_shape_unknown")
    if value["schema"] != SCHEMA:
        raise AllowlistDenied("allowlist_schema_unknown")
    created = _epoch(value["created_at"], "created_at") if "created_at" in value else 0.0
    expires = _epoch(value["expires_at"], "expires_at")
    if not expires > created:
        raise AllowlistDenied("allowlist_expiry_not_after_creation")
    rows = value["entries"]
    if type(rows) is not list or not rows:
        raise AllowlistDenied("allowlist_entries_empty")
    entries = [_entry(row, i) for i, row in enumerate(rows)]
    if len({r["symbol"] for r in entries}) != len(entries):
        raise AllowlistDenied("allowlist_duplicate_symbol")
    total = value.get("max_total_notional")
    if type(total) not in (int, float) or not total > 0:
        raise AllowlistDenied("allowlist_max_total_unknown")
    if total < sum(notional(r) for r in entries):
        raise AllowlistDenied("allowlist_max_total_below_named_orders")
    reprice = value.get("reprice_at_send", False)
    if type(reprice) is not bool:
        raise AllowlistDenied("allowlist_reprice_flag_unknown")
    if reprice and any("budget" not in r for r in entries):
        raise AllowlistDenied("allowlist_budget_required_for_reprice")
    return {"schema": SCHEMA, "created_at": created, "expires_at": expires,
            "max_total_notional": float(total), "reprice_at_send": reprice, "entries": entries}


def _path(path: str | Path | None) -> Path:
    if path is not None:
        return Path(path)
    override = os.environ.get(PATH_ENV)
    if not override:
        raise AllowlistDenied("allowlist_path_unconfigured")
    return Path(override)


def load(path: str | Path | None = None) -> dict[str, Any] | None:
    """Parsed allowlist, ``None`` when absent; a malformed file raises."""
    target = _path(path)
    try:
        raw = target.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    try:
        value = json.loads(raw)
    except json.JSONDecodeError:
        raise AllowlistDenied("allowlist_json_invalid") from None
    return _parse(value)


def active(path: str | Path | None = None, *, now: float | None = None) -> bool:
    value = load(path)
    return bool(value) and (time.time() if now is None else now) < value["expires_at"]


def match(symbol: Any, quantity: Any, limit_price: Any, *,
          path: str | Path | None = None, now: float | None = None) -> dict[str, Any]:
    """Return the allowlist row for an exact order, else raise AllowlistDenied."""
    value = load(path)
    if value is None:
        raise AllowlistDenied("allowlist_missing")
    if (time.time() if now is None else now) >= value["expires_at"]:
        raise AllowlistDenied("allowlist_expired")
    if type(symbol) is not str or not symbol.strip():
        raise AllowlistDenied("allowlist_symbol_unknown")
    if type(quantity) is not int or quantity < 1:
        raise AllowlistDenied("allowlist_quantity_unknown")
    if type(limit_price) is not float or not limit_price > 0:
        raise AllowlistDenied("allowlist_limit_price_unknown")
    name = symbol.strip().upper()
    for row in value["entries"]:
        if row["symbol"] != name:
            continue
        if row["quantity"] != quantity:
            raise AllowlistDenied("allowlist_quantity_mismatch")
        if round(row["limit_price"], 2) != round(limit_price, 2):
            raise AllowlistDenied("allowlist_limit_price_mismatch")
        return dict(row)
    raise AllowlistDenied("allowlist_symbol_not_listed")


def seal(symbol: Any, quantity: Any, limit_price: Any, *, path: str | Path | None = None) -> dict[str, Any]:
    """Freeze one reprice-at-send row to the order about to be placed (within its budget)."""
    target = _path(path)
    value = load(target)
    if value is None:
        raise AllowlistDenied("allowlist_missing")
    if not value["reprice_at_send"]:
        raise AllowlistDenied("allowlist_reprice_not_authorised")
    if type(symbol) is not str or type(quantity) is not int or quantity < 1:
        raise AllowlistDenied("allowlist_seal_shape_unknown")
    if type(limit_price) is not float or not limit_price > 0:
        raise AllowlistDenied("allowlist_seal_price_unknown")
    price, name = round(limit_price, 2), symbol.strip().upper()
    for row in value["entries"]:
        if row["symbol"] != name:
            continue
        if notional({"quantity": quantity, "limit_price": price}) > row["budget"]:
            raise AllowlistDenied("allowlist_seal_over_budget")
        raw = json.loads(target.read_text(encoding="utf-8"))
        raw["entries"] = [({**item, "quantity": quantity, "limit_price": price}
                           if item.get("symbol") == name else item) for item in raw["entries"]]
        tmp = target.with_suffix(target.suffix + ".tmp")
        tmp.write_text(json.dumps(raw, indent=2) + "\n", encoding="utf-8")
        os.chmod(tmp, 0o600)
        os.replace(tmp, target)
        return {**row, "quantity": quantity, "limit_price": price}
    raise AllowlistDenied("allowlist_symbol_not_listed")


__all__ = ["AllowlistDenied", "SCHEMA", "PATH_ENV", "load", "active", "match", "seal", "notional"]
