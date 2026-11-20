"""Ideas pipeline: coded judge, human approval, monitoring, closing. Run: python -m unittest discover tests"""
from __future__ import annotations
import sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from arena import ideas, safety
from arena.clock import IST
from arena.committee import Committee
from arena.config import load_config
from arena.judge import judge
from arena.runner import Runner
from arena.feeds.base import PriceFeed

NOW = datetime(2026, 11, 20, 16, 15, tzinfo=IST)


def frame(n=300, drift=0.002, start="2025-09-01"):
    idx = pd.bdate_range(start, periods=n, tz=IST); c = 100 * np.cumprod(np.full(n, 1 + drift))
    return pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": np.full(n, 3e6)}, index=idx)


def paths(t): return Path(t) / "pending.json", Path(t) / "picks.json"


class FakeShort:
    def __init__(self, frames, setups=None, mom=None): self.pool = list(frames); self.frames = frames; self.setups = setups or {}; self.mom_pct = mom or {}


class FakeCommittee(Committee):
    def __init__(self, cfg, news=None, fund=None, macro=None):
        super().__init__(cfg); self._n, self._f, self._m = news or {}, fund or {}, macro or {"stance": "calm", "reasons": []}; self._fund = {"stocks": {}}
    def news(self, s): return self._n.get(s, {"stance": "quiet", "flags": [], "headlines": []})
    def fundamentals(self, s, p): return self._f.get(s, {"stance": "no data", "points": []})
    def macro(self): return self._m


def memo(cfg, news="quiet", fund="no data", setups=(), mom=0.97, d=None):
    d = d if d is not None else frame()
    t = Committee.technical_daily(d, [{"setup": s, "why": s} for s in setups], mom)
    return {"analysts": {"technical": t, "fundamentals": {"stance": fund, "points": []}, "news": {"stance": news, "flags": ["probe"] if news == "negative flags" else [], "headlines": []}}}


class Judge(unittest.TestCase):
    cfg = load_config()
    def test_strong_candidate_approved_with_audit_trail(self):
        v = judge(memo(self.cfg, setups=["breakout20"], fund="supportive"), {"stance": "calm"}, self.cfg)
        self.assertEqual(v["verdict"], "APPROVE"); self.assertTrue(v["rules"]); self.assertIn("no AI", v["by"])
    def test_hard_rejects(self):
        m = memo(self.cfg, setups=["breakout20"])
        self.assertEqual(judge(m, {}, self.cfg, {"AAA"}, "AAA")["verdict"], "REJECT")
        self.assertEqual(judge(m, {}, self.cfg, set(), "AAA", {"AAA": "ASM"})["verdict"], "REJECT")
        n = memo(self.cfg, news="negative flags", setups=["breakout20"]); self.assertEqual(judge(n, {}, self.cfg)["verdict"], "REJECT")
        self.cfg["ideas"]["news_hard_reject"] = False
        try: self.assertNotEqual(judge(n, {}, self.cfg)["verdict"], "REJECT")
        finally: self.cfg["ideas"]["news_hard_reject"] = True
    def test_macro_and_fundamentals_move_score_but_do_not_block(self):
        base = judge(memo(self.cfg, setups=["breakout20"]), {"stance": "calm"}, self.cfg)["score"]
        self.assertEqual(judge(memo(self.cfg, setups=["breakout20"]), {"stance": "cautious", "reasons": ["x"]}, self.cfg)["score"], base - 1)
        self.assertEqual(judge(memo(self.cfg, setups=["breakout20"], fund="concerning"), {}, self.cfg)["score"], base - 1)
    def test_weak_chart_rejected(self):
        self.assertEqual(judge(memo(self.cfg, mom=0.3, d=frame(drift=-0.002)), {}, self.cfg)["verdict"], "REJECT")


class Levels(unittest.TestCase):
    def test_levels(self):
        cfg = load_config(); lv = ideas.levels(100.0, 2.0, "short", cfg)
        self.assertEqual((lv["entry_lo"], lv["entry_hi"], lv["stop"], lv["target"]), (99.2, 100.8, 97.0, 107.5))
        m = ideas.levels(100.0, 2.0, "medium", cfg); self.assertEqual((m["stop"], m["target"], m["days"]), (95.0, 115.0, 60))


class Flow(unittest.TestCase):
    cfg = load_config()
    def test_scan_queues_but_posts_nothing_until_approved(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); fr = {"AAA": frame(), "BBB": frame(drift=-0.002)}
            sl = FakeShort(fr, {"AAA": [{"setup": "breakout20", "why": "b"}]}, {"AAA": 0.97, "BBB": 0.2})
            pen = ideas.scan(self.cfg, sl, FakeCommittee(self.cfg, fund={"AAA": {"stance": "supportive", "points": []}}), NOW, pp, pu)
            self.assertEqual([q["sym"] for q in pen["queue"]], ["AAA"]); self.assertEqual(pen["queue"][0]["status"], "awaiting decision")
            self.assertEqual(ideas.public(pu)["live"], [])
            idea = ideas.approve(pen["queue"][0]["id"], "Siva", NOW, pp, pu)
            self.assertEqual(idea["status"], "live"); self.assertEqual(idea["approved_by"], "Siva"); self.assertEqual(len(ideas.public(pu)["live"]), 1)
            self.assertEqual(ideas.pending(pp)["queue"], [])
            with self.assertRaises(KeyError): ideas.approve("nope", "x", NOW, pp, pu)
    def test_no_duplicate_when_already_live_and_wording(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); sl = FakeShort({"AAA": frame()}, {"AAA": [{"setup": "breakout20", "why": "b"}]}, {"AAA": 0.97}); fc = FakeCommittee(self.cfg)
            q = ideas.scan(self.cfg, sl, fc, NOW, pp, pu)["queue"][0]; ideas.approve(q["id"], "S", NOW, pp, pu)
            self.assertEqual(ideas.scan(self.cfg, sl, fc, NOW, pp, pu)["queue"], [])
            txt = open(pu).read().lower(); self.assertIn("bullish view", txt); self.assertNotIn('"buy"', txt); self.assertNotIn('"sell"', txt)
    def _live(self, t):
        pp, pu = paths(t)
        q = ideas.scan(self.cfg, FakeShort({"AAA": frame()}, {"AAA": [{"setup": "breakout20", "why": "b"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)["queue"][0]
        return ideas.approve(q["id"], "S", NOW, pp, pu), pp, pu
    def _after(self, rows):
        base = frame(); idx = pd.bdate_range(base.index[-1] + pd.Timedelta(days=1), periods=len(rows), tz=IST)
        add = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"]); add["volume"] = 3e6
        return pd.concat([base, add])
    def test_target_hit_flags_then_human_closes_into_closed(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); p = idea["entry_price"]
            fr = {"AAA": self._after([(p, p * 1.02, p * 0.99, p * 1.01), (p, idea["target"] * 1.01, p, idea["target"])])}
            sig = ideas.monitor(self.cfg, fr, NOW, pu)
            self.assertEqual(sig[0]["reason"], "Target hit"); self.assertEqual(sig[0]["price"], idea["target"])
            self.assertEqual(len(ideas.public(pu)["live"]), 1)                       # flagged, NOT closed
            self.assertEqual(ideas.public(pu)["live"][0]["exit_signal"]["reason"], "Target hit")
            done = ideas.close("AAA", "Siva", NOW, "Target hit", sig[0]["price"], pu)
            pub = ideas.public(pu); self.assertEqual((len(pub["live"]), len(pub["closed"])), (0, 1))
            self.assertGreater(done["ret_pct"], 0); self.assertEqual(done["close_kind"], "target"); self.assertEqual(pub["recent_alerts"][0]["type"], "exit")
    def test_gap_through_stop_uses_the_open_and_stop_beats_target_in_same_bar(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t)
            g = ideas.monitor(self.cfg, {"AAA": self._after([(idea["stop"] * 0.95, idea["stop"], idea["stop"] * 0.9, idea["stop"] * 0.92)])}, NOW, pu)
            self.assertEqual(g[0]["reason"], "Stop-loss hit"); self.assertEqual(g[0]["price"], round(idea["stop"] * 0.95, 2))
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); p = idea["entry_price"]
            g = ideas.monitor(self.cfg, {"AAA": self._after([(p, idea["target"] * 1.1, idea["stop"] * 0.99, p)])}, NOW, pu)
            self.assertEqual(g[0]["reason"], "Stop-loss hit")
    def test_time_up_and_auto_close_switch(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); p = idea["entry_price"]
            fr = {"AAA": self._after([(p, p * 1.005, p * 0.995, p)] * idea["days"])}
            self.assertEqual(ideas.monitor(self.cfg, fr, NOW, pu)[0]["reason"], "Time up")
            self.cfg["ideas"]["auto_close"] = True
            try:
                ideas.monitor(self.cfg, fr, NOW, pu); pub = ideas.public(pu)
                self.assertEqual(len(pub["closed"]), 1); self.assertEqual(pub["closed"][0]["reason"], "Time up")
            finally:
                self.cfg["ideas"]["auto_close"] = False
    def test_approve_posts_alert_and_chart_bars_in_the_website_schema(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); fr = {"AAA": frame()}
            q = ideas.scan(self.cfg, FakeShort(fr, {"AAA": [{"setup": "breakout20", "why": "b"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)["queue"][0]
            ideas.approve(q["id"], "Siva", NOW, pp, pu, frames=fr)
            pub = ideas.public(pu); e = pub["live"][0]
            for k in ("sym", "status", "entry_date", "entry_price", "price_now", "rank_now", "nifty_pct", "term", "stop", "target", "approved_by"): self.assertIn(k, e)
            self.assertEqual(pub["recent_alerts"][0]["type"], "entry"); self.assertEqual(pub["alerts_today"][0]["sym"], "AAA")
            self.assertEqual(pub["bars"]["AAA"][-1][0], e["entry_date"])                            # the chart ends on the idea's own close
    def test_momentum_rank_candidate_is_scored_and_becomes_medium_term(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t)
            pen = ideas.scan(self.cfg, FakeShort({"AAA": frame()}, {}, {}), FakeCommittee(self.cfg, fund={"AAA": {"stance": "supportive", "points": []}}), NOW, pp, pu,
                             ranks={"AAA": 7}, moms={"AAA": 0.8})
            q = pen["queue"][0]; self.assertEqual((q["term"], q["entry_rank"], q["mom_pct"]), ("medium", 7, 80.0))
            self.assertTrue(any("momentum rank #7" in r["rule"] for r in q["judge"]["rules"]))
    def test_faded_rank_is_flagged_not_closed_and_only_for_momentum_ideas(self):
        sys.path.insert(0, str(ROOT.parent / "pipeline")); import core
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); fr = {"AAA": frame(), "BBB": frame()}
            pen = ideas.scan(self.cfg, FakeShort(fr, {"BBB": [{"setup": "breakout20", "why": "b"}]}, {"BBB": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": 3, "BBB": 400})
            for q in pen["queue"]: ideas.approve(q["id"], "S", NOW, pp, pu)
            state = ideas.public(pu); terms = {e["sym"]: e["term"] for e in state["live"]}; self.assertEqual(terms, {"AAA": "medium", "BBB": "short"})
            self.assertEqual(core.rank_exit_flags(state, {"AAA": 80, "BBB": 400}, NOW), ["AAA"])      # BBB is a setup idea: rank does not judge it
            core.refresh_live(state, {"AAA": 80}, {"AAA": 150.0}, 26000, NOW)
            ideas._save(pu, state)
            sig = ideas.monitor(self.cfg, fr, NOW, pu); self.assertEqual([x["id"][:3] for x in sig], ["AAA"]); self.assertIn("rank fell to #80", sig[0]["reason"])
            self.assertEqual(len(ideas.public(pu)["live"]), 2)                                       # flagged only
            done = ideas.close("AAA", "Siva", NOW, public_path=pu)                                   # default = the flagged signal
            self.assertEqual((done["close_kind"], done["closed_by"]), ("rank", "Siva")); self.assertEqual(len(ideas.public(pu)["live"]), 1)
    def test_legacy_auto_idea_without_levels_is_monitored_and_closable(self):
        with tempfile.TemporaryDirectory() as t:
            _, pu = paths(t); old = {"sym": "OLD", "status": "live", "entry_date": "2026-01-01", "entry_price": 100.0, "entry_rank": 12, "price_now": 110.0, "nifty_entry": 25000}
            ideas._save(pu, {"live": [old], "closed": []}, compact=True)
            self.assertEqual(ideas.monitor(self.cfg, {"OLD": frame()}, NOW, pu), [])
            done = ideas.close("OLD", "S", NOW, public_path=pu); self.assertEqual((done["ret_pct"], done["close_kind"]), (10.0, "manual"))
    def test_no_realtime_trading(self):
        class Live(PriceFeed): name = "x"; live = True
        with self.assertRaises(safety.RealtimeBlocked): Runner(load_config(), Live(), None, 1)


if __name__ == "__main__":
    unittest.main()
