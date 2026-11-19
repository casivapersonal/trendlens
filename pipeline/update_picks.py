"""TrendLens daily screener: real NSE prices -> picks.json (event-driven ideas)

Run:  python update_picks.py <old picks.json or "none"> <new picks.json>
Rules (back-tested 2011-2026 on ~500 NSE stocks):
  universe : 300 most-traded NSE stocks (126-day median traded value), >= 260 days listed
  score    : 12-month return skipping the latest month (price 21 sessions ago / 252 sessions ago)
  ENTRY    : stock ranks in the top 20 -> entry alert, idea goes Live at that day's close
  EXIT     : rank falls below 50 (or stock leaves the universe) -> exit alert, idea goes to Closed
The rules are replayed from REPLAY_START every run, so the history is deterministic and auditable.
Prices: Yahoo Finance (research use only).
"""
import json, sys, os, datetime as dt
import pandas as pd, numpy as np, yfinance as yf

ENTRY_RANK, EXIT_RANK, UNIVERSE, REPLAY_START, GO_LIVE = 20, 50, 300, "2026-01-01", "2026-11-19"
STATS = {"rule": "Enter at top 20, exit below #50", "periods": [
  {"period": "2011–2019", "cagr": 33.7, "nifty": 8.1, "maxdd": -25.8, "trades": 1079, "win": 48.9, "avg_win": 28.8, "avg_loss": -8.3, "avg": 9.84, "med_days": 50},
  {"period": "2020–2026", "cagr": 41.2, "nifty": 11.4, "maxdd": -40.1, "trades": 657, "win": 47.2, "avg_win": 37.9, "avg_loss": -10.1, "avg": 12.55, "med_days": 65}]}
HERE = os.path.dirname(os.path.abspath(__file__))
OLD, NEW = sys.argv[1], sys.argv[2]
old = json.load(open(OLD)) if os.path.exists(OLD) else {}
syms = list(dict.fromkeys(open(os.path.join(HERE, "syms.txt")).read().split()))

frames = {}
tick = [s + ".NS" for s in syms] + ["^NSEI", "^NSEBANK"]
for k in range(0, len(tick), 80):
    ch = tick[k:k + 80]
    d = yf.download(ch, period="2y", progress=False, auto_adjust=True, group_by="ticker", threads=True)
    for t in ch:
        try:
            x = d[t].dropna(subset=["Close"])
        except Exception:
            continue
        x = x[(x["Volume"] > 0) & (x["Close"] > 0)] if not t.startswith("^") else x
        if len(x) >= 200:
            frames[t] = x
    print(f"downloaded {k + len(ch)}/{len(tick)}", flush=True)
D = {t[:-3]: x for t, x in frames.items() if t.endswith(".NS")}
NIFTY, BANK = frames["^NSEI"]["Close"], frames["^NSEBANK"]["Close"]
C = pd.DataFrame({s: x["Close"] for s, x in D.items()}).sort_index()
cov = C.notna().sum(axis=1)
C = C[cov >= 0.9 * cov.max()]          # drop incomplete days (e.g. a session still in progress)
TV = pd.DataFrame({s: x["Close"] * x["Volume"] for s, x in D.items()}).reindex(C.index)
listed = C.notna().cumsum() > 0
C = C.ffill(limit=5).where(listed)     # bridge short data gaps so a missing print is not read as an exit
NIFTY = NIFTY.reindex(C.index).ffill()
liq = TV.rolling(126, min_periods=100).median()
elig = (C.notna().cumsum() >= 260) & C.notna()
in_uni = liq.where(elig).rank(axis=1, ascending=False) <= UNIVERSE
mom = C.shift(21) / C.shift(252) - 1
rank = mom.where(in_uni).rank(axis=1, ascending=False)

live, closed, alerts = {}, [], []
days = C.index[C.index >= REPLAY_START]
for d in days:
    r = rank.loc[d]
    for s in list(live):
        rk = r.get(s, np.nan); px = C.at[d, s]
        if not (rk <= EXIT_RANK) or px != px:
            e = live.pop(s)
            out = dict(e, status="closed", exit_date=str(d.date()), exit_price=round(float(px), 2) if px == px else e["entry_price"],
                       exit_rank=None if rk != rk else int(rk),
                       reason=("No price data (trading halted or suspended)" if px != px else "Left the 300 most-traded stocks" if rk != rk else f"Momentum rank fell to #{int(rk)} (exit rule: below #{EXIT_RANK})"),
                       nifty_pct=round((float(NIFTY[d]) / e["nifty_entry"] - 1) * 100, 2))
            out["ret_pct"] = round((out["exit_price"] / e["entry_price"] - 1) * 100, 2)
            closed.append(out); alerts.append(dict(type="exit", sym=s, date=out["exit_date"], rank=out["exit_rank"], price=out["exit_price"], ret_pct=out["ret_pct"]))
    for s in r[r <= ENTRY_RANK].sort_values().index:
        if s not in live:
            e = dict(sym=s, entry_date=str(d.date()), entry_price=round(float(C.at[d, s]), 2), entry_rank=int(r[s]),
                     mom_pct=round(float(mom.at[d, s]) * 100, 1), nifty_entry=float(NIFTY[d]), backfilled=str(d.date()) < GO_LIVE)
            live[s] = e; alerts.append(dict(type="entry", sym=s, date=e["entry_date"], rank=e["entry_rank"], price=e["entry_price"]))
today = C.index[-1]
for s, e in live.items():
    e.update(status="live", rank_now=int(rank.at[today, s]), price_now=round(float(C.at[today, s]), 2),
             nifty_pct=round((float(NIFTY[today]) / e["nifty_entry"] - 1) * 100, 2))
watch = [dict(sym=s, rank=int(v)) for s, v in rank.loc[today].dropna().sort_values().head(30).items()]
# Publish only ideas that started on/after go-live: the feed fills through alerts, no back-filled dump.
pub_live = [e for e in live.values() if e["entry_date"] >= GO_LIVE]
pub_closed = [c for c in closed if c["entry_date"] >= GO_LIVE]
pub_alerts = [a for a in alerts if a["date"] >= GO_LIVE and (a["type"] == "entry" or any(c["sym"] == a["sym"] and c["exit_date"] == a["date"] for c in pub_closed))]

need = {e["sym"] for e in pub_live} | {c["sym"] for c in pub_closed}
def ohlc(x, n=330):
    x = x.iloc[-n:]
    return [[str(i.date()), round(float(r.Open), 2), round(float(r.High), 2), round(float(r.Low), 2), round(float(r.Close), 2), int(r.Volume)] for i, r in zip(x.index, x.itertuples())]
bars = {s: ohlc(D[s]) for s in need}
bars["NIFTY 50"] = ohlc(frames["^NSEI"]); bars["NIFTY BANK"] = ohlc(frames["^NSEBANK"])
ist = dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)
provisional = today.date() == ist.date() and ist.time() < dt.time(15, 35)
out = dict(asof=str(today.date()), provisional=provisional, updated_ist=ist.strftime("%d %b %Y, %I:%M %p IST"), updated=dt.datetime.utcnow().strftime("%Y-%m-%dT%H:%MZ"),
           rules=dict(entry_rank=ENTRY_RANK, exit_rank=EXIT_RANK, universe=UNIVERSE, replay_start=REPLAY_START, go_live=GO_LIVE),
           live=pub_live, closed=pub_closed, alerts_today=[a for a in pub_alerts if a["date"] == str(today.date())],
           recent_alerts=sorted(pub_alerts, key=lambda a: a["date"], reverse=True)[:40], watching=len(rank.loc[today].dropna()), bars=bars,
           stats=old.get("stats") or STATS)
json.dump(out, open(NEW, "w"), separators=(",", ":"))
print("asof", out["asof"], "live", len(pub_live), "closed", len(pub_closed), "alerts today", len(out["alerts_today"]), "(tracking", len(live), "in top-50 band internally)")
