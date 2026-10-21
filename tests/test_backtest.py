import unittest

import pandas as pd

from trade_safety_kit import backtest as bt


def bars(n=80):
    rows = []
    for i in range(n):
        base = 100 + i * 0.5
        rows.append({"Open": base, "High": base + 2, "Low": base - 2, "Close": base + 0.3})
    return pd.DataFrame(rows, index=pd.date_range("2020-01-01", periods=n, freq="D"))


class LeakageGuardTests(unittest.TestCase):
    def test_signal_never_sees_future_bars(self):
        df = bars()
        seen = []

        def sig(window):
            seen.append(window.index[-1])
            return None

        bt.run(df, sig, bt.BTConfig(warmup_bars=10))
        for i, last in enumerate(seen, start=10):
            self.assertEqual(last, df.index[i])

    def test_fills_next_open_not_signal_close(self):
        df = bars()

        def sig(window):
            c = float(window["Close"].iloc[-1])
            return bt.Signal(entry=c, stop=c - 3, target=c + 6)

        out = bt.run(df, sig, bt.BTConfig(warmup_bars=10, fee_bps=0, slip_bps=0))
        t = out["trades"][0]
        self.assertEqual(t["entry_i"], t["signal_i"] + 1)
        self.assertEqual(t["entry"], float(df["Open"].iloc[t["entry_i"]]))

    def test_gap_through_stop_fills_at_open(self):
        self.assertEqual(bt.threshold_exit_price(90, 85, 95, 95, 110), (90, "stop"))
        self.assertEqual(bt.threshold_exit_price(100, 94, 111, 95, 110), (95, "stop"))  # stop-first

    def test_prior_rolling_max_excludes_current_bar(self):
        s = pd.Series([1.0, 2.0, 3.0, 10.0])
        self.assertEqual(bt.prior_rolling_max(s, 2).iloc[3], 3.0)


if __name__ == "__main__":
    unittest.main()
