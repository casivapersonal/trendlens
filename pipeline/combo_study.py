"""Five untested rule changes for the momentum and bull-flag ideas, alone and combined (Nov 2026).

 1. Stop on the CLOSE instead of a touch (sell at the close only if the day closes below the stop): momentum and bull flag.
 2. Bull flag BREAKOUT entry: buy when the price crosses the flag's high (the next 3 sessions; a gap above it fills at the open),
    stop unchanged (signal close - 1.5 ATR), target 2.5x the risk from the fill, risk <= 12% of the price.
 3. Momentum: SKIP STRETCHED buys (close > 25% above the 50-day average or up > 20% in 5 sessions at the signal), or skip them
    only when they also gap up more than 2% at the open; the slot goes to the next stock by rank (as the live CEO does).
 4. Bull flag TIME EXITS at another time of the last day (5-minute data: 9:30, 11:00, 13:00, day VWAP vs the close).
 5. COMBINATIONS: breakout + close stop (flag); skip stretched + close stop (momentum).
Momentum = the full portfolio back-test (20 slots, rank 20 in / 50 out, 6 ATR stop, sector caps, costs, 0.1% slippage).
Bull flag = every signal as in setup_study(new_defs=True), net R after costs. Periods: 2011-2019 and 2020-2026 (as the audit);
the 5-minute test uses 2014-2021 and 2022-2026. A change is worth making only if it helps in both periods.
Writes combo_summary.json / combo_summary.md."""
from __future__ import annotations
import argparse, json, pickle, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import backtest as B, core  # noqa: E402

P1, P2 = ("2011-01-01", "2019-12-31"), ("2020-01-01", "2026-12-31")


# ------------------------------------------------------------------ momentum portfolio with variants
def simulate(idx, syms, P, ranks, stop_mode="touch", skip=None, stop_atr=6, sector_caps=None, start="2010-12-01"):
    """pipeline/backtest.simulate with two switches: stop_mode 'touch' (as live) or 'close'; skip(i, j, open_i) -> True to skip a buy."""
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    col = {s: j for j, s in enumerate(syms)}
    tr = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0))))
    ATR = pd.DataFrame(tr).rolling(14, min_periods=10).mean().to_numpy()
    cash, hold, trades, eq = 2_000_000, {}, [], []
    first = max(min(ranks), idx.searchsorted(pd.Timestamp(start)))
    pend_buy, pend_sell = [], []; skipped = 0
    for i in range(first, len(idx)):
        for s, why in pend_sell:
            h = hold.pop(s, None)
            if not h: continue
            j = col[s]; px = O[i, j] if O[i, j] == O[i, j] else C[i - 1, j]
            px *= (1 - B.SLIP); val = h["q"] * px; cash += val * (1 - B.cost("SELL", val, px))
            trades.append({"sym": s, "in": h["d"], "out": i, "ret": px / h["px"] - 1, "why": why})
        eqv = cash + sum(h["q"] * (C[i - 1, col[s]] if C[i - 1, col[s]] == C[i - 1, col[s]] else h["px"]) for s, h in hold.items())
        for s in pend_buy:
            j = col[s]; px = O[i, j]
            if px != px or len(hold) >= B.SLOTS: continue
            if skip and skip(i, j, px): skipped += 1; continue
            px *= (1 + B.SLIP); val = min(eqv / B.SLOTS, cash)
            if val < 0.25 * eqv / B.SLOTS: continue
            q = val / px / (1 + B.cost("BUY", val, px)); cash -= val
            a = ATR[i - 1, j]
            hold[s] = {"q": q, "px": px, "d": i, "stop": (px - stop_atr * a) if stop_atr and a == a else None}
        pend_buy, pend_sell = [], []
        for s in list(hold):
            h, j = hold[s], col[s]
            if not h["stop"] or L[i, j] != L[i, j]: continue
            hit = (L[i, j] <= h["stop"]) if stop_mode == "touch" else (C[i, j] <= h["stop"])
            if not hit: continue
            px = (min(O[i, j], h["stop"]) if O[i, j] == O[i, j] else h["stop"]) if stop_mode == "touch" else C[i, j]
            px *= (1 - B.SLIP); val = h["q"] * px; cash += val * (1 - B.cost("SELL", val, px))
            trades.append({"sym": s, "in": h["d"], "out": i, "ret": px / h["px"] - 1, "why": "stop"}); hold.pop(s)
        val = cash + sum(h["q"] * (C[i, col[s]] if C[i, col[s]] == C[i, col[s]] else h["px"]) for s, h in hold.items())
        eq.append(val)
        rk = ranks.get(i, {})
        for s in hold:
            r = rk.get(s)
            if r is None or r > B.EXIT: pend_sell.append((s, "rank"))
        free = B.SLOTS - (len(hold) - len(pend_sell))
        if free > 0:
            secs, inds = {}, {}
            if sector_caps:
                for s in hold:
                    if (s, "rank") in pend_sell: continue
                    f = sector_caps["map"].get(s, {}); secs[f.get("sector")] = secs.get(f.get("sector"), 0) + 1; inds[f.get("industry")] = inds.get(f.get("industry"), 0) + 1
            for s in sorted((s for s, r in rk.items() if r <= B.ENTRY and s not in hold), key=rk.get):
                if free <= 0: break
                if skip and getattr(skip, "at_close", None) and skip.at_close(i, col[s]): continue      # stretched at the close: next rank instead
                if sector_caps:
                    f = sector_caps["map"].get(s, {}); sc, ind = f.get("sector"), f.get("industry")
                    if sc and secs.get(sc, 0) >= sector_caps["broad"]: continue
                    if ind and inds.get(ind, 0) >= sector_caps["industry"]: continue
                    secs[sc] = secs.get(sc, 0) + 1; inds[ind] = inds.get(ind, 0) + 1
                pend_buy.append(s); free -= 1
    return pd.Series(eq, index=idx[first:]), trades, skipped


def mom_stats(eq, trades, idx):
    out = {}
    for name, (a, b) in (("2011-2019", P1), ("2020-2026", P2)):
        s = B.stats(eq, trades, idx, pd.Timestamp(a), pd.Timestamp(b)) or {}
        out[name] = {k: s.get(k) for k in ("cagr", "maxdd", "trades", "win")}
    return out


# ------------------------------------------------------------------ bull flags with variants
def flags(idx, syms, P, R, bench):
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    trh = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0)))); ATR = pd.DataFrame(trh).rolling(14, min_periods=10).mean().to_numpy()
    TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy(); lvl = bench.reindex(idx).ffill().bfill().to_numpy()
    up = (2.5 * 1.5 - 2.0 * 1.5) / 3.0; s0 = idx.searchsorted(pd.Timestamp("2011-01-01")); rows = []

    def net(fill, ex, stop):
        fc, ec = fill * (1 + B.SLIP), ex * (1 - B.SLIP)
        return (ec * (1 - B.cost("SELL", 100000, ec)) - fc * (1 + B.cost("BUY", 100000, fc))) / (fill - stop)

    def walk(o, h, l, c, f0, fill, stop, tgt, mode):
        for u in range(f0, min(f0 + 15, len(idx))):
            oo = fill if u == f0 else o[u]
            if oo != oo: continue
            if u > f0 and oo >= tgt: return oo, u, "target"
            if mode == "touch":
                if u > f0 and oo <= stop: return oo, u, "stop"
                if l[u] <= stop: return stop, u, "stop"
            if h[u] >= tgt and u != f0: return tgt, u, "target"
            if mode == "close" and c[u] <= stop: return c[u], u, "stop"
            if mode == "hybrid":                     # emergency stop: a touch 1.5x the risk below the fill; otherwise the close rule
                hard = fill - 1.5 * (fill - stop)
                if u > f0 and oo <= hard: return oo, u, "stop"
                if l[u] <= hard: return hard, u, "stop"
            if mode in ("close_open", "hybrid") and c[u] <= stop and u + 1 < len(idx) and o[u + 1] == o[u + 1]: return o[u + 1], u + 1, "stop"
        xd = min(f0 + 14, len(idx) - 1); return c[xd], xd, "time"

    for j, sym in enumerate(syms):
        c, h, l, o, a = C[:, j], H[:, j], L[:, j], O[:, j], ATR[:, j]; last = -99
        for t in range(max(s0, 60), len(idx) - 17):
            if t - last < 15 or c[t] != c[t] or a[t] != a[t] or c[t] < 50 or a[t] / c[t] > 0.07: continue
            if not TV[t, j] == TV[t, j] or TV[t, j] < core.MIN_TURNOVER_CR * 1e7 * lvl[t] / lvl[-1]: continue
            rk = R.get(t, {}).get(sym)
            if rk is not None and rk <= B.ENTRY: continue
            kk = None
            for k in range(3, 11):
                rng = np.nanmax(h[t - k + 1:t + 1]) - np.nanmin(l[t - k + 1:t + 1]); b0 = t - k
                if rng < 1.5 * a[t] and c[b0] / c[b0 - 10] - 1 >= 0.15 and c[t] > np.nanmean(c[t - 19:t + 1]): kk = k; break
            if kk is None: continue
            last = t; row = {"sym": sym, "day": str(idx[t].date())}
            stop, tgt = c[t] - 1.5 * a[t], c[t] + 2.5 * 1.5 * a[t]; lo, hi = c[t] - 0.4 * a[t], c[t] + up * a[t]
            # rule entry: the entry range within 3 sessions, re-checked for 2:1 at the fill
            fill = f0 = None
            for u in range(t + 1, t + 4):
                if o[u] != o[u] or o[u] <= stop: break
                if l[u] <= hi: fill, f0 = (min(o[u], hi) if o[u] >= lo else o[u]), u; break
            if fill is not None and fill > stop and (tgt - fill) / (fill - stop) >= 2.0:
                for mode in ("touch", "close", "close_open", "hybrid"):
                    ex, xd, kind = walk(o, h, l, c, f0, fill, stop, tgt, mode)
                    row[f"range_{mode}"] = net(fill, ex, stop); row[f"range_{mode}_kind"] = kind
                row["exit_day"] = str(idx[walk(o, h, l, c, f0, fill, stop, tgt, "touch")[1]].date()); row["exit_px"] = walk(o, h, l, c, f0, fill, stop, tgt, "touch")[0]
            # breakout entry: buy-stop just above the flag high
            fh = float(np.nanmax(h[t - kk + 1:t + 1])) * 1.001; fill = f0 = None
            for u in range(t + 1, t + 4):
                if o[u] != o[u] or l[u] <= stop and h[u] < fh: break
                if h[u] >= fh: fill, f0 = max(o[u], fh), u; break
            if fill is not None and fill > stop and (fill - stop) / fill <= 0.12:
                tg2 = fill + 2.5 * (fill - stop)
                for mode in ("touch", "close"):
                    ex, xd, kind = walk(o, h, l, c, f0, fill, stop, tg2, mode)
                    row[f"breakout_{mode}"] = net(fill, ex, stop)
            rows.append(row)
    return pd.DataFrame(rows)


def flag_stats(D):
    out = {}
    for name, (a, b) in (("2011-2019", P1), ("2020-2026", P2)):
        g = D[(D.day >= a) & (D.day <= b)]; o = {"signals": int(len(g))}
        for v in ("range_touch", "range_close", "range_close_open", "range_hybrid", "breakout_touch", "breakout_close"):
            x = g[v].dropna() if v in g else pd.Series(dtype=float)
            if len(x) < 30: o[v] = {"trades": int(len(x))}; continue
            se = x.std(ddof=1) / np.sqrt(len(x))
            o[v] = {"trades": int(len(x)), "avg_R": round(float(x.mean()), 3), "ci95": [round(float(x.mean() - 1.96 * se), 3), round(float(x.mean() + 1.96 * se), 3)],
                    "total_R": round(float(x.sum()), 1), "win_pct": round(100 * float((x > 0).mean()), 1)}
        both = g.dropna(subset=["range_touch", "range_close", "range_close_open", "range_hybrid"]) if "range_close" in g else g
        for v in ("range_close", "range_close_open", "range_hybrid"):
            if len(both) > 30:
                d = both[v] - both.range_touch; o[v + "_minus_touch_same_trades"] = {"avg_R": round(float(d.mean()), 3), "t": round(float(d.mean() / (d.std(ddof=1) / np.sqrt(len(d)))), 2)}
        out[name] = o
    return out


# ------------------------------------------------------------------ 4: time exits on 5-minute data
def time_exits(D, dirs):
    from swing_timing_study import load_bars, price_at
    T = D[(D.get("range_touch_kind") == "time")].dropna(subset=["exit_day"]) if "range_touch_kind" in D else D.iloc[:0]
    need = {}
    for r in T.itertuples(): need.setdefault(r.sym, set()).add(r.exit_day)
    bars = load_bars(dirs, need) if need else {}
    out = {}
    for how in ("09:30", "11:00", "13:00", "vwap_day"):
        for name, a, b in (("2014-2021", "2014-01-01", "2021-12-31"), ("2022-2026", "2022-01-01", "2026-12-31")):
            x = []
            for r in T.itertuples():
                if not (a <= r.exit_day <= b): continue
                bb = bars.get(r.sym, {}).get(r.exit_day)
                if bb is None: continue
                x.append((price_at(bb, how) / price_at(bb, "close") - 1) * 100)        # + = selling at this time beats the close by x %
            x = np.array(x)
            out.setdefault(how, {})[name] = ({"n": int(len(x)), "avg_pct": round(float(x.mean()), 3), "t": round(float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))), 2)}
                                              if len(x) >= 20 else {"n": int(len(x))})
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default="daily_cache.pkl"); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--out", default="combo_results")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(exist_ok=True)
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    data = pickle.load(open(a.cache, "rb")) if Path(a.cache).exists() else B.download(syms, a.cache)
    idx, syms, P = B.panel(data)
    bench = data["^CRSLDX"]["close"] if data.get("^CRSLDX") is not None else data["^NSEI"]["close"]
    R = B.daily_ranks(idx, syms, P, bench=bench); print("ranks done", flush=True)
    funds = json.loads((HERE.parent / "web" / "fundamentals.json").read_text()).get("stocks", {})
    caps = {"map": funds, "broad": 6, "industry": 3}
    C = P["close"].to_numpy(); SMA50 = P["close"].rolling(50).mean().to_numpy()

    def stretched(i, j):                       # at the close of day i
        return C[i, j] == C[i, j] and SMA50[i, j] == SMA50[i, j] and (C[i, j] / SMA50[i, j] - 1 > 0.25 or (i >= 5 and C[i, j] / C[i - 5, j] - 1 > 0.20))
    class SkipAll:                              # never buy while stretched: the next rank gets the slot
        at_close = staticmethod(stretched)
        def __call__(self, i, j, o): return False
    class SkipGap:                              # skip only a stretched stock that also opens > 2% higher
        def __call__(self, i, j, o): return stretched(i - 1, j) and C[i - 1, j] == C[i - 1, j] and o / C[i - 1, j] - 1 > 0.02
    res = {"momentum": {}}
    for name, kw in (("A. live rule (touch stop)", {}), ("1. stop on the close", {"stop_mode": "close"}),
                     ("3a. skip stretched buys", {"skip": SkipAll()}), ("3b. skip stretched buys that gap up > 2%", {"skip": SkipGap()}),
                     ("5. skip stretched + stop on the close", {"skip": SkipAll(), "stop_mode": "close"})):
        eq, tr, sk = simulate(idx, syms, P, R, sector_caps=caps, **kw)
        res["momentum"][name] = {**mom_stats(eq, tr, idx), "stops": sum(1 for x in tr if x["why"] == "stop"), "skipped_at_open": sk}
        print(name, res["momentum"][name], flush=True)
    D = flags(idx, syms, P, R, bench); D.to_csv(out / "flag_variants.csv", index=False)
    res["bull_flag"] = flag_stats(D); print(json.dumps(res["bull_flag"], indent=1), flush=True)
    if a.old and a.new: res["bull_flag_time_exits"] = time_exits(D, [a.old, a.new]); print(json.dumps(res["bull_flag_time_exits"]), flush=True)
    (out / "combo_summary.json").write_text(json.dumps(res, indent=1))
    L = ["# Five untested rule changes", "", "## Momentum (portfolio, after costs)", "", "| variant | 2011-19 CAGR / max fall | 2020-26 CAGR / max fall | stops |", "|---|---|---|---|"]
    for k, v in res["momentum"].items():
        L.append(f"| {k} | {v['2011-2019']['cagr']}% / {v['2011-2019']['maxdd']}% | {v['2020-2026']['cagr']}% / {v['2020-2026']['maxdd']}% | {v['stops']} |")
    L += ["", "## Bull flag (net R a trade)", "", "| variant | 2011-19 | 2020-26 |", "|---|---|---|"]
    for v in ("range_touch", "range_close", "range_close_open", "range_hybrid", "breakout_touch", "breakout_close"):
        f = lambda o: f"{o[v].get('avg_R')} ({o[v].get('ci95')}, {o[v].get('trades')} trades, total {o[v].get('total_R')}R)"
        L.append(f"| {v} | {f(res['bull_flag']['2011-2019'])} | {f(res['bull_flag']['2020-2026'])} |")
    if "bull_flag_time_exits" in res:
        L += ["", "## Bull flag time exits vs the close (%, + = better)", ""] + [f"- {k}: {json.dumps(v)}" for k, v in res["bull_flag_time_exits"].items()]
    (out / "combo_summary.md").write_text("\n".join(L)); print("\n".join(L))
