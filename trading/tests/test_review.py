"""Review layer: bull/bear debate, entry timer, 1R advice, sizing, risk check, daily Time Machine, wiring into scan/approve/monitor."""
from __future__ import annotations
import sys, tempfile, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "tests"))
from arena import ideas, review, timemachine
from arena.config import load_config
from arena.judge import judge
from test_ideas import FakeCommittee, FakeShort, NOW, frame, memo, paths


class Review(unittest.TestCase):
    cfg = load_config()

    def test_debate_has_both_sides_and_economy(self):
        m = memo(self.cfg, setups=["breakout20"], fund="concerning")
        d = review.debate(m, {"stance": "cautious", "reasons": ["India VIX 25 is elevated"]})
        self.assertTrue(d["bull"]); self.assertTrue(any("economic" in x for x in d["bear"])); self.assertIn("concerning", " ".join(d["bear"]))

    def test_entry_state(self):
        i = {"entry_price": 100, "entry_lo": 99, "entry_hi": 101, "stop": 95}
        self.assertTrue(review.entry_state(i, 100).startswith("triggered"))
        self.assertTrue(review.entry_state(i, 105).startswith("waiting: above"))
        self.assertTrue(review.entry_state(i, 97).startswith("waiting: below"))
        self.assertTrue(review.entry_state(i, 94).startswith("invalidated"))

    def test_one_r_advice(self):
        i = {"entry_price": 100, "stop": 95}
        self.assertIsNone(review.one_r_advice(i, 104.9)); self.assertEqual(review.one_r_advice(i, 105)["move_stop_to"], 100)

    def test_risk_check_on_the_idea_itself(self):
        ok = {"r": 2.5, "term": "short", "entry_price": 100.0, "stop": 95.0, "ret1": 0.02}
        self.assertEqual(review.risk_check(ok, self.cfg, 0)["verdict"], "PASS")
        self.assertEqual(review.risk_check({**ok, "r": 1.2}, self.cfg, 0)["verdict"], "BLOCK")
        self.assertEqual(review.risk_check({**ok, "term": "medium", "r": 1.2}, self.cfg, 0)["verdict"], "PASS")       # momentum: no reward-to-risk rule
        self.assertEqual(review.risk_check(ok, self.cfg, self.cfg["ideas"]["max_live"])["verdict"], "BLOCK")
        self.assertEqual(review.risk_check({**ok, "stop": 80.0}, self.cfg, 0)["verdict"], "BLOCK")                  # short-term stop 20% away
        self.assertEqual(review.risk_check({**ok, "ret1": 0.6}, self.cfg, 0)["verdict"], "BLOCK")                    # possible unadjusted split

    def test_daily_time_machine_is_repeatable(self):
        d = frame(); e = float(d["close"].iloc[-1])
        a = timemachine.run_daily(d, e, e * 0.95, e * 1.10, 15, self.cfg, n=300); b = timemachine.run_daily(d, e, e * 0.95, e * 1.10, 15, self.cfg, n=300)
        self.assertEqual(a, b); self.assertIn("simulation", a["note"].lower()); self.assertIn("drift-neutral", a["method"])
        self.assertEqual(a["real"]["sessions"], 0)                       # 300 bars: not enough history before the selection year
        long = frame(n=520); e2 = float(long["close"].iloc[-1]); c = timemachine.run_daily(long, e2, e2 * 0.95, e2 * 1.10, 15, self.cfg, n=200)
        self.assertGreater(c["real"]["sessions"], 20)                    # replay uses only the bars before the last 252

    def test_judge_uses_time_machine_with_enough_samples(self):
        m = memo(self.cfg, setups=["breakout20"]); base = judge(m, {}, self.cfg)["score"]
        bad = {**m, "timemachine": {"sim": {"target": 10, "stop": 30}, "real": {"sessions": 40}, "verdict": "past sessions lost money after charges: unfavourable"}}
        self.assertEqual(judge(bad, {}, self.cfg)["score"], base - 1)
        few = {**m, "timemachine": {"sim": {"target": None}, "real": {"sessions": 5}, "verdict": "no history"}}
        self.assertEqual(judge(few, {}, self.cfg)["score"], base)
        self.cfg["ideas"]["timemachine_gate"] = True
        try: self.assertEqual(judge(bad, {}, self.cfg)["verdict"], "REJECT")
        finally: self.cfg["ideas"]["timemachine_gate"] = False

    def test_momentum_ideas_get_no_time_machine_and_no_1r_advice(self):        # audit fixes #10 and #11
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); d = frame()
            q = ideas.scan(self.cfg, FakeShort({"AAA": d}, {}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": 3}, moms={"AAA": 0.5})["queue"][0]
            self.assertEqual(q["term"], "medium"); self.assertIsNone(q["review"]["time_machine"]); self.assertEqual(q["fill_rule"], "next_open")
            ideas.approve(q["id"], "me", NOW, pp, pu); live = ideas.public(pu)["live"][0]; e, s = live["entry_price"], live["stop"]
            import pandas as pd
            nxt = pd.DataFrame({"open": [e], "high": [e + (e - s) * 1.1], "low": [e * 0.999], "close": [e * 1.01], "volume": [3e6]}, index=[d.index[-1] + pd.offsets.BDay(1)])
            ideas.monitor(self.cfg, {"AAA": pd.concat([d, nxt])}, NOW, pu)
            self.assertIsNone(ideas.public(pu)["live"][0]["stop_advice"])

    def test_scan_adds_review_block_and_approve_respects_risk(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); d = frame()
            pen = ideas.scan(self.cfg, FakeShort({"AAA": d}, {"AAA": [{"setup": "bull_flag", "why": "x"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)
            q = pen["queue"][0]
            for k in ("review", "risk", "entry_state"): self.assertIn(k, q)
            self.assertIn("bull", q["review"]); self.assertIn("proof", q["review"]["time_machine"])
            q["risk"] = {"verdict": "BLOCK", "reasons": ["test"]}; ideas._save(pp, pen)
            with self.assertRaises(ValueError): ideas.approve(q["id"], "me", NOW, pp, pu)
            self.assertEqual(ideas.approve(q["id"], "me", NOW, pp, pu, force=True)["approved_by"], "me")

    def test_monitor_sets_entry_state_and_1r_advice(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); d = frame()
            ideas.scan(self.cfg, FakeShort({"AAA": d}, {"AAA": [{"setup": "bull_flag", "why": "x"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)
            qid = ideas.pending(pp)["queue"][0]["id"]; ideas.approve(qid, "me", NOW, pp, pu, force=True)
            live = ideas.public(pu)["live"][0]; e, s = live["entry_price"], live["stop"]
            d2 = d.copy(); import pandas as pd
            nxt = pd.DataFrame({"open": [e], "high": [e + (e - s) * 1.1], "low": [e * 0.999], "close": [e * 1.01], "volume": [3e6]}, index=[d.index[-1] + pd.offsets.BDay(1)])
            ideas.monitor(self.cfg, {"AAA": pd.concat([d2, nxt])}, NOW, pu)
            i = ideas.public(pu)["live"][0]
            self.assertTrue(i["entry_state"]); self.assertEqual(i["stop_advice"]["move_stop_to"], round(e, 2))


if __name__ == "__main__":
    unittest.main()


class AuditV2(unittest.TestCase):
    cfg = load_config()
    def test_momentum_idea_approved_on_rank_without_trend_points(self):
        m = memo(self.cfg); m["analysts"]["technical"]["metrics"]["rank"] = 5
        v = judge(m, {"stance": "cautious", "reasons": ["x"]}, self.cfg)
        self.assertEqual(v["verdict"], "APPROVE"); self.assertFalse(any("50-day" in r["rule"] or "macro" in r["rule"] for r in v["rules"]))
    def test_drift_neutral_time_machine_ignores_the_run_up(self):
        import numpy as np
        d = frame(); rng = np.random.default_rng(1); d = d.copy(); d["close"] = d["close"] * np.exp(np.cumsum(rng.normal(0, 0.02, len(d))))
        d["high"] = d["close"] * 1.01; d["low"] = d["close"] * 0.99; e = float(d["close"].iloc[-1])
        r = timemachine.run_daily(d, e, e * 0.9, e * 1.1, 20, self.cfg, n=1000)
        self.assertLess(abs(r["sim"]["target"] - r["sim"]["stop"]), 12)   # symmetric levels, no drift: roughly even
