import unittest

from trade_safety_kit.brokers import Broker
from trade_safety_kit.brokers.alpaca import AlpacaClient, AlpacaConfig, to_alpaca_symbol
from trade_safety_kit.reconcile import ReconcileError, Snapshot, collect_pages, reconcile


class AlpacaTests(unittest.TestCase):
    def test_config_from_env_defaults_to_dry_run_paper(self):
        self.assertIsNone(AlpacaConfig.from_env({}))
        cfg = AlpacaConfig.from_env({"ALPACA_API_KEY": "fake-key", "ALPACA_SECRET_KEY": "fake-secret"})
        self.assertFalse(cfg.execute)
        self.assertTrue(cfg.paper)

    def test_dry_run_never_sends(self):
        class Boom:
            def open(self, *a, **k):
                raise AssertionError("network used")

        client = AlpacaClient(AlpacaConfig("fake-key", "fake-secret"), opener=Boom())
        self.assertIsInstance(client, Broker)
        out = client.submit_order("brk-b", 1, "buy", limit_price=10.0)
        self.assertTrue(out["dry_run"])
        self.assertEqual(out["body"]["symbol"], "BRK.B")
        with self.assertRaises(ValueError):
            client.submit_order("FAKEA", 1, "buy")  # limit without price
        self.assertEqual(to_alpaca_symbol("bf-b"), "BF.B")


class ReconcileTests(unittest.TestCase):
    def test_pagination_complete(self):
        pages = {None: ([{"symbol": "fakea", "qty": "2"}], "c1"), "c1": ([{"symbol": "FAKEB", "qty": 1}], None)}
        snap = collect_pages(lambda c: pages[c])
        self.assertTrue(snap.authoritative)
        drift = reconcile({"FAKEA": 2, "FAKEC": 5}, snap)
        self.assertEqual(drift.missing_at_broker, {"FAKEC": 5.0})
        self.assertEqual(drift.unknown_at_broker, {"FAKEB": 1.0})

    def test_incomplete_is_unknown_not_empty(self):
        loop = collect_pages(lambda c: ([], "same"))
        self.assertEqual(loop.error, "cursor_repeated")

        def boom(_):
            raise OSError("down")

        err = collect_pages(boom)
        self.assertFalse(err.authoritative)
        with self.assertRaises(ReconcileError):
            reconcile({"FAKEA": 1}, err)
        self.assertTrue(reconcile({"FAKEA": 1}, Snapshot({"FAKEA": 1.0}, True)).clean)


if __name__ == "__main__":
    unittest.main()
