"""Paired-statistics guards.

Why these exist: the two shipped backtest reports compared profile arms by
*total* R, which folds "seats the gate removed" into the improvement and made
54-session samples look decisive. The tests pin the behaviours that keep a
delta honest: seat matching, null-R exclusion (a blocked seat is not a zero),
a deterministic bootstrap, an exact sign test, and the n<30 verdict gate.
"""
from __future__ import annotations

import json
import unittest
from pathlib import Path

from trade_safety_kit import stats as st


def _rows(pairs):
    return [{"candidate_id": cid, "r": r} for cid, r in pairs]


class BootstrapTests(unittest.TestCase):
    def test_ci_is_deterministic_for_a_seed(self):
        values = [1.0, -1.0, 0.5, 2.0, -0.25, 0.75]
        first = st.bootstrap_mean_ci(values, draws=200, seed=7)
        second = st.bootstrap_mean_ci(values, draws=200, seed=7)
        self.assertEqual(first, second)

    def test_ci_brackets_the_mean(self):
        values = [1.0, -1.0, 0.5, 2.0, -0.25, 0.75]
        lo, hi = st.bootstrap_mean_ci(values, draws=500, seed=7)
        self.assertLessEqual(lo, sum(values) / len(values))
        self.assertGreaterEqual(hi, sum(values) / len(values))

    def test_total_ci_scales_with_n(self):
        values = [1.0] * 10
        lo, hi = st.bootstrap_total_ci(values, draws=50, seed=3)
        self.assertEqual((lo, hi), (10.0, 10.0))

    def test_empty_sample_has_no_ci(self):
        self.assertIsNone(st.bootstrap_mean_ci([]))
        self.assertIsNone(st.bootstrap_total_ci([]))


class SignTestTests(unittest.TestCase):
    def test_three_wins_no_losses_is_not_significant(self):
        self.assertAlmostEqual(st.sign_test(3, 0), 0.25)

    def test_balanced_is_p_one(self):
        self.assertAlmostEqual(st.sign_test(6, 6), 1.0)

    def test_many_wins_is_significant(self):
        self.assertLess(st.sign_test(18, 3), 0.01)

    def test_no_observations(self):
        self.assertEqual(st.sign_test(0, 0), 1.0)


class PairedDeltaTests(unittest.TestCase):
    def test_matches_seats_and_excludes_null_r(self):
        off = _rows([("A:x", -1.0), ("B:x", 0.5), ("C:x", -1.0)])
        on = _rows([("A:x", 1.0), ("B:x", 0.5), ("C:x", None)])
        delta = st.paired_delta(off, on, draws=100, seed=5)
        self.assertEqual(delta["n_shared"], 3)
        self.assertEqual(delta["n_paired"], 2)
        self.assertEqual(delta["n_excluded_null"], 1)
        self.assertAlmostEqual(delta["delta_total_r"], 2.0)

    def test_seats_only_one_arm_traded_are_not_in_the_delta(self):
        off = _rows([("A:x", -1.0), ("B:x", -1.0)])
        on = _rows([("A:x", 1.0), ("D:x", 3.0)])
        delta = st.paired_delta(off, on, draws=100, seed=5)
        self.assertEqual(delta["n_on_only"], 1)
        self.assertEqual(delta["n_off_only"], 1)
        self.assertAlmostEqual(delta["delta_total_r"], 2.0)

    def test_small_sample_is_provisional_not_significant(self):
        off = _rows([(f"S{i}", -1.0) for i in range(5)])
        on = _rows([(f"S{i}", 1.0) for i in range(5)])
        delta = st.paired_delta(off, on, draws=200, seed=5)
        self.assertEqual(delta["verdict"], "provisional")
        self.assertFalse(delta["significant"])

    def test_large_consistent_improvement_is_significant(self):
        off = _rows([(f"S{i}", -1.0) for i in range(40)])
        on = _rows([(f"S{i}", 1.0) for i in range(40)])
        delta = st.paired_delta(off, on, draws=400, seed=5)
        self.assertEqual(delta["verdict"], "reportable")
        self.assertTrue(delta["significant"])
        self.assertLess(delta["sign_p"], 0.01)

    def test_noise_is_not_significant(self):
        # Half the seats gain 0.2R, half lose 0.2R: a mean delta of zero.
        off = _rows([(f"S{i}", -1.0) for i in range(40)])
        on = _rows([(f"S{i}", -0.8 if i % 2 else -1.2) for i in range(40)])
        delta = st.paired_delta(off, on, draws=400, seed=5)
        self.assertFalse(delta["significant"])
        self.assertGreater(delta["sign_p"], 0.05)

    def test_no_shared_seats_reports_no_data(self):
        delta = st.paired_delta(_rows([("A:x", -1.0)]), _rows([("B:x", 1.0)]))
        self.assertEqual(delta["verdict"], "no_data")

    def test_format_names_the_verdict(self):
        off = _rows([(f"S{i}", -1.0) for i in range(4)])
        on = _rows([(f"S{i}", 1.0) for i in range(4)])
        line = st.format_paired("neutral/h1_b", st.paired_delta(off, on, draws=100, seed=1))
        self.assertIn("not-significant", line)
        self.assertIn("provisional", line)


class SampleVerdictTests(unittest.TestCase):
    def test_floor_is_thirty_seats(self):
        self.assertEqual(st.sample_verdict(29), "provisional")
        self.assertEqual(st.sample_verdict(30), "reportable")


if __name__ == "__main__":
    unittest.main()
