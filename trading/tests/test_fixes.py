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
        self.assertEqual(S([("WELSPUNLIV shares rise on export orders", "")], "Welspun Living Limited", "WELSPUNLIV")["headlines"][0][:7], "WELSPUN")
        self.assertEqual(S([("Welspun India Share Price - Live NSE: WELSPUNLIV", "")], "Welspun Living Limited", "WELSPUNLIV")["headlines"], [])   # quote pages are not news
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


class CEOAndQueue(unittest.TestCase):
    """CEO posts / declines / closes; scan queue is re-scored and the log keeps one entry per day."""
    def setUp(self):
        import tempfile, json
        from pathlib import Path
        self.d = Path(tempfile.mkdtemp()); self.pen = self.d / "pending.json"; self.pub = self.d / "picks.json"
        self.pub.write_text(json.dumps({"live": [], "closed": [], "recent_alerts": [], "bars": {}}))
        self.cfg = {"ideas": {"max_live": 10, "max_new_per_day": 3, "entry_rank": 20}, "ceo": {"max_per_sector": 3}}

    def idea(self, sym, score=9, rank=None, lean="bull", tm="tilted toward the target", risk="PASS", verdict="APPROVE", tech="bullish"):
        return {"id": f"{sym}-2026-11-20-s", "sym": sym, "term": "short", "entry_price": 100.0, "entry_lo": 99, "entry_hi": 101, "stop": 95, "target": 112.5,
                "days": 15, "r": 2.5, "entry_rank": rank, "status": "awaiting decision", "entry_state": "triggered: price is inside the entry range",
                "risk": {"verdict": risk, "reasons": [] if risk == "PASS" else ["x"]}, "judge": {"verdict": verdict, "score": score, "rules": []},
                "review": {"lean": lean, "time_machine": {"verdict": tm}}, "committee": {"agreement": "analysts agree (supportive)", "macro": "calm",
                "analysts": {"technical": {"stance": tech}, "fundamentals": {"stance": "supportive"}, "news": {"stance": "quiet"}}}}

    def run_ceo(self, queue):
        import json
        from datetime import datetime
        from unittest import mock
        from arena import ceo, ideas
        self.pen.write_text(json.dumps({"asof": None, "queue": queue, "log": []}))
        with mock.patch("arena.committee.Committee._funds", return_value={"stocks": {}}):
            out = ceo.decide_entries(self.cfg, datetime(2026, 11, 23, 16, 20), ideas, pending_path=self.pen, public_path=self.pub)
        return {d["sym"]: d["decision"] for d in out}, json.loads(self.pub.read_text()), json.loads(self.pen.read_text())

    def test_posts_good_declines_blocked_and_doubtful(self):
        dec, pub, pen = self.run_ceo([self.idea("GOOD", 11, rank=5), self.idea("BLOCKED", 12, risk="BLOCK"),
                                      self.idea("DOUBT", 9, tm="tilted toward the stop"), self.idea("MOMO", 8, rank=10, tm="tilted toward the stop")])
        # audit fix #4: a Time Machine doubt is already inside the judge score; it is noted, not counted again as a veto
        self.assertEqual(dec, {"BLOCKED": "DECLINE", "GOOD": "POST", "DOUBT": "POST", "MOMO": "POST"})
        self.assertEqual({i["sym"] for i in pub["live"]}, {"GOOD", "MOMO", "DOUBT"}); self.assertEqual(pub["live"][0]["approved_by"], "CEO")
        self.assertTrue(any("already in the judge score" for n in next(i for i in pub["live"] if i["sym"] == "DOUBT")["ceo"]["noted"]))
        self.assertEqual({q["sym"]: q["status"] for q in pen["queue"]}, {"BLOCKED": "watch"}); self.assertEqual(len(pen["ceo_log"]), 4)

    def test_daily_limit(self):
        dec, pub, _ = self.run_ceo([self.idea(s, 10, rank=3) for s in ("A", "B", "C", "D")])
        self.assertEqual(sum(v == "POST" for v in dec.values()), 3)

    def test_exit_closes_flagged_idea(self):
        import json
        from datetime import datetime
        from arena import ceo, ideas
        live = {**self.idea("X"), "status": "live", "posted": "2026-11-10", "nifty_entry": None, "exit_signal": {"reason": "Target hit", "price": 112.5, "date": "2026-11-21"}}
        self.pub.write_text(json.dumps({"live": [live], "closed": [], "recent_alerts": [], "bars": {}}))
        out = ceo.decide_exits(self.cfg, datetime(2026, 11, 23, 16, 20), ideas, public_path=self.pub)
        pub = json.loads(self.pub.read_text())
        self.assertEqual(out[0]["reason"], "Target hit"); self.assertEqual(pub["live"], []); self.assertEqual(pub["closed"][0]["closed_by"], "CEO")
        self.assertIn("CLOSE X", pub["closed"][0]["ceo_exit"])
