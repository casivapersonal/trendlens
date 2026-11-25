"""Intraday segment (exhaustion fade, short). Run: python -m unittest discover tests"""
from __future__ import annotations
import json, sys, tempfile, unittest
from datetime import datetime, timedelta
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(ROOT.parent / "pipeline"))
import numpy as np, pandas as pd
from arena import intraday_ideas as ii
from arena.clock import IST
from arena.config import load_config

CFG = load_config()
_WATCH = ii.WATCH
def tearDownModule(): ii.WATCH = _WATCH
DAY = datetime(2026, 11, 24)                                   # a Tuesday


def daily(kind: str, n=120, base=100.0):
    """Daily bars ending the day before DAY. kind: run (up 25% in 5 sessions), flat, ext (35% over the 50-day), quiet."""
    px = np.full(n, base); vol = np.full(n, 2e6)
    if kind == "run": px[-5:] = base * np.array([1.05, 1.10, 1.15, 1.20, 1.27])
    if kind == "ext": px[-40:] = np.linspace(base, base * 1.55, 40)
    if kind == "flat": px = px + np.sin(np.arange(n)) * 0.5
    idx = pd.bdate_range(end=DAY - timedelta(days=1), periods=n)
    d = pd.DataFrame({"open": px * 0.995, "high": px * 1.01, "low": px * 0.99, "close": px, "volume": vol}, index=idx)
    return d


def candles(o0, path, start=(9, 15)):
    """Five-minute candles from 9:15: first candle opens at o0; path = list of (open, high, low, close)."""
    t0 = datetime(DAY.year, DAY.month, DAY.day, *start); out = []
    for k, (o, h, l, c) in enumerate(path):
        out.append({"t": t0 + timedelta(minutes=5 * k), "o": o, "h": h, "l": l, "c": c})
    return out


class Prep(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); ii.WATCH = Path(self.tmp.name) / "watch.json"

    def test_watch_list_uses_the_backtest_conditions(self):
        w = ii.prep(CFG, {"RUN": daily("run"), "EXT": daily("ext"), "FLAT": daily("flat")}, DAY)
        self.assertIn("RUN", w["stocks"]); self.assertIn("EXT", w["stocks"]); self.assertNotIn("FLAT", w["stocks"])
        self.assertTrue(any("in 5 sessions" in x for x in w["stocks"]["RUN"]["why"]))
        self.assertTrue(any("50-day" in x for x in w["stocks"]["EXT"]["why"]))

    def test_indicators_match_the_backtest(self):
        import intraday_system_test as T
        d = daily("run"); f = ii.indicators(d)
        per = {x.date(): np.array([[r.open] * 75, [r.high] * 75, [r.low] * 75, [r.close] * 75, [r.volume / 75] * 75], dtype=np.float64)
               for x, r in zip(d.index, d.itertuples())}
        bt = T.daily(per).iloc[-1]
        self.assertAlmostEqual(f["atr"], float(bt.atr), places=3); self.assertAlmostEqual(f["ret5"], float(bt.ret5), places=3)
        self.assertAlmostEqual(f["ext50"], float(bt.ext50), places=3)

    def test_next_session_skips_holidays_and_stale_stocks_are_dropped(self):
        self.assertEqual(ii._next_session(datetime(2026, 12, 24, 16), {"market": {"holidays": ["2026-12-25"]}}), "2026-12-28")
        late = daily("run").iloc[:-1]                                      # this stock's feed missed the last close
        w = ii.prep(CFG, {"RUN": late, "EXT": daily("ext")}, DAY - timedelta(hours=8))
        self.assertEqual(set(w["stocks"]), {"EXT"})

    def test_a_run_during_the_session_ignores_todays_partial_bar(self):
        d = daily("ext"); w = ii.prep(CFG, {"EXT": d}, datetime.combine(d.index[-1].date(), datetime.min.time()).replace(hour=11))
        self.assertEqual(w["last_close"], d.index[-2].date().isoformat())

    def test_illiquid_or_cheap_stocks_are_left_out(self):
        d = daily("run"); d["volume"] = 1000
        self.assertEqual(ii.prep(CFG, {"THIN": d}, DAY)["stocks"], {})


class ScanAndCeo(unittest.TestCase):
    PREV = (DAY - timedelta(days=1)).date().isoformat()

    def setUp(self, **kw):
        self.tmp = tempfile.TemporaryDirectory(); ii.WATCH = Path(self.tmp.name) / "watch.json"
        ii.prep(CFG, {"RUN": daily("run"), "EXT": daily("ext")}, DAY - timedelta(hours=7, minutes=45), results=kw.get("results", {}), blocked={},
                news_fn=kw.get("news_fn"), next_session=DAY.date().isoformat())
        self.w = json.loads(ii.WATCH.read_text())

    def bars_gap(self, sym, gap, e=None):
        pc = self.w["stocks"][sym]["close"]; o = pc * (1 + gap / 100); e = e or o * 0.996
        return candles(o, [(o, o * 1.002, o * 0.995, o * 0.996), (e, e * 1.002, e * 0.998, e * 0.999), (e, e * 1.001, e * 0.999, e)])

    def scan(self, pub, t, bars, prev=None, fill=True):
        if fill: bars = {**{s: self.bars_gap(s, 0) for s in self.w["stocks"]}, **bars}      # the other watched stocks: data, no gap
        return ii.scan(CFG, pub, datetime(DAY.year, DAY.month, DAY.day, *t), bars=bars, prev_session=prev or self.PREV)

    def test_missing_candles_are_retried_until_10(self):
        pub = {}
        self.assertEqual(self.scan(pub, (9, 21), {"RUN": self.bars_gap("RUN", 3)[:1]}), [])          # 9:20 candle not out yet
        self.assertEqual(pub["intraday"]["days"][DAY.date().isoformat()]["waiting"], ["RUN"])
        self.assertEqual(len(self.scan(pub, (9, 26), {"RUN": self.bars_gap("RUN", 3)})), 1)          # the next run posts
        pub = {}
        self.assertEqual(self.scan(pub, (9, 30), {}, fill=False), [])                               # feed down: nothing marked
        self.assertIsNone(pub["intraday"]["days"][DAY.date().isoformat()]["scanned"])
        self.assertEqual(self.scan(pub, (10, 1), {}, fill=False), [])                               # after 10:00: given up, with a note
        self.assertIn("no five-minute data", pub["intraday"]["days"][DAY.date().isoformat()]["note"])

    def test_each_watched_stock_records_whether_it_triggered(self):
        pub = {}; self.scan(pub, (9, 26), {"RUN": self.bars_gap("RUN", 3)})
        ch = pub["intraday"]["days"][DAY.date().isoformat()]["checked"]
        self.assertTrue(ch["RUN"]["triggered"]); self.assertAlmostEqual(ch["RUN"]["gap_pct"], 3.0, delta=0.05)
        self.assertFalse(ch["EXT"]["triggered"]); self.assertAlmostEqual(ch["EXT"]["gap_pct"], 0.0, delta=0.05)     # shown as "Not triggered"

    def test_a_stock_that_is_already_live_is_not_posted_again(self):
        for book in ("main", "intraday"):
            with self.subTest(book):
                pub = {"live": [{"sym": "RUN", "term": "medium"}]} if book == "main" else {}
                if book == "intraday": ii.book(pub)["live"].append({"sym": "RUN", "term": "intraday"})
                self.assertEqual(self.scan(pub, (9, 26), {"RUN": self.bars_gap("RUN", 3)}), [])
                self.assertEqual(pub["intraday"]["days"][DAY.date().isoformat()]["checked"]["RUN"]["skipped"], "already a live idea")

    def test_nse_holiday_list_is_merged_into_the_config(self):
        from arena.config import nse_holidays
        p = Path(self.tmp.name) / "h.csv"; p.write_text("DATE,DESCRIPTION\n2026-11-24,Guru Nanak\n")
        self.assertEqual(nse_holidays(p), ["2026-11-24"]); self.assertEqual(nse_holidays(Path(self.tmp.name) / "none.csv"), [])
        self.assertIn("2026-12-25", CFG["market"]["holidays"])           # from the repo's nse/holidays.csv

    def test_results_already_out_do_not_block(self):
        self.setUp(results={"RUN": "2026-10-30"})
        self.assertEqual(self.w["stocks"]["RUN"]["judge"]["verdict"], "APPROVE")

    def test_published_the_evening_before_with_a_trigger_band(self):
        f = self.w["stocks"]["RUN"]; self.assertEqual(self.w["for"], DAY.date().isoformat())
        self.assertAlmostEqual(f["trigger_lo"], f["close"] * 1.015, delta=0.05); self.assertAlmostEqual(f["trigger_hi"], f["close"] * 1.08, delta=0.05)
        pub = {}; ii.watch_public(pub); self.assertEqual({x["sym"] for x in pub["intraday"]["watch"]["stocks"]}, {"RUN", "EXT"})

    def test_entry_is_the_920_open_with_atr_levels_even_if_the_run_is_late(self):
        pub = {}; e = self.w["stocks"]["RUN"]["close"] * 1.02
        posted = self.scan(pub, (9, 41), {"RUN": self.bars_gap("RUN", 3, e)})
        self.assertEqual(len(posted), 1); x = posted[0]; atr = self.w["stocks"]["RUN"]["atr"]
        self.assertEqual(x["entry_price"], round(e, 2)); self.assertEqual(x["time"], "09:20"); self.assertEqual(x["posted_at"], "09:41")
        self.assertAlmostEqual(x["stop"] - x["entry_price"], atr, delta=0.06); self.assertAlmostEqual(x["entry_price"] - x["target"], 2 * atr, delta=0.06)
        self.assertEqual(pub["recent_alerts"][0]["side"], "short")
        self.assertEqual(self.scan(pub, (9, 46), {"RUN": self.bars_gap("RUN", 3, e)}), [])        # once a day

    def test_open_outside_the_trigger_band_is_not_a_trade(self):
        for g in (0.8, 9.0):
            self.assertEqual(self.scan({}, (9, 22), {"RUN": self.bars_gap("RUN", g)}), [])

    def test_before_920_and_after_1515_nothing_is_posted(self):
        for t in ((9, 17), (15, 16)):
            self.assertEqual(self.scan({}, t, {"RUN": self.bars_gap("RUN", 3)}), [])

    def test_920_candle_needed(self):
        self.assertEqual(self.scan({}, (9, 22), {"RUN": self.bars_gap("RUN", 3)[:1]}), [])

    def test_results_day_and_serious_news_are_declined_the_evening_before(self):
        self.setUp(results={"RUN": DAY.date().isoformat()})
        self.assertEqual(self.w["stocks"]["RUN"]["judge"]["verdict"], "REJECT")
        pub = {}; self.assertEqual(self.scan(pub, (9, 22), {"RUN": self.bars_gap("RUN", 3)}), [])
        self.assertEqual(pub["intraday"]["days"][DAY.date().isoformat()]["candidates"][0]["decision"], "DECLINE")
        self.setUp(news_fn=lambda s: {"neg_flags": ["fraud"]})
        self.assertEqual(self.scan({}, (9, 22), {"RUN": self.bars_gap("RUN", 3)}), [])

    def test_ceo_keeps_the_daily_limit_biggest_run_up_first(self):
        cands = [{"sym": f"S{k}", "facts": {"ret5": r, "why": ["x"]}, "gap_pct": 2, "entry_price": 100, "stop": 102, "target": 96,
                  "judge": {"verdict": "APPROVE", "score": 3, "rejected_because": []}} for k, r in enumerate([0.1, 0.4, 0.3, 0.2])]
        d = ii.ceo(cands, 3); post = [x["sym"] for x in d if x["ceo"]["decision"] == "POST"]
        self.assertEqual(post, ["S1", "S2", "S3"])

    def test_a_stale_watch_list_is_not_used(self):
        self.assertEqual(self.scan({}, (9, 22), {"RUN": self.bars_gap("RUN", 3)}, prev="2026-11-20"), [])


class Exits(unittest.TestCase):
    idea = {"sym": "X", "date": DAY.date().isoformat(), "time": "09:20", "entry_price": 100.0, "stop": 102.0, "target": 96.0}

    def run_path(self, path, now=(11, 0)):
        pub = {"intraday": {"live": [dict(self.idea)], "closed": [], "days": {}, "stats": {}}}
        bb = candles(100, path)
        ii.monitor(CFG, pub, datetime(DAY.year, DAY.month, DAY.day, *now), bars={"X": bb})
        return pub

    def test_stop_target_and_square_off(self):
        flat = (100, 100.5, 99.5, 100)
        pub = self.run_path([flat, flat, (100, 102.5, 99.8, 102)])                     # 9:25 candle touches the stop
        c = pub["intraday"]["closed"][0]; self.assertEqual(c["reason"], "Stop-loss hit"); self.assertEqual(c["exit_price"], 102.0)
        pub = self.run_path([flat, flat, (100, 100.2, 95.5, 96)])
        self.assertEqual(pub["intraday"]["closed"][0]["reason"], "Target hit")
        pub = self.run_path([flat] * 74, now=(15, 20))
        self.assertEqual(pub["intraday"]["closed"][0]["reason"], "Squared off at 15:15")

    def test_a_candle_touching_both_counts_as_the_stop(self):
        pub = self.run_path([(100, 100.5, 99.5, 100)] * 2 + [(100, 102.5, 95.0, 97)])
        self.assertEqual(pub["intraday"]["closed"][0]["reason"], "Stop-loss hit")

    def test_the_920_candle_counts_and_earlier_ones_do_not(self):
        pub = self.run_path([(100, 103, 99, 100), (100, 100.5, 99.5, 100)])          # 9:15 spike before the entry: ignored
        self.assertEqual(pub["intraday"]["closed"], [])
        pub = self.run_path([(100, 100.5, 99.5, 100), (100, 102.5, 99.5, 101)])      # 9:20 candle (the entry candle) hits the stop
        self.assertEqual(pub["intraday"]["closed"][0]["reason"], "Stop-loss hit")

    def test_gap_through_the_stop_fills_at_the_open(self):
        pub = self.run_path([(100, 100.5, 99.5, 100)] * 2 + [(103, 104, 102.5, 103.5)])
        self.assertEqual(pub["intraday"]["closed"][0]["exit_price"], 103.0)

    def test_result_after_costs_and_stats(self):
        g, n = ii.net_pct(100, 96, ii.conf(CFG))
        self.assertAlmostEqual(g, 4.1667, places=3); self.assertLess(n, g - 0.2); self.assertGreater(n, g - 0.4)
        pub = self.run_path([(100, 100.5, 99.5, 100)] * 2 + [(100, 100.2, 95.5, 96)])
        st = pub["intraday"]["stats"]; self.assertEqual(st["trades"], 1); self.assertEqual(st["wins"], 1)

    def test_no_data_is_not_a_fake_result(self):
        pub = {"intraday": {"live": [dict(self.idea)], "closed": [], "days": {}, "stats": {}}}
        ii.monitor(CFG, pub, datetime(DAY.year, DAY.month, DAY.day, 15, 20), bars={})
        self.assertEqual(len(pub["intraday"]["live"]), 1)                       # retried later the same day
        ii.monitor(CFG, pub, datetime(DAY.year, DAY.month, DAY.day, 16, 40), bars={})
        c = pub["intraday"]["closed"][0]; self.assertIsNone(c["net_pct"]); self.assertEqual(pub["intraday"]["stats"]["trades"], 0)

    def test_yesterdays_idea_is_closed_at_its_last_price(self):
        pub = {"intraday": {"live": [{**self.idea, "date": "2026-11-20", "price_now": 98.0, "price_at": "15:10"}], "closed": [], "days": {}, "stats": {}}}
        ii.monitor(CFG, pub, datetime(DAY.year, DAY.month, DAY.day, 9, 40), bars={"X": candles(90, [(90, 91, 89, 90)] * 6)})
        self.assertEqual(pub["intraday"]["closed"][0]["exit_price"], 98.0)


if __name__ == "__main__":
    unittest.main()
