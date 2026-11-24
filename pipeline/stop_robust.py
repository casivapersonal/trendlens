"""Robustness check for the bull-flag close-based stop (Nov 2026, after the independent review).
Extra slippage (0, 0.5%, 1%) on the new rule's stop exits (next-open after a close below the stop, and the hybrid
emergency stop); the live touch stop keeps today's costs. Standard errors clustered by signal week and by month
(trades bunch on sell-off days). Periods 2011-2019 and 2020-2026. Writes robust_summary.json / .md."""
from __future__ import annotations
import argparse, json, pickle, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import backtest as B, core  # noqa: E402

PERIODS = (("2011-2019", "2011-01-01", "2019-12-31"), ("2020-2026", "2020-01-01", "2026-12-31"))
EXTRAS = (0.0, 0.005, 0.01)
MODES = (("close_open", None), ("hybrid", 1.5), ("hybrid2.0", 2.0), ("hybrid2.5", 2.5), ("hybrid3.0", 3.0))   # emergency stop at k x the risk below the fill


def flags(idx, syms, P, R, bench, k=1.5):
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    trh = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0)))); ATR = pd.DataFrame(trh).rolling(14, min_periods=10).mean().to_numpy()
    TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy(); lvl = bench.reindex(idx).ffill().bfill().to_numpy()
    up = (2.5 * 1.5 - 2.0 * 1.5) / 3.0; s0 = idx.searchsorted(pd.Timestamp("2011-01-01")); rows = []

    def net(fill, ex, stop, extra=0.0):
        fc, ec = fill * (1 + B.SLIP), ex * (1 - B.SLIP - extra)
        return (ec * (1 - B.cost("SELL", 100000, ec)) - fc * (1 + B.cost("BUY", 100000, fc))) / (fill - stop)

    def walk(o, h, l, c, f0, fill, stop, tgt, mode, hard_k=1.5):            # -> (price, kind)
        hard = fill - hard_k * (fill - stop)
        for u in range(f0, min(f0 + 15, len(idx))):
            oo = fill if u == f0 else o[u]
            if oo != oo: continue
            if u > f0 and oo >= tgt: return oo, "target"
            if mode == "touch":
                if u > f0 and oo <= stop: return oo, "stop"
                if l[u] <= stop: return stop, "stop"
            if mode == "hybrid":
                if u > f0 and oo <= hard: return oo, "stop"
                if l[u] <= hard: return hard, "stop"
            if h[u] >= tgt and u != f0: return tgt, "target"
            if mode in ("close_open", "hybrid") and c[u] <= stop and u + 1 < len(idx) and o[u + 1] == o[u + 1]: return o[u + 1], "stop"
        return c[min(f0 + 14, len(idx) - 1)], "time"

    for j, sym in enumerate(syms):
        c, h, l, o, a = C[:, j], H[:, j], L[:, j], O[:, j], ATR[:, j]; last = -99
        for t in range(max(s0, 60), len(idx) - 17):
            if t - last < 15 or c[t] != c[t] or a[t] != a[t] or c[t] < 50 or a[t] / c[t] > 0.07: continue
            if not TV[t, j] == TV[t, j] or TV[t, j] < core.MIN_TURNOVER_CR * 1e7 * lvl[t] / lvl[-1]: continue
            rk = R.get(t, {}).get(sym)
            if rk is not None and rk <= B.ENTRY: continue
            found = False
            for kk in range(3, 11):
                rng = np.nanmax(h[t - kk + 1:t + 1]) - np.nanmin(l[t - kk + 1:t + 1]); b0 = t - kk
                if rng < 1.5 * a[t] and c[b0] / c[b0 - 10] - 1 >= 0.15 and c[t] > np.nanmean(c[t - 19:t + 1]): found = True; break
            if not found: continue
            last = t; lo, hi = c[t] - 0.4 * a[t], c[t] + up * a[t]
            stop, tgt = c[t] - k * a[t], c[t] + 2.5 * k * a[t]; fill = f0 = None
            for u in range(t + 1, t + 4):
                if o[u] != o[u] or o[u] <= stop: break
                if l[u] <= hi: fill, f0 = (min(o[u], hi) if o[u] >= lo else o[u]), u; break
            if fill is None or fill <= stop or (tgt - fill) / (fill - stop) < 2.0: continue
            row = {"sym": sym, "day": idx[t]}
            ex, _ = walk(o, h, l, c, f0, fill, stop, tgt, "touch"); row["live"] = net(fill, ex, stop)
            for mode, hk in MODES:
                ex, kind = walk(o, h, l, c, f0, fill, stop, tgt, "hybrid" if hk else mode, hk or 1.5)
                for e in EXTRAS:
                    row[f"{mode}_{e}"] = net(fill, ex, stop, e if kind == "stop" else 0.0)
            rows.append(row)
    return pd.DataFrame(rows)


def clustered(d, groups):
    """Mean, naive t and cluster-robust t of d (pd.Series) with clusters given by groups."""
    n = len(d); m = d.mean(); se0 = d.std(ddof=1) / np.sqrt(n)
    g = (d - m).groupby(groups).sum(); G = len(g)
    se = np.sqrt((g ** 2).sum() * G / max(G - 1, 1)) / n
    return {"n": int(n), "clusters": int(G), "avg_R": round(float(m), 3), "t_naive": round(float(m / se0), 2), "t_clustered": round(float(m / se), 2)}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default="daily_cache.pkl"); ap.add_argument("--out", default="robust_results")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(exist_ok=True)
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    data = pickle.load(open(a.cache, "rb")) if Path(a.cache).exists() else B.download(syms, a.cache)
    idx, syms, P = B.panel(data)
    bench = data["^CRSLDX"]["close"] if data.get("^CRSLDX") is not None else data["^NSEI"]["close"]
    R = B.daily_ranks(idx, syms, P, bench=bench); print("ranks done", flush=True)
    D = flags(idx, syms, P, R, bench); D.to_csv(out / "robust_trades.csv", index=False)
    res = {}
    L = ["# Bull-flag close-based stop: robustness", "", "Gain vs the live touch stop, net R a trade, same trades. t = naive / clustered by week / clustered by month.", "",
         "| rule, extra slippage on its stop exits | 2011-2019 | 2020-2026 |", "|---|---|---|"]
    for mode, _ in MODES:
        for e in EXTRAS:
            v = f"{mode}_{e}"; cells = []
            for name, s, t in PERIODS:
                g = D[(D.day >= s) & (D.day <= t)]; d = g[v] - g["live"]
                wk = clustered(d, g.day.dt.to_period("W")); mo = clustered(d, g.day.dt.to_period("M"))
                res.setdefault(v, {})[name] = {"week": wk, "month": mo, "avg_R_rule": round(float(g[v].mean()), 3), "avg_R_live": round(float(g["live"].mean()), 3)}
                x = g[v]; tail = f"worst {x.min():.2f}R, {100 * (x < -1.5).mean():.1f}% < -1.5R, {100 * (x < -2.5).mean():.1f}% < -2.5R (live {g['live'].min():.2f}R, {100 * (g['live'] < -1.5).mean():.1f}%)"
                res[v][name]["tail"] = tail
                cells.append(f"{wk['avg_R']:+.3f} (t {wk['t_naive']} / {wk['t_clustered']} / {mo['t_clustered']}); rule {g[v].mean():+.3f} vs live {g['live'].mean():+.3f}; {tail}")
            L.append(f"| {mode}, +{e * 100:.1f}% | {cells[0]} | {cells[1]} |")
    (out / "robust_summary.json").write_text(json.dumps(res, indent=1)); (out / "robust_summary.md").write_text("\n".join(L)); print("\n".join(L), flush=True)
