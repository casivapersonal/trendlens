"""Tests for the TrendLens additions. Run: python -m unittest discover tests"""
from __future__ import annotations

import json, sys, tempfile, unittest, random
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
import numpy as np, pandas as pd
from arena import riskplan, scanner, timemachine, universe, safety
from arena.clock import IST
from arena.committee import Committee
from arena.config import load_config
from arena.db import Journal
from arena.feeds.base import BarReplayFeed
from arena.runner import Runner


def cfg_tmp(tmp):
    c = load_config()
    c["paths"].update(db=f"{tmp}/t.db", report=f"{tmp}/r.html", data_dir=tmp, log=f"{tmp}/l.log")
    c["strategy"]["brain"] = "rules"; c["committee"]["enabled"] = False
    return c


def daily(n=60, last_breakout=False, vol=2e6, price=100.0):
    idx = pd.date_range("2026-08-01", periods=n, freq="B", tz=IST); c = np.full(n, price)
    df = pd.DataFrame({"open": c, "high": c + 1, "low": c - 1, "close": c, "volume": np.full(n, vol)}, index=idx)
    if last_breakout:
        df.iloc[-1] = [price + 1, price + 6, price, price + 5, vol * 3]
    return df


def sessions_df(days=30, seed=1):
    rnd = random.Random(seed); rows = []; idx = []
    for d in range(days):
        day = datetime(2026, 9, 1, tzinfo=IST) + timedelta(days=d)
        if day.weekday() >= 5: continue
        p = 100.0
        for i in range(75):
            ts = day.replace(hour=9, minute=15) + timedelta(minutes=5 * i); o = p; p *= 1 + rnd.uniform(-0.003, 0.003)
            rows.append((o, max(o, p) * 1.001, min(o, p) * 0.999, p, 10000)); idx.append(ts)
    return pd.DataFrame(rows, index=pd.DatetimeIndex(idx), columns=["open", "high", "low", "close", "volume"])


class Universe(unittest.TestCase):
    def test_csv_and_fallback(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "n.csv"; p.write_text("Company Name,Industry,Symbol,Series,ISIN Code\nA,X,AAA,EQ,1\nB,X,BBB,EQ,2\n")
            self.assertEqual(universe.load({"universe": {"file": str(p)}}), ["AAA", "BBB"])
        self.assertGreater(len(universe.load({"universe": {"name": "nope"}})), 100)    # TrendLens syms.txt fallback


class Scanner(unittest.TestCase):
    cfg = load_config()
    def test_breakout_found_and_filters(self):
        self.assertIn("breakout20", [s["setup"] for s in scanner.daily_setups(daily(last_breakout=True))])
        self.assertEqual(scanner.daily_setups(daily()), [])
        self.assertTrue(scanner.liquid(daily(), self.cfg))
        self.assertFalse(scanner.liquid(daily(vol=1e3), self.cfg))        # illiquid
        self.assertFalse(scanner.liquid(daily(price=10), self.cfg))       # too cheap
    def test_shortlist_ranks_setup_first_and_falls_back(self):
        class F:
            def history(self, syms, period, interval):
                return {s: daily(120, last_breakout=(s == "WIN")) for s in syms}
        cfg = load_config(); cfg["universe"]["file"] = "x"
        import arena.universe as U
        orig = U.load; U.load = lambda c=None: ["AAA", "WIN", "BBB"]
        try:
            sl = scanner.Shortlister(cfg, F()); out = sl.open_day(datetime(2026, 11, 19, 9, 20, tzinfo=IST), ["FALL"])
            self.assertEqual(out[0], "WIN"); self.assertIn("20-day high", sl.why["WIN"])
            self.assertEqual(scanner.Shortlister(cfg, object()).open_day(datetime(2026, 11, 19, tzinfo=IST), ["FALL"]), ["FALL"])
        finally:
            U.load = orig


class Risk(unittest.TestCase):
    def test_sizing_respects_loss_limit_including_charges(self):
        cfg = load_config()
        a = riskplan.cap_alloc(cfg, 100000, 1000, 1.5, 20)               # Rs 500 / Rs 15 per share = 33 shares = 33% -> capped by 20% asked
        self.assertLessEqual(a, 20)
        a2 = riskplan.cap_alloc(cfg, 100000, 1000, 5.0, 20)              # Rs 50 per share -> at most 9 shares (<=Rs 450 + charges)
        self.assertLess(a2, 10); self.assertGreater(a2, 0)
        cfg["risk"]["max_loss_per_trade"] = 0
        self.assertEqual(riskplan.cap_alloc(cfg, 100000, 1000, 5.0, 20), 20)   # 0 = off
    def test_trades_per_day_gate(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_tmp(t); j = Journal(cfg["paths"]["db"]); rid = j.new_run("t", "live", "x", "rules", "", 100000, cfg, "2026-11-19T09:00:00")
            now = datetime(2026, 11, 19, 10, 0, tzinfo=IST); self.assertIsNone(riskplan.gate(cfg, j, rid, now))
            for i in range(3):
                j.x("INSERT INTO orders(run_id, ts, symbol, side, qty, quote_price, fill_price, slippage_cost, charges, trigger, status) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                    (rid, now.isoformat(), f"S{i}", "BUY", 1, 1, 1, 0, 0, "ai", "filled"))
            self.assertIn("trades-a-day", riskplan.gate(cfg, j, rid, now))


class TimeMachine(unittest.TestCase):
    def test_deterministic_and_sane(self):
        cfg = load_config(); df = sessions_df(); now = datetime(2026, 10, 15, 10, 30, tzinfo=IST)
        r1 = timemachine.run(df, now, 100, 1.5, 3.0, 50, cfg, n=1500, seed=19); r2 = timemachine.run(df, now, 100, 1.5, 3.0, 50, cfg, n=1500, seed=19)
        self.assertEqual(r1, r2); self.assertAlmostEqual(sum(r1["sim"].values()), 100, delta=0.3)
        self.assertGreaterEqual(r1["real"]["sessions"], 20); self.assertIn("verdict", r1)
    def test_gap_fills_at_open_not_stop(self):
        sess = {1: [(630, 100, 100.2, 99.8, 100), (635, 90, 90.5, 89.5, 90), (640, 90, 90, 90, 90)]}
        r = timemachine.replay_same_trade(sess, 630, 900, 1.5, 3.0, 100, "intraday", {})
        self.assertEqual(r["stop"], 1); self.assertLess(r["net_pnl"], -900)       # ~Rs 1,000 loss at 90, not Rs 150 at the 98.5 stop


class CommitteeTests(unittest.TestCase):
    def mk(self, d, stocks):
        p = Path(d) / "f.json"; p.write_text(json.dumps({"asof": "2026-11-19", "stocks": stocks})); return Committee(load_config(), p)
    def test_fundamentals_use_live_price(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.mk(d, {"AAA": {"eps_ttm": 10, "roe_pct": 20, "earn_growth_pct": 25, "rev_growth_pct": 18, "de_ratio": 0.3},
                            "LOSS": {"eps_ttm": -5, "de_ratio": 3.0}})
            a = c.fundamentals("AAA", 200); self.assertEqual(a["stance"], "supportive"); self.assertIn("P/E 20.0x on the live price", a["points"])
            self.assertIn("P/E 40.0x on the live price", c.fundamentals("AAA", 400)["points"])
            b = c.fundamentals("LOSS", 50); self.assertEqual(b["stance"], "concerning"); self.assertEqual(c.fundamentals("NONE", 1)["stance"], "no data")
    def test_news_flags_and_failure(self):
        with tempfile.TemporaryDirectory() as d:
            c = self.mk(d, {}); xml = b"<rss><channel><item><title>SEBI order against promoter, probe widens</title><pubDate>Thu, 19 Nov 2026</pubDate></item></channel></rss>"
            class R:
                content = xml
                def raise_for_status(self): pass
            c.http.get = lambda *a, **k: R()
            n = c.news("AAA"); self.assertEqual(n["stance"], "negative flags"); self.assertIn("probe", n["flags"])
            c2 = self.mk(d, {})
            def boom(*a, **k): raise RuntimeError("offline")
            c2.http.get = boom; self.assertEqual(c2.news("AAA")["stance"], "no data")
    def test_news_host_is_allow_listed_and_orders_still_blocked(self):
        safety.check_url("GET", "https://news.google.com/rss/search")
        with self.assertRaises(safety.RealMoneyBlocked): safety.check_url("POST", "https://api.upstox.com/v2/order/place")
        with self.assertRaises(safety.RealMoneyBlocked): safety.check_url("GET", "https://example.com/rss/search")
    def test_memo_agreement_and_only_risk_officer_blocks(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_tmp(t); j = Journal(cfg["paths"]["db"]); rid = j.new_run("t", "live", "x", "rules", "", 100000, cfg, "2026-11-19T09:00:00")
            c = self.mk(t, {"AAA": {"eps_ttm": -2, "de_ratio": 4}}); c.news = lambda s: {"stance": "negative flags", "flags": ["probe"], "headlines": []}
            snap = {"ltp": 100, "sma9": 101, "sma21": 99, "rsi14": 60, "above_vwap": True, "mom_30m_pct": 0.5}
            m = c.memo("AAA", snap, "", j, rid, datetime(2026, 11, 19, 10, 0, tzinfo=IST))
            self.assertEqual(m["analysts"]["technical"]["stance"], "bullish"); self.assertEqual(m["agreement"], "analysts disagree")
            self.assertEqual(m["risk_officer"]["verdict"], "PASS")          # bad fundamentals + news do NOT block; they are advice
            self.assertIn("not back-tested", m["evidence"]["news"])


class RunnerIntegration(unittest.TestCase):
    def test_scan_mode_and_risk_in_a_replay(self):
        with tempfile.TemporaryDirectory() as t:
            cfg = cfg_tmp(t); cfg["market"]["watchlist_mode"] = "scan"; cfg["account"]["challenge_days"] = 3
            df = sessions_df(8); feed = BarReplayFeed({"AAA": df, "BBB": df * 1.0})
            cfg["market"]["watchlist"] = ["AAA", "BBB"]; j = Journal(cfg["paths"]["db"])
            rid = j.new_run("t", "replay", "replay", "rules", "", 100000, cfg, "2026-09-01T09:00:00")
            Runner(cfg, feed, j, rid).run()                                 # replay feed has no daily history: scanner falls back to the watchlist
            self.assertEqual(j.one("SELECT status FROM runs WHERE id=?", (rid,))["status"], "complete")
            per_day = j.q("SELECT substr(ts,1,10) d, COUNT(*) n FROM orders WHERE run_id=? AND side='BUY' AND status='filled' GROUP BY d", (rid,))
            self.assertTrue(all(r["n"] <= cfg["risk"]["max_trades_per_day"] for r in per_day))
            for r in j.q("SELECT net_pnl, entry_price, qty FROM trades WHERE run_id=?", (rid,)):   # no stopped trade loses far more than the Rs 500 limit
                self.assertGreater(r["net_pnl"], -cfg["risk"]["max_loss_per_trade"] * 2.2)


if __name__ == "__main__":
    unittest.main()
