"""Ideas pipeline, one store: web/picks.json (what the website's Trade Ideas tab reads).
scan -> committee -> coded judge -> PENDING (trading/ideas/pending.json) -> decision-maker approves -> LIVE idea
-> monitor flags exit signals (stop, target, time, faded momentum rank) -> decision-maker closes -> CLOSED idea.
Stops (Nov 2026 study, pipeline/stop_final_check.py + stop_robust.py): momentum ideas exit on a touch of the stop; bull-flag ideas
(stop_rule "close") exit at the next open after a CLOSE below the stop, plus an emergency stop short_hard_stop_r x the risk below the fill.
Time exit (Nov 2026, pipeline/exit_check.py): a bull flag still open on its last session (day `days`) is sold through that day; the result is
counted at the day's VWAP from 5-minute candles (beat the close by +0.19% (t 2.6) in 2014-21 and +0.21% (t 7.9) in 2022-26). The site warns
on the last day. If the day's candles cannot be read, the close is used and the note says so.
Nothing is posted and nothing is closed without the decision-maker, unless auto_close is switched on. No real-time loop.
Wording is a "Bullish view", never buy or sell."""
from __future__ import annotations

import json
from datetime import date, datetime
from pathlib import Path

from . import review, timemachine, universe
from .judge import judge

REPO = universe.REPO
PENDING = REPO / "trading" / "ideas" / "pending.json"
PUBLIC = REPO / "web" / "picks.json"
tick = lambda p: round(round(p / 0.05) * 0.05, 2)


def day_vwap(sym: str, day: date) -> float | None:
    """The session's volume-weighted average price, (high + low + close) / 3 weighted by volume over the 5-minute candles of `day`
    (as in the back-test). Yahoo serves today's candles only, so an earlier day returns None. Needs most of the session (60+ candles)."""
    try:
        import requests
        r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}.NS", params={"interval": "5m", "range": "1d"},
                         headers={"User-Agent": "Mozilla/5.0 TrendLens"}, timeout=15)
        r.raise_for_status(); res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]
        pv = vv = 0.0; n = 0
        for k, ts in enumerate(res.get("timestamp") or []):
            if datetime.utcfromtimestamp(ts + 19800).date() != day: continue
            h, l, c, v = q["high"][k], q["low"][k], q["close"][k], q["volume"][k]
            if None in (h, l, c, v) or v <= 0: continue
            pv += (h + l + c) / 3 * v; vv += v; n += 1
        return round(pv / vv, 2) if vv > 0 and n >= 60 else None
    except Exception:
        return None


VWAP_FN = day_vwap          # tests replace this
KINDS = {"Target hit": "target", "Stop-loss hit": "stop", "Time up": "time"}


def _load(p: Path, default):
    try:
        return json.loads(p.read_text())
    except Exception:
        return default


def _save(p: Path, obj, compact=False):
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(obj, separators=(",", ":"), ensure_ascii=False) if compact else json.dumps(obj, indent=1, ensure_ascii=False))


def public(path: Path | None = None) -> dict:
    d = _load(path or PUBLIC, {})
    for k, v in (("live", []), ("closed", []), ("recent_alerts", []), ("alerts_today", []), ("bars", {})): d.setdefault(k, v)
    return d


def pending(path: Path | None = None) -> dict:
    return _load(path or PENDING, {"asof": None, "queue": [], "log": []})


def _publish(pub: dict, now: datetime, path: Path | None = None) -> None:
    pub["alerts_today"] = [a for a in pub["recent_alerts"] if a.get("date") == now.date().isoformat()]
    pub["updated"] = now.isoformat(timespec="seconds"); pub["updated_ist"] = now.strftime("%d %b %Y, %I:%M %p IST")
    _save(path or PUBLIC, pub, compact=True)               # same compact format the website pipeline writes


def _nifty(pub: dict, index: str = "NIFTY 50") -> float | None:
    b = pub.get("bars", {}).get(index)
    return float(b[-1][4]) if b else None


def ohlc_rows(d, n: int = 330) -> list:
    d = d.iloc[-n:]
    return [[x.date().isoformat(), round(float(r.open), 2), round(float(r.high), 2), round(float(r.low), 2), round(float(r.close), 2), int(r.volume)]
            for x, r in zip(d.index, d.itertuples())]


def update_bars(pub: dict, frames: dict) -> None:
    """Chart bars for every live idea and the 60 most recent closed ones (the website draws charts from picks.json)."""
    for s in {i["sym"] for i in pub["live"]} | {c["sym"] for c in pub["closed"][:60]}:
        d = frames.get(s)
        if d is not None and not d.empty: pub["bars"][s] = ohlc_rows(d)


# ---------------------------------------------------------------- levels
def levels(close: float, atr: float, term: str, cfg: dict) -> dict:
    ic = cfg["ideas"]; k = ic[f"{term}_stop_atr"]; r = ic[f"{term}_r"]
    stop_d = k * atr
    up = ic["entry_atr"]
    if term == "short":          # top of the range still gives min_rr at the fill (audit re-check): (r*k - x) / (k + x) >= min_rr
        mr = ic.get("min_rr", 2.0); up = min(up, max(0.0, (r * k - mr * k) / (1 + mr)))
    return {"entry_lo": tick(close - ic["entry_atr"] * atr), "entry_hi": tick(close + up * atr),
            "stop": tick(close - stop_d), "target": tick(close + r * stop_d), "days": ic[f"{term}_days"], "r": r}


def term_for(setups: list[str], rank: int | None = None, entry_rank: int = 20) -> str | None:
    """Medium term = the back-tested momentum rule (top `entry_rank`, held until the rank falls below the exit level), even if a setup
    also fired. Short term = a fresh daily setup on a stock outside the top ranks (not back-tested). Neither = not an idea."""
    if rank is not None and rank <= entry_rank: return "medium"
    return "short" if setups else None


def committee_proof() -> dict:
    from .committee import PROOF
    return dict(PROOF)


def verdict_status(v: str) -> str:
    return "awaiting decision" if v == "APPROVE" else "watch"


def _sessions_until(today, iso) -> int:
    """Trading sessions (Mon-Fri) from the session after `today` up to and including `iso`."""
    import numpy as np
    return int(np.busday_count(today + __import__("datetime").timedelta(days=1), date.fromisoformat(iso) + __import__("datetime").timedelta(days=1)))


def _rank_resets(pub: dict, ranks: dict, now: datetime, cfg: dict, public_path: Path | None = None) -> None:
    """Optional re-entry block after a stop (config reentry_block, off: the back-test showed it lowers returns).
    A stopped-out stock is cleared once its rank has fallen below the exit rank and at least 10 sessions have passed."""
    rb = pub.get("reentry_block") or {}
    if not rb or not ranks: return
    er = cfg["ideas"].get("exit_rank", 50); ch = False
    for sym, b in list(rb.items()):
        r = ranks.get(sym)
        if (r is None or r > er) and not b.get("reset"): b["reset"] = True; ch = True
        if b.get("reset") and _sessions_until(date.fromisoformat(b["since"]), now.date().isoformat()) >= 10: rb.pop(sym); ch = True
    if ch: pub["reentry_block"] = rb; _publish(pub, now, public_path)


def _fill_checks(i: dict, fill: float, cfg: dict) -> str | None:
    """After the actual fill (audit fix #5): momentum keeps its stop distance from the fill (as back-tested); setup ideas keep their
    chart levels and are skipped if the fill leaves less than min_rr reward-to-risk. Returns a reason to skip, or None."""
    if i.get("term") == "medium":
        st0 = i.setdefault("stop_posted", i.get("stop")); tg0 = i.setdefault("target_posted", i.get("target"))
        dist = (i.get("entry_price") or fill) - (st0 or 0)
        if st0 and dist > 0: i["stop"] = tick(fill - dist); i["target"] = tick(fill + (i.get("r") or 2) * dist)
        return None
    st, tg = i.get("stop"), i.get("target")
    if not st or not tg: return None
    if fill <= st: return "Not entered: price opened at or below the stop"
    rr = (tg - fill) / (fill - st)
    i["r_at_fill"] = round(rr, 2)
    if rr < cfg["ideas"].get("min_rr", 2.0): return f"Not entered: at the fill price {fill:.2f} reward-to-risk is {rr:.1f} (below {cfg['ideas'].get('min_rr', 2.0)})"
    if i.get("stop_rule") == "close": i["hard_stop"] = tick(fill - cfg["ideas"].get("short_hard_stop_r", 3.0) * (fill - st))
    return None


def _hard_stop(i: dict, cfg: dict) -> float | None:
    """Emergency stop of a close-rule idea: short_hard_stop_r x the risk below the fill (sold on a touch, even inside the day)."""
    if i.get("stop_rule") != "close" or not i.get("stop") or not i.get("filled"): return None
    if not i.get("hard_stop"):
        f = i["filled"]["price"]; i["hard_stop"] = tick(f - cfg["ideas"].get("short_hard_stop_r", 3.0) * (f - i["stop"]))
    return i["hard_stop"]


def _locked(r) -> bool:
    """A session that traded at one price all day (circuit-locked): nobody could sell at the stop."""
    return float(r["high"]) == float(r["low"])


# ---------------------------------------------------------------- scan -> pending
def scan(cfg: dict, shortlister, committee, now: datetime, pending_path: Path | None = None, public_path: Path | None = None,
         ranks: dict | None = None, moms: dict | None = None, entry_time: str = "after close") -> dict:
    """Candidates = the scanner's pool (daily setups + momentum) plus the stocks in the top `entry_rank` of the TrendLens momentum
    ranking (the original entry rule). `shortlister.frames` must hold daily bars for all of them."""
    ranks, moms = ranks or {}, moms or {}
    pub, pen = public(public_path), pending(pending_path)
    live_syms = {i["sym"] for i in pub["live"]}
    # Every scan re-scores the whole queue on fresh data, so nothing waits on stale levels, sizing or verdicts.
    # An idea already awaiting a decision keeps its place if it still earns APPROVE.
    was_waiting = {q["sym"] for q in pen["queue"] if q["status"] == "awaiting decision"}
    macro = committee.macro(); bl = universe.blocked(); res = universe.results()
    _rank_resets(pub, ranks, now, cfg, public_path)
    room = max(0, cfg["ideas"]["max_live"] - len(live_syms))
    top = [s for s, r in sorted(ranks.items(), key=lambda x: x[1]) if r <= cfg["ideas"].get("entry_rank", 20)]
    out, log = [], []
    for s in dict.fromkeys(list(shortlister.pool) + top):
        d = shortlister.frames.get(s)
        if d is None or s in live_syms: continue
        tech = committee.technical_daily(d, shortlister.setups.get(s), shortlister.mom_pct.get(s))
        tech["metrics"]["rank"] = ranks.get(s)
        memo = {"analysts": {"technical": tech, "fundamentals": committee.fundamentals(s, tech["metrics"]["close"]), "news": committee.news(s)}}
        m = tech["metrics"]; term = term_for(m["setups"], ranks.get(s), cfg["ideas"].get("entry_rank", 20))
        if term is None: log.append({"sym": s, "verdict": "REJECT", "score": 0, "why": ["no fresh setup and not in the momentum top ranks"]}); continue
        lv = levels(m["close"], m["atr"], term, cfg)
        rdate = res.get(s) if res.get(s, "") >= now.date().isoformat() else None
        # window = up to 3 sessions to enter + the hold + 2 sessions of buffer (the count is Mon-Fri and ignores NSE holidays)
        if term == "short" and rdate and cfg["ideas"].get("results_skip", True) and _sessions_until(now.date(), rdate) <= lv["days"] + 3 + 2:
            log.append({"sym": s, "verdict": "REJECT", "score": 0, "why": [f"company results on {rdate}, inside the {lv['days']}-day holding period (audit fix #12)"]}); continue
        memo["timemachine"] = None
        if term == "short":          # audit fix #10: no Time Machine on momentum ideas (the rank is the back-tested evidence)
            try: memo["timemachine"] = timemachine.run_daily(d, m["close"], lv["stop"], lv["target"], lv["days"], cfg)
            except Exception: memo["timemachine"] = None
        v = judge(memo, macro, cfg, live_syms, s, bl)
        log.append({"sym": s, "verdict": v["verdict"], "score": v["score"], "why": v["rejected_because"]})
        if v["verdict"] == "REJECT": continue
        pdate = d.index[-1].date().isoformat()          # the date of the close the levels are built from
        st = [x["stance"] for x in memo["analysts"].values()]
        agree = ("analysts agree (supportive)" if sum(x in ("bullish", "supportive", "positive flags") for x in st) >= 2
                 and not any(x in ("bearish", "concerning", "negative flags") for x in st) else "mixed views")
        deb = review.debate(memo, macro)
        ret1 = float(d["close"].iloc[-1] / d["close"].iloc[-2] - 1) if len(d) > 1 else None
        risk = review.risk_check({"r": lv["r"], "term": term, "entry_price": m["close"], "stop": lv["stop"], "ret1": ret1}, cfg, 0)   # book limits are the CEO's job
        tm = memo.get("timemachine")
        setnames = [x if isinstance(x, str) else x.get("setup") for x in (m["setups"] or [])]
        fill_rule = "next_open" if term == "medium" else "breakout" if "breakout20" in setnames and "bull_flag" not in setnames else "range"
        bo_level = tick(float(d["high"].iloc[-21:-1].max())) if fill_rule == "breakout" else None
        out.append({"id": f"{s}-{pdate}-{term[0]}", "sym": s, "name": (committee._funds().get("stocks", {}).get(s) or {}).get("name") or s,
                    "view": "Bullish view", "term": term, "entry_date": pdate, "entry_time": entry_time, "entry_price": round(m["close"], 2),
                    "entry_rank": ranks.get(s), "mom_pct": round(moms[s] * 100, 1) if s in moms else None,
                    "fill_rule": fill_rule, "breakout_level": bo_level,
                    "stop_rule": "close" if term == "short" and cfg["ideas"].get("short_stop_mode", "touch") == "close" else "touch", "results_date": rdate, "warnings": v.get("warnings", []),
                    **{k: lv[k] for k in ("entry_lo", "entry_hi", "stop", "target", "days", "r")},
                    # the back-tested rank rule reaches the CEO even when the untested judge points only reach WATCH
                    "status": ("awaiting decision" if v["verdict"] == "APPROVE" or (term == "medium" and v["verdict"] == "WATCH") else "watch") if risk["verdict"] == "PASS" else "watch",
                    "entry_state": review.entry_state({**lv, "entry_price": m["close"]}, m["close"]), "risk": risk,
                    "review": {"bull": deb["bull"], "bear": deb["bear"], "lean": deb["lean"], "economic": deb["economic"],
                               "time_machine": ({"sim": tm["sim"], "baseline": tm.get("baseline"), "edge_vs_baseline": tm.get("edge_vs_baseline"), "past": {k: tm["real"][k] for k in ("sessions", "target", "stop", "neither")}, "verdict": tm["verdict"], "proof": "simulation vs a no-edge baseline; not back-tested"} if tm else None),
                               "proof": {**committee_proof(), "judge": "weights not back-tested"}, "decision_needed": "approve, reject or leave on watch"},
                    "judge": v, "committee": {"agreement": agree, "macro": macro.get("stance"),
                                              "analysts": {k: {"stance": x["stance"], "points": x.get("points") or x.get("flags", [])} for k, x in memo["analysts"].items()},
                                              "news": memo["analysts"]["news"].get("headlines", [])}})
    er = cfg["ideas"].get("entry_rank", 20)
    # the back-tested rule first (top momentum ranks, best rank first), then untested setups by judge score
    key = lambda x: (x["sym"] not in was_waiting, not ((x.get("entry_rank") or 999) <= er), x.get("entry_rank") or 999 if (x.get("entry_rank") or 999) <= er else 0, -x["judge"]["score"])
    out.sort(key=key)
    approvals = [x for x in out if x["status"] == "awaiting decision"]   # every approval goes to the CEO, who applies the book, daily and sector limits
    watch = [x for x in out if x["status"] == "watch"][:15]
    pen["queue"] = approvals + watch
    pen["asof"] = now.date().isoformat()
    pen["log"] = ([e for e in pen.get("log", []) if e.get("date") != pen["asof"]] + [{"date": pen["asof"], "results": log}])[-30:]   # one entry per day
    _save(pending_path or PENDING, pen)
    return pen


# ---------------------------------------------------------------- decision-maker actions
def approve(idea_id: str, by: str, now: datetime, pending_path: Path | None = None, public_path: Path | None = None, frames: dict | None = None, force: bool = False, open_px: float | None = None) -> dict:
    pen, pub = pending(pending_path), public(public_path)
    q = next((x for x in pen["queue"] if x["id"] == idea_id), None)
    if not q: raise KeyError(f"no pending idea {idea_id}")
    if any(i["sym"] == q["sym"] for i in pub["live"]): raise ValueError(f"{q['sym']} is already live")
    if (q.get("risk") or {}).get("verdict") == "BLOCK" and not force: raise ValueError(f"risk check blocks {q['sym']}: " + "; ".join(q["risk"]["reasons"]) + " (use force to override)")
    idea = {**q, "status": "live", "posted": now.date().isoformat(), "approved_by": by, "approved_at": now.isoformat(timespec="minutes"),
            "nifty_entry": _nifty(pub), "n500_entry": _nifty(pub, "NIFTY 500"), "price_now": q["entry_price"], "rank_now": q.get("entry_rank"), "nifty_pct": 0.0,
            "results_date": q.get("results_date"), "surveillance": None, "days_held": 0, "exit_signal": None, "fill_rule": q.get("fill_rule") or "range", "filled": None}
    if open_px is not None:          # posted at the open: the entry IS the opening price, filled the same moment (levels were re-checked by post_at_open)
        o = round(float(open_px), 2)
        idea.update(signal_close=q["entry_price"], entry_price=o, entry_time=(q.get("open_check") or {}).get("label", "at the open"), price_now=o, filled={"price": o, "date": now.date().isoformat(), "time": now.strftime("%H:%M")})
    pen["queue"] = [x for x in pen["queue"] if x["id"] != idea_id]
    pub["live"].insert(0, idea)
    pub["recent_alerts"] = ([{"type": "entry", "sym": idea["sym"], "date": now.date().isoformat(), "time": now.strftime("%I:%M %p"), "price": idea["entry_price"],
                              "stop": idea["stop"], "target": idea["target"], "term": idea["term"], "rank": idea.get("entry_rank")}] + pub["recent_alerts"])[:60]
    if frames: update_bars(pub, frames)
    _publish(pub, now, public_path); _save(pending_path or PENDING, pen)
    return idea


def post_at_open(cfg: dict, now: datetime, opens: dict, pending_path: Path | None = None, public_path: Path | None = None, frames: dict | None = None,
                 prev_session: str | None = None, final: bool = False, by: str = "CEO", at_override: str | None = None) -> list[dict]:
    """Morning step of the post_at_open rule. Ideas the CEO chose on last evening's scan wait as 'scheduled for open'. At the open each is
    checked against TODAY'S OPENING PRICE: inside the entry range -> posted live, entry = the opening price, filled at once; outside the
    range (above or below), or reward-to-risk too low at that price, or the book full -> NOT posted. `opens` = {sym: opening price or None}.
    A symbol with no price yet stays scheduled until `final` (the cut-off time), then it is dropped. Returns one record per idea decided."""
    ic = cfg["ideas"]; today = now.date().isoformat(); pen = pending(pending_path); out = []
    at = at_override or ic.get("open_post_at", "09:15"); label = "at the open" if at == "09:15" else f"at {at}"      # at_override: a manual run that checks the LIVE price at this time
    for q in [x for x in pen["queue"] if x["status"] == "scheduled for open"]:
        pub = public(public_path); o = opens.get(q["sym"]); lo, hi = q.get("entry_lo") or q["entry_price"], q.get("entry_hi") or q["entry_price"]
        rec = {"date": today, "time": now.strftime("%H:%M"), "sym": q["sym"], "id": q["id"], "term": q["term"], "range": [lo, hi], "open": o}
        mom = q.get("term") == "medium"; why = None
        live = pub["live"]
        if prev_session and q.get("entry_date") != prev_session: why = f"stale: the levels are from the close of {q.get('entry_date')}, not the last session ({prev_session})"
        elif o is None:
            if not final: continue                                   # no opening price yet: try again on the next run
            why = f"the {at} price could not be read before the cut-off"
        elif not lo <= o <= hi: why = f"the price at {at} was {o:.2f}, {'above' if o > hi else 'below'} the entry range {lo:.2f}-{hi:.2f}"
        elif any(i["sym"] == q["sym"] for i in live): why = "already a live idea"
        elif mom and sum(1 for i in live if i.get("term") == "medium") >= ic.get("max_live", 20): why = "the momentum book is full"
        t = json.loads(json.dumps(q))
        if why is None: why = _fill_checks(t, o, cfg)                 # momentum: stop and target re-anchored to the opening price; setups: reward-to-risk at that price
        if why is None:
            t["open_check"] = {"open": round(o, 2), "at": at, "label": label, "range": [lo, hi], "result": f"inside the entry range at {at}: posted"}
            p2 = pending(pending_path); p2["queue"] = [t if x["id"] == q["id"] else x for x in p2["queue"]]; _save(pending_path or PENDING, p2)
            try:
                idea = approve(q["id"], by, now, pending_path, public_path, frames=frames, open_px=o)
                rec.update(decision="POSTED", memo=f"{q['sym']} was at {o:.2f} at {at}, inside the entry range {lo:.2f}-{hi:.2f}: posted, entry {o:.2f}, stop {idea['stop']}, target {idea['target']}.")
            except Exception as e: why = str(e)
        if why is not None:
            rec.update(decision="NOT POSTED", memo=f"{q['sym']} not posted: {why}.")
            p2 = pending(pending_path); p2["queue"] = [x for x in p2["queue"] if x["id"] != q["id"]]; _save(pending_path or PENDING, p2)
        out.append(rec)
    p = pending(pending_path); p["open_log"] = (p.get("open_log", []) + out)[-200:]; _save(pending_path or PENDING, p)
    return out


def reject(idea_id: str, pending_path: Path | None = None) -> None:
    pen = pending(pending_path); pen["queue"] = [x for x in pen["queue"] if x["id"] != idea_id]; _save(pending_path or PENDING, pen)


def close(key: str, by: str, now: datetime, reason: str | None = None, price: float | None = None, public_path: Path | None = None) -> dict:
    """key = idea id or symbol. With no reason/price, uses the flagged exit signal if there is one, else the last price seen."""
    pub = public(public_path)
    i = next((x for x in pub["live"] if x.get("id") == key or x["sym"] == key.upper()), None)
    if not i: raise KeyError(f"no live idea {key}")
    sig = i.get("exit_signal"); at_signal = price is None and sig
    reason = reason or (sig["reason"] if sig else "Closed by decision-maker")
    px = float(price if price is not None else sig["price"] if sig else i.get("price_now") or i["entry_price"])
    kind = KINDS.get(reason) or ("rank" if reason.startswith(("Momentum rank", "Left the")) else "not_entered" if reason.startswith("Not entered") else "manual")
    nf, n5 = _nifty(pub), _nifty(pub, "NIFTY 500")
    done = {k: v for k, v in i.items() if k not in ("exit_signal", "rank_exit")}
    done.update(status="closed", exit_date=sig["date"] if at_signal else now.date().isoformat(), exit_time="after close", exit_price=round(px, 2),
                exit_rank=i.get("rank_now"), reason=reason, close_kind=kind, closed_by=by, closed_at=now.isoformat(timespec="minutes"),
                ret_pct=None if (sig or {}).get("not_entered") else round((px / ((i.get("filled") or {}).get("price") or i["entry_price"]) - 1) * 100, 2), not_entered=bool((sig or {}).get("not_entered")),
                nifty_pct=round((nf / i["nifty_entry"] - 1) * 100, 2) if nf and i.get("nifty_entry") else None,
                n500_pct=round((n5 / i["n500_entry"] - 1) * 100, 2) if n5 and i.get("n500_entry") else None)
    pub["live"] = [x for x in pub["live"] if x is not i]; pub["closed"].insert(0, done)
    if kind == "stop": pub.setdefault("reentry_block", {})[i["sym"]] = {"since": done["exit_date"], "reset": False}
    pub["recent_alerts"] = ([{"type": "exit", "sym": i["sym"], "date": now.date().isoformat(), "time": now.strftime("%I:%M %p"), "price": done["exit_price"],
                              "rank": done["exit_rank"], "ret_pct": done["ret_pct"], "reason": reason}] + pub["recent_alerts"])[:60]
    _publish(pub, now, public_path)
    return done


# ---------------------------------------------------------------- monitor live ideas on daily bars
def monitor(cfg: dict, frames: dict, now: datetime, public_path: Path | None = None) -> list[dict]:
    """Updates bars and flags exit signals: stop, target, time up (price levels on daily bars) or faded momentum rank
    (set by pipeline/update_picks.py as `rank_exit`). Closes only if auto_close is on; otherwise the CEO / decision-maker closes.
    Entry rules (audit fixes #5, #6): momentum = next session's open (as back-tested); breakout = next open, cancelled if it opens
    back below the breakout level; pullback (flag) = first session inside the entry range within 3 sessions. Every fill is re-checked."""
    pub = public(public_path); signals = []; to_close = []

    def flag(i, sg):
        i["exit_signal"] = sg; signals.append({"id": i.get("id", i["sym"]), **sg})
        if cfg["ideas"].get("auto_close") and not sg.get("awaiting"): to_close.append(i["sym"])

    for i in list(pub["live"]):
        d = frames.get(i["sym"])
        if d is None or d.empty: continue
        posted = date.fromisoformat(i["entry_date"]); after = d[[x.date() > posted for x in d.index]]
        sig = None
        momentum = i.get("term") == "medium"           # back-tested rule: held until the rank fades (or the safety stop); no target or time exit
        rule = i.get("fill_rule")
        if i.get("filled") is None and not rule:                             # ideas posted before the entry rules: entered at the posted price
            i["filled"] = {"price": i["entry_price"], "date": (after.index[0].date().isoformat() if len(after) else i["entry_date"])}
        # ---- entry
        if rule in ("next_open", "breakout") and len(after) and (i.get("filled") is None or i["filled"].get("provisional")):
            ts, r0 = after.index[0], after.iloc[0]; o = float(r0["open"]) if r0["open"] == r0["open"] else float(r0["close"])
            if rule == "breakout" and i.get("breakout_level") and o < i["breakout_level"]:
                i["filled"] = None
                flag(i, {"reason": "Not entered: opened back below the breakout level", "price": i["entry_price"], "date": ts.date().isoformat(), "not_entered": True}); continue
            i["filled"] = {"price": round(o, 2), "date": ts.date().isoformat()}
            why = _fill_checks(i, o, cfg)
            if why: i["filled"] = None; flag(i, {"reason": why, "price": i["entry_price"], "date": ts.date().isoformat(), "not_entered": True}); continue
        elif rule == "range" and (i.get("filled") is None or i["filled"].get("provisional")) and len(after):
            i["filled"] = None
            lo_e, hi_e = i.get("entry_lo") or i["entry_price"], i.get("entry_hi") or i["entry_price"]; stop0 = i.get("stop")
            for k, (ts, r) in enumerate(after.iterrows()):
                o, l = float(r["open"]), float(r["low"])
                if stop0 and o <= stop0: break                                  # gapped below the stop before entry: never entered
                if l <= hi_e:
                    i["filled"] = {"price": round(min(o, hi_e) if o >= lo_e else o, 2), "date": ts.date().isoformat()}; break
                if k >= 2: break                                                # 3 sessions above the range: let it go
            if i.get("filled") is None and (len(after) >= 3 or (stop0 and float(after["open"].iloc[0]) <= stop0)):
                flag(i, {"reason": "Not entered: price never came into the entry range", "price": i["entry_price"], "date": after.index[-1].date().isoformat(), "not_entered": True}); continue
            if i.get("filled"):
                why = _fill_checks(i, i["filled"]["price"], cfg)
                if why: fd = i["filled"]["date"]; i["filled"] = None; flag(i, {"reason": why, "price": i["entry_price"], "date": fd, "not_entered": True}); continue
        if i.get("filled") is None:
            i["days_held"] = 0; i["entry_state"] = review.entry_state(i, float(d["close"].iloc[-1])); i["exit_signal"] = None
            if i.get("rank_exit"):                                              # the reason for the idea faded before it was entered
                flag(i, {"reason": i["rank_exit"]["reason"] + " (before entry)", "price": i["entry_price"], "date": d.index[-1].date().isoformat(), "not_entered": True})
            continue
        stop, tgt = i.get("stop"), i.get("target")
        fdate = date.fromisoformat(i["filled"]["date"]); held_bars = after[[x.date() >= fdate for x in after.index]]
        win = held_bars if momentum or not i.get("days") else held_bars.iloc[:i["days"]]
        awaiting = None; await_note = None
        by_close = not momentum and i.get("stop_rule") == "close"            # Nov 2026: bull flags exit at the next open after a CLOSE below the stop
        hard = _hard_stop(i, cfg) if by_close else None
        later = lambda ts: [(t2, r2) for t2, r2 in held_bars[[x > ts for x in held_bars.index]].iterrows() if not _locked(r2)]
        for n, (ts, r) in enumerate(win.iterrows()):
            o, h, l = float(r["open"]), float(r["high"]), float(r["low"])
            if n == 0 and rule: o = i["filled"]["price"]                                 # the entry bar starts at the fill
            if not momentum and tgt and o >= tgt: sig = ("Target hit", o, ts); break      # gap up through the target fills at the open
            if by_close:
                if hard and (o <= hard or l <= hard):                                       # emergency stop: sold on a touch
                    if _locked(r):
                        nxt = later(ts)
                        if nxt: sig = ("Stop-loss hit", float(nxt[0][1]["open"]), nxt[0][0], "emergency stop (locked at the lower circuit: exited at the next tradable price)")
                        else: awaiting, await_note = ts, "emergency stop reached at the lower circuit: exit at the next tradable price"
                        break
                    sig = ("Stop-loss hit", o if o <= hard else hard, ts, f"emergency stop {hard:,.2f} ({cfg['ideas'].get('short_hard_stop_r', 3.0):g}x the risk below the entry) reached"); break
                if tgt and h >= tgt: sig = ("Target hit", max(o, tgt), ts); break
                if stop and float(r["close"]) <= stop:                                          # closed below the stop: sell at the next open
                    nxt = later(ts)
                    if nxt: sig = ("Stop-loss hit", float(nxt[0][1]["open"]), nxt[0][0], f"closed below the stop on {ts.date().isoformat()}: exited at the next open")
                    else: awaiting, await_note = ts, "closed below the stop: exit at the next session's open"
                    break
                continue
            if stop and (o <= stop or l <= stop):
                if _locked(r):                                                 # audit fix #15: locked at the lower circuit, no sale possible
                    nxt = [(t2, r2) for t2, r2 in held_bars[[x > ts for x in held_bars.index]].iterrows() if not _locked(r2)]
                    if nxt: sig = ("Stop-loss hit", float(nxt[0][1]["open"]), nxt[0][0], "lower circuit: exited at the next tradable price")
                    else: awaiting = ts
                    break
                sig = ("Stop-loss hit", o if o <= stop else stop, ts); break          # gap through the stop fills at the open; else at the stop
            if not momentum and tgt and h >= tgt: sig = ("Target hit", max(o, tgt), ts); break
        held = len(held_bars); last = float(d["close"].iloc[-1]); i["days_held"] = held
        i["entry_state"] = review.entry_state(i, last)
        # audit fix #11: no "protect the gain" advice on momentum ideas (not part of the tested rule); a tip only on setups
        i["stop_advice"] = None if (sig or momentum or awaiting is not None) else review.one_r_advice({**i, "entry_price": i["filled"]["price"]}, float(win["high"].max()) if len(win) else None)
        if awaiting is not None:
            if await_note:
                flag(i, {"reason": "Stop-loss hit", "price": round(float(d.loc[awaiting, "close"]), 2), "date": awaiting.date().isoformat(), "awaiting": True, "note": await_note}); continue
            flag(i, {"reason": "Stop-loss hit (locked at the lower circuit: exit at the next tradable price)", "price": stop, "date": awaiting.date().isoformat(), "awaiting": True}); continue
        if not sig and not momentum and i.get("days") and held >= i["days"]:
            px, note = float(win["close"].iloc[-1]), None
            if cfg["ideas"].get("short_time_exit", "close") == "vwap":
                vw = VWAP_FN(i["sym"], win.index[-1].date())
                if vw: px, note = vw, "last session: sold through the day, counted at the day's average price (VWAP)"
                else: note = "last session: the day's average price (VWAP) was not available, so the close is used"
            sig = ("Time up", px, win.index[-1]) + ((note,) if note else ())
        # Nov 2026: tomorrow is the last session -> the site tells users to sell through that day (VWAP exit)
        i["last_day"] = bool(not sig and not momentum and i.get("days") and held == i["days"] - 1
                             and cfg["ideas"].get("short_time_exit", "close") == "vwap")
        if i["last_day"] and not i.get("last_day_alerted"):      # one phone alert per idea, the evening before its last session
            i["last_day_alerted"] = now.date().isoformat()
            pub["recent_alerts"] = ([{"type": "lastday", "sym": i["sym"], "date": now.date().isoformat(), "time": now.strftime("%I:%M %p"),
                                      "price": round(last, 2), "term": i.get("term")}] + pub["recent_alerts"])[:60]
        if not sig and i.get("rank_exit"): sig = (i["rank_exit"]["reason"], last, d.index[-1])
        i["exit_signal"] = None
        if momentum and not sig and tgt and len(win) and float(win["high"].max()) >= tgt and not i.get("target_reached"):
            i["target_reached"] = {"date": d.index[-1].date().isoformat(), "note": "first objective reached; the momentum rule holds until the rank fades"}
        if sig:
            sg = {"reason": sig[0], "price": round(sig[1], 2), "date": sig[2].date().isoformat()}
            if len(sig) > 3: sg["note"] = sig[3]
            flag(i, sg)
    update_bars(pub, frames)
    _publish(pub, now, public_path)
    for sym in to_close:                      # only if auto_close is on; otherwise the decision-maker closes
        close(sym, "auto (rule)", now, public_path=public_path)
    return signals


# ---------------------------------------------------------------- intraday: live prices every 5 minutes in market hours
def _at_circuit(p: float, pc: float | None) -> bool:
    """Price sitting on a typical NSE lower band (-2, -5, -10 or -20% from the previous close): a stop there may not be sellable."""
    if not pc: return False
    mv = p / pc - 1
    return any(abs(mv + b) <= 0.0015 for b in (0.02, 0.05, 0.10, 0.20))


def monitor_live(cfg: dict, prices: dict, now: datetime, public_path: Path | None = None, prev_close: dict | None = None) -> list[dict]:
    """Marks entries and flags stop / target hits on live prices during market hours (the 16:15 run still checks daily bars,
    time limits and momentum rank). A price checked every few minutes can be past the level: the exit uses the worse of the two."""
    pub = public(public_path); signals = []; hm = now.strftime("%H:%M")
    for i in pub["live"]:
        p = prices.get(i["sym"])
        if not p or i.get("exit_signal"): continue
        i["price_now"] = round(float(p), 2)
        rule = i.get("fill_rule")
        if i.get("filled") is None and rule:
            if now.date().isoformat() <= i["entry_date"]: continue           # posted after today's close: entry starts next session
            lo, hi, stop0 = i.get("entry_lo") or i["entry_price"], i.get("entry_hi") or i["entry_price"], i.get("stop")
            why = None
            if rule in ("next_open", "breakout"):                            # first price of the session; the 16:15 run replaces it with the open
                if rule == "breakout" and i.get("breakout_level") and p < i["breakout_level"]:
                    why = "Not entered: fell back below the breakout level"
                else:
                    i["filled"] = {"price": round(float(p), 2), "date": now.date().isoformat(), "time": hm, "provisional": True}
            elif lo <= p <= hi or (stop0 and stop0 < p < lo):
                i["filled"] = {"price": round(float(p), 2), "date": now.date().isoformat(), "time": hm}
            if i.get("filled") and not why:
                why = _fill_checks(i, float(p), cfg)
                if why: i["filled"] = None
            if why:
                i["exit_signal"] = {"reason": why, "price": i["entry_price"], "date": now.date().isoformat(), "time": hm, "not_entered": True}
                signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
            continue
        stop, tgt = i.get("stop"), i.get("target")
        if i.get("filled") is None: continue
        sig = None
        if i.get("stop_rule") == "close" and i.get("term") != "medium":
            # Nov 2026: no exit on a touch of the stop; only the emergency stop sells inside the day. The 16:15 run checks the close.
            hard = _hard_stop(i, cfg); i["below_stop"] = bool(stop and p <= stop)
            if hard and p <= hard:
                i["exit_signal"] = {"reason": "Stop-loss hit", "price": round(min(float(p), hard), 2), "date": now.date().isoformat(), "time": hm,
                                    "note": f"emergency stop {hard:,.2f} reached"}
                signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
            elif tgt and p >= tgt:
                i["exit_signal"] = {"reason": "Target hit", "price": round(float(tgt), 2), "date": now.date().isoformat(), "time": hm}
                signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
            i["entry_state"] = review.entry_state(i, float(p)); continue
        if stop and p <= stop and _at_circuit(float(p), (prev_close or {}).get(i["sym"])):
            # audit fix #15: possibly locked at the lower circuit; the 16:15 daily check decides the real exit (next tradable price)
            i["exit_signal"] = {"reason": "Stop-loss hit (price at the lower circuit band: exit confirmed after the close)", "price": round(float(p), 2),
                                "date": now.date().isoformat(), "time": hm, "awaiting": True}
            signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]}); continue
        if stop and p <= stop: sig = ("Stop-loss hit", min(float(p), stop))
        elif i.get("term") != "medium" and tgt and p >= tgt: sig = ("Target hit", float(tgt))
        if sig:
            i["exit_signal"] = {"reason": sig[0], "price": round(sig[1], 2), "date": now.date().isoformat(), "time": hm}
            signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
        i["entry_state"] = review.entry_state(i, float(p))
    _publish(pub, now, public_path)
    return signals
