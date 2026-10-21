"""Broker-vs-local position reconciliation (read-only, fail-closed).

Generalised from a production reconciler. The contract:

* A :class:`Snapshot` is only *authoritative* when the broker read was complete
  (no error, no truncated pagination, no repeated cursor). Callers must treat an
  incomplete snapshot as **unknown** and must never drop a local hold from it.
* :func:`reconcile` compares local holds with an authoritative snapshot and
  reports drift; it never places, cancels or modifies an order.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping


class ReconcileError(RuntimeError):
    pass


@dataclass(frozen=True)
class Snapshot:
    positions: Mapping[str, float]
    complete: bool
    error: str | None = None

    @property
    def authoritative(self) -> bool:
        return self.complete and self.error is None


def collect_pages(fetch_page: Callable[[str | None], tuple[Iterable[Mapping[str, Any]], str | None]],
                  *, max_pages: int = 50, symbol_key: str = "symbol", qty_key: str = "qty") -> Snapshot:
    """Drain a cursor-paginated position endpoint into a :class:`Snapshot`.

    ``fetch_page(cursor) -> (rows, next_cursor)``; ``next_cursor`` is ``None`` at the end.
    """
    positions: dict[str, float] = {}
    seen: set[str] = set()
    cursor: str | None = None
    try:
        for _ in range(max_pages):
            rows, nxt = fetch_page(cursor)
            for row in rows:
                sym = str(row[symbol_key]).strip().upper()
                qty = float(row[qty_key])
                if qty != qty:  # NaN
                    return Snapshot(positions, False, "qty_not_numeric")
                positions[sym] = positions.get(sym, 0.0) + qty
            if nxt is None:
                return Snapshot(positions, True)
            if nxt in seen:
                return Snapshot(positions, False, "cursor_repeated")
            seen.add(nxt)
            cursor = nxt
        return Snapshot(positions, False, "pagination_truncated")
    except Exception as exc:  # broker errors -> unknown, never "empty"
        return Snapshot(positions, False, f"fetch_failed:{type(exc).__name__}")


@dataclass
class Drift:
    missing_at_broker: dict[str, float] = field(default_factory=dict)
    unknown_at_broker: dict[str, float] = field(default_factory=dict)
    qty_mismatch: dict[str, tuple[float, float]] = field(default_factory=dict)

    @property
    def clean(self) -> bool:
        return not (self.missing_at_broker or self.unknown_at_broker or self.qty_mismatch)


def reconcile(local: Mapping[str, float], snapshot: Snapshot, *, tolerance: float = 1e-9) -> Drift:
    if not snapshot.authoritative:
        raise ReconcileError(f"snapshot_not_authoritative:{snapshot.error or 'incomplete'}")
    broker = {k: v for k, v in snapshot.positions.items() if abs(v) > tolerance}
    loc = {k.strip().upper(): float(v) for k, v in local.items() if abs(float(v)) > tolerance}
    drift = Drift()
    for sym, qty in loc.items():
        if sym not in broker:
            drift.missing_at_broker[sym] = qty
        elif abs(broker[sym] - qty) > tolerance:
            drift.qty_mismatch[sym] = (qty, broker[sym])
    for sym, qty in broker.items():
        if sym not in loc:
            drift.unknown_at_broker[sym] = qty
    return drift
