"""Nov 2026: ideas chosen on the evening scan are posted at the next open, only if the opening price is inside the entry range.
Run: python -m unittest discover tests"""
from __future__ import annotations
import json, sys, tempfile, unittest
from datetime import datetime
from pathlib import Path
from unittest import mock
ROOT = Path(__file__).resolve().parent.parent; sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(Path(__file__).resolve().parent))
import pandas as pd
from arena import ideas, ceo
from arena.clock import IST
from arena.config import load_config
from test_ideas import frame, paths, FakeShort, FakeCommittee, NOW

MORNING = datetime(2026, 11, 23, 9, 21, tzinfo=IST)          # the session after NOW (Fri 20 Nov -> Mon 23 Nov)
PREV = "2026-11-20"


class OpenPost(unittest.TestCase):
    cfg = load_config()

    def scheduled(self, t, rank=3):
        pp, pu = paths(t); pu.write_text(json.dumps({"live": [], "closed": []}))
        today = frame(start=str(pd.bdate_range(end="2026-11-20", periods=300)[0].date()))
        ideas.scan(self.cfg, FakeShort({"AAA": today}, {}, {"AAA": 0.97}), FakeCommittee(self.cfg), NOW, pp, pu, ranks={"AAA": rank}, moms={"AAA": 0.5})
        with mock.patch("arena.committee.Committee._funds", return_value={"stocks": {}}):
            out = ceo.decide_entries(self.cfg, NOW, ideas, pending_path=pp, public_path=pu)
        return pp, pu, out

    def test_evening_scan_schedules_and_posts_nothing(self):
        self.assertTrue(self.cfg["ideas"]["post_at_open"])
        with tempfile.TemporaryDirectory() as t:
            pp, pu, out = self.scheduled(t)
            self.assertEqual(out[0]["decision"], "POST AT OPEN"); self.assertEqual(ideas.public(pu)["live"], [])
            self.assertEqual(ideas.pending(pp)["queue"][0]["status"], "scheduled for open")

    def test_open_inside_range_posts_at_the_opening_price(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu, _ = self.scheduled(t); q = ideas.pending(pp)["queue"][0]
            o = round((q["entry_lo"] + q["entry_hi"]) / 2, 2)
            r = ideas.post_at_open(self.cfg, MORNING, {"AAA": o}, pp, pu, prev_session=PREV)
            self.assertEqual(r[0]["decision"], "POSTED")
            i = ideas.public(pu)["live"][0]
            self.assertEqual(i["entry_price"], o); self.assertEqual(i["filled"]["price"], o); self.assertEqual(i["filled"]["date"], "2026-11-23")
            self.assertNotIn("provisional", i["filled"]); self.assertEqual(i["posted"], "2026-11-23"); self.assertEqual(i["entry_time"], "at the open")
            self.assertEqual(i["signal_close"], q["entry_price"]); self.assertEqual(ideas.pending(pp)["queue"], [])
            dist = q["entry_price"] - q["stop"]; self.assertAlmostEqual(i["stop"], ideas.tick(o - dist), places=2)   # momentum stop measured from the fill, as back-tested
            self.assertEqual(ideas.public(pu)["recent_alerts"][0]["price"], o)

    def test_open_outside_range_is_not_posted(self):
        for name, f in (("above", lambda q: q["entry_hi"] * 1.01), ("below", lambda q: q["entry_lo"] * 0.99)):
            with self.subTest(name), tempfile.TemporaryDirectory() as t:
                pp, pu, _ = self.scheduled(t); q = ideas.pending(pp)["queue"][0]
                r = ideas.post_at_open(self.cfg, MORNING, {"AAA": round(f(q), 2)}, pp, pu, prev_session=PREV)
                self.assertEqual(r[0]["decision"], "NOT POSTED"); self.assertIn(name, r[0]["memo"])
                self.assertEqual(ideas.public(pu)["live"], []); self.assertEqual(ideas.pending(pp)["queue"], [])
                self.assertEqual(ideas.pending(pp)["open_log"][-1]["decision"], "NOT POSTED")

    def test_range_edges_are_inside(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu, _ = self.scheduled(t); q = ideas.pending(pp)["queue"][0]
            self.assertEqual(ideas.post_at_open(self.cfg, MORNING, {"AAA": q["entry_hi"]}, pp, pu, prev_session=PREV)[0]["decision"], "POSTED")

    def test_no_opening_price_waits_then_drops_at_the_cutoff(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu, _ = self.scheduled(t)
            self.assertEqual(ideas.post_at_open(self.cfg, MORNING, {"AAA": None}, pp, pu, prev_session=PREV), [])
            self.assertEqual(ideas.pending(pp)["queue"][0]["status"], "scheduled for open")
            r = ideas.post_at_open(self.cfg, MORNING.replace(hour=10, minute=1), {"AAA": None}, pp, pu, prev_session=PREV, final=True)
            self.assertEqual(r[0]["decision"], "NOT POSTED"); self.assertEqual(ideas.public(pu)["live"], [])

    def test_stale_levels_are_not_posted(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu, _ = self.scheduled(t); q = ideas.pending(pp)["queue"][0]
            r = ideas.post_at_open(self.cfg, MORNING, {"AAA": q["entry_price"]}, pp, pu, prev_session="2026-11-23")
            self.assertEqual(r[0]["decision"], "NOT POSTED"); self.assertIn("stale", r[0]["memo"])

    def test_a_stock_already_live_is_not_posted_again(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu, _ = self.scheduled(t); q = ideas.pending(pp)["queue"][0]
            p = ideas.public(pu); p["live"].append({"sym": "AAA", "term": "short", "id": "old"}); pu.write_text(json.dumps(p))
            r = ideas.post_at_open(self.cfg, MORNING, {"AAA": q["entry_price"]}, pp, pu, prev_session=PREV)
            self.assertEqual(r[0]["decision"], "NOT POSTED"); self.assertIn("already a live idea", r[0]["memo"])
            self.assertEqual(len(ideas.public(pu)["live"]), 1)

    def test_the_monitor_keeps_the_open_fill(self):
        with tempfile.TemporaryDirectory() as t:
            pp, pu, _ = self.scheduled(t); q = ideas.pending(pp)["queue"][0]; o = q["entry_price"]
            ideas.post_at_open(self.cfg, MORNING, {"AAA": o}, pp, pu, prev_session=PREV)
            d = frame(start=str(pd.bdate_range(end="2026-11-20", periods=300)[0].date()))
            add = pd.DataFrame([(o, o * 1.01, o * 0.995, o * 1.005)], index=pd.bdate_range("2026-11-23", periods=1, tz=IST), columns=["open", "high", "low", "close"]); add["volume"] = 3e6
            ideas.monitor(self.cfg, {"AAA": pd.concat([d, add])}, MORNING.replace(hour=16, minute=15), pu)
            i = ideas.public(pu)["live"][0]; self.assertEqual(i["filled"]["price"], o); self.assertIsNone(i.get("exit_signal"))

    def test_flag_off_keeps_the_old_evening_posting(self):
        cfg = json.loads(json.dumps(self.cfg)); cfg["ideas"]["post_at_open"] = False
        with tempfile.TemporaryDirectory() as t:
            pp, pu = paths(t); pu.write_text(json.dumps({"live": [], "closed": []}))
            today = frame(start=str(pd.bdate_range(end="2026-11-20", periods=300)[0].date()))
            ideas.scan(cfg, FakeShort({"AAA": today}, {}, {"AAA": 0.97}), FakeCommittee(cfg), NOW, pp, pu, ranks={"AAA": 3}, moms={"AAA": 0.5})
            with mock.patch("arena.committee.Committee._funds", return_value={"stocks": {}}):
                ceo.decide_entries(cfg, NOW, ideas, pending_path=pp, public_path=pu)
            self.assertEqual(len(ideas.public(pu)["live"]), 1)


if __name__ == "__main__":
    unittest.main()
