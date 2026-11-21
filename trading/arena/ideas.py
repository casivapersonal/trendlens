"""Ideas pipeline, one store: web/picks.json (what the website's Trade Ideas tab reads).
scan -> committee -> coded judge -> PENDING (trading/ideas/pending.json) -> decision-maker approves -> LIVE idea
-> monitor flags exit signals (stop, target, time, faded momentum rank) -> decision-maker closes -> CLOSED idea.
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


def _nifty(pub: dict) -> float | None:
    b = pub.get("bars", {}).get("NIFTY 50")
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
    return {"entry_lo": tick(close - ic["entry_atr"] * atr), "entry_hi": tick(close + ic["entry_atr"] * atr),
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
    macro = committee.macro(); bl = universe.blocked()
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
        out.append({"id": f"{s}-{pdate}-{term[0]}", "sym": s, "name": (committee._funds().get("stocks", {}).get(s) or {}).get("name") or s,
                    "view": "Bullish view", "term": term, "entry_date": pdate, "entry_time": entry_time, "entry_price": round(m["close"], 2),
                    "entry_rank": ranks.get(s), "mom_pct": round(moms[s] * 100, 1) if s in moms else None,
                    **{k: lv[k] for k in ("entry_lo", "entry_hi", "stop", "target", "days", "r")},
                    # the back-tested rank rule reaches the CEO even when the untested judge points only reach WATCH
                    "status": ("awaiting decision" if v["verdict"] == "APPROVE" or (term == "medium" and v["verdict"] == "WATCH") else "watch") if risk["verdict"] == "PASS" else "watch",
                    "entry_state": review.entry_state({**lv, "entry_price": m["close"]}, m["close"]), "risk": risk,
                    "review": {"bull": deb["bull"], "bear": deb["bear"], "lean": deb["lean"], "economic": deb["economic"],
                               "time_machine": ({"sim": tm["sim"], "past": {k: tm["real"][k] for k in ("sessions", "target", "stop", "neither")}, "verdict": tm["verdict"], "proof": "simulation; not back-tested"} if tm else None),
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
def approve(idea_id: str, by: str, now: datetime, pending_path: Path | None = None, public_path: Path | None = None, frames: dict | None = None, force: bool = False) -> dict:
    pen, pub = pending(pending_path), public(public_path)
    q = next((x for x in pen["queue"] if x["id"] == idea_id), None)
    if not q: raise KeyError(f"no pending idea {idea_id}")
    if any(i["sym"] == q["sym"] for i in pub["live"]): raise ValueError(f"{q['sym']} is already live")
    if (q.get("risk") or {}).get("verdict") == "BLOCK" and not force: raise ValueError(f"risk check blocks {q['sym']}: " + "; ".join(q["risk"]["reasons"]) + " (use force to override)")
    idea = {**q, "status": "live", "posted": now.date().isoformat(), "approved_by": by, "approved_at": now.isoformat(timespec="minutes"),
            "nifty_entry": _nifty(pub), "price_now": q["entry_price"], "rank_now": q.get("entry_rank"), "nifty_pct": 0.0,
            "results_date": None, "surveillance": None, "days_held": 0, "exit_signal": None, "fill_rule": "range", "filled": None}
    pen["queue"] = [x for x in pen["queue"] if x["id"] != idea_id]
    pub["live"].insert(0, idea)
    pub["recent_alerts"] = ([{"type": "entry", "sym": idea["sym"], "date": now.date().isoformat(), "time": now.strftime("%I:%M %p"), "price": idea["entry_price"],
                              "stop": idea["stop"], "target": idea["target"], "term": idea["term"], "rank": idea.get("entry_rank")}] + pub["recent_alerts"])[:60]
    if frames: update_bars(pub, frames)
    _publish(pub, now, public_path); _save(pending_path or PENDING, pen)
    return idea


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
    nf = _nifty(pub)
    done = {k: v for k, v in i.items() if k not in ("exit_signal", "rank_exit")}
    done.update(status="closed", exit_date=sig["date"] if at_signal else now.date().isoformat(), exit_time="after close", exit_price=round(px, 2),
                exit_rank=i.get("rank_now"), reason=reason, close_kind=kind, closed_by=by, closed_at=now.isoformat(timespec="minutes"),
                ret_pct=None if (sig or {}).get("not_entered") else round((px / ((i.get("filled") or {}).get("price") or i["entry_price"]) - 1) * 100, 2), not_entered=bool((sig or {}).get("not_entered")),
                nifty_pct=round((nf / i["nifty_entry"] - 1) * 100, 2) if nf and i.get("nifty_entry") else None)
    pub["live"] = [x for x in pub["live"] if x is not i]; pub["closed"].insert(0, done)
    pub["recent_alerts"] = ([{"type": "exit", "sym": i["sym"], "date": now.date().isoformat(), "time": now.strftime("%I:%M %p"), "price": done["exit_price"],
                              "rank": done["exit_rank"], "ret_pct": done["ret_pct"], "reason": reason}] + pub["recent_alerts"])[:60]
    _publish(pub, now, public_path)
    return done


# ---------------------------------------------------------------- monitor live ideas on daily bars
def monitor(cfg: dict, frames: dict, now: datetime, public_path: Path | None = None) -> list[dict]:
    """Updates bars and flags exit signals: stop, target, time up (price levels on daily bars) or faded momentum rank
    (set by pipeline/update_picks.py as `rank_exit`). Closes only if auto_close is on; otherwise the decision-maker closes."""
    pub = public(public_path); signals = []; to_close = []
    for i in list(pub["live"]):
        d = frames.get(i["sym"])
        if d is None or d.empty: continue
        posted = date.fromisoformat(i["entry_date"]); after = d[[x.date() > posted for x in d.index]]
        stop, tgt, sig = i.get("stop"), i.get("target"), None
        momentum = i.get("term") == "medium"           # back-tested rule: held until the rank fades (or the stop); no target or time exit
        # entry: the idea is entered on the first session that trades inside the range (a gap above it is not chased)
        if i.get("filled") is None and not i.get("fill_rule"):              # ideas posted before the entry-range rule: entered at the posted price
            i["filled"] = {"price": i["entry_price"], "date": (after.index[0].date().isoformat() if len(after) else i["entry_date"])}
        if i.get("filled") is None and len(after):
            lo_e, hi_e = i.get("entry_lo") or i["entry_price"], i.get("entry_hi") or i["entry_price"]
            for k, (ts, r) in enumerate(after.iterrows()):
                o, h, l = float(r["open"]), float(r["high"]), float(r["low"])
                if stop and o <= stop: break                                    # gapped below the stop before entry: never entered
                if l <= hi_e:
                    i["filled"] = {"price": round(min(o, hi_e) if o >= lo_e else o, 2), "date": ts.date().isoformat()}; break
                if k >= 2: break                                                # 3 sessions above the range: let it go
            if i.get("filled") is None and (len(after) >= 3 or (stop and float(after["open"].iloc[0]) <= stop)):
                i["exit_signal"] = {"reason": "Not entered: price never came into the entry range", "price": i["entry_price"], "date": after.index[-1].date().isoformat(), "not_entered": True}
                signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
                if cfg["ideas"].get("auto_close"): to_close.append(i["sym"])
                continue
        if i.get("filled") is None:
            i["days_held"] = 0; i["entry_state"] = review.entry_state(i, float(d["close"].iloc[-1])); i["exit_signal"] = None
            if i.get("rank_exit"):                                              # the reason for the idea faded before it was entered
                i["exit_signal"] = {"reason": i["rank_exit"]["reason"] + " (before entry)", "price": i["entry_price"], "date": d.index[-1].date().isoformat(), "not_entered": True}
                signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
                if cfg["ideas"].get("auto_close"): to_close.append(i["sym"])
            continue
        fdate = date.fromisoformat(i["filled"]["date"]); held_bars = after[[x.date() >= fdate for x in after.index]]
        win = held_bars if momentum or not i.get("days") else held_bars.iloc[:i["days"]]
        for n, (ts, r) in enumerate(win.iterrows()):
            o, h, l = float(r["open"]), float(r["high"]), float(r["low"])
            if n == 0 and i.get("fill_rule"): o = i["filled"]["price"]                                 # the entry bar starts at the fill
            if not momentum and tgt and o >= tgt: sig = ("Target hit", o, ts); break      # gap up through the target fills at the open
            if stop and o <= stop: sig = ("Stop-loss hit", o, ts); break               # gap through the stop fills at the open
            if stop and l <= stop: sig = ("Stop-loss hit", stop, ts); break             # stop is checked first inside a bar
            if not momentum and tgt and h >= tgt: sig = ("Target hit", max(o, tgt), ts); break
        held = len(held_bars); last = float(d["close"].iloc[-1]); i["days_held"] = held
        i["entry_state"] = review.entry_state(i, last)
        i["stop_advice"] = None if sig else review.one_r_advice({**i, "entry_price": i["filled"]["price"]}, float(win["high"].max()) if len(win) else None)
        if not sig and not momentum and i.get("days") and held >= i["days"]: sig = ("Time up", float(win["close"].iloc[-1]), win.index[-1])
        if not sig and i.get("rank_exit"): sig = (i["rank_exit"]["reason"], last, d.index[-1])
        i["exit_signal"] = {"reason": sig[0], "price": round(sig[1], 2), "date": sig[2].date().isoformat()} if sig else None
        if momentum and not sig and tgt and len(win) and float(win["high"].max()) >= tgt and not i.get("target_reached"):
            i["target_reached"] = {"date": d.index[-1].date().isoformat(), "note": "first objective reached; the momentum rule holds until the rank fades"}
        if sig:
            signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
            if cfg["ideas"].get("auto_close"): to_close.append(i["sym"])
    update_bars(pub, frames)
    _publish(pub, now, public_path)
    for sym in to_close:                      # only if auto_close is on; otherwise the decision-maker closes
        close(sym, "auto (rule)", now, public_path=public_path)
    return signals


# ---------------------------------------------------------------- intraday: live prices every 5 minutes in market hours
def monitor_live(cfg: dict, prices: dict, now: datetime, public_path: Path | None = None) -> list[dict]:
    """Marks entries and flags stop / target hits on live prices during market hours (the 16:15 run still checks daily bars,
    time limits and momentum rank). A price checked every few minutes can be past the level: the exit uses the worse of the two."""
    pub = public(public_path); signals = []; hm = now.strftime("%H:%M")
    for i in pub["live"]:
        p = prices.get(i["sym"])
        if not p or i.get("exit_signal"): continue
        i["price_now"] = round(float(p), 2)
        stop, tgt = i.get("stop"), i.get("target")
        if i.get("filled") is None and i.get("fill_rule"):
            lo, hi = i.get("entry_lo") or i["entry_price"], i.get("entry_hi") or i["entry_price"]
            if lo <= p <= hi or (stop and stop < p < lo):
                i["filled"] = {"price": round(float(p), 2), "date": now.date().isoformat(), "time": hm}
            continue
        if i.get("filled") is None: continue
        sig = None
        if stop and p <= stop: sig = ("Stop-loss hit", min(float(p), stop))
        elif i.get("term") != "medium" and tgt and p >= tgt: sig = ("Target hit", float(tgt))
        if sig:
            i["exit_signal"] = {"reason": sig[0], "price": round(sig[1], 2), "date": now.date().isoformat(), "time": hm}
            signals.append({"id": i.get("id", i["sym"]), **i["exit_signal"]})
        i["entry_state"] = review.entry_state(i, float(p))
    _publish(pub, now, public_path)
    return signals
