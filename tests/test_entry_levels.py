import unittest

from trade_safety_kit.entry_levels import EntrySafetyError, validate_entry_levels


class EntryLevelTests(unittest.TestCase):
    def test_valid_long(self):
        out = validate_entry_levels(100, 95, 110)
        self.assertAlmostEqual(out["rr"], 2.0)
        self.assertAlmostEqual(out["risk_pct"], 0.05)

    def test_rejects(self):
        for args in [(100, 105, 110), (100, 95, 99), ("x", 95, 110), (float("nan"), 95, 110),
                     (100, 50, 200), (100, 99.999, 110), (-1, 0.5, 2)]:
            with self.assertRaises(EntrySafetyError):
                validate_entry_levels(*args)


if __name__ == "__main__":
    unittest.main()
