"""Intraday segment: the exhaustion fade (short), back-tested Nov 2026 (pipeline/intraday_*_test.py).

Rule (fixed by the tests, see trading/TRENDLENS.md):
  universe   F&O stocks only (no circuit limits, never in ASM / GSM / trade-to-trade, always shortable intraday), price >= Rs 20,
             20-day average traded value >= Rs 5 Cr
  setup      at yesterday's close the stock had run up: > 25% above its 50-day average, or up > 20% in 5 sessions, or closed at a
             20-day high on 1.5x its 50-day average volume
  trigger    today it opens 1.5-8% above yesterday's close
  entry      short at the open of the 9:20 five-minute candle. The setups are PUBLISHED THE EVENING BEFORE as conditional ideas
             (trigger band, entry time, stop and target rules), because the edge fades fast after 9:20 (late-entry test: +0.34% a
             trade at 9:20, +0.16% at 9:30, +0.04% at 10:00 in 2022-2026) and scheduled runs can start late; the scorecard uses
             the 9:20 open, which a reader following the published rule gets
  exit       stop = entry + 1 x ATR(14), target = entry - 2 x ATR (2R), whatever is open is squared off at 15:15
  limits     at most 3 a day, biggest 5-day run-up first; skipped on results days and on serious news
Process (same as the other segments): prep after the close -> coded judge scores each candidate -> the CEO (coded rules) posts
or declines with a memo -> live prices every 5 minutes close it at the stop, target or 15:15. Results are after costs.
Back-test, F&O stocks, Indian intraday charges + 0.10% slippage each side: +0.61% a trade in 2014-2021 (367 trades), +0.34% in
2022-2026 (714 trades), profitable in 4 of 5 years in both periods."""
from __future__ import annotations

import csv, io, json, logging
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, time as dtime
from pathlib import Path

import requests

from . import charges, universe
from .judge import SEVERE

log = logging.getLogger("arena")
REPO = universe.REPO
WATCH = REPO / "trading" / "ideas" / "intraday_watch.json"
FNO = REPO / "nse" / "fno.csv"
UA = {"User-Agent": "Mozilla/5.0 (TrendLens)"}
DEFAULTS = {"enabled": True, "gap_min": 1.5, "gap_max": 8.0, "ext50": 0.25, "ret5": 0.20, "use_breakout": True,
            "stop_atr": 1.0, "target_r": 2.0, "entry_at": "09:20", "square_off": "15:15",
            "max_per_day": 3, "min_price": 20, "min_value_cr": 5, "max_stop_pct": 8.0, "slippage_pct": 0.10, "notional": 100000}
tick = lambda p: round(round(p / 0.05) * 0.05, 2)
hm = lambda s: dtime(int(s[:2]), int(s[3:5]))


def conf(cfg: dict) -> dict:
    return {**DEFAULTS, **(cfg.get("intraday") or {})}


def _load(p: Path, default):
    try: return json.loads(p.read_text())
    except Exception: return default


# ---------------------------------------------------------------- data
def fno_list() -> set[str]:
    """NSE F&O stocks (nse/fno.csv, refreshed by prep from NSE's fo_mktlots.csv; the saved copy is used if NSE is unreachable)."""
    try:
        r = requests.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", headers={**UA, "Accept": "*/*"}, timeout=30); r.raise_for_status()
        syms = sorted({row[1].strip() for row in csv.reader(io.StringIO(r.text)) if len(row) > 2 and row[1].strip() and row[1].strip() != "SYMBOL"})
        if len(syms) > 100:
            FNO.parent.mkdir(parents=True, exist_ok=True); FNO.write_text("SYMBOL\n" + "\n".join(syms) + "\n")
    except Exception as e:
        log.warning("NSE F&O list not refreshed: %s", e)
    try: return {x.strip() for x in FNO.read_text().splitlines()[1:] if x.strip()}
    except Exception: return set()


def bars_5m(sym: str) -> list[dict]:
    """Today's five-minute candles from Yahoo, IST, from 09:15 (the candle in progress included)."""
    r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(sym)}.NS", params={"interval": "5m", "range": "1d"}, headers=UA, timeout=20)
    r.raise_for_status(); res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]; out = []
    for k, ts in enumerate(res.get("timestamp") or []):
        t = datetime.utcfromtimestamp(ts + 19800)
        o, h, l, c = q["open"][k], q["high"][k], q["low"][k], q["close"][k]
        if None in (o, h, l, c) or t.time() < dtime(9, 15): continue
        out.append({"t": t, "o": float(o), "h": float(h), "l": float(l), "c": float(c)})
    return out


def many_5m(syms) -> dict:
    def one(s):
        try: return s, bars_5m(s)
        except Exception as e: log.warning("5-minute bars for %s failed: %s", s, e); return s, []
    with ThreadPoolExecutor(8) as ex: return dict(ex.map(one, syms))


# ---------------------------------------------------------------- after the close: who could set up tomorrow
def indicators(d) -> dict | None:
    """Facts at the last close, computed exactly as in the back-test (pipeline/intraday_system_test.daily)."""
    import pandas as pd
    if d is None or len(d) < 60: return None
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    atr = float(tr.rolling(14).mean().iloc[-1]); sma50 = float(c.rolling(50).mean().iloc[-1]); pc = float(c.iloc[-1])
    ret5 = pc / float(c.iloc[-6]) - 1; ext50 = pc / sma50 - 1
    bo20 = bool(pc > float(h.iloc[-21:-1].max()) and float(v.iloc[-1]) > 1.5 * float(v.iloc[-51:-1].mean()))
    value_cr = float((c * v).iloc[-20:].mean()) / 1e7
    if not (atr == atr and atr > 0): return None
    return {"close": round(pc, 2), "date": d.index[-1].date().isoformat(), "atr": round(atr, 4), "sma50": round(sma50, 2), "ret5": round(ret5, 4),
            "ext50": round(ext50, 4), "bo20": bo20, "value_cr": round(value_cr, 1)}


def qualifies(f: dict, c: dict) -> list[str]:
    why = []
    if f["ext50"] > c["ext50"]: why.append(f"{f['ext50'] * 100:.0f}% above its 50-day average")
    if f["ret5"] > c["ret5"]: why.append(f"up {f['ret5'] * 100:.0f}% in 5 sessions")
    if c["use_breakout"] and f["bo20"]: why.append("closed at a 20-day high on heavy volume")
    return why


def prep(cfg: dict, frames: dict, now: datetime, results: dict | None = None, blocked: dict | None = None, news_fn=None, next_session: str | None = None) -> dict:
    """Run after the close. `frames` = daily bars for the F&O stocks. Writes trading/ideas/intraday_watch.json: the conditional ideas
    for the next session, each with its trigger band, already checked by the judge (results, surveillance, serious news)."""
    c = conf(cfg); out = {}
    results = results if results is not None else universe.results(); blocked = blocked if blocked is not None else universe.blocked()
    nxt = next_session or _next_session(now)
    for s, d in frames.items():
        f = indicators(d)
        if not f or f["close"] < c["min_price"] or f["value_cr"] < c["min_value_cr"]: continue
        why = qualifies(f, c)
        if not why: continue
        f = {**f, "why": why, "trigger_lo": tick(f["close"] * (1 + c["gap_min"] / 100)), "trigger_hi": tick(f["close"] * (1 + c["gap_max"] / 100)),
             "stop_dist": round(c["stop_atr"] * f["atr"], 2), "target_dist": round(c["target_r"] * c["stop_atr"] * f["atr"], 2)}
        f["judge"] = judge_watch(s, f, results, blocked, news_fn(s) if news_fn else None, nxt)
        out[s] = f
    w = {"built": now.isoformat(timespec="minutes"), "last_close": max((x["date"] for x in out.values()), default=None), "for": nxt, "stocks": out}
    WATCH.parent.mkdir(parents=True, exist_ok=True); WATCH.write_text(json.dumps(w, indent=1))
    return w


def _next_session(now: datetime) -> str:
    import numpy as np
    return str(np.busday_offset(now.date(), 1, roll="forward"))


# ---------------------------------------------------------------- judge and CEO (coded rules, no AI)
def judge_watch(sym: str, f: dict, results: dict, blocked: dict, news: dict | None, session: str) -> dict:
    """Evening judge. Points are information (the rule itself is the back-tested evidence); hard rejects are risk controls."""
    pts, rules, hard = 0, [], []
    def add(n, why):
        nonlocal pts; pts += n; rules.append({"points": n, "rule": why})
    if f["ext50"] > 0.25: add(2, f"{f['ext50'] * 100:.0f}% above the 50-day average (strongest back-tested condition)")
    if f["ret5"] > 0.20: add(2, f"up {f['ret5'] * 100:.0f}% in 5 sessions")
    if f.get("bo20"): add(1, "20-day high on heavy volume")
    rd = results.get(sym)
    if rd and rd <= session: hard.append(f"company results on {rd}")
    if sym in blocked: hard.append(f"{blocked[sym]} (NSE surveillance)")
    flags = [x for x in ((news or {}).get("neg_flags") or []) if x in SEVERE]
    if flags: hard.append("serious headline flags: " + ", ".join(flags))
    if f["stop_dist"] / f["close"] * 100 > 8.0: hard.append(f"stop {f['stop_dist'] / f['close'] * 100:.1f}% away: too volatile for an intraday idea")
    return {"score": pts, "rules": rules, "rejected_because": hard, "verdict": "REJECT" if hard else "APPROVE", "by": "coded intraday judge"}


def judge(cand: dict) -> dict:
    """Morning: the evening verdict plus the first candle (information only)."""
    j = json.loads(json.dumps(cand["facts"]["judge"]))
    if cand["c1_red"]: j["score"] += 1; j["rules"].append({"points": 1, "rule": "first five-minute candle closed red"})
    return j


def ceo(cands: list[dict], room: int) -> list[dict]:
    """Posts the approved candidates with the biggest 5-day run-up first (the back-tested order), up to today's free slots."""
    ok = sorted([x for x in cands if x["judge"]["verdict"] == "APPROVE"], key=lambda x: -x["facts"]["ret5"])
    out = []
    for k, x in enumerate(ok):
        post = k < room
        x["ceo"] = {"decision": "POST" if post else "DECLINE", "conviction": "high" if x["judge"]["score"] >= 4 else "normal",
                    "memo": (f"Exhaustion fade: {'; '.join(x['facts']['why'])}; opened {x['gap_pct']:+.1f}% above yesterday's close. "
                             f"Short at {x['entry_price']:.2f}, stop {x['stop']:.2f} (1 ATR), target {x['target']:.2f} (2R), out by 15:15.")
                    if post else f"Daily limit reached ({room} intraday ideas a day); ranked lower on the 5-day run-up."}
        out.append(x)
    for x in cands:
        if x["judge"]["verdict"] != "APPROVE":
            x["ceo"] = {"decision": "DECLINE", "conviction": "-", "memo": "; ".join(x["judge"]["rejected_because"])}; out.append(x)
    return out


# ---------------------------------------------------------------- store (picks.json -> "intraday")
def book(pub: dict) -> dict:
    b = pub.setdefault("intraday", {})
    for k, v in (("live", []), ("closed", []), ("days", {}), ("stats", {})): b.setdefault(k, v)
    b["rules"] = {"name": "Exhaustion fade (intraday short)", "universe": "F&O stocks", "gap": "opens 1.5-8% above yesterday's close",
                  "setup": "> 25% above the 50-day average, or up > 20% in 5 sessions, or a 20-day high on heavy volume",
                  "entry": "published the evening before; short at the open of the 9:20 candle if it opened inside the trigger band", "stop": "entry + 1 x daily ATR", "target": "entry - 2 x ATR (2R)",
                  "exit": "stop, target or 15:15", "max_per_day": 3,
                  "backtest": {"2014-2021": {"trades": 367, "net_pct": 0.61, "years_positive": "4/5"},
                               "2022-2026": {"trades": 714, "net_pct": 0.34, "years_positive": "4/5"},
                               "costs": "Indian intraday charges + 0.10% slippage each side"}}
    return b


def net_pct(entry: float, exit_: float, c: dict) -> tuple[float, float]:
    """Short result in % before and after costs (Rs 1 lakh notional, intraday charges both sides, slippage both sides)."""
    gross = (entry / exit_ - 1) * 100
    q = max(1, int(c["notional"] / entry))
    ch = charges.compute("SELL", q, entry, "intraday").total + charges.compute("BUY", q, exit_, "intraday").total
    return round(gross, 3), round(gross - ch / (q * entry) * 100 - 2 * c["slippage_pct"], 3)


def _stats(b: dict) -> None:
    cl = [x for x in b["closed"] if x.get("net_pct") is not None]
    n = len(cl); wins = sum(1 for x in cl if x["net_pct"] > 0)
    b["stats"] = {"trades": n, "wins": wins, "win_pct": round(wins / n * 100, 1) if n else None,
                  "avg_net_pct": round(sum(x["net_pct"] for x in cl) / n, 3) if n else None,
                  "total_net_pct": round(sum(x["net_pct"] for x in cl), 2) if n else None, "since": cl[-1]["date"] if n else None}


# ---------------------------------------------------------------- 9:20 onwards: scan and post
def watch_public(pub: dict) -> None:
    """Copies the conditional ideas for the next session into picks.json (what the site and the evening alert show)."""
    w = _load(WATCH, {}); b = book(pub)
    b["watch"] = {"for": w.get("for"), "built": w.get("built"), "stocks": [
        {"sym": s, "prev_close": f["close"], "trigger_lo": f["trigger_lo"], "trigger_hi": f["trigger_hi"], "stop_dist": f["stop_dist"],
         "target_dist": f["target_dist"], "why": f["why"], "ret5": f["ret5"], "score": f["judge"]["score"],
         "verdict": f["judge"]["verdict"], "rejected_because": f["judge"]["rejected_because"]}
        for s, f in sorted((w.get("stocks") or {}).items(), key=lambda x: -x[1]["ret5"])]}


def _prev_session(today: str) -> str | None:
    """The last trading day before `today`, from web/history.json (written after every close)."""
    try: return max(d for d in json.loads((REPO / "web" / "history.json").read_text())["dates"] if d < today)
    except Exception: return None


def scan(cfg: dict, pub: dict, now: datetime, bars: dict | None = None, prev_session: str | None = None) -> list[dict]:
    """Once a day, from 9:20 (it may run late): which published conditional ideas triggered, the CEO keeps the daily limit, posted
    with the 9:20 candle's open as the entry (the price a reader following the published rule got)."""
    c = conf(cfg); b = book(pub); today = now.date().isoformat()
    day = b["days"].setdefault(today, {"scanned": None, "candidates": []})
    if day["scanned"] or now.time() < hm(c["entry_at"]) or now.time() >= hm(c["square_off"]): return []
    w = _load(WATCH, {}); st = w.get("stocks") or {}
    prev = prev_session or _prev_session(today)
    if not st or not w.get("last_close") or w["last_close"] >= today or (prev and w["last_close"] != prev):   # built from the last close before today
        day["scanned"] = now.isoformat(timespec="minutes"); day["note"] = "no conditional ideas were published for today"; return []
    bars = bars if bars is not None else many_5m(list(st))
    ea = hm(c["entry_at"]); cands = []
    for s, f in st.items():
        bb = [x for x in (bars.get(s) or []) if x["t"].date() == now.date()]
        e = next((k for k, x in enumerate(bb) if x["t"].time() == ea), None)
        if not bb or bb[0]["t"].time() != dtime(9, 15) or e is None: continue     # need the 9:15 candle and the 9:20 candle's open
        o0 = bb[0]["o"]
        if not f["trigger_lo"] <= o0 <= f["trigger_hi"]: continue
        entry = bb[e]["o"]
        cand = {"sym": s, "facts": f, "gap_pct": round((o0 / f["close"] - 1) * 100, 2), "c1_red": bb[0]["c"] < o0, "entry_price": round(entry, 2),
                "stop": tick(entry + f["stop_dist"]), "target": tick(entry - f["target_dist"])}
        cand["judge"] = judge(cand)
        cands.append(cand)
    room = max(0, c["max_per_day"] - sum(1 for x in b["live"] + b["closed"] if x.get("date") == today))
    decided = ceo(cands, room); posted = []
    for x in decided:
        if x["ceo"]["decision"] != "POST": continue
        idea = {"id": f"{x['sym']}-{today}-i", "sym": x["sym"], "term": "intraday", "side": "short", "view": "Bearish view (intraday short)",
                "date": today, "time": c["entry_at"], "posted_at": now.strftime("%H:%M"), "published": w.get("built"),
                "entry_price": x["entry_price"], "stop": x["stop"], "target": x["target"],
                "atr": x["facts"]["atr"], "prev_close": x["facts"]["close"], "gap_pct": x["gap_pct"], "why": x["facts"]["why"],
                "judge": x["judge"], "ceo": x["ceo"], "status": "live", "price_now": x["entry_price"], "pnl_now_pct": 0.0}
        b["live"].insert(0, idea); posted.append(idea)
        pub.setdefault("recent_alerts", []).insert(0, {"type": "entry", "sym": x["sym"], "date": today, "time": now.strftime("%I:%M %p"), "price": x["entry_price"],
                                                       "stop": x["stop"], "target": x["target"], "term": "intraday", "side": "short"})
    day["scanned"] = now.isoformat(timespec="minutes")
    day["candidates"] = [{"sym": x["sym"], "gap_pct": x["gap_pct"], "score": x["judge"]["score"], "decision": x["ceo"]["decision"], "memo": x["ceo"]["memo"]} for x in decided]
    b["days"] = dict(sorted(b["days"].items())[-30:])
    pub["recent_alerts"] = pub.get("recent_alerts", [])[:60]
    return posted


# ---------------------------------------------------------------- every 5 minutes: stop, target, 15:15
def walk(idea: dict, bb: list[dict], square: dtime) -> tuple | None:
    """Exit from today's five-minute candles after the entry candle, as in the back-test: stop first (a candle that touches both
    counts as a loss), a gap through the stop fills at the open, the target at the target, and the 15:15 candle's open squares off."""
    et = datetime.combine(bb[0]["t"].date(), hm(idea["time"])) if bb else None
    for x in bb:
        if x["t"] < et: continue                       # entry at the open of the 9:20 candle: that candle counts, as in the back-test
        if x["t"].time() >= square: return ("Squared off at 15:15", x["o"], x["t"])
        if x["o"] >= idea["stop"]: return ("Stop-loss hit", x["o"], x["t"])
        if x["h"] >= idea["stop"]: return ("Stop-loss hit", idea["stop"], x["t"])
        if x["l"] <= idea["target"]: return ("Target hit", min(x["o"], idea["target"]), x["t"])
    return None


def monitor(cfg: dict, pub: dict, now: datetime, bars: dict | None = None) -> list[dict]:
    c = conf(cfg); b = book(pub); done = []
    if not b["live"]: _stats(b); return done
    bars = bars if bars is not None else many_5m([i["sym"] for i in b["live"]])
    square = hm(c["square_off"])
    for i in list(b["live"]):
        bb = bars.get(i["sym"]) or []
        if i["date"] < now.date().isoformat(): bb = []                        # yesterday's idea (a run was missed): today's candles do not apply
        if bb: i["price_now"] = round(bb[-1]["c"], 2); i["pnl_now_pct"] = net_pct(i["entry_price"], bb[-1]["c"], c)[0]
        sig = walk(i, bb, square) if bb else None
        if not sig and (now.time() >= square or i["date"] < now.date().isoformat()):
            px = bb[-1]["c"] if bb else i.get("price_now") or i["entry_price"]      # no 15:15 candle seen: last price
            sig = ("Squared off at 15:15", px, None)
        if not sig: continue
        g, n = net_pct(i["entry_price"], sig[1], c)
        closed = {**i, "status": "closed", "exit_price": round(sig[1], 2), "exit_time": sig[2].strftime("%H:%M") if sig[2] else now.strftime("%H:%M"),
                  "reason": sig[0], "ret_pct": g, "net_pct": n, "closed_by": "CEO (coded rule)"}
        b["live"] = [x for x in b["live"] if x is not i]; b["closed"].insert(0, closed); done.append(closed)
        pub.setdefault("recent_alerts", []).insert(0, {"type": "exit", "sym": i["sym"], "date": now.date().isoformat(), "time": now.strftime("%I:%M %p"),
                                                       "price": closed["exit_price"], "ret_pct": n, "reason": sig[0], "term": "intraday", "side": "short"})
    b["closed"] = b["closed"][:300]; pub["recent_alerts"] = pub.get("recent_alerts", [])[:60]
    _stats(b)
    return done
