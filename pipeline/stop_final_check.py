"""Last check before the close-based stop goes live (Nov 2026). Follows pipeline/combo_study.py.

 1. Worst-case losses: worst trade, 1st percentile, share of big losses, longest losing run - per stop rule.
 2. Momentum: sell at the NEXT OPEN after a close below the stop (as well as at that day's close).
 3. Momentum: emergency stop - a touch 1.5x the planned risk below the entry still sells at once.
 4. Stop distance: momentum 5 / 6 / 7 ATR, bull flag 1.25 / 1.5 / 1.75 ATR (target stays 2.5x the risk).
Periods 2011-2019 and 2020-2026; a change counts only if it helps in both. Writes stop_summary.json / stop_summary.md."""
from __future__ import annotations
import argparse, json, pickle, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import backtest as B, core  # noqa: E402

P1, P2 = ("2011-01-01", "2019-12-31"), ("2020-01-01", "2026-12-31")
PERIODS = (("2011-2019", P1), ("2020-2026", P2))


def simulate(idx, syms, P, ranks, mode="touch", stop_atr=6, hard_k=1.5, sector_caps=None, start="2010-12-01"):
    """Momentum portfolio as combo_study.simulate. mode: touch | close | close_open | hybrid (close_open + emergency touch stop)."""
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    col = {s: j for j, s in enumerate(syms)}
    tr = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0))))
    ATR = pd.DataFrame(tr).rolling(14, min_periods=10).mean().to_numpy()
    cash, hold, trades, eq = 2_000_000, {}, [], []
    first = max(min(ranks), idx.searchsorted(pd.Timestamp(start)))
    pend_buy, pend_sell = [], []

    def sell(s, px, i, why):
        nonlocal cash
        h = hold.pop(s); px *= (1 - B.SLIP); val = h["q"] * px; cash += val * (1 - B.cost("SELL", val, px))
        trades.append({"sym": s, "in": h["d"], "out": i, "ret": px / h["px"] - 1, "why": why})

    for i in range(first, len(idx)):
        for s, why in pend_sell:
            if s not in hold: continue
            j = col[s]; sell(s, O[i, j] if O[i, j] == O[i, j] else C[i - 1, j], i, why)
        eqv = cash + sum(h["q"] * (C[i - 1, col[s]] if C[i - 1, col[s]] == C[i - 1, col[s]] else h["px"]) for s, h in hold.items())
        for s in pend_buy:
            j = col[s]; px = O[i, j]
            if px != px or len(hold) >= B.SLOTS: continue
            px *= (1 + B.SLIP); val = min(eqv / B.SLOTS, cash)
            if val < 0.25 * eqv / B.SLOTS: continue
            q = val / px / (1 + B.cost("BUY", val, px)); cash -= val; a = ATR[i - 1, j]
            ok = a == a
            hold[s] = {"q": q, "px": px, "d": i, "stop": px - stop_atr * a if ok else None, "hard": px - hard_k * stop_atr * a if ok else None}
        pend_buy, pend_sell = [], []
        for s in list(hold):
            h, j = hold[s], col[s]
            if not h["stop"] or L[i, j] != L[i, j]: continue
            o = O[i, j]
            if mode == "touch" and L[i, j] <= h["stop"]: sell(s, min(o, h["stop"]) if o == o else h["stop"], i, "stop"); continue
            if mode == "hybrid" and L[i, j] <= h["hard"]: sell(s, min(o, h["hard"]) if o == o else h["hard"], i, "stop"); continue
            if C[i, j] <= h["stop"]:
                if mode == "close": sell(s, C[i, j], i, "stop")
                elif mode in ("close_open", "hybrid"): pend_sell.append((s, "stop"))
        eq.append(cash + sum(h["q"] * (C[i, col[s]] if C[i, col[s]] == C[i, col[s]] else h["px"]) for s, h in hold.items()))
        rk = ranks.get(i, {}); leaving = {s for s, _ in pend_sell}
        for s in hold:
            r = rk.get(s)
            if s not in leaving and (r is None or r > B.EXIT): pend_sell.append((s, "rank")); leaving.add(s)
        free = B.SLOTS - (len(hold) - len(leaving))
        if free > 0:
            secs, inds = {}, {}
            if sector_caps:
                for s in hold:
                    if s in leaving: continue
                    f = sector_caps["map"].get(s, {}); secs[f.get("sector")] = secs.get(f.get("sector"), 0) + 1; inds[f.get("industry")] = inds.get(f.get("industry"), 0) + 1
            for s in sorted((s for s, r in rk.items() if r <= B.ENTRY and s not in hold), key=rk.get):
                if free <= 0: break
                if sector_caps:
                    f = sector_caps["map"].get(s, {}); sc, ind = f.get("sector"), f.get("industry")
                    if sc and secs.get(sc, 0) >= sector_caps["broad"]: continue
                    if ind and inds.get(ind, 0) >= sector_caps["industry"]: continue
                    secs[sc] = secs.get(sc, 0) + 1; inds[ind] = inds.get(ind, 0) + 1
                pend_buy.append(s); free -= 1
    return pd.Series(eq, index=idx[first:]), trades


def longest_run(x):
    best = cur = 0
    for v in x:
        cur = cur + 1 if v <= 0 else 0; best = max(best, cur)
    return best


def mom_stats(eq, trades, idx):
    out = {}
    for name, (a, b) in PERIODS:
        s = B.stats(eq, trades, idx, pd.Timestamp(a), pd.Timestamp(b)) or {}
        r = np.array([t["ret"] for t in sorted(trades, key=lambda t: t["out"]) if a <= str(idx[t["out"]].date()) <= b]) * 100
        out[name] = {"cagr": s.get("cagr"), "maxdd": s.get("maxdd"), "trades": s.get("trades"), "win": s.get("win"),
                     "worst_pct": round(float(r.min()), 1) if len(r) else None, "p1_pct": round(float(np.percentile(r, 1)), 1) if len(r) else None,
                     "loss_over_20pct": int((r < -20).sum()), "longest_losing_run": longest_run(r)}
    return out


def flags(idx, syms, P, R, bench, ks=(1.25, 1.5, 1.75)):
    """Bull-flag signals as combo_study.flags (current entry range); stop k*ATR below the signal close, target 2.5x the risk."""
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    trh = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0)))); ATR = pd.DataFrame(trh).rolling(14, min_periods=10).mean().to_numpy()
    TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy(); lvl = bench.reindex(idx).ffill().bfill().to_numpy()
    up = (2.5 * 1.5 - 2.0 * 1.5) / 3.0; s0 = idx.searchsorted(pd.Timestamp("2011-01-01")); rows = []

    def net(fill, ex, stop):
        fc, ec = fill * (1 + B.SLIP), ex * (1 - B.SLIP)
        return (ec * (1 - B.cost("SELL", 100000, ec)) - fc * (1 + B.cost("BUY", 100000, fc))) / (fill - stop)

    def walk(o, h, l, c, f0, fill, stop, tgt, mode):
        hard = fill - 1.5 * (fill - stop)
        for u in range(f0, min(f0 + 15, len(idx))):
            oo = fill if u == f0 else o[u]
            if oo != oo: continue
            if u > f0 and oo >= tgt: return oo
            if mode == "touch":
                if u > f0 and oo <= stop: return oo
                if l[u] <= stop: return stop
            if mode == "hybrid":
                if u > f0 and oo <= hard: return oo
                if l[u] <= hard: return hard
            if h[u] >= tgt and u != f0: return tgt
            if mode in ("close_open", "hybrid") and c[u] <= stop and u + 1 < len(idx) and o[u + 1] == o[u + 1]: return o[u + 1]
        return c[min(f0 + 14, len(idx) - 1)]

    for j, sym in enumerate(syms):
        c, h, l, o, a = C[:, j], H[:, j], L[:, j], O[:, j], ATR[:, j]; last = -99
        for t in range(max(s0, 60), len(idx) - 17):
            if t - last < 15 or c[t] != c[t] or a[t] != a[t] or c[t] < 50 or a[t] / c[t] > 0.07: continue
            if not TV[t, j] == TV[t, j] or TV[t, j] < core.MIN_TURNOVER_CR * 1e7 * lvl[t] / lvl[-1]: continue
            rk = R.get(t, {}).get(sym)
            if rk is not None and rk <= B.ENTRY: continue
            found = False
            for k in range(3, 11):
                rng = np.nanmax(h[t - k + 1:t + 1]) - np.nanmin(l[t - k + 1:t + 1]); b0 = t - k
                if rng < 1.5 * a[t] and c[b0] / c[b0 - 10] - 1 >= 0.15 and c[t] > np.nanmean(c[t - 19:t + 1]): found = True; break
            if not found: continue
            last = t; row = {"sym": sym, "day": str(idx[t].date())}; lo, hi = c[t] - 0.4 * a[t], c[t] + up * a[t]
            for kk in ks:
                stop, tgt = c[t] - kk * a[t], c[t] + 2.5 * kk * a[t]; fill = f0 = None
                for u in range(t + 1, t + 4):
                    if o[u] != o[u] or o[u] <= stop: break
                    if l[u] <= hi: fill, f0 = (min(o[u], hi) if o[u] >= lo else o[u]), u; break
                if fill is None or fill <= stop or (tgt - fill) / (fill - stop) < 2.0: continue
                for mode in ("touch", "close_open", "hybrid"):
                    row[f"k{kk}_{mode}"] = net(fill, walk(o, h, l, c, f0, fill, stop, tgt, mode), stop)
            rows.append(row)
    return pd.DataFrame(rows)


def flag_stats(D, cols):
    out = {}
    for name, (a, b) in PERIODS:
        g = D[(D.day >= a) & (D.day <= b)].sort_values("day"); o = {}
        for v in cols:
            x = g[v].dropna() if v in g else pd.Series(dtype=float)
            if len(x) < 30: o[v] = {"trades": int(len(x))}; continue
            se = x.std(ddof=1) / np.sqrt(len(x))
            o[v] = {"trades": int(len(x)), "avg_R": round(float(x.mean()), 3), "ci95": [round(float(x.mean() - 1.96 * se), 3), round(float(x.mean() + 1.96 * se), 3)],
                    "total_R": round(float(x.sum()), 1), "worst_R": round(float(x.min()), 2), "p1_R": round(float(np.percentile(x, 1)), 2),
                    "pct_below_-1.5R": round(100 * float((x < -1.5).mean()), 2), "pct_below_-2R": round(100 * float((x < -2).mean()), 2),
                    "longest_losing_run": longest_run(x.to_numpy())}
        base = "k1.5_touch"
        for v in cols:
            if v == base or v not in g or base not in g: continue
            both = g.dropna(subset=[v, base])
            if len(both) > 30:
                d = both[v] - both[base]; o[v]["vs_live_same_trades"] = {"n": int(len(d)), "avg_R": round(float(d.mean()), 3), "t": round(float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))), 2)}
        out[name] = o
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default="daily_cache.pkl"); ap.add_argument("--out", default="stop_results")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(exist_ok=True)
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    data = pickle.load(open(a.cache, "rb")) if Path(a.cache).exists() else B.download(syms, a.cache)
    idx, syms, P = B.panel(data)
    bench = data["^CRSLDX"]["close"] if data.get("^CRSLDX") is not None else data["^NSEI"]["close"]
    R = B.daily_ranks(idx, syms, P, bench=bench); print("ranks done", flush=True)
    funds = json.loads((HERE.parent / "web" / "fundamentals.json").read_text()).get("stocks", {})
    caps = {"map": funds, "broad": 6, "industry": 3}
    res = {"momentum": {}}
    for atr in (5, 6, 7):
        for mode in ("touch", "close", "close_open", "hybrid"):
            name = f"{atr} ATR, {mode}"
            eq, tr = simulate(idx, syms, P, R, mode=mode, stop_atr=atr, sector_caps=caps)
            res["momentum"][name] = {**mom_stats(eq, tr, idx), "stops": sum(1 for x in tr if x["why"] == "stop")}
            print(name, json.dumps(res["momentum"][name]), flush=True)
    D = flags(idx, syms, P, R, bench); D.to_csv(out / "flag_stop_variants.csv", index=False)
    cols = [f"k{k}_{m}" for k in (1.25, 1.5, 1.75) for m in ("touch", "close_open", "hybrid")]
    res["bull_flag"] = flag_stats(D, cols)
    (out / "stop_summary.json").write_text(json.dumps(res, indent=1))
    L = ["# Close-based stop: final check", "", "## Momentum (portfolio, after costs). live = 6 ATR, touch", "",
         "| variant | 11-19 CAGR / max fall / worst trade / losses >20% | 20-26 CAGR / max fall / worst trade / losses >20% | stops |", "|---|---|---|---|"]
    for k, v in res["momentum"].items():
        f = lambda p: f"{v[p]['cagr']}% / {v[p]['maxdd']}% / {v[p]['worst_pct']}% / {v[p]['loss_over_20pct']}"
        L.append(f"| {k} | {f('2011-2019')} | {f('2020-2026')} | {v['stops']} |")
    L += ["", "## Bull flag (net R a trade). live = k1.5_touch", "", "| variant | 11-19 avg R (vs live, t) / worst / % < -1.5R | 20-26 avg R (vs live, t) / worst / % < -1.5R |", "|---|---|---|"]
    for v in cols:
        def f(p):
            o = res["bull_flag"][p].get(v, {}); d = o.get("vs_live_same_trades", {})
            return f"{o.get('avg_R')} ({d.get('avg_R')}, t {d.get('t')}) / {o.get('worst_R')} / {o.get('pct_below_-1.5R')}% [{o.get('trades')} tr]"
        L.append(f"| {v} | {f('2011-2019')} | {f('2020-2026')} |")
    (out / "stop_summary.md").write_text("\n".join(L)); print("\n".join(L), flush=True)
