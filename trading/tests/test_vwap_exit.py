"""Nov 2026: bull flags still open on their last session are sold through that day, counted at the day's VWAP (pipeline/exit_check.py).
Run: python -m unittest discover tests"""
from __future__ import annotations
import sys, tempfile, unittest
from datetime import date
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT.parent / "pipeline")); sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
from arena import ideas
from arena.clock import IST
from arena.config import load_config
from test_ideas import frame, paths, FakeShort, FakeCommittee, NOW


class VwapTimeExit(unittest.TestCase):
    cfg = load_config()

    def _live(self, t):
        pp, pu = paths(t)
        q = ideas.scan(self.cfg, FakeShort({"AAA": frame()}, {"AAA": [{"setup": "bull_flag", "why": "b"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)["queue"][0]
        return ideas.approve(q["id"], "S", NOW, pp, pu), pp, pu

    def _flat(self, idea, n):
        """n sessions after posting that stay inside the entry range, never near the stop or the target."""
        p = idea["entry_price"]; base = frame()
        idx = pd.bdate_range(base.index[-1] + pd.Timedelta(days=1), periods=n, tz=IST)
        add = pd.DataFrame([(p, p * 1.003, p * 0.997, p)] * n, index=idx, columns=["open", "high", "low", "close"]); add["volume"] = 3e6
        return pd.concat([base, add])

    def test_config_uses_vwap(self):
        self.assertEqual(self.cfg["ideas"]["short_time_exit"], "vwap")

    def test_last_session_flagged_the_day_before(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); n = idea["days"] - 1
            with mock.patch.object(ideas, "VWAP_FN", side_effect=AssertionError("no VWAP before the last day")):
                g = ideas.monitor(self.cfg, {"AAA": self._flat(idea, n)}, NOW, pu)
            self.assertEqual(g, []); live = ideas.public(pu)["live"][0]
            self.assertTrue(live["last_day"]); self.assertEqual(live["days_held"], n)

    def test_time_exit_at_the_days_vwap(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); n = idea["days"]; fr = self._flat(idea, n); vw = round(idea["entry_price"] * 1.004, 2)
            seen = []
            with mock.patch.object(ideas, "VWAP_FN", side_effect=lambda s, d: seen.append((s, d)) or vw):
                g = ideas.monitor(self.cfg, {"AAA": fr}, NOW, pu)
            self.assertEqual(g[0]["reason"], "Time up"); self.assertEqual(g[0]["price"], vw); self.assertIn("VWAP", g[0]["note"])
            self.assertEqual(seen, [("AAA", fr.index[-1].date())])                           # the last held session
            self.assertFalse(ideas.public(pu)["live"][0]["last_day"])
            done = ideas.close("AAA", "CEO", NOW, public_path=pu)
            self.assertEqual(done["exit_price"], vw); self.assertEqual(done["close_kind"], "time")

    def test_falls_back_to_the_close_when_vwap_is_missing(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); fr = self._flat(idea, idea["days"])
            with mock.patch.object(ideas, "VWAP_FN", return_value=None):
                g = ideas.monitor(self.cfg, {"AAA": fr}, NOW, pu)
            self.assertEqual(g[0]["reason"], "Time up"); self.assertEqual(g[0]["price"], round(float(fr["close"].iloc[-1]), 2))
            self.assertIn("not available", g[0]["note"])

    def test_close_rule_kept_when_switched_off(self):
        cfg = {**self.cfg, "ideas": {**self.cfg["ideas"], "short_time_exit": "close"}}
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); fr = self._flat(idea, idea["days"])
            with mock.patch.object(ideas, "VWAP_FN", side_effect=AssertionError("not called")):
                g = ideas.monitor(cfg, {"AAA": fr}, NOW, pu)
            self.assertEqual(g[0]["price"], round(float(fr["close"].iloc[-1]), 2)); self.assertNotIn("note", g[0])

    def test_day_vwap_formula(self):
        ts0 = int(pd.Timestamp("2026-11-27 09:15", tz="Asia/Kolkata").timestamp())
        n = 75; tsl = [ts0 + 300 * k for k in range(n)]
        hi = [101.0] * n; lo = [99.0] * n; cl = [100.0] * 74 + [130.0]; vol = [1000] * 74 + [74000]
        body = {"chart": {"result": [{"timestamp": tsl, "indicators": {"quote": [{"high": hi, "low": lo, "close": cl, "volume": vol}]}}]}}
        class R:
            def raise_for_status(self): pass
            def json(self): return body
        with mock.patch("requests.get", return_value=R()):
            v = ideas.day_vwap("AAA", date(2026, 11, 27))
        tp_last = (101 + 99 + 130) / 3
        self.assertAlmostEqual(v, round((74 * 1000 * 100 + 74000 * tp_last) / (148000), 2))
        with mock.patch("requests.get", return_value=R()):
            self.assertIsNone(ideas.day_vwap("AAA", date(2026, 11, 26)))                  # another day: no candles
        with mock.patch("requests.get", side_effect=OSError("down")):
            self.assertIsNone(ideas.day_vwap("AAA", date(2026, 11, 27)))


if __name__ == "__main__":
    unittest.main()
