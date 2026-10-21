"""Alpaca trading adapter (stdlib only, dry-run by default).

Configuration comes from environment variables only:

  ALPACA_API_KEY, ALPACA_SECRET_KEY
  ALPACA_BASE_URL   default https://paper-api.alpaca.markets
  ALPACA_EXECUTE=1  actually send orders (default: dry-run, nothing is sent)

Pass ``opener`` (e.g. a :class:`trade_safety_kit.transport_gate.GuardedOpener`)
to route every request through the deny-by-default transport gate.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

PAPER_URL = "https://paper-api.alpaca.markets"
_FALSE = {"0", "false", "no", ""}


def to_alpaca_symbol(ticker: str) -> str:
    """``BRK-B`` -> ``BRK.B``"""
    return ticker.strip().upper().replace("-", ".")


@dataclass
class AlpacaConfig:
    api_key: str
    secret_key: str
    base_url: str = PAPER_URL
    execute: bool = False

    @property
    def paper(self) -> bool:
        return self.base_url.startswith(PAPER_URL)

    @classmethod
    def from_env(cls, env: dict[str, str] | None = None) -> "AlpacaConfig | None":
        env = os.environ if env is None else env
        key = (env.get("ALPACA_API_KEY") or "").strip()
        sec = (env.get("ALPACA_SECRET_KEY") or "").strip()
        if not key or not sec:
            return None
        base = (env.get("ALPACA_BASE_URL") or PAPER_URL).strip().rstrip("/")
        if base.endswith("/v2"):
            base = base[:-3]
        execute = (env.get("ALPACA_EXECUTE") or "0").strip().lower() not in _FALSE
        return cls(api_key=key, secret_key=sec, base_url=base, execute=execute)


class AlpacaClient:
    def __init__(self, cfg: AlpacaConfig, opener: Any = None):
        self.cfg = cfg
        self._opener = opener

    @property
    def execute(self) -> bool:
        return self.cfg.execute

    def _request(self, method: str, path: str, body: dict[str, Any] | None = None) -> Any:
        headers = {"APCA-API-KEY-ID": self.cfg.api_key, "APCA-API-SECRET-KEY": self.cfg.secret_key}
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(f"{self.cfg.base_url}{path}", data=data, headers=headers, method=method)
        try:
            if self._opener is not None:
                resp = self._opener.open(req, timeout=30)
            else:
                resp = urllib.request.urlopen(req, timeout=30)
            with resp:
                raw = resp.read().decode("utf-8")
                return json.loads(raw) if raw else {}
        except urllib.error.HTTPError as exc:
            raise RuntimeError(f"alpaca_http_{exc.code}") from None

    def get_account(self) -> dict[str, Any]:
        return self._request("GET", "/v2/account")

    def list_positions(self) -> list[dict[str, Any]]:
        data = self._request("GET", "/v2/positions")
        return data if isinstance(data, list) else []

    def submit_order(self, symbol: str, qty: float, side: str, *, order_type: str = "limit",
                     limit_price: float | None = None, client_order_id: str | None = None,
                     time_in_force: str = "day") -> dict[str, Any]:
        side = side.lower()
        if side not in {"buy", "sell"}:
            raise ValueError("bad_side")
        if order_type not in {"limit", "market"}:
            raise ValueError("bad_order_type")
        qty_s = f"{qty:.6f}".rstrip("0").rstrip(".")
        if not qty_s or float(qty_s) <= 0:
            raise ValueError("qty_must_be_positive")
        body: dict[str, Any] = {"symbol": to_alpaca_symbol(symbol), "qty": qty_s, "side": side,
                                "type": order_type, "time_in_force": time_in_force}
        if order_type == "limit":
            if limit_price is None or not limit_price > 0:
                raise ValueError("limit_price_required")
            body["limit_price"] = f"{limit_price:.2f}"
        if client_order_id:
            body["client_order_id"] = client_order_id[:48]
        if not self.cfg.execute:
            return {"id": "dry-run", "status": "dry_run", "dry_run": True, "body": body}
        return self._request("POST", "/v2/orders", body)

    def close_position(self, symbol: str) -> dict[str, Any]:
        sym = to_alpaca_symbol(symbol)
        if not self.cfg.execute:
            return {"id": "dry-run-close", "status": "dry_run", "symbol": sym, "dry_run": True}
        return self._request("DELETE", f"/v2/positions/{sym}")


def get_client(opener: Any = None) -> AlpacaClient | None:
    cfg = AlpacaConfig.from_env()
    return AlpacaClient(cfg, opener=opener) if cfg else None
