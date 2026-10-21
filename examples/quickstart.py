"""Minimal runnable demo - no network, no credentials."""
import json
import tempfile
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trade_safety_kit import allowlist
from trade_safety_kit.entry_levels import validate_entry_levels
from trade_safety_kit.transport_gate import TransportDenied, TransportPolicy

now = datetime.now(timezone.utc)
path = Path(tempfile.mkdtemp()) / "allowlist.json"
path.write_text(json.dumps({
    "schema": allowlist.SCHEMA,
    "expires_at": (now + timedelta(hours=1)).isoformat(),
    "max_total_notional": 1000.0,
    "entries": [{"symbol": "FAKEA", "quantity": 5, "limit_price": 20.0}],
}))

print(validate_entry_levels(20.0, 19.0, 23.0))

policy = TransportPolicy(origin="https://broker.example.test",
                         entry_paths=frozenset({"/v1/buy"}), allowlist_path=path)
claim = {"symbol": "FAKEA", "quantity": 5, "limit_price": 20.0}
print(policy.classify("POST", "https://broker.example.test/v1/buy", entry_claim=claim))
try:
    policy.classify("POST", "https://broker.example.test/v1/buy", entry_claim={**claim, "quantity": 6})
except TransportDenied as exc:
    print("denied:", exc)
