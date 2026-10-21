"""Paired statistics for strategy A/B comparisons, so a delta is never just "arm total".

Comparing two strategy arms by *total* R mixes "trades one arm filtered out"
into the delta and makes a small sample look decisive. This module provides:

* ``bootstrap_mean_ci`` / ``bootstrap_total_ci`` - percentile bootstrap over the
  shared trades (deterministic seed, so a report is reproducible).
* ``sign_test`` - exact two-sided binomial p for "how many shared trades got
  better/worse", with no distributional assumption.
* ``paired_delta`` - matches the two arms trade by trade on ``candidate_id``,
  drops rows with no R (a blocked trade has no R, it is not a zero), and returns
  the delta with its CI, sign test and an explicit verdict.
* ``sample_verdict`` - n<30 means "provisional".

Stdlib only.
"""
from __future__ import annotations

import math
import random
import statistics as st
from typing import Any, Iterable, Mapping, Sequence

STATS_MIN_SEATS = 30
BOOTSTRAP_DRAWS = 2000
BOOTSTRAP_SEED = 20261021


def bootstrap_mean_ci(
    values: Sequence[float],
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the mean. ``None`` for an empty sample."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    means = []
    for _ in range(draws):
        means.append(sum(values[rng.randrange(n)] for _ in range(n)) / n)
    means.sort()
    lo = means[int((alpha / 2) * draws)]
    hi = means[min(draws - 1, int((1 - alpha / 2) * draws))]
    return (float(lo), float(hi))


def bootstrap_total_ci(
    values: Sequence[float],
    *,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
    alpha: float = 0.05,
) -> tuple[float, float] | None:
    """Percentile bootstrap CI of the sum (= mean × n), same resampling."""
    if not values:
        return None
    rng = random.Random(seed)
    n = len(values)
    totals = []
    for _ in range(draws):
        totals.append(float(sum(values[rng.randrange(n)] for _ in range(n))))
    totals.sort()
    lo = totals[int((alpha / 2) * draws)]
    hi = totals[min(draws - 1, int((1 - alpha / 2) * draws))]
    return (float(lo), float(hi))


def sign_test(up: int, down: int) -> float:
    """Exact two-sided binomial p for ``up`` wins out of ``up + down`` at p=0.5."""
    n = up + down
    if n == 0:
        return 1.0

    def pmf(i: int) -> float:
        return math.comb(n, i) * 0.5**n

    observed = pmf(up)
    total = sum(pmf(i) for i in range(n + 1) if pmf(i) <= observed + 1e-12)
    return float(min(1.0, total))


def sample_verdict(n: int, *, min_seats: int = STATS_MIN_SEATS) -> str:
    """``reportable`` only when the shared-seat count clears the floor."""
    return "reportable" if int(n) >= int(min_seats) else "provisional"


def _r_by_key(rows: Iterable[Mapping[str, Any]], key: str) -> dict[str, float | None]:
    out: dict[str, float | None] = {}
    for row in rows:
        k = row.get(key)
        if k is None:
            continue
        value = row.get("r")
        out[str(k)] = None if value is None else float(value)
    return out


def paired_delta(
    off_rows: Iterable[Mapping[str, Any]],
    on_rows: Iterable[Mapping[str, Any]],
    *,
    key: str = "candidate_id",
    min_seats: int = STATS_MIN_SEATS,
    draws: int = BOOTSTRAP_DRAWS,
    seed: int = BOOTSTRAP_SEED,
) -> dict[str, Any]:
    """Seat-by-seat ``on - off`` delta with CI, sign test and a verdict.

    A seat that only one arm traded is counted separately (``on_only`` /
    ``off_only``) instead of being folded into the delta: that is precisely the
    confound the arm-total comparison hid.
    """
    off = _r_by_key(off_rows, key)
    on = _r_by_key(on_rows, key)
    shared = sorted(set(off) & set(on))
    diffs: list[float] = []
    excluded_null = 0
    for k in shared:
        o, n = off[k], on[k]
        if o is None or n is None:
            excluded_null += 1
            continue
        diffs.append(n - o)

    base = {
        "key": key,
        "n_shared": len(shared),
        "n_excluded_null": excluded_null,
        "n_on_only": len(set(on) - set(off)),
        "n_off_only": len(set(off) - set(on)),
    }
    if not diffs:
        return {**base, "verdict": "no_data", "delta_total_r": None, "delta_mean_r": None,
                "mean_ci": None, "total_ci": None, "sign_p": None, "significant": False}

    mean_ci = bootstrap_mean_ci(diffs, draws=draws, seed=seed)
    total_ci = bootstrap_total_ci(diffs, draws=draws, seed=seed)
    up = sum(1 for d in diffs if d > 1e-9)
    down = sum(1 for d in diffs if d < -1e-9)
    flat = len(diffs) - up - down
    p = sign_test(up, down)
    verdict = sample_verdict(len(diffs), min_seats=min_seats)
    significant = bool(
        verdict == "reportable"
        and mean_ci is not None
        and (mean_ci[0] > 0.0 or mean_ci[1] < 0.0)
        and p < 0.05
    )
    return {
        **base,
        "n_paired": len(diffs),
        "delta_total_r": round(sum(diffs), 4),
        "delta_mean_r": round(st.fmean(diffs), 6),
        "mean_ci": [round(mean_ci[0], 6), round(mean_ci[1], 6)],
        "total_ci": [round(total_ci[0], 4), round(total_ci[1], 4)],
        "up": up,
        "down": down,
        "flat": flat,
        "sign_p": round(p, 4),
        "verdict": verdict,
        "significant": significant,
    }


def format_paired(label: str, delta: Mapping[str, Any]) -> str:
    """One-line log/report form, explicit about what is *not* proven."""
    if delta.get("verdict") in (None, "no_data"):
        return f"PAIRED {label:28s} no comparable seats (shared={delta.get('n_shared')})"
    ci = delta.get("mean_ci") or [float("nan"), float("nan")]
    return (
        f"PAIRED {label:28s} n={delta['n_paired']:3d} "
        f"dTot={delta['delta_total_r']:+.2f} dMean={delta['delta_mean_r']:+.3f} "
        f"CI[{ci[0]:+.3f},{ci[1]:+.3f}] sign {delta['up']}/{delta['down']} p={delta['sign_p']:.4f} "
        f"{'SIGNIFICANT' if delta['significant'] else 'not-significant'} ({delta['verdict']}) "
        f"on-only={delta['n_on_only']} off-only={delta['n_off_only']} null={delta['n_excluded_null']}"
    )
