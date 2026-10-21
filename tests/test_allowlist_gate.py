import json
import tempfile
import unittest
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from trade_safety_kit import allowlist as allow
from trade_safety_kit.transport_gate import GuardedOpener, TransportDenied, TransportPolicy

ORIGIN = "https://broker.example.test"


def _doc(**over):
    now = datetime.now(timezone.utc)
    value = {
        "schema": allow.SCHEMA,
        "created_at": (now - timedelta(minutes=5)).isoformat(timespec="seconds"),
        "expires_at": (now + timedelta(hours=6)).isoformat(timespec="seconds"),
        "max_total_notional": 10_000.0,
        "entries": [
            {"symbol": "FAKEA", "quantity": 10, "limit_price": 12.34},
            {"symbol": "FAKEB", "quantity": 3, "limit_price": 100.0},
        ],
    }
    value.update(over)
    return value


class Base(unittest.TestCase):
    def setUp(self):
        self.path = Path(tempfile.mkdtemp()) / "allow.json"
        self.write(_doc())
        self.policy = TransportPolicy(origin=ORIGIN, read_paths=frozenset({"/v1/positions"}),
                                      entry_paths=frozenset({"/v1/buy"}), exit_paths=frozenset({"/v1/sell"}),
                                      allowlist_path=self.path)

    def write(self, value):
        self.path.write_text(json.dumps(value), encoding="utf-8")


class AllowlistTests(Base):
    def test_exact_order_matches(self):
        self.assertEqual(allow.match("fakea", 10, 12.34, path=self.path)["symbol"], "FAKEA")

    def test_neighbours_denied(self):
        for args, reason in [((("FAKEA", 9, 12.34)), "allowlist_quantity_mismatch"),
                             (("FAKEA", 10, 12.35), "allowlist_limit_price_mismatch"),
                             (("OTHER", 1, 1.0), "allowlist_symbol_not_listed"),
                             (("FAKEA", "10", 12.34), "allowlist_quantity_unknown"),
                             (("FAKEA", 10, None), "allowlist_limit_price_unknown")]:
            with self.assertRaises(allow.AllowlistDenied) as ctx:
                allow.match(*args, path=self.path)
            self.assertEqual(str(ctx.exception), reason)

    def test_expired_and_missing(self):
        now = datetime.now(timezone.utc)
        self.write(_doc(created_at=(now - timedelta(hours=2)).isoformat(),
                        expires_at=(now - timedelta(hours=1)).isoformat()))
        with self.assertRaisesRegex(allow.AllowlistDenied, "allowlist_expired"):
            allow.match("FAKEA", 10, 12.34, path=self.path)
        self.path.unlink()
        with self.assertRaisesRegex(allow.AllowlistDenied, "allowlist_missing"):
            allow.match("FAKEA", 10, 12.34, path=self.path)

    def test_malformed_fails_closed(self):
        self.path.write_text("{not json", encoding="utf-8")
        with self.assertRaisesRegex(allow.AllowlistDenied, "json_invalid"):
            allow.load(self.path)
        self.write(_doc(max_total_notional=1.0))
        with self.assertRaisesRegex(allow.AllowlistDenied, "below_named_orders"):
            allow.load(self.path)

    def test_unconfigured_path_denies(self):
        with self.assertRaisesRegex(allow.AllowlistDenied, "path_unconfigured"):
            allow.load()

    def test_seal_within_budget(self):
        self.write(_doc(reprice_at_send=True, entries=[
            {"symbol": "FAKEA", "quantity": 10, "limit_price": 12.34, "budget": 200.0}]))
        allow.seal("FAKEA", 15, 13.0, path=self.path)
        self.assertEqual(allow.match("FAKEA", 15, 13.0, path=self.path)["quantity"], 15)
        with self.assertRaisesRegex(allow.AllowlistDenied, "over_budget"):
            allow.seal("FAKEA", 100, 13.0, path=self.path)


class GateTests(Base):
    def test_entry_requires_matching_claim(self):
        with self.assertRaisesRegex(TransportDenied, "no_entry_claim"):
            self.policy.classify("POST", ORIGIN + "/v1/buy")
        claim = {"symbol": "FAKEA", "quantity": 10, "limit_price": 12.34}
        self.assertEqual(self.policy.classify("POST", ORIGIN + "/v1/buy", entry_claim=claim), "entry_allowlisted")
        with self.assertRaisesRegex(TransportDenied, "quantity_mismatch"):
            self.policy.classify("POST", ORIGIN + "/v1/buy", entry_claim={**claim, "quantity": 11})

    def test_reads_exits_and_unknowns(self):
        self.assertEqual(self.policy.classify("GET", ORIGIN + "/v1/positions"), "read")
        self.assertEqual(self.policy.classify("POST", ORIGIN + "/v1/sell"), "exit")
        for method, url in [("POST", "https://evil.example.test/v1/sell"), ("POST", ORIGIN + "/v1/other"),
                            ("POST", ORIGIN + "/v1/sell?x=1"), ("DELETE", ORIGIN + "/v1/sell"),
                            ("POST", "https://u:p@broker.example.test/v1/sell")]:
            with self.assertRaises(TransportDenied):
                self.policy.classify(method, url)

    def test_guarded_opener_blocks_before_wire(self):
        sent = []

        class Delegate:
            def open(self, req, timeout=None):
                sent.append(req.full_url)
                return "ok"

        opener = GuardedOpener(Delegate(), self.policy)
        self.assertEqual(opener.open(urllib.request.Request(ORIGIN + "/v1/positions")), "ok")
        with self.assertRaises(TransportDenied):
            opener.open(urllib.request.Request(ORIGIN + "/v1/buy", data=b"{}", method="POST"))
        req = urllib.request.Request(ORIGIN + "/v1/positions", headers={"Host": "evil.example.test"})
        with self.assertRaises(TransportDenied):
            opener.open(req)
        self.assertEqual(sent, [ORIGIN + "/v1/positions"])


if __name__ == "__main__":
    unittest.main()
