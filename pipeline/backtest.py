"""TrendLens momentum back-test (audit fixes #7, #9, #19 and the re-tests of #2 and #3).

  python pipeline/backtest.py                 # downloads ~20 years of daily bars from Yahoo, caches them, runs every variant
  python pipeline/backtest.py --cache bt.pkl  # reuse a cache

Uses core.momentum_score, the same function the live ranking uses (fix #9), and the same liquidity rule, made relative to the
market level so 2011 is not judged with 2026 rupees (fix #7). Signals at the close, trades at the next open, with Indian delivery
charges and 0.1% slippage per side. Known bias that cannot be removed with free data: the stock list is TODAY's NIFTY 500
(stocks that were dropped or delisted are missing), which flatters the result. Results are written to web/backtest.json."""
from __future__ import annotations

import argparse, json, pickle, sys, time
from pathlib import Path
from statistics import median

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import core                                  # noqa: E402
from arena import charges                    # noqa: E402

SLIP = 0.001
SLOTS, ENTRY, EXIT = 20, core.ENTRY_RANK, core.EXIT_RANK


def download(syms, path):
    import requests
    from concurrent.futures import ThreadPoolExecutor

    def get(y):
        for _ in range(3):
            try:
                r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(y)}?interval=1d&period1=1104537600&period2=2000000000",
                                 headers={"User-Agent": "Mozilla/5.0 TrendLens"}, timeout=30)
                res = r.json()["chart"]["result"][0]; q = res["indicators"]["quote"][0]
                d = pd.DataFrame({k: q[k] for k in ("open", "high", "low", "close", "volume")},
                                 index=(pd.to_datetime(res["timestamp"], unit="s") + pd.Timedelta(hours=5, minutes=30)).normalize())
                return y, d[~d.index.duplicated(keep="last")]
            except Exception:
                time.sleep(2)
        return y, None
    with ThreadPoolExecutor(12) as ex:
        out = dict(ex.map(get, [s + ".NS" for s in syms] + ["^NSEI", "^CRSLDX"]))
    pickle.dump(out, open(path, "wb"))
    return out


def panel(data):
    idx = data["^NSEI"].dropna(subset=["close"]).index
    syms = sorted(k[:-3] for k, v in data.items() if k.endswith(".NS") and v is not None and len(v) > 300)
    P = {f: pd.DataFrame({s: data[s + ".NS"][f].reindex(idx) for s in syms}) for f in ("open", "high", "low", "close", "volume")}
    return idx, syms, P


def daily_ranks(idx, syms, P, relative_liquidity=True, skip_month=False, bench=None):
    """{day index: {sym: rank}} at each close, via core.momentum_score (the live function)."""
    C = P["close"].to_numpy(); TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy()
    nifty = P["close"].mean(axis=1)                    # only used for scale if no index
    lvl = (bench if bench is not None else nifty).reindex(idx).ffill().bfill().to_numpy()
    out = {}
    for i in range(260, len(idx)):
        scale = (lvl[i] / lvl[-1]) if relative_liquidity else 1.0
        score = {}
        for j, s in enumerate(syms):
            p = C[i, j]
            if not p == p or TV[i, j] != TV[i, j] or TV[i, j] < core.MIN_TURNOVER_CR * 1e7 * scale or p < core.MIN_PRICE * scale:
                continue
            past = C[i - 252:i, j] if not skip_month else C[i - 252 - 21:i - 21, j]
            px = p if not skip_month else C[i - 21, j]
            past = [None if x != x else float(x) for x in past]
            if len(past) < 252: continue
            r = core.momentum_score(past, float(px))
            if r: score[s] = r[1]
        order = sorted(score, key=lambda s: (-score[s], s))
        out[i] = {s: k + 1 for k, s in enumerate(order)}
    return out


def cost(side, value, px):
    q = max(1, int(value / px))
    return charges.compute(side, q, px, "delivery").total / (q * px)


def simulate(idx, syms, P, ranks, stop_atr=None, reentry_block=False, sector_caps=None, start=None, capital=2_000_000):
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    col = {s: j for j, s in enumerate(syms)}
    tr = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0))))
    ATR = pd.DataFrame(tr).rolling(14, min_periods=10).mean().to_numpy()
    cash, hold, trades, eq = capital, {}, [], []
    blocked = {}                                         # sym -> {"since": day, "reset": bool}
    first = min(ranks); first = max(first, idx.searchsorted(pd.Timestamp(start))) if start else first
    pend_buy, pend_sell = [], []
    for i in range(first, len(idx)):
        # 1) orders decided at yesterday's close fill at today's open
        for s, why in pend_sell:
            h = hold.pop(s, None)
            if not h: continue
            j = col[s]; px = O[i, j] if O[i, j] == O[i, j] else C[i - 1, j]
            px *= (1 - SLIP); val = h["q"] * px; cash += val * (1 - cost("SELL", val, px))
            trades.append({"sym": s, "in": h["d"], "out": i, "ret": px / h["px"] - 1, "why": why})
        eqv = cash + sum(h["q"] * (C[i - 1, col[s]] if C[i - 1, col[s]] == C[i - 1, col[s]] else h["px"]) for s, h in hold.items())
        for s in pend_buy:
            j = col[s]; px = O[i, j]
            if px != px or len(hold) >= SLOTS: continue
            px *= (1 + SLIP); val = min(eqv / SLOTS, cash)
            if val < 0.25 * eqv / SLOTS: continue
            q = val / px / (1 + cost("BUY", val, px)); cash -= val
            a = ATR[i - 1, j]
            hold[s] = {"q": q, "px": px, "d": i, "stop": (px - stop_atr * a) if stop_atr and a == a else None}
        pend_buy, pend_sell = [], []
        # 2) stops during the day (gap through the stop fills at the open)
        if stop_atr:
            for s in list(hold):
                h, j = hold[s], col[s]
                if h["stop"] and L[i, j] == L[i, j] and L[i, j] <= h["stop"] and i > h["d"] - 1:
                    px = min(O[i, j], h["stop"]) if O[i, j] == O[i, j] else h["stop"]
                    px *= (1 - SLIP); val = h["q"] * px; cash += val * (1 - cost("SELL", val, px))
                    trades.append({"sym": s, "in": h["d"], "out": i, "ret": px / h["px"] - 1, "why": "stop"}); hold.pop(s)
                    if reentry_block: blocked[s] = {"since": i, "reset": False}
        # 3) close: mark to market, rank exits and entries for tomorrow's open
        val = cash + sum(h["q"] * (C[i, col[s]] if C[i, col[s]] == C[i, col[s]] else h["px"]) for s, h in hold.items())
        eq.append(val)
        rk = ranks.get(i, {})
        for s in hold:
            r = rk.get(s)
            if r is None or r > EXIT: pend_sell.append((s, "rank"))
        for s, b in list(blocked.items()):
            r = rk.get(s)
            if r is None or r > EXIT: b["reset"] = True
            if b["reset"] and i - b["since"] >= 10 and r is not None and r <= ENTRY: blocked.pop(s)
        free = SLOTS - (len(hold) - len(pend_sell))
        if free > 0:
            secs, inds = {}, {}
            if sector_caps:
                for s in hold:
                    if (s, "rank") in pend_sell: continue
                    f = sector_caps["map"].get(s, {}); secs[f.get("sector")] = secs.get(f.get("sector"), 0) + 1; inds[f.get("industry")] = inds.get(f.get("industry"), 0) + 1
            for s in sorted((s for s, r in rk.items() if r <= ENTRY and s not in hold), key=rk.get):
                if free <= 0: break
                if s in blocked: continue
                if sector_caps:
                    f = sector_caps["map"].get(s, {}); sc, ind = f.get("sector"), f.get("industry")
                    if sc and secs.get(sc, 0) >= sector_caps["broad"]: continue
                    if ind and inds.get(ind, 0) >= sector_caps["industry"]: continue
                    secs[sc] = secs.get(sc, 0) + 1; inds[ind] = inds.get(ind, 0) + 1
                pend_buy.append(s); free -= 1
    return pd.Series(eq, index=idx[first:]), trades


def stats(eq, trades, idx, a, b):
    e = eq[(eq.index >= a) & (eq.index <= b)]
    if len(e) < 50: return None
    yrs = (e.index[-1] - e.index[0]).days / 365.25
    cagr = (e.iloc[-1] / e.iloc[0]) ** (1 / yrs) - 1
    dd = (e / e.cummax() - 1).min()
    t = [x for x in trades if a <= idx[x["out"]] <= b]
    w = [x["ret"] for x in t if x["ret"] > 0]; lo = [x["ret"] for x in t if x["ret"] <= 0]
    return {"cagr": round(cagr * 100, 1), "maxdd": round(dd * 100, 1), "trades": len(t), "win": round(len(w) / len(t) * 100, 1) if t else None,
            "avg_win": round(np.mean(w) * 100, 1) if w else None, "avg_loss": round(np.mean(lo) * 100, 1) if lo else None,
            "med_days": int(median(x["out"] - x["in"] for x in t)) if t else None,
            "stop_exits": sum(1 for x in t if x["why"] == "stop")}


def bench(s, a, b):
    s = s[(s.index >= a) & (s.index <= b)].dropna()
    yrs = (s.index[-1] - s.index[0]).days / 365.25
    return round(((s.iloc[-1] / s.iloc[0]) ** (1 / yrs) - 1) * 100, 1)


def setup_study(idx, syms, P, ranks, new_defs, start="2011-01-01", bench=None):
    """Short-term setups as live: signal at the close on a stock outside the momentum top 20, ATR levels (stop 1.5 ATR, target
    2.5R, 15 sessions), entry by the live rules (breakout: next open, cancelled below the breakout level; flag: inside the entry range
    within 3 sessions), skipped if the fill leaves less than 2:1, charges + slippage. Result in R (1R = the stop distance)."""
    O, H, L, C, V = (P[f].to_numpy() for f in ("open", "high", "low", "close", "volume"))
    tr = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0))))
    ATR = pd.DataFrame(tr).rolling(14, min_periods=10).mean().to_numpy()
    TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy()
    lvl = (bench if bench is not None else P["close"].mean(axis=1)).reindex(idx).ffill().bfill().to_numpy()
    up = (2.5 * 1.5 - 2.0 * 1.5) / 3.0                  # top of the entry range that still gives 2:1 (as live: 0.25 ATR)
    s0 = idx.searchsorted(pd.Timestamp(start)); out = {"breakout": [], "flag": []}
    for j in range(len(syms)):
        c, h, l, v, o, a = C[:, j], H[:, j], L[:, j], V[:, j], O[:, j], ATR[:, j]
        last = -99                                       # one signal per stock per 15 sessions (no overlapping trades)
        for t in range(max(s0, 60), len(idx) - 17):
            if t - last < 15: continue
            if c[t] != c[t] or a[t] != a[t] or c[t] < 50: continue
            if a[t] / c[t] > 0.07: continue              # live judge: too volatile above 7% daily ATR
            if not TV[t, j] == TV[t, j] or TV[t, j] < core.MIN_TURNOVER_CR * 1e7 * lvl[t] / lvl[-1]: continue
            rk = ranks.get(t, {}).get(syms[j])
            if rk is not None and rk <= ENTRY: continue
            kinds = []
            vavg = np.nanmean(v[t - 50:t]) if new_defs else np.nanmean(v[t - 20:t])
            if c[t] > np.nanmax(h[t - 20:t]) and v[t] > 1.5 * vavg: kinds.append("breakout")
            if new_defs:
                for k in range(3, 11):
                    rng = np.nanmax(h[t - k + 1:t + 1]) - np.nanmin(l[t - k + 1:t + 1]); b0 = t - k
                    if rng < 1.5 * a[t] and c[b0] / c[b0 - 10] - 1 >= 0.15 and c[t] > np.nanmean(c[t - 19:t + 1]): kinds.append("flag"); break
            else:
                run, rng = c[t - 5] / c[t - 15] - 1, (np.nanmax(h[t - 4:t + 1]) - np.nanmin(l[t - 4:t + 1])) / c[t]
                if run >= 0.15 and rng <= 0.06 and c[t] > np.nanmean(c[t - 19:t + 1]): kinds.append("flag")
            if not kinds: continue
            kind = "flag" if "flag" in kinds else "breakout"
            stop, tgt = c[t] - 1.5 * a[t], c[t] + 2.5 * 1.5 * a[t]; fill = None; f0 = None
            if kind == "breakout":
                bo_lv = np.nanmax(h[t - 20:t])
                if o[t + 1] == o[t + 1] and o[t + 1] >= bo_lv: fill, f0 = o[t + 1], t + 1
            else:
                lo, hi = c[t] - 0.4 * a[t], c[t] + up * a[t]
                for u in range(t + 1, t + 4):
                    if o[u] != o[u] or o[u] <= stop: break
                    if l[u] <= hi: fill, f0 = (min(o[u], hi) if o[u] >= lo else o[u]), u; break
            if fill is None or fill <= stop or (tgt - fill) / (fill - stop) < 2.0: continue
            last = t; ex = None
            for u in range(f0, min(f0 + 15, len(idx))):
                oo = fill if u == f0 else o[u]
                if oo != oo: continue
                if oo >= tgt: ex = oo; break
                if oo <= stop: ex = oo; break
                if l[u] <= stop: ex = stop; break
                if h[u] >= tgt and not (u == f0 and kind == "flag"): ex = tgt; break   # fill day of a range fill: the high may have come first
            if ex is None: ex = c[min(f0 + 14, len(idx) - 1)]
            if ex != ex: continue
            fill_c, ex_c = fill * (1 + SLIP), ex * (1 - SLIP)
            net = (ex_c * (1 - cost("SELL", 100000, ex_c)) - fill_c * (1 + cost("BUY", 100000, fill_c)))
            out[kind].append(net / (fill - stop))
    rng = np.random.default_rng(7)
    def ci(v):                                           # 95% bootstrap interval of the average R
        if len(v) < 30: return None
        m = [float(np.mean(rng.choice(v, len(v)))) for _ in range(1000)]
        return [round(float(np.percentile(m, 2.5)), 3), round(float(np.percentile(m, 97.5)), 3)]
    return {k: {"trades": len(v), "avg_R": round(float(np.mean(v)), 3) if v else None, "ci95": ci(np.array(v)),
                "win": round(100 * float(np.mean([x > 0 for x in v])), 1) if v else None} for k, v in out.items()}


BENCH = None

if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default=str(HERE.parent / "backtest_cache.pkl")); ap.add_argument("--fixed-liquidity", action="store_true")
    ap.add_argument("--setups", action="store_true"); ap.add_argument("--setups-only", action="store_true"); ap.add_argument("--out", default=str(HERE.parent / "web" / "backtest.json")); a = ap.parse_args()
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    data = pickle.load(open(a.cache, "rb")) if Path(a.cache).exists() else download(syms, a.cache)
    idx, syms, P = panel(data)
    BENCH = data["^CRSLDX"]["close"] if data.get("^CRSLDX") is not None else data["^NSEI"]["close"]
    print("stocks:", len(syms), "days:", len(idx), idx[0].date(), "->", idx[-1].date())
    t0 = time.time(); R = daily_ranks(idx, syms, P, relative_liquidity=not a.fixed_liquidity, bench=BENCH); print("ranks", round(time.time() - t0), "s")
    if a.setups_only:
        res = json.loads(Path(a.out).read_text()) if Path(a.out).exists() else {}
        res["setups"] = {"old definitions (5-day pause <= 6%, 20-day volume)": setup_study(idx, syms, P, R, False, bench=BENCH),
                         "new definitions (3-10 day pause < 1.5x ATR, 50-day volume)": setup_study(idx, syms, P, R, True, bench=BENCH),
                         "method": "one signal per stock per 15 sessions; ATR <= 7%; liquidity floor; entry as live; R after costs; 95% bootstrap interval"}
        print("setups", res["setups"]); Path(a.out).write_text(json.dumps(res, indent=1)); sys.exit(0)
    funds = json.loads((HERE.parent / "web" / "fundamentals.json").read_text()).get("stocks", {})
    caps = {"map": funds, "broad": 6, "industry": 3}
    variants = {
        "A. Rule as published: top 20 in, below #50 out, no stop": dict(),
        "B. A + 6x ATR stop (live momentum stop)": dict(stop_atr=6),
        "C. B + no re-entry after a stop until the rank resets (fix #2)": dict(stop_atr=6, reentry_block=True),
        "D. A + sector caps 6 / industry 3 (fix #3)": dict(sector_caps=caps),
        "E. B + C + D (all audit rules)": dict(stop_atr=6, reentry_block=True, sector_caps=caps),
        "G. B + D: the LIVE rules (6x ATR stop + sector caps; re-entry block not adopted)": dict(stop_atr=6, sector_caps=caps),
    }
    periods = [("2011-2019", "2011-01-01", "2019-12-31"), ("2020-2026", "2020-01-01", str(idx[-1].date())),
               ("Last 5 years", str((idx[-1] - pd.DateOffset(years=5)).date()), str(idx[-1].date()))]
    res = {"asof": str(idx[-1].date()), "stocks": len(syms), "liquidity": "relative (scaled by NIFTY 500 level)" if not a.fixed_liquidity else "fixed Rs 10 cr",
           "bias": "Stock list is today's NIFTY 500: stocks dropped from the index or delisted since are missing, which flatters results.",
           "costs": "Indian delivery charges (STT, exchange, SEBI, stamp, GST, DP) + 0.1% slippage per side; dividends ignored",
           "benchmark": {p: {"NIFTY 500 (price)": bench(BENCH, x, y), "NIFTY 50 (price)": bench(data["^NSEI"]["close"], x, y)} for p, x, y in periods},
           "variants": {}}
    for name, kw in variants.items():
        eq, tr = simulate(idx, syms, P, R, start="2010-12-01", **kw)
        res["variants"][name] = {p: stats(eq, tr, idx, pd.Timestamp(x), pd.Timestamp(y)) for p, x, y in periods}
        print(name); [print("   ", p, v) for p, v in res["variants"][name].items()]
    R2 = daily_ranks(idx, syms, P, relative_liquidity=not a.fixed_liquidity, skip_month=True, bench=BENCH)
    eq, tr = simulate(idx, syms, P, R2, start="2010-12-01")
    res["variants"]["F. A with 12-minus-1-month momentum (info only, fix #9)"] = {p: stats(eq, tr, idx, pd.Timestamp(x), pd.Timestamp(y)) for p, x, y in periods}
    print("F", res["variants"]["F. A with 12-minus-1-month momentum (info only, fix #9)"])
    print("benchmark", res["benchmark"])
    if a.setups:
        res["setups"] = {"old definitions (5-day pause <= 6%, 20-day volume)": setup_study(idx, syms, P, R, False, bench=BENCH),
                         "new definitions (3-10 day pause < 1.5x ATR, 50-day volume)": setup_study(idx, syms, P, R, True, bench=BENCH)}
        print("setups", res["setups"])
    Path(a.out).write_text(json.dumps(res, indent=1))
