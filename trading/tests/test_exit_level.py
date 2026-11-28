"""Nov 2026: momentum ideas show an "Exit level" price instead of the rank rule (pipeline core.rank_exit_prices).
Run: python -m unittest discover tests"""
from __future__ import annotations
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT.parent / "pipeline"))
import numpy as np
import core


def hist(n=80, days=270, seed=3):
    rng = np.random.default_rng(seed); closes = {}
    for k in range(n):
        drift = 0.0002 + 0.00002 * k                      # later stocks trend harder
        closes[f"S{k:02d}"] = [round(float(x), 2) for x in 100 * np.cumprod(1 + rng.normal(drift, 0.015, days))]
    dates = [f"2026-{1 + d // 28:02d}-{1 + d % 28:02d}" for d in range(days)]
    return {"dates": dates, "universe": list(closes), "rank_universe": list(closes), "closes": closes}


class ExitLevel(unittest.TestCase):
    def test_level_is_the_rank_boundary(self):
        h = hist(); today = h["dates"][-1]
        prices = {s: c[-1] for s, c in h["closes"].items()}
        rk, _ = core.ranks_now(h, prices, today)
        top = [s for s, r in sorted(rk.items(), key=lambda x: x[1])[:10]]
        lv = core.rank_exit_prices(h, prices, today, top)
        for s in top:
            v = lv[s]; self.assertIsNotNone(v); self.assertLess(v, prices[s])
            above = dict(prices, **{s: v * 1.002}); below = dict(prices, **{s: v * 0.998})
            self.assertLessEqual(core.ranks_now(h, above, today)[0][s], core.EXIT_RANK)
            self.assertGreater(core.ranks_now(h, below, today)[0].get(s, 999), core.EXIT_RANK)

    def test_already_below_returns_todays_price(self):
        h = hist(); today = h["dates"][-1]
        prices = {s: c[-1] for s, c in h["closes"].items()}
        rk, _ = core.ranks_now(h, prices, today)
        weak = max(rk, key=rk.get)                                      # ranked last: already past the exit rank
        self.assertEqual(core.rank_exit_prices(h, prices, today, [weak])[weak], round(prices[weak], 2))

    def test_missing_data_gives_none(self):
        h = hist(); today = h["dates"][-1]
        prices = {s: c[-1] for s, c in h["closes"].items()}
        self.assertIsNone(core.rank_exit_prices(h, prices, today, ["NOPE"])["NOPE"])
        small = {**h, "rank_universe": h["universe"][:10]}                # fewer than 50 other stocks
        self.assertIsNone(core.rank_exit_prices(small, prices, today, ["S00"])["S00"])


if __name__ == "__main__":
    unittest.main()
