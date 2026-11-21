"""Daily data job (pre-market and after close): refresh 2 years of prices, the NIFTY 500 universe (pipeline/nifty500.txt),
history.json for the live screener, and chart bars in picks.json. After the close it also
refreshes prices and ranks on live ideas and flags momentum ideas whose rank has faded. It never opens or
closes an idea: new ideas come from `python -m arena ideas scan`, and a decision-maker approves and closes them.
Usage: python pipeline/update_picks.py web/picks.json web/history.json
"""
import json, os, sys, datetime as dt
import pandas as pd, numpy as np, yfinance as yf
from core import fill_last_close, MIN_TURNOVER_CR, MIN_PRICE, UNIVERSE, STATS, ENTRY_RANK, EXIT_RANK, ist_now, market_open, ranks_now, refresh_live, rank_exit_flags, load_nse

PICKS, HIST = sys.argv[1], sys.argv[2]
HERE = os.path.dirname(os.path.abspath(__file__))
state = json.load(open(PICKS)) if os.path.exists(PICKS) else {}
state.setdefault("live", []); state.setdefault("closed", [])
syms = list(dict.fromkeys(open(os.path.join(HERE, "syms.txt")).read().split()))
frames, raw = {}, {}
tick = [s + ".NS" for s in syms] + ["^NSEI", "^NSEBANK"]
for k in range(0, len(tick), 80):
    d = yf.download(tick[k:k + 80], period="2y", progress=False, auto_adjust=True, group_by="ticker", threads=True)
    for t in tick[k:k + 80]:
        try: raw[t] = d[t].dropna(how="all").copy()
        except Exception: continue
    print(f"downloaded {k + 80}/{len(tick)}", flush=True)
print("filled empty last closes from Yahoo chart meta:", fill_last_close(raw))
for t, x in raw.items():
    x = x.dropna(subset=["Close"])
    if not t.startswith("^"): x = x[(x["Volume"] > 0) & (x["Close"] > 0)]
    if len(x) >= 200: frames[t] = x
D = {t[:-3]: x for t, x in frames.items() if t.endswith(".NS")}
C = pd.DataFrame({s: x["Close"] for s, x in D.items()}).sort_index()
cov = C.notna().sum(axis=1); C = C[cov >= 0.5 * cov.max()]     # 0.9 dropped whole days when Yahoo served a partial close
TV = pd.DataFrame({s: x["Close"] * x["Volume"] for s, x in D.items()}).reindex(C.index)
C = C.ffill(limit=5).where(C.notna().cumsum() > 0)
liq = TV.rolling(126, min_periods=100).median().iloc[-1]
elig = (C.notna().sum() >= 260) & C.iloc[-1].notna()
n500 = os.path.join(HERE, "nifty500.txt")
members = set(open(n500).read().split()) if os.path.exists(n500) else set()
universe = [s for s in liq[elig].dropna().sort_values(ascending=False).index if s in members] if members else []
if len(universe) < 200: universe = list(liq[elig].dropna().nlargest(UNIVERSE).index)   # fallback: most-traded stocks
now = ist_now(); today = now.date().isoformat()
last = C.index[-1].date().isoformat()
if last == today and market_open(now):          # drop today's partial session: history is closed days only
    C = C.iloc[:-1]; last = C.index[-1].date().isoformat()
tail = C.iloc[-260:]
liq20 = TV.rolling(20, min_periods=15).median().iloc[-1]; px_last = C.iloc[-1]
live_syms = {e["sym"] for e in state["live"]}
rank_universe = [s for s in universe if (liq20.get(s, 0) or 0) >= MIN_TURNOVER_CR * 1e7 and (px_last.get(s, 0) or 0) >= MIN_PRICE or s in live_syms]
print("ranked universe after the liquidity floor:", len(rank_universe), "of", len(universe))
keep = set(universe) | live_syms
hist = {"dates": [d.date().isoformat() for d in tail.index], "universe": universe, "rank_universe": rank_universe,
        "closes": {s: [None if np.isnan(v) else round(float(v), 2) for v in tail[s].values] for s in keep if s in tail}}
json.dump(hist, open(HIST, "w"), separators=(",", ":"))

nifty = frames["^NSEI"]["Close"]
if last == today and not market_open(now):     # after the close: refresh live ideas on closing prices and flag faded momentum
    prices = {s: hist["closes"][s][-1] for s in hist["closes"] if hist["closes"][s][-1]}
    rk, mom = ranks_now(hist, prices, today)
    blocked, results = load_nse(os.path.join(HERE, "..", "nse"))
    refresh_live(state, rk, prices, float(nifty.iloc[-1]), now, blocked, results)
    print("exit flags (decision-maker closes):", rank_exit_flags(state, rk, now))
def ohlc(x, n=330):
    x = x.iloc[-n:]
    return [[str(i.date()), round(float(r.Open), 2), round(float(r.High), 2), round(float(r.Low), 2), round(float(r.Close), 2), int(r.Volume)] for i, r in zip(x.index, x.itertuples())]
need = {e["sym"] for e in state["live"]} | {c["sym"] for c in state["closed"][:60]}
state["bars"] = {s: ohlc(D[s]) for s in need if s in D}
state["bars"]["NIFTY 50"] = ohlc(frames["^NSEI"]); state["bars"]["NIFTY BANK"] = ohlc(frames["^NSEBANK"])
state.update(asof=last, updated_ist=now.strftime("%d %b %Y, %I:%M %p IST"), provisional=False, watching=len(universe), stats=STATS,
             rules=dict(entry_rank=ENTRY_RANK, exit_rank=EXIT_RANK, universe=UNIVERSE, basis="12-month return divided by 12-month volatility, closing prices", approval="decision-maker"))
state.setdefault("recent_alerts", []); state["alerts_today"] = [a for a in state["recent_alerts"] if a["date"] == today]
json.dump(state, open(PICKS, "w"), separators=(",", ":"))
print("asof", last, "universe", len(universe), "live", len(state["live"]), "closed", len(state["closed"]))
