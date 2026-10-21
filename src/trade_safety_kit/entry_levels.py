"""Pure validation helpers for automated entry orders.

This module deliberately has no broker, environment, filesystem, or state
imports.  Order paths can validate a candidate before touching live clients or
writing an intent/protection record, and offline tests can import it without
network or ``.env`` side effects.
"""

from __future__ import annotations

import math
from typing import Any


class EntrySafetyError(ValueError):
    """Raised when an entry price/stop/target is unsafe to submit."""


def _finite_positive(value: Any, name: str) -> float:
    try:
        out = float(value)
    except (TypeError, ValueError) as exc:
        raise EntrySafetyError(f"{name}_not_numeric") from exc
    if not math.isfinite(out) or out <= 0.0:
        raise EntrySafetyError(f"{name}_not_finite_positive")
    return out


def validate_entry_levels(
    entry_px: Any,
    stop_px: Any,
    target_px: Any,
    *,
    max_risk_pct: float = 0.15,
    min_risk_pct: float = 0.0001,
) -> dict[str, float]:
    """Validate and normalize long-entry levels.

    The helper is intentionally side-effect free.  It accepts values from JSON
    or scanner records and returns plain floats suitable for persisted order
    records.  A long entry must satisfy ``stop < entry < target``; the stop
    distance is bounded to avoid accidentally sizing against a wildly stale or
    malformed level.
    """

    entry = _finite_positive(entry_px, "entry_px")
    stop = _finite_positive(stop_px, "stop_px")
    target = _finite_positive(target_px, "target_px")
    if not stop < entry < target:
        raise EntrySafetyError("levels_not_ordered_for_long_entry")

    try:
        max_risk = float(max_risk_pct)
        min_risk = float(min_risk_pct)
    except (TypeError, ValueError) as exc:
        raise EntrySafetyError("risk_bounds_not_numeric") from exc
    if not math.isfinite(max_risk) or not math.isfinite(min_risk):
        raise EntrySafetyError("risk_bounds_not_finite")
    if min_risk < 0.0 or max_risk <= 0.0 or min_risk > max_risk:
        raise EntrySafetyError("risk_bounds_invalid")

    risk_pct = (entry - stop) / entry
    if risk_pct < min_risk:
        raise EntrySafetyError(f"risk_too_small:{risk_pct:.8f}")
    if risk_pct > max_risk:
        raise EntrySafetyError(f"risk_too_large:{risk_pct:.8f}")

    reward = target - entry
    rr = reward / (entry - stop)
    return {
        "entry_px": entry,
        "stop_px": stop,
        "target_px": target,
        "risk_pct": risk_pct,
        "rr": rr,
    }
