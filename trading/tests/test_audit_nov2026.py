"""The 20 audit fixes approved in Nov 2026 (entry / exit rules). Run: python -m unittest discover tests"""
from __future__ import annotations
import json, sys, tempfile, unittest
from datetime import datetime, date
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT.parent / "pipeline")); sys.path.insert(0, str(Path(__file__).resolve().parent))
import numpy as np, pandas as pd
from arena import ideas, ceo, timemachine, scanner
from arena.clock import IST
from arena.config import load_config
from arena.judge import judge
from test_ideas import frame, paths, FakeShort, FakeCommittee, memo, NOW
import core

T1 = datetime(2026, 11, 23, 10, 0, tzinfo=IST)            # the session after NOW (Fri 20 Nov -> Mon 23 Nov)


def after(base, rows):
    idx = pd.bdate_range(base.index[-1] + pd.Timedelta(days=1), periods=len(rows), tz=IST)
    add = pd.DataFrame(rows, index=idx, columns=["open", "high", "low", "close"]); add["volume"] = 3e6
    return pd.concat([base, add])


class Momentum(unittest.TestCase):
    cfg = load_config()

    def _live(self, t, rank=3, fr=None):
        pp, pu = paths(t)
        q = ideas.scan(self.cfg, FakeShort({"AAA": fr if fr is not None else frame()}, {}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": rank}, moms={"AAA": 0.5})["queue"][0]
        return ideas.approve(q["id"], "S", NOW, pp, pu), pp, pu

    def test_9_one_shared_score_function(self):
        h = {"dates": [f"d{i}" for i in range(260)], "universe": ["A", "B"],
             "closes": {"A": list(np.linspace(100, 150, 260)), "B": list(100 + 5 * np.sin(np.arange(260)))}}
        rk, mom = core.ranks_now(h, {"A": 151.0, "B": 101.0}, "today")
        self.assertEqual(rk["A"], 1); self.assertAlmostEqual(mom["A"], core.momentum_score(h["closes"]["A"], 151.0)[0])
        import backtest                                                    # the back-test imports the same function
        self.assertIs(backtest.core.momentum_score, core.momentum_score)

    def test_8_10_momentum_enters_at_next_open_without_time_machine(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t)
            self.assertEqual(idea["fill_rule"], "next_open"); self.assertIsNone(idea["review"]["time_machine"])
            d = frame(); gap = idea["entry_price"] * 1.04                           # opens 4% higher: still entered, at the open
            ideas.monitor(self.cfg, {"AAA": after(d, [(gap, gap * 1.01, gap * 0.99, gap)])}, NOW, pu)
            i = ideas.public(pu)["live"][0]
            self.assertEqual(i["filled"]["price"], round(gap, 2))
            dist = idea["entry_price"] - idea["stop"]
            self.assertAlmostEqual(i["stop"], ideas.tick(gap - dist), places=2)      # fix #5 / #19: 6x ATR stop measured from the actual fill
            ideas.monitor(self.cfg, {"AAA": after(d, [(gap, gap * 1.01, gap * 0.99, gap)])}, NOW, pu)   # idempotent
            self.assertAlmostEqual(ideas.public(pu)["live"][0]["stop"], ideas.tick(gap - dist), places=2)

    def test_intraday_provisional_fill_is_replaced_by_the_open(self):
        with tempfile.TemporaryDirectory() as t:
            today = frame(start=str(pd.bdate_range(end="2026-11-20", periods=300)[0].date()))   # posted on NOW's date
            idea, pp, pu = self._live(t, fr=today)
            self.assertEqual(ideas.monitor_live(self.cfg, {"AAA": idea["entry_price"]}, NOW.replace(hour=11), pu), [])   # same day: no entry
            self.assertIsNone(ideas.public(pu)["live"][0]["filled"])
            ideas.monitor_live(self.cfg, {"AAA": idea["entry_price"] * 1.01}, T1, pu)
            self.assertTrue(ideas.public(pu)["live"][0]["filled"]["provisional"])
            o = idea["entry_price"] * 1.005
            ideas.monitor(self.cfg, {"AAA": after(today, [(o, o * 1.01, o * 0.99, o)])}, T1, pu)
            f = ideas.public(pu)["live"][0]["filled"]; self.assertEqual(f["price"], round(o, 2)); self.assertNotIn("provisional", f)

    def test_11_no_protect_the_gain_on_momentum(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); e = idea["entry_price"]
            ideas.monitor(self.cfg, {"AAA": after(frame(), [(e, e * 1.6, e, e * 1.5)])}, NOW, pu)
            self.assertIsNone(ideas.public(pu)["live"][0]["stop_advice"])

    def test_15_circuit_locked_stop_exits_at_next_tradable_price(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t); e = idea["entry_price"]; lock = idea["stop"] * 0.9
            d1 = after(frame(), [(e, e, e, e), (lock, lock, lock, lock)])           # day 2 locked at the lower circuit
            g = ideas.monitor(self.cfg, {"AAA": d1}, NOW, pu)
            self.assertTrue(g[0].get("awaiting"))
            with mock.patch("arena.committee.Committee._funds", return_value={"stocks": {}}):
                self.assertEqual(ceo.decide_exits(self.cfg, NOW, ideas, public_path=pu), [])   # the CEO waits for a tradable price
            nx = lock * 0.95
            g = ideas.monitor(self.cfg, {"AAA": after(frame(), [(e, e, e, e), (lock, lock, lock, lock), (nx, lock, nx * 0.98, nx)])}, NOW, pu)
            self.assertEqual(g[0]["reason"], "Stop-loss hit"); self.assertEqual(g[0]["price"], round(nx, 2)); self.assertIn("circuit", g[0]["note"])

    def test_15_intraday_stop_at_circuit_band_waits_for_the_close(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t)
            ideas.monitor_live(self.cfg, {"AAA": idea["entry_price"]}, T1, pu)
            pc = idea["stop"] / 0.9 * 0.99; p = round(pc * 0.80, 2)                 # -20% band, below the stop
            if p > idea["stop"]: p = idea["stop"] * 0.99; pc = p / 0.80
            g = ideas.monitor_live(self.cfg, {"AAA": p}, T1.replace(hour=12), pu, prev_close={"AAA": pc})
            self.assertTrue(g and g[0].get("awaiting"))

    def test_3_separate_books_and_momentum_caps(self):
        cfg = {"ideas": {"max_live": 20, "max_new_per_day": 1, "entry_rank": 20}, "ceo": {"max_per_sector": 3, "max_live_short": 1, "max_per_broad_sector": 6}}
        base = {"entry_price": 100.0, "entry_lo": 99, "entry_hi": 101, "stop": 90, "target": 120, "days": 60, "r": 2, "status": "awaiting decision",
                "risk": {"verdict": "PASS", "reasons": []}, "review": {"lean": "bull", "time_machine": None}, "committee": {"analysts": {}}}
        q = [{**base, "id": f"M{k}-x-m", "sym": f"M{k}", "term": "medium", "entry_rank": k + 1, "judge": {"verdict": "APPROVE", "score": 5, "rules": []}} for k in range(5)]
        q += [{**base, "id": f"S{k}-x-s", "sym": f"S{k}", "term": "short", "entry_rank": None, "judge": {"verdict": "APPROVE", "score": 9, "rules": []}} for k in range(3)]
        funds = {"stocks": {**{f"M{k}": {"sector": "Tech", "industry": "Software"} for k in range(5)}, **{f"S{k}": {"sector": "Tech", "industry": "Software"} for k in range(3)}}}
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); pu.write_text(json.dumps({"live": [], "closed": []})); pp.write_text(json.dumps({"queue": q, "log": []}))
            with mock.patch("arena.committee.Committee._funds", return_value=funds):
                out = {d["sym"]: d["decision"] for d in ceo.decide_entries(cfg, NOW, ideas, pending_path=pp, public_path=pu)}
        self.assertEqual([s for s in ("M0", "M1", "M2", "M3", "M4") if out[s] == "POST"], ["M0", "M1", "M2"])   # industry cap 3, in rank order
        self.assertEqual(out["S0"], "DECLINE")      # setups count the whole book: the industry is full

    def test_2_reentry_block_is_optional_and_off(self):
        self.assertFalse(load_config()["ideas"]["reentry_block"])


class Setups(unittest.TestCase):
    cfg = load_config()

    def _live(self, t, setup):
        pp, pu = paths(t)
        q = ideas.scan(self.cfg, FakeShort({"AAA": frame()}, {"AAA": [{"setup": setup, "why": "x"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)["queue"][0]
        return ideas.approve(q["id"], "S", NOW, pp, pu), pp, pu

    def test_6_breakout_enters_at_open_and_cancels_below_the_level(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t, "breakout20"); lv = idea["breakout_level"]
            self.assertEqual(idea["fill_rule"], "breakout")
            g = ideas.monitor(self.cfg, {"AAA": after(frame(), [(lv * 0.99, lv, lv * 0.98, lv * 0.99)])}, NOW, pu)
            self.assertIn("below the breakout level", g[0]["reason"]); self.assertTrue(g[0]["not_entered"])
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t, "breakout20"); o = idea["entry_price"]
            pub = ideas.public(pu); pub["live"][0]["breakout_level"] = round(o * 0.99, 2); ideas._save(pu, pub)   # a real breakout closes above its level
            ideas.monitor(self.cfg, {"AAA": after(frame(), [(o, o * 1.01, o * 0.995, o)])}, NOW, pu)
            self.assertEqual(ideas.public(pu)["live"][0]["filled"]["price"], round(o, 2))

    def test_5_fill_that_leaves_less_than_2_to_1_is_skipped(self):
        with tempfile.TemporaryDirectory() as t:
            idea, pp, pu = self._live(t, "breakout20"); o = idea["target"] * 0.97       # gapped up close to the target
            g = ideas.monitor(self.cfg, {"AAA": after(frame(), [(o, o * 1.005, o * 0.995, o)])}, NOW, pu)
            self.assertIn("reward-to-risk", g[0]["reason"]); self.assertTrue(g[0]["not_entered"])

    def test_12_results_inside_holding_period_skips_setup(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t)
            with mock.patch("arena.universe.results", return_value={"AAA": "2026-11-27"}):
                pen = ideas.scan(self.cfg, FakeShort({"AAA": frame()}, {"AAA": [{"setup": "bull_flag", "why": "x"}]}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu)
            self.assertEqual(pen["queue"], []); self.assertIn("results", pen["log"][-1]["results"][0]["why"][0])
            with mock.patch("arena.universe.results", return_value={"AAA": "2026-11-27"}):  # momentum: shown as a badge, not skipped
                q = ideas.scan(self.cfg, FakeShort({"AAA": frame()}, {}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": 2})["queue"][0]
            self.assertEqual(q["results_date"], "2026-11-27")

    def test_18_definitions(self):
        n = 71; c = np.full(n, 100.0); c[55:66] = np.linspace(100, 120, 11); c[66:] = 120 + np.array([0.3, -0.2, 0.1, 0.2, 0.3])
        idx = pd.bdate_range("2026-06-01", periods=n, tz=IST)
        d = pd.DataFrame({"open": c, "high": c + 1.5, "low": c - 1.5, "close": c, "volume": np.full(n, 1e6)}, index=idx)
        self.assertEqual([x["setup"] for x in scanner.daily_setups(d)], ["bull_flag"])
        d2 = d.copy(); d2.iloc[-1, d2.columns.get_loc("high")] += 10       # pause wider than 1.5x ATR
        d2.iloc[-1, d2.columns.get_loc("low")] -= 10
        self.assertEqual(scanner.daily_setups(d2), [])


class Scoring(unittest.TestCase):
    cfg = load_config()

    def test_4_13_news_counted_once(self):
        n = memo(self.cfg, news="negative flags", setups=["bull_flag"])
        self.assertEqual(judge(n, {}, self.cfg)["verdict"], "REJECT")                     # setup: serious keyword = veto
        top = memo(self.cfg, news="negative flags"); top["analysts"]["technical"]["metrics"]["rank"] = 3
        v = judge(top, {}, self.cfg)
        self.assertEqual(v["verdict"], "APPROVE"); self.assertTrue(v["warnings"])         # momentum: warning, not blocked
        self.assertFalse(any("headline" in r["rule"] for r in v["rules"]))                 # and not scored

    def test_20_no_rsi_penalty_on_strength_setups(self):
        m = memo(self.cfg, setups=["bull_flag"]); m["analysts"]["technical"]["metrics"]["rsi"] = 92
        self.assertFalse(any("RSI" in r["rule"] and r["points"] < 0 for r in judge(m, {}, self.cfg)["rules"]))

    def test_1_time_machine_is_judged_against_a_no_edge_baseline(self):
        rng = np.random.default_rng(1); n = 300; r = rng.normal(0, 0.015, n); c = 100 * np.cumprod(1 + r)
        d = pd.DataFrame({"open": c, "high": c * 1.01, "low": c * 0.99, "close": c, "volume": 1e6}, index=pd.bdate_range("2024-01-01", periods=n))
        e = float(c[-1]); out = timemachine.run_daily(d, e, e * 0.95, e * 1.125, 15, self.cfg, n=1500)
        self.assertIn("baseline", out); self.assertLess(abs(out["edge_vs_baseline"]), 0.10)   # symmetric moves: no edge, whatever r is
        self.assertEqual(out["verdict"], "close to a coin flip")


class Data(unittest.TestCase):
    def test_14_next_results_date_and_fetcher_parses_any_layout(self):
        import nse_fetch
        j = {"data": [{"symbol": "AAA", "x": 1}, {"nested": [{"symbol": "BBB"}]}]}
        self.assertEqual([r["symbol"] for r in nse_fetch.rows(j)], ["AAA", "BBB"])
        with tempfile.TemporaryDirectory() as t:
            Path(t, "results_auto.csv").write_text("SYMBOL,DATE,PURPOSE\nAAA,01-01-2020,Financial Results\nAAA,15-12-2099,Financial Results\nAAA,10-12-2099,Dividend\n")
            self.assertEqual(core.load_nse(t)[1]["AAA"], "2099-12-15")                      # the NEXT results date, not the oldest

    def test_16_17_scorecard_after_costs_and_nifty500(self):
        html = (ROOT.parent / "web" / "index.html").read_text()
        self.assertIn("netRet", html); self.assertIn("Beat NIFTY 500", html); self.assertIn("n500:e.n500_pct", html)
        self.assertIn("netRet", (ROOT.parent / "web" / "dashboard.html").read_text())

    def test_8_intraday_never_posts_new_ideas(self):
        src = (ROOT / "arena" / "__main__.py").read_text()
        block = src[src.index('elif a.action == "intraday"'):src.index('elif a.action == "monitor"')]
        self.assertNotIn("ideas.scan(", block); self.assertNotIn("decide_entries", block)


if __name__ == "__main__":
    unittest.main()
