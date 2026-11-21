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

    def test_sizing_respects_loss_limit_and_charges(self):
        s = review.size_idea(self.cfg, 200.0, 190.0)
        self.assertGreater(s["qty"], 0); from arena.riskplan import loss_limit; self.assertLessEqual(s["loss_at_stop"], loss_limit(self.cfg)); self.assertEqual(loss_limit(self.cfg), 3000.0)   # 3% of Rs 1 lakh
        self.assertEqual(review.size_idea(self.cfg, 200.0, 250.0)["qty"], 0)

    def test_risk_check_blocks_low_reward_and_full_book(self):
        ok = {"r": 2.5, "size": {"qty": 10}}
        self.assertEqual(review.risk_check(ok, self.cfg, 0)["verdict"], "PASS")
        self.assertEqual(review.risk_check({**ok, "r": 1.2}, self.cfg, 0)["verdict"], "BLOCK")
        self.assertEqual(review.risk_check(ok, self.cfg, self.cfg["ideas"]["max_live"])["verdict"], "BLOCK")
        self.assertEqual(review.risk_check({**ok, "size": {"qty": 0}}, self.cfg, 0)["verdict"], "BLOCK")

    def test_daily_time_machine_is_repeatable(self):
        d = frame(); e = float(d["close"].iloc[-1])
        a = timemachine.run_daily(d, e, e * 0.95, e * 1.10, 15, self.cfg, n=300); b = timemachine.run_daily(d, e, e * 0.95, e * 1.10, 15, self.cfg, n=300)
        self.assertEqual(a, b); self.assertGreater(a["real"]["sessions"], 20); self.assertIn("simulation", a["note"].lower())

    def test_judge_uses_time_machine_with_enough_samples(self):
        m = memo(self.cfg, setups=["breakout20"]); base = judge(m, {}, self.cfg)["score"]
        bad = {**m, "timemachine": {"real": {"sessions": 40}, "verdict": "past sessions lost money after charges: unfavourable"}}
        self.assertEqual(judge(bad, {}, self.cfg)["score"], base - 1)
        few = {**m, "timemachine": {"real": {"sessions": 5}, "verdict": "past sessions lost money after charges: unfavourable"}}
        self.assertEqual(judge(few, {}, self.cfg)["score"], base)
        self.cfg["ideas"]["timemachine_gate"] = True
        try: self.assertEqual(judge(bad, {}, self.cfg)["verdict"], "REJECT")
        finally: self.cfg["ideas"]["timemachine_gate"] = False

    def test_scan_adds_review_block_and_approve_respects_risk(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); d = frame()
            pen = ideas.scan(self.cfg, FakeShort({"AAA": d}, {"AAA": [{"setup": "breakout20", "why": "x"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": 3}, moms={"AAA": 0.5})
            q = pen["queue"][0]
            for k in ("review", "size", "risk", "entry_state"): self.assertIn(k, q)
            self.assertIn("bull", q["review"]); self.assertIn("proof", q["review"]["time_machine"])
            q["risk"] = {"verdict": "BLOCK", "reasons": ["test"]}; ideas._save(pp, pen)
            with self.assertRaises(ValueError): ideas.approve(q["id"], "me", NOW, pp, pu)
            self.assertEqual(ideas.approve(q["id"], "me", NOW, pp, pu, force=True)["approved_by"], "me")

    def test_monitor_sets_entry_state_and_1r_advice(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); d = frame()
            ideas.scan(self.cfg, FakeShort({"AAA": d}, {"AAA": [{"setup": "breakout20", "why": "x"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": 3}, moms={"AAA": 0.5})
            qid = ideas.pending(pp)["queue"][0]["id"]; ideas.approve(qid, "me", NOW, pp, pu, force=True)
            live = ideas.public(pu)["live"][0]; e, s = live["entry_price"], live["stop"]
            d2 = d.copy(); import pandas as pd
            nxt = pd.DataFrame({"open": [e], "high": [e + (e - s) * 1.1], "low": [e * 0.999], "close": [e * 1.01], "volume": [3e6]}, index=[d.index[-1] + pd.offsets.BDay(1)])
            ideas.monitor(self.cfg, {"AAA": pd.concat([d2, nxt])}, NOW, pu)
            i = ideas.public(pu)["live"][0]
            self.assertTrue(i["entry_state"]); self.assertEqual(i["stop_advice"]["move_stop_to"], round(e, 2))


if __name__ == "__main__":
    unittest.main()
