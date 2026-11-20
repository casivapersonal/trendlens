"""Fixes found on the first real scan: whole-word news flags, headline relevance, R-based daily Time Machine verdict."""
from __future__ import annotations
import sys, unittest
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
from arena.committee import Committee
from arena.timemachine import verdict_daily

S = Committee.score_headlines


class News(unittest.TestCase):
    def test_bank_and_urban_are_not_ban(self):
        r = S([("HDFC Bank share price today", ""), ("HDFC Bank urban lending", "")], "HDFC Bank Limited", "HDFCBANK"); self.assertEqual(r["stance"], "quiet")
    def test_past_tense_words_are_not_ed(self):
        r = S([("Gland Pharma scheduled one-on-one meeting with ADIA", "")], "Gland Pharma Limited", "GLAND"); self.assertEqual(r["stance"], "quiet")
    def test_real_flags_still_fire(self):
        for h, f in (("SEBI order against Acme Corp promoter", "sebi order"), ("ED raids Acme premises", "raid"), ("ED summons Acme chairman", "ED probe"), ("Acme banned from market", "ban")):
            r = S([(h, "")], "Acme Corp Limited", "ACME"); self.assertEqual(r["stance"], "negative flags", h); self.assertIn(f, r["neg_flags"])
    def test_unrelated_headlines_are_ignored(self):
        r = S([("Atlantic Acquisition Corp I: Performance & Quotes", ""), ("Brompton Energy Split Corp seasonal returns", "")], "Bajaj Holdings & Investment Limited", "BAJAJHLDNG")
        self.assertEqual(r["stance"], "quiet"); self.assertEqual(r["headlines"], [])
    def test_symbol_or_spaced_name_matches(self):
        self.assertEqual(S([("Welspun India Share Price - Live NSE: WELSPUNLIV", "")], "Welspun Living Limited", "WELSPUNLIV")["headlines"][0][:7], "Welspun")
        self.assertTrue(S([("JK Cement Ltd. acquires stake in solar firm", "")], "JKCEMENT", "JKCEMENT")["stance"] == "positive flags")
    def test_positive_flags_not_reported_as_negative(self):
        r = S([("Acme dividend declared; profit jumps", "")], "Acme Limited", "ACME"); self.assertEqual(r["neg_flags"], []); self.assertEqual(r["stance"], "positive flags")


class News2(unittest.TestCase):
    def test_bank_of_baroda_is_not_state_bank_of_india(self):
        r = S([("State Bank of India (SBIN.NS) Stock Price, News", ""), ("Bank of Baroda Q2 profit rises", "")], "Bank of Baroda Limited", "BANKBARODA")
        self.assertEqual(r["headlines"], ["Bank of Baroda Q2 profit rises"])
    def test_eds_is_not_ed(self):
        self.assertEqual(S([("GIC Re appoints Canara Bank, Bank of India EDs as directors", "")], "Canara Bank", "CANBK")["stance"], "quiet")


class QueueRoom(unittest.TestCase):
    def test_watch_items_do_not_block_new_approvals_and_are_rescored(self):
        import tempfile, copy
        from arena import ideas
        from arena.config import load_config
        sys.path.insert(0, str(ROOT / "tests"))
        from test_ideas import FakeCommittee, FakeShort, NOW, frame, paths
        cfg = load_config()
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t)
            ideas._save(pp, {"asof": "x", "queue": [{"id": f"Z{i}", "sym": f"Z{i}", "status": "watch"} for i in range(10)] + [{"id": "AAA-old", "sym": "AAA", "status": "watch"}], "log": []})
            pen = ideas.scan(cfg, FakeShort({"AAA": frame()}, {"AAA": [{"setup": "breakout20", "why": "x"}]}, {"AAA": .97}), FakeCommittee(cfg), NOW, pp, pu, ranks={"AAA": 3}, moms={"AAA": .5})
            ids = [q["id"] for q in pen["queue"]]
            self.assertNotIn("AAA-old", ids); self.assertTrue(any(i.startswith("AAA-") and i != "AAA-old" for i in ids))


class Verdict(unittest.TestCase):
    def test_high_reward_ideas_judged_in_r(self):
        real = {"sessions": 233, "net_pnl": 5.0}
        self.assertEqual(verdict_daily({"target": 17.1, "stop": 29.4}, real, 2.5), "close to a coin flip")   # raw rates said 'tilted toward the stop'
        self.assertEqual(verdict_daily({"target": 44.5, "stop": 36.3}, real, 3.0), "tilted toward the target")
    def test_losing_history_is_unfavourable(self):
        self.assertTrue(verdict_daily({"target": 13.7, "stop": 55.6}, {"sessions": 233, "net_pnl": -40.0}, 2.5).startswith("past sessions lost money"))
    def test_too_few_samples(self):
        self.assertIn("too few", verdict_daily({"target": 50, "stop": 10}, {"sessions": 5, "net_pnl": 1}, 2))

if __name__ == "__main__":
    unittest.main()
