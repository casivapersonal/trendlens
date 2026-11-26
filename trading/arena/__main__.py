"""Command line: python -m arena <command>"""
from __future__ import annotations

import argparse
import copy
import json
import logging
import os
import sys
import webbrowser
from datetime import date, datetime, timedelta
from pathlib import Path

from .clock import IST, now_ist
from .config import ROOT, load_config, load_env, set_env_value
from .db import Journal
from .safety import RealMoneyBlocked, install_guards

log = logging.getLogger("arena")


def setup_logging(cfg: dict) -> None:
    Path(cfg["paths"]["log"]).parent.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s  %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(cfg["paths"]["log"], encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    for noisy in ("httpx", "yfinance", "urllib3", "anthropic", "peewee"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    logging.getLogger("yfinance").setLevel(logging.CRITICAL)  # its errors are reported by our own retry/warn messages


def _brain_cfg(cfg: dict, brain: str | None) -> dict:
    cfg = copy.deepcopy(cfg)
    if brain:
        cfg["strategy"]["brain"] = brain
    if cfg["strategy"]["brain"] == "auto":
        import shutil
        cfg["strategy"]["brain"] = ("claude-cli" if shutil.which("claude")
                                    else "claude-api" if os.environ.get("ANTHROPIC_API_KEY") else "rules")
    return cfg


def _open(path: str | Path, no_open: bool) -> None:
    print(f"\nResults page: {path}")
    if not no_open:
        try:
            webbrowser.open(Path(path).resolve().as_uri())
        except Exception:
            pass


def _make_run(j: Journal, cfg: dict, name: str, mode: str, feed_name: str, started) -> int:
    st = cfg["strategy"]
    return j.new_run(name, mode, feed_name, st["brain"], st.get("model", ""), cfg["account"]["starting_capital"], cfg, started.isoformat())


# ---- commands ----------------------------------------------------------
def cmd_run(a, cfg):
    from .feeds import make_live_feed
    from .runner import Runner

    cfg = _brain_cfg(cfg, a.brain)
    j = Journal(cfg["paths"]["db"])
    feed = make_live_feed(cfg)
    run = None if a.new else j.active_run("live")
    if run:
        run_id = run["id"]
        log.info("Resuming live run #%d '%s'", run_id, run["name"])
    else:
        run_id = _make_run(j, cfg, a.name or f"Live challenge {now_ist():%d %b %Y}", "live", feed.name, now_ist())
        log.info("Started live run #%d with Rs %s virtual capital", run_id, f"{cfg['account']['starting_capital']:,}")
    log.info("Feed: %s | Brain: %s | PAPER ONLY", feed.name, cfg["strategy"]["brain"])
    log.info("Results page (auto-refreshes): %s", cfg["paths"]["report"])
    try:
        Runner(cfg, feed, j, run_id).run()
    except KeyboardInterrupt:
        log.info("Stopped by you. Positions and history are saved; `python -m arena run` resumes.")
        from .report import write
        write(j, run_id, cfg["paths"]["report"], live=False)


def cmd_replay(a, cfg):
    from .feeds import load_history
    from .feeds.base import BarReplayFeed
    from .runner import Runner

    cfg = _brain_cfg(cfg, a.brain)
    bars = load_history(cfg, cfg["market"]["watchlist"])
    if not bars:
        sys.exit("No history found in data/history. Run `python -m arena fetch` first.")
    start = datetime.combine(date.fromisoformat(a.start), datetime.min.time(), IST) if a.start else None
    end = datetime.combine(date.fromisoformat(a.end), datetime.max.time(), IST) if a.end else None
    feed = BarReplayFeed(bars, start, end)
    days = sorted({t.date() for t in feed.timeline})
    if a.days:
        days = days[-a.days:]
        feed = BarReplayFeed(bars, datetime.combine(days[0], datetime.min.time(), IST), end)
    cfg["account"]["challenge_days"] = len(days)
    j = Journal(cfg["paths"]["db"])
    run_id = _make_run(j, cfg, a.name or f"Replay {days[0]} to {days[-1]} ({cfg['strategy']['brain']})", "replay", "history", feed.now())
    out = Path(cfg["paths"]["data_dir"]).parent / "reports" / f"replay_run{run_id}.html"
    log.info("Replaying %d trading days (%s to %s) with %s", len(days), days[0], days[-1], cfg["strategy"]["brain"])
    Runner(cfg, feed, j, run_id, report_path=out).run()
    _open(out, a.no_open)
    return run_id


def cmd_demo(a, cfg):
    from .feeds.sim import SimFeed
    from .runner import Runner

    cfg = _brain_cfg(cfg, a.brain or "rules")  # demo stays free unless you ask for Claude
    cfg["paths"]["db"] = str(Path(cfg["paths"]["data_dir"]) / "demo.db")
    feed = SimFeed(cfg["market"]["watchlist"], n_days=a.days, seed=a.seed)
    cfg["account"]["challenge_days"] = a.days
    j = Journal(cfg["paths"]["db"])
    run_id = _make_run(j, cfg, f"Demo on synthetic prices ({cfg['strategy']['brain']})", "demo", "synthetic", feed.now())
    out = Path(cfg["paths"]["data_dir"]).parent / "reports" / "demo.html"
    log.info("DEMO: %d days of synthetic prices, brain=%s. Not real market data.", a.days, cfg["strategy"]["brain"])
    Runner(cfg, feed, j, run_id, report_path=out).run()
    _open(out, a.no_open)


def cmd_fetch(a, cfg):
    from .feeds import save_history

    syms = cfg["market"]["watchlist"]
    if a.source == "upstox":
        from .feeds.upstox import UpstoxFeed
        bars = UpstoxFeed(cache_dir=cfg["paths"]["data_dir"]).history(syms, days=a.days)
    else:
        from .feeds.yahoo import YahooFeed
        period, interval = ("7d", "1m") if a.interval == "1m" else (f"{min(a.days, 59)}d", "5m")
        bars = YahooFeed().history(syms, period=period, interval=interval)
    paths = save_history(cfg, bars)
    for s, df in bars.items():
        print(f"  {s:12s} {len(df):6d} bars  {df.index[0]:%Y-%m-%d} to {df.index[-1]:%Y-%m-%d}")
    print(f"Saved {len(paths)} files to {Path(cfg['paths']['data_dir']) / 'history'}")


def cmd_report(a, cfg):
    from .brain import make_brain
    from .insights import ai_review
    from .report import write

    db = cfg["paths"]["db"] if not a.demo else str(Path(cfg["paths"]["data_dir"]) / "demo.db")
    j = Journal(db)
    run = j.one("SELECT * FROM runs WHERE id=?", (a.run,)) if a.run else j.latest_run()
    if not run:
        sys.exit("No runs yet.")
    if a.ai:
        brain_name = a.brain or (cfg["strategy"]["brain"] if cfg["strategy"]["brain"] != "rules" else "claude-api")
        print("Asking Claude for a coach review...")
        print(ai_review(make_brain(_brain_cfg(cfg, brain_name)), j, run["id"]))
    out = a.out or (cfg["paths"]["report"] if run["mode"] == "live" else Path(cfg["paths"]["data_dir"]).parent / "reports" / f"{run['mode']}_run{run['id']}.html")
    write(j, run["id"], out, live=False)
    _open(out, a.no_open)


def cmd_status(a, cfg):
    j = Journal(cfg["paths"]["db"])
    rows = j.q("SELECT r.id, r.name, r.mode, r.brain, r.status, r.starting_capital, a.cash, a.realized, a.charges "
               "FROM runs r JOIN account a ON a.run_id=r.id ORDER BY r.id DESC LIMIT 20")
    if not rows:
        print("No runs yet.")
    for r in rows:
        pos = j.q("SELECT symbol, qty, avg_price, last_price FROM positions WHERE run_id=?", (r["id"],))
        eq = r["cash"] + sum(p["qty"] * (p["last_price"] or p["avg_price"]) for p in pos)
        days = j.one("SELECT COUNT(*) n FROM days WHERE run_id=?", (r["id"],))["n"]
        print(f"#{r['id']:<3} {r['status']:8s} {r['mode']:7s} {r['brain']:10s} days={days:<3} equity=Rs {eq:,.2f} "
              f"P&L={eq - r['starting_capital']:+,.2f} charges={r['charges']:,.2f}  {r['name']}")


def cmd_compete(a, cfg):
    brains = [b.strip() for b in a.brains.split(",") if b.strip()]
    ids = []
    for b in brains:
        ns = argparse.Namespace(brain=b, start=a.start, end=a.end, days=a.days, name=f"Compete: {b}", no_open=True)
        ids.append((b, cmd_replay(ns, cfg)))
    j = Journal(cfg["paths"]["db"])
    print("\nLEADERBOARD (same prices, same capital)")
    for b, rid in sorted(ids, key=lambda x: -j.one("SELECT realized FROM account WHERE run_id=?", (x[1],))["realized"]):
        acc = j.one("SELECT * FROM account WHERE run_id=?", (rid,))
        print(f"  {b:12s} run #{rid}: net P&L Rs {acc['realized']:+,.2f}  charges Rs {acc['charges']:,.2f}")


def cmd_upstox_login(a, cfg):
    from .feeds.upstox import login_flow

    key = os.environ.get("UPSTOX_API_KEY") or input("Upstox API key: ").strip()
    secret = os.environ.get("UPSTOX_API_SECRET") or input("Upstox API secret: ").strip()
    redirect = os.environ.get("UPSTOX_REDIRECT_URI") or "http://127.0.0.1:5055/callback"
    token = login_flow(key, secret, redirect)
    set_env_value("UPSTOX_API_KEY", key)
    set_env_value("UPSTOX_API_SECRET", secret)
    set_env_value("UPSTOX_REDIRECT_URI", redirect)
    set_env_value("UPSTOX_ACCESS_TOKEN", token)
    print("Saved UPSTOX_ACCESS_TOKEN to .env (valid until early tomorrow morning).")


def cmd_check(a, cfg):
    from .safety import SELF_TEST_URLS, check_url

    ok = True
    for url in SELF_TEST_URLS:
        try:
            check_url("POST", url)
            print(f"  FAIL: {url} was not blocked")
            ok = False
        except RealMoneyBlocked:
            print(f"  ok   real-order URL blocked: {url}")
    try:
        __import__("kiteconnect")
        print("  FAIL: broker SDK import was allowed")
        ok = False
    except RealMoneyBlocked:
        print("  ok   broker SDK imports blocked")
    except ImportError:
        print("  ok   broker SDK imports blocked")
    try:
        from .feeds import make_live_feed
        feed = make_live_feed(cfg)
        q = feed.quotes(cfg["market"]["watchlist"][:3])
        for s, x in q.items():
            print(f"  ok   {feed.name}: {s} {x.ltp:.2f} ({x.change_pct:+.2f}%) at {x.ts:%d %b %H:%M}")
        if not q:
            print(f"  WARN {feed.name}: no quotes returned"); ok = False
    except Exception as e:
        print(f"  FAIL market data: {e}"); ok = False
    brain = _brain_cfg(cfg, None)["strategy"]["brain"]
    print(f"  info using brain: {brain}" + ("  (free rules strategy; install Claude Code to let Claude trade)" if brain == "rules" else ""))
    if brain == "claude-api":
        print("  ok   ANTHROPIC_API_KEY set" if os.environ.get("ANTHROPIC_API_KEY") else "  WARN ANTHROPIC_API_KEY not set (or use `ant auth login`)")
    elif brain == "claude-cli":
        import shutil
        print("  ok   claude CLI found" if shutil.which("claude") else "  FAIL claude CLI not on PATH"); ok = ok and bool(shutil.which("claude"))
    print("\nAll good." if ok else "\nSome checks need attention.")


def cmd_scan(a, cfg):
    """Scan the universe (Nifty 500 by default) and print the short list the arena would watch today."""
    from .feeds import make_live_feed
    from .scanner import Shortlister
    sl = Shortlister(cfg, make_live_feed(cfg))
    out = sl.open_day(now_ist(), [])
    for s in out:
        print(f"{s:<12} {sl.why.get(s, '')}")
    print(f"{len(out)} of {len(sl.pool)} pooled stocks shown")


def cmd_committee(a, cfg):
    """Print the committee memo for one stock (needs live prices)."""
    from .committee import Committee
    from .feeds import make_live_feed
    from .indicators import snapshot
    feed, sym = make_live_feed(cfg), a.symbol.upper()
    q = feed.quotes([sym])[sym]
    j = Journal(cfg["paths"]["db"])
    c = Committee(cfg)
    print(json.dumps({"macro": c.macro(), sym: c.memo(sym, snapshot(q, feed.candles(sym, 5, 3)), "", j, 0, now_ist())}, indent=1, default=str))


def cmd_timemachine(a, cfg):
    """10,000 replays of the rest of today for one stock at its current price."""
    from . import timemachine
    from .feeds import make_live_feed
    feed, sym = make_live_feed(cfg), a.symbol.upper()
    q = feed.quotes([sym])[sym]
    st = cfg["strategy"]
    df = feed.history([sym], period="60d", interval="5m")[sym] if hasattr(feed, "history") else feed.candles(sym, 5, 60)
    r = timemachine.run(df, now_ist(), q.ltp, a.stop or st["default_stop_loss_pct"], a.target or st["default_target_pct"], a.qty, cfg, n=a.replays)
    print(json.dumps(r, indent=1))


def _momentum_ranks():
    """TrendLens momentum ranks (the original entry/exit rule) from the website pipeline's history.json: ({sym: rank}, {sym: 12m return})."""
    from . import universe
    f = universe.REPO / "web" / "history.json"
    if not f.exists(): return {}, {}
    sys.path.insert(0, str(universe.REPO / "pipeline"))
    try:
        import core
        h = json.loads(f.read_text()); prices = {s: c[-1] for s, c in h["closes"].items() if c and c[-1]}
        return core.ranks_now(h, prices, h["dates"][-1])
    except Exception as e:
        logging.getLogger("arena").warning("momentum ranks unavailable: %s", e); return {}, {}


def _history(feed, syms, period="2y"):
    out = {}
    for k in range(0, len(syms), 100):
        try: out.update(feed.history(syms[k:k + 100], period=period, interval="1d"))
        except Exception as e: logging.getLogger("arena").warning("history batch failed: %s", e)
    return out


def market_open_ist(now) -> bool:
    import datetime as _dt
    return now.weekday() < 5 and _dt.time(9, 15) <= now.time() <= _dt.time(15, 30)


def cmd_ideas(a, cfg):
    """Ideas pipeline on web/picks.json. scan -> pending (judge) -> approve -> live -> monitor -> close.  No trading, no real-time loop."""
    from . import ideas
    now, who = now_ist(), a.by or os.environ.get("DECISION_MAKER", "decision-maker")
    if a.action == "scan":
        if market_open_ist(now) and not a.force:
            print("Market is open: today's daily bar is not final. Scan after 15:30 IST (or pass --force)."); return
        from .committee import Committee
        from .feeds import make_live_feed
        from .scanner import Shortlister, daily_setups
        feed = make_live_feed(cfg)
        if market_open_ist(now):
            # a forced scan during market hours must use CLOSED daily bars only: drop today's partial bar, so the levels are the last close's
            # (otherwise the ideas get today's date and the open check rejects them as stale)
            _h = feed.history
            def _closed(syms, period="2y", interval="1d", **kw):
                out = _h(syms, period=period, interval=interval, **kw)
                if interval != "1d": return out
                return {k: (d[d.index.date < now.date()] if d is not None and len(d) else d) for k, d in out.items()}
            feed.history = _closed
        sl = Shortlister(cfg, feed); sl.open_day(now, [])
        lastbar = max((d.index[-1].date() for d in sl.frames.values() if d is not None and len(d)), default=None)
        if lastbar != now.date() and not a.force:
            print(f"No trading session today (latest bar {lastbar}): nothing to scan."); return
        ranks, moms = _momentum_ranks()
        extra = [x for x, r in ranks.items() if r <= cfg["ideas"].get("entry_rank", 20) and x not in sl.frames]
        for x, d in _history(feed, extra, "2y").items():
            if d is not None and not d.empty and len(d) > 30: sl.frames[x] = d; sl.setups[x] = daily_setups(d, cfg["ideas"].get("setups"))
        pen = ideas.scan(cfg, sl, Committee(cfg), now, ranks=ranks, moms=moms)
        for q in pen["queue"]:
            print(f"{q['status']:<18} {q['id']:<26} score {q['judge']['score']:>2}  entry {q['entry_lo']}-{q['entry_hi']}  stop {q['stop']}  target {q['target']}  {q['term']}")
        print(len(pen["queue"]), "in the queue.")
        if cfg.get("ceo", {}).get("enabled", True):
            from . import ceo
            fe = make_live_feed(cfg)
            for d in ceo.decide_entries(cfg, now, ideas, frames_for=lambda syms: _history(fe, syms)):
                print(f"CEO {d['decision']:<8} {d['sym']:<12} {d['conviction']:<7} {d['memo']}")
    elif a.action == "intraday":
        # every 5 minutes in market hours: live prices -> entries filled, stop / target exits closed
        import datetime as _dt, time as _t
        if not (market_open_ist(now) and now.time() >= _dt.time(9, 15)) and not a.force:
            print("Market is closed: the intraday CEO runs 09:15-15:30 IST."); return
        from . import universe as uni
        sys.path.insert(0, str(uni.REPO / "pipeline")); import core
        h = json.loads((uni.REPO / "web" / "history.json").read_text())
        pub = ideas.public(); live = [i["sym"] for i in pub["live"]]
        fresh = lambda v: bool(v and v.get("t") and _t.time() - float(v["t"]) < 600)       # traded in the last 10 minutes
        nifty = core.quote("NIFTY 50")[1]
        if not a.force and not fresh(nifty):
            print("No trading session right now (NIFTY has not traded in the last 10 minutes: holiday or feed delay). Nothing done."); return
        q = core.quotes(live) if live else {}
        prices = {s: v["p"] for s, v in q.items() if v and v.get("p") and (a.force or fresh(v))}
        last = {s: c[-1] for s, c in h["closes"].items() if c and c[-1]}
        jump = [s for s, p in prices.items() if s in last and abs(p / last[s] - 1) > cfg["ideas"].get("max_day_move", 0.35)]
        for s in jump: prices.pop(s)                                       # possible split / bonus not yet adjusted: never act on it
        if jump: print("held for review (one-day move above 35%, possible corporate action):", ", ".join(jump))
        print(f"live prices for {len(prices)} stocks ({len(q) - len(prices)} stale or held)")
        if market_open_ist(now):                   # a forced test outside market hours must not mark entries or exits on stale prices
            pcs = {s: v.get("pc") for s, v in q.items() if v}
            for g in ideas.monitor_live(cfg, prices, now, prev_close=pcs): print("EXIT SIGNAL", g)
        else: print("market closed: entries and exits are not checked on a forced run")
        if cfg.get("ceo", {}).get("enabled", True):
            from . import ceo
            for d in ceo.decide_exits(cfg, now, ideas): print("CEO CLOSED", d)
        # audit fix #8: NEW ideas are CHOSEN only by the evening scan on closing prices (as back-tested), never on intraday prices.
        # Nov 2026: with post_at_open they are then POSTED by `ideas open` at the next opening price (only inside the entry range);
        # this run only marks entries and flags stop / target exits on live prices.
        print("entries and exits checked; new ideas are chosen only by the evening scan on closing prices and posted at the open")
    elif a.action == "open":
        # from open_post_at (09:30) (market loop): post the ideas the CEO chose last evening if the price then is inside the entry range; outside = not posted
        import datetime as _dt
        from . import intraday_ideas as ii
        if not cfg["ideas"].get("post_at_open"): print("post_at_open is off: ideas are posted by the 16:15 scan."); return
        at = cfg["ideas"].get("open_post_at", "09:15"); atT = _dt.time(int(at[:2]), int(at[3:5]))
        if not market_open_ist(now) and not a.force: print(f"Market is closed: the check runs from {at} IST."); return
        if now.time() < atT and not a.force: print(f"too early: ideas are checked and posted from {at} IST"); return
        sched = [q for q in ideas.pending()["queue"] if q["status"] == "scheduled for open"]
        if not sched: print("nothing scheduled for the open"); return
        at_override = None
        if a.force:
            # MANUAL run (--force): check the LIVE price right now instead of the 9:30 candle. Inside the entry range -> posted at the live price
            # (reward-to-risk is re-checked at that price); outside -> not posted. Used to post ideas the scheduled run missed.
            from . import universe as uni
            sys.path.insert(0, str(uni.REPO / "pipeline")); import core
            lq = core.quotes([q["sym"] for q in sched])
            opens = {q["sym"]: (lq.get(q["sym"]) or {}).get("p") for q in sched}
            print("live prices:", {s: o for s, o in opens.items()})
            at_override = now.strftime("%H:%M"); final = True
        else:
            bars = ii.many_5m([q["sym"] for q in sched]); today_bars = {s: [x for x in bb if x["t"].date() == now.date()] for s, bb in bars.items()}
            if not any(today_bars.values()): print("no 5-minute data for today yet (holiday or feed delay): the ideas stay scheduled"); return
            opens = {s: next((x["o"] for x in bb if x["t"].time() == atT), None) for s, bb in today_bars.items()}       # the open of the {at} five-minute candle
            cut = cfg["ideas"].get("open_post_until", "10:00"); final = now.time() >= _dt.time(int(cut[:2]), int(cut[3:5]))
        fe = None
        from .feeds import make_live_feed
        fe = make_live_feed(cfg)
        for r in ideas.post_at_open(cfg, now, opens, frames=_history(fe, [q["sym"] for q in sched]), prev_session=ii._prev_session(now.date().isoformat()), final=final, at_override=at_override): print(r["decision"], r["memo"])
    elif a.action == "monitor":
        from .feeds import make_live_feed
        pub = ideas.public(); live = [i["sym"] for i in pub["live"]]
        frames = _history(make_live_feed(cfg), live) if live else {}
        for g in ideas.monitor(cfg, frames, now): print("EXIT SIGNAL", g)
        if cfg.get("ceo", {}).get("enabled", True):
            from . import ceo
            for d in ceo.decide_exits(cfg, now, ideas): print("CEO CLOSED", d)
        print("monitored", len(live), "live ideas. Close one with: python -m arena ideas close <SYM> --by \"Name\"")
    elif a.action == "pending":
        for q in ideas.pending()["queue"]:
            print(f"{q['status']:<18} {q['id']:<26} score {q['judge']['score']:>2}  {q['committee']['agreement']}")
            for r in q["judge"]["rules"]: print(f"      {r['points']:+d} {r['rule']}")
            rv = q.get("review") or {}
            if rv: print(f"      BULL: {'; '.join(rv['bull']) or '-'}\n      BEAR: {'; '.join(rv['bear']) or '-'}  (lean: {rv['lean']}, economy: {rv['economic']['stance']})")
            print(f"      risk check: {q['risk']['verdict']} {'; '.join(q['risk']['reasons'])} | entry: {q.get('entry_state')}")
            if rv.get("time_machine"): print(f"      Time Machine: {rv['time_machine']['verdict']} (sim target {rv['time_machine']['sim']['target']}%, stop {rv['time_machine']['sim']['stop']}%; {rv['time_machine']['proof']})")
    elif a.action == "approve":
        frames = {}
        try:
            from .feeds import make_live_feed
            sym = next((q["sym"] for q in ideas.pending()["queue"] if q["id"] == a.target), None)
            frames = _history(make_live_feed(cfg), [sym]) if sym else {}
        except Exception as e: print("chart bars not fetched (the next refresh adds them):", e)
        print(json.dumps(ideas.approve(a.target, who, now, frames=frames, force=a.force), indent=1))
    elif a.action == "reject": ideas.reject(a.target); print("removed", a.target)
    elif a.action == "close": print(json.dumps(ideas.close(a.target, who, now, a.reason, a.price), indent=1))


def cmd_intraday(a, cfg):
    """Intraday segment (exhaustion fade, short). prep = after the close, build tomorrow's watch list from daily bars;
    run = every 5 minutes in market hours: the 9:20 scan (judge -> CEO -> post) and stop / target / 15:15 exits."""
    import datetime as _dt
    from . import ideas, intraday_ideas as ii
    now = now_ist(); c = ii.conf(cfg)
    if not c["enabled"]: print("intraday segment is switched off ([intraday] enabled = false)"); return
    holidays = set(ii._holidays(cfg))
    if (now.weekday() >= 5 or now.date().isoformat() in holidays) and a.action != "prep" and not a.force:
        if now.date().isoformat() in holidays and now.weekday() < 5:        # say so on the site instead of leaving the setups unanswered
            pub = ideas.public(); day = ii.book(pub)["days"].setdefault(now.date().isoformat(), {"scanned": None, "candidates": []})
            if not day.get("scanned"):
                day.pop("waiting", None); day.update(scanned=now.isoformat(timespec="minutes"), holiday=True, note="NSE holiday: the market is closed today, so no setup could trigger")
                ideas._publish(pub, now)
        print("Not a trading day: nothing to do."); return
    if a.action == "prep":
        from .feeds import make_live_feed
        pub = ideas.public()
        for x in ii.monitor(cfg, pub, now):           # anything a late or skipped 15:15 run left open: closed at the 15:15 candle
            print(f"CLOSED  {x['sym']:<12} {x['reason']} at {x['exit_price']}  net {x['net_pct']}")
        ideas._publish(pub, now)
        fno = sorted(ii.fno_list())
        if not fno: print("no F&O list: nothing prepared"); return
        frames = _history(make_live_feed(cfg), fno, "1y")
        from .committee import Committee
        w = ii.prep(cfg, frames, now, news_fn=Committee(cfg).news)
        pub = ideas.public(); ii.book(pub).pop("watch", None)      # Nov 2026: tomorrow's conditional setups are NOT published; an intraday idea appears on the site only when it is posted at 9:20
        ib = pub["intraday"]                                  # daily bars for the cards, the About pop-up and the PDF (charts on the website)
        need = {i["sym"] for i in ib["live"]} | {x["sym"] for x in ib["closed"][:40]}
        ib["bars"] = {s: ideas.ohlc_rows(frames[s], 200) for s in sorted(need) if frames.get(s) is not None and not frames[s].empty}
        pub["recent_alerts"] = [x for x in pub.get("recent_alerts", []) if x.get("type") != "watch"]      # no evening "intraday setups" alert any more
        ideas._publish(pub, now)
        print(f"F&O stocks {len(fno)}, daily bars {len(frames)}, conditional ideas for {w['for']}: {len(w['stocks'])}")
        for s, f in sorted(w["stocks"].items(), key=lambda x: -x[1]["ret5"]):
            print(f"  {f['judge']['verdict']:<8} {s:<14} opens {f['trigger_lo']}-{f['trigger_hi']}: {'; '.join(f['why'])} {'; '.join(f['judge']['rejected_because'])}")
        return
    if not (_dt.time(9, 15) <= now.time() <= _dt.time(16, 0)) and not a.force:      # until 16:00 so a late run still books the 15:15 exit
        print("Outside 09:15-16:00 IST: intraday ideas are not checked now."); return
    pub = ideas.public()
    posted = ii.scan(cfg, pub, now)
    for x in posted: print(f"POSTED  {x['sym']:<12} short {x['entry_price']} stop {x['stop']} target {x['target']}  ({x['ceo']['memo']})")
    day = ii.book(pub)["days"].get(now.date().isoformat(), {})
    if day.get("waiting"): print("waiting for the 9:15 / 9:20 candles of: " + ", ".join(day["waiting"]))
    for x in day.get("candidates", []) if posted else []: print(f"  {x['decision']:<8} {x['sym']:<12} gap {x['gap_pct']:+.1f}%  score {x['score']}")
    for x in ii.monitor(cfg, pub, now): print(f"CLOSED  {x['sym']:<12} {x['reason']} at {x['exit_price']}  net {x['net_pct']}")
    ideas._publish(pub, now)
    b = pub["intraday"]; print(f"intraday live {len(b['live'])}, closed {len(b['closed'])}, stats {b['stats']}")


def cmd_gui(a, cfg):
    from .gui import serve
    serve(a.port, not a.no_open)


def main(argv=None):
    load_env()
    p = argparse.ArgumentParser(prog="arena", description="Breaking Money AI Trading Arena (paper trading only)")
    p.add_argument("--config", help="path to config.toml")
    sub = p.add_subparsers(dest="cmd", required=True)

    r = sub.add_parser("run", help="DISABLED in TrendLens (real-time trading is off); use `ideas`")
    r.add_argument("--new", action="store_true", help="start a fresh run instead of resuming")
    r.add_argument("--name"); r.add_argument("--brain", choices=["auto", "claude-api", "claude-cli", "rules"])

    rp = sub.add_parser("replay", help="replay saved history without lookahead")
    rp.add_argument("--start"); rp.add_argument("--end"); rp.add_argument("--days", type=int)
    rp.add_argument("--name"); rp.add_argument("--brain", choices=["auto", "claude-api", "claude-cli", "rules"])
    rp.add_argument("--no-open", action="store_true")

    d = sub.add_parser("demo", help="offline demo on synthetic prices")
    d.add_argument("--days", type=int, default=15); d.add_argument("--seed", type=int, default=7)
    d.add_argument("--brain", choices=["auto", "claude-api", "claude-cli", "rules"]); d.add_argument("--no-open", action="store_true")

    f = sub.add_parser("fetch", help="download history for replay")
    f.add_argument("--source", choices=["yahoo", "upstox"], default="yahoo")
    f.add_argument("--days", type=int, default=30); f.add_argument("--interval", choices=["1m", "5m"], default="5m")

    rep = sub.add_parser("report", help="rebuild the results page")
    rep.add_argument("--run", type=int); rep.add_argument("--out"); rep.add_argument("--demo", action="store_true")
    rep.add_argument("--ai", action="store_true", help="add Claude's coach review")
    rep.add_argument("--brain", choices=["claude-api", "claude-cli"]); rep.add_argument("--no-open", action="store_true")

    sub.add_parser("status", help="list runs and their P&L")
    c = sub.add_parser("compete", help="several brains on the same replay data")
    c.add_argument("--brains", default="rules,claude-api"); c.add_argument("--start"); c.add_argument("--end"); c.add_argument("--days", type=int)
    sub.add_parser("upstox-login", help="get today's Upstox market-data token")
    sub.add_parser("check", help="test safety guard, market data and Claude setup")
    sub.add_parser("scan", help="scan the universe and print today's short list")
    ix = sub.add_parser("ideas", help="ideas pipeline: scan, pending, approve, reject, monitor, close")
    ix.add_argument("action", choices=["scan", "intraday", "open", "pending", "approve", "reject", "monitor", "close"]); ix.add_argument("target", nargs="?")
    ix.add_argument("--reason"); ix.add_argument("--price", type=float); ix.add_argument("--by"); ix.add_argument("--force", action="store_true", help="approve even if the risk check says BLOCK")
    it = sub.add_parser("intraday", help="intraday segment: prep (after the close) | run (every 5 minutes in market hours)")
    it.add_argument("action", choices=["prep", "run"]); it.add_argument("--force", action="store_true", help="run outside market hours (testing)")
    cm = sub.add_parser("committee", help="committee memo for one stock"); cm.add_argument("symbol")
    tm = sub.add_parser("timemachine", help="10,000 replays for one stock")
    tm.add_argument("symbol"); tm.add_argument("--stop", type=float); tm.add_argument("--target", type=float)
    tm.add_argument("--qty", type=int, default=100); tm.add_argument("--replays", type=int, default=10000)
    g = sub.add_parser("gui", help="open the control panel in your browser")
    g.add_argument("--port", type=int, default=8787); g.add_argument("--no-open", action="store_true")

    a = p.parse_args(argv)
    cfg = load_config(a.config)
    install_guards(cfg)
    setup_logging(cfg)
    {"run": cmd_run, "replay": cmd_replay, "demo": cmd_demo, "fetch": cmd_fetch, "report": cmd_report,
     "status": cmd_status, "compete": cmd_compete, "upstox-login": cmd_upstox_login, "check": cmd_check, "gui": cmd_gui,
     "scan": cmd_scan, "ideas": cmd_ideas, "intraday": cmd_intraday, "committee": cmd_committee, "timemachine": cmd_timemachine}[a.cmd](a, cfg)


if __name__ == "__main__":
    main()
