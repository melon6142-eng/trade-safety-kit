"""Minimal broker interface shared by adapters.

Adapters must be dry-run by default: no order leaves the process unless the
caller explicitly enables execution.
"""
from __future__ import annotations

from typing import Any, Protocol, runtime_checkable


@runtime_checkable
class Broker(Protocol):
    execute: bool

    def get_account(self) -> dict[str, Any]: ...

    def list_positions(self) -> list[dict[str, Any]]: ...

    def submit_order(self, symbol: str, qty: float, side: str, *, order_type: str = "limit",
                     limit_price: float | None = None, client_order_id: str | None = None) -> dict[str, Any]: ...

    def close_position(self, symbol: str) -> dict[str, Any]: ...
