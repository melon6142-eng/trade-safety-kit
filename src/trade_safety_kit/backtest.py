"""Bar-by-bar long-only R-multiple backtester with an explicit look-ahead guard.

Leakage rules enforced here:

1. The signal function only ever receives ``bars.iloc[: i + 1]`` (a copy), so it
   physically cannot read bar ``i + 1`` or later.
2. A signal on bar ``i`` fills at the **open of bar i + 1**, never at bar ``i``'s close.
3. Stop/target are checked gap-aware: an open through the stop fills at the open.
   If both levels are inside one bar's range the stop is assumed first (conservative).
4. Indicator helpers that build breakout levels must ``shift(1)`` (see
   :func:`prior_rolling_max`) so a bar never breaks out of a level that includes itself.

Requires pandas (``pip install trade-safety-kit[backtest]``).
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Callable

import pandas as pd


@dataclass(frozen=True)
class Signal:
    entry: float
    stop: float
    target: float


@dataclass
class BTConfig:
    fee_bps: float = 5.0
    slip_bps: float = 5.0
    risk_pct: float = 1.0
    max_hold_bars: int = 40
    warmup_bars: int = 50


def fee_adj(price: float, bps: float, side: str) -> float:
    f = bps / 10000.0
    return price * (1 + f) if side == "buy" else price * (1 - f)


def threshold_exit_price(open_px: float, low: float, high: float, stop: float, target: float):
    if open_px <= stop:
        return open_px, "stop"
    if open_px >= target:
        return open_px, "target"
    if low <= stop:
        return stop, "stop"
    if high >= target:
        return target, "target"
    return None, None


def max_drawdown(curve: list[float]) -> float:
    if not curve:
        return 0.0
    peak, mdd = curve[0], 0.0
    for x in curve:
        peak = max(peak, x)
        if peak > 0:
            mdd = min(mdd, (x - peak) / peak)
    return mdd * 100


def cagr(start: float, end: float, years: float) -> float:
    if start <= 0 or end <= 0 or years <= 0:
        return 0.0
    return ((end / start) ** (1 / years) - 1) * 100


def prior_rolling_max(series: pd.Series, window: int) -> pd.Series:
    """Rolling max of the *previous* ``window`` bars (excludes the current bar)."""
    return series.rolling(window).max().shift(1)


SignalFn = Callable[[pd.DataFrame], "Signal | None"]


def run(bars: pd.DataFrame, signal_fn: SignalFn, cfg: BTConfig | None = None) -> dict[str, Any]:
    """Run ``signal_fn`` over OHLC ``bars`` (columns Open/High/Low/Close)."""
    cfg = cfg or BTConfig()
    for col in ("Open", "High", "Low", "Close"):
        if col not in bars.columns:
            raise ValueError(f"missing_column:{col}")
    cost = cfg.fee_bps + cfg.slip_bps
    n, i = len(bars), cfg.warmup_bars
    trades: list[dict[str, Any]] = []
    equity, curve = 1.0, []
    while i < n - 1:
        sig = signal_fn(bars.iloc[: i + 1].copy())  # look-ahead guard
        if sig is None:
            curve.append(equity)
            i += 1
            continue
        planned = sig.entry - sig.stop
        if planned <= 0 or sig.target <= sig.entry:
            i += 1
            continue
        e = i + 1
        entry = fee_adj(float(bars["Open"].iloc[e]), cost, "buy")
        stop = entry - planned * (entry / sig.entry)
        rr = (sig.target - sig.entry) / planned
        target = entry + rr * (entry - stop)
        risk = entry - stop
        exit_px = exit_i = reason = None
        for j in range(e, min(e + cfg.max_hold_bars, n)):
            raw, why = threshold_exit_price(float(bars["Open"].iloc[j]), float(bars["Low"].iloc[j]),
                                            float(bars["High"].iloc[j]), stop, target)
            if raw is not None:
                exit_px, exit_i, reason = fee_adj(raw, cost, "sell"), j, why
                break
        if exit_px is None:
            exit_i = min(e + cfg.max_hold_bars, n) - 1
            exit_px, reason = fee_adj(float(bars["Close"].iloc[exit_i]), cost, "sell"), "time"
        r = (exit_px - entry) / risk
        equity *= 1.0 + (cfg.risk_pct / 100.0) * r
        trades.append({"signal_i": i, "entry_i": e, "exit_i": exit_i, "entry": entry,
                       "stop": stop, "target": target, "exit": exit_px, "r_mult": r, "reason": reason})
        curve.append(equity)
        i = exit_i + 1
    rs = [t["r_mult"] for t in trades]
    return {"trades": trades, "n": len(trades), "total_r": sum(rs),
            "win_rate": 100 * sum(r > 0 for r in rs) / len(rs) if rs else 0.0,
            "equity": equity, "max_drawdown_pct": max_drawdown(curve)}
