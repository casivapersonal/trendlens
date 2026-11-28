"""Exit-rule check before any live change (Nov 2026). Every exit here is one a person can follow by hand: a close below the
stop is sold at the NEXT session's open (as the live bull-flag rule), never "at the close".
 MOMENTUM (full portfolio back-test as pipeline/combo_study.simulate: 20 slots, rank 20 in / 50 out, 6 ATR stop, sector caps,
   costs, 0.1% slippage): touch stop (live) vs close-then-next-open, plus sell-at-the-close as a reference only.
 BULL FLAG (every signal, net R after costs): close-then-next-open with an emergency stop at 1.5R, 2R, 3R (live) or none; and the
   touch stop. Differences against the live 3R rule are on the same trades, with t-stats.
 TIME EXIT: for live-rule ideas that reach the 15-session limit, selling at the day's VWAP (5-minute bars) vs the close.
Periods 2011-2019 and 2020-2026 (5-minute: 2014-2021 and 2022-2026). A change counts only if it helps in both periods.
Writes exit_check.json / exit_check.md and appends the table to the GitHub job summary."""
from __future__ import annotations
import argparse, json, os, pickle, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import backtest as B, core  # noqa: E402
import combo_study as CS  # noqa: E402

P1, P2 = ("2011-01-01", "2019-12-31"), ("2020-01-01", "2026-12-31")
HARD = (("live 3R", 3.0), ("1.5R", 1.5), ("2R", 2.0), ("no emergency stop", None))


def simulate(idx, syms, P, ranks, stop_mode="touch", stop_atr=6, sector_caps=None, start="2010-12-01"):
    """combo_study.simulate with stop_mode 'touch' (live), 'close' (sell at the close: reference only) or 'close_open'
    (a close below the stop is sold at the next open)."""
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    col = {s: j for j, s in enumerate(syms)}
    tr = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0))))
    ATR = pd.DataFrame(tr).rolling(14, min_periods=10).mean().to_numpy()
    cash, hold, trades, eq = 2_000_000, {}, [], []
    first = max(min(ranks), idx.searchsorted(pd.Timestamp(start)))
    pend_buy, pend_sell = [], []
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
            px *= (1 + B.SLIP); val = min(eqv / B.SLOTS, cash)
            if val < 0.25 * eqv / B.SLOTS: continue
            q = val / px / (1 + B.cost("BUY", val, px)); cash -= val
            a = ATR[i - 1, j]
            hold[s] = {"q": q, "px": px, "d": i, "stop": (px - stop_atr * a) if stop_atr and a == a else None}
        pend_buy, pend_sell = [], []
        for s in list(hold):
            h, j = hold[s], col[s]
            if not h["stop"] or L[i, j] != L[i, j]: continue
            if stop_mode == "touch" and L[i, j] <= h["stop"]:
                px = min(O[i, j], h["stop"]) if O[i, j] == O[i, j] else h["stop"]
            elif stop_mode == "close" and C[i, j] <= h["stop"]:
                px = C[i, j]
            elif stop_mode == "close_open" and C[i, j] <= h["stop"]:
                pend_sell.append((s, "stop")); continue                      # sold at tomorrow's open
            else:
                continue
            px *= (1 - B.SLIP); val = h["q"] * px; cash += val * (1 - B.cost("SELL", val, px))
            trades.append({"sym": s, "in": h["d"], "out": i, "ret": px / h["px"] - 1, "why": "stop"}); hold.pop(s)
        val = cash + sum(h["q"] * (C[i, col[s]] if C[i, col[s]] == C[i, col[s]] else h["px"]) for s, h in hold.items())
        eq.append(val)
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


def flags(idx, syms, P, R, bench):
    """Bull-flag signals exactly as combo_study.flags (range entry), with every exit variant on the same trades."""
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    trh = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0)))); ATR = pd.DataFrame(trh).rolling(14, min_periods=10).mean().to_numpy()
    TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy(); lvl = bench.reindex(idx).ffill().bfill().to_numpy()
    up = (2.5 * 1.5 - 2.0 * 1.5) / 3.0; s0 = idx.searchsorted(pd.Timestamp("2011-01-01")); rows = []

    def net(fill, ex, stop):
        fc, ec = fill * (1 + B.SLIP), ex * (1 - B.SLIP)
        return (ec * (1 - B.cost("SELL", 100000, ec)) - fc * (1 + B.cost("BUY", 100000, fc))) / (fill - stop)

    def walk(o, h, l, c, f0, fill, stop, tgt, mode, k=None):     # -> (price, day index, kind)
        hard = fill - k * (fill - stop) if k else None
        for u in range(f0, min(f0 + 15, len(idx))):
            oo = fill if u == f0 else o[u]
            if oo != oo: continue
            if u > f0 and oo >= tgt: return oo, u, "target"
            if mode == "touch":
                if u > f0 and oo <= stop: return oo, u, "stop"
                if l[u] <= stop: return stop, u, "stop"
            else:
                if hard is not None:
                    if u > f0 and oo <= hard: return oo, u, "stop"
                    if l[u] <= hard: return hard, u, "stop"
            if h[u] >= tgt and u != f0: return tgt, u, "target"
            if mode == "close_open" and c[u] <= stop and u + 1 < len(idx) and o[u + 1] == o[u + 1]: return o[u + 1], u + 1, "stop"
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
            last = t
            stop, tgt = c[t] - 1.5 * a[t], c[t] + 2.5 * 1.5 * a[t]; lo, hi = c[t] - 0.4 * a[t], c[t] + up * a[t]
            fill = f0 = None
            for u in range(t + 1, t + 4):
                if o[u] != o[u] or o[u] <= stop: break
                if l[u] <= hi: fill, f0 = (min(o[u], hi) if o[u] >= lo else o[u]), u; break
            if fill is None or fill <= stop or (tgt - fill) / (fill - stop) < 2.0: continue
            row = {"sym": sym, "day": str(idx[t].date())}
            ex, xd, kind = walk(o, h, l, c, f0, fill, stop, tgt, "touch"); row["touch"] = net(fill, ex, stop)
            for name, k in HARD:
                ex, xd, kind = walk(o, h, l, c, f0, fill, stop, tgt, "close_open", k)
                row[name] = net(fill, ex, stop)
                if name == "live 3R": row["exit_day"] = str(idx[xd].date()); row["live_kind"] = kind
            rows.append(row)
    return pd.DataFrame(rows)


def tstat(x):
    x = np.asarray(x, float); return float(x.mean() / (x.std(ddof=1) / np.sqrt(len(x)))) if len(x) > 2 and x.std(ddof=1) > 0 else None


def flag_stats(D):
    out = {}
    for name, (a, b) in (("2011-2019", P1), ("2020-2026", P2)):
        g = D[(D.day >= a) & (D.day <= b)]; o = {"trades": int(len(g))}
        for v in ["touch"] + [n for n, _ in HARD]:
            x = g[v]; o[v] = {"avg_R": round(float(x.mean()), 3), "t": round(tstat(x) or 0, 2), "worst_R": round(float(x.min()), 2),
                              "share_below_-1.5R": round(100 * float((x < -1.5).mean()), 1)}
            if v != "live 3R":
                d = x - g["live 3R"]; o[v]["vs_live_R"] = round(float(d.mean()), 3); o[v]["vs_live_t"] = round(tstat(d) or 0, 2)
        out[name] = o
    return out


def time_exit(D, dirs):
    T = D[D.live_kind == "time"].rename(columns={"live_kind": "range_touch_kind"})
    return CS.time_exits(T, dirs)


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--cache", default="daily_cache.pkl"); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--out", default="exit_results")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(exist_ok=True)
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    data = pickle.load(open(a.cache, "rb")) if Path(a.cache).exists() else B.download(syms, a.cache)
    idx, syms, P = B.panel(data)
    bench = data["^CRSLDX"]["close"] if data.get("^CRSLDX") is not None else data["^NSEI"]["close"]
    R = B.daily_ranks(idx, syms, P, bench=bench); print("ranks done", flush=True)
    funds = json.loads((HERE.parent / "web" / "fundamentals.json").read_text()).get("stocks", {})
    caps = {"map": funds, "broad": 6, "industry": 3}
    res = {"momentum": {}}
    for name, mode in (("live: touch stop", "touch"), ("close below stop, sell next open", "close_open"), ("reference: sell at the close", "close")):
        eq, tr = simulate(idx, syms, P, R, stop_mode=mode, sector_caps=caps)
        res["momentum"][name] = {**CS.mom_stats(eq, tr, idx), "stops": sum(1 for x in tr if x["why"] == "stop")}
        print(name, res["momentum"][name], flush=True)
    D = flags(idx, syms, P, R, bench); D.to_csv(out / "flag_exits.csv", index=False)
    res["bull_flag"] = flag_stats(D); print(json.dumps(res["bull_flag"], indent=1), flush=True)
    if a.old and a.new: res["time_exit"] = time_exit(D, [a.old, a.new]); print(json.dumps(res["time_exit"]), flush=True)
    (out / "exit_check.json").write_text(json.dumps(res, indent=1))
    L = ["# Exit-rule check (all exits followable by hand)", "", "## Momentum portfolio (after costs)", "",
         "| stop rule | 2011-19 CAGR / max fall / win% | 2020-26 CAGR / max fall / win% | stop exits |", "|---|---|---|---|"]
    for k, v in res["momentum"].items():
        f = lambda p: f"{v[p]['cagr']}% / {v[p]['maxdd']}% / {v[p]['win']}%"
        L.append(f"| {k} | {f('2011-2019')} | {f('2020-2026')} | {v['stops']} |")
    L += ["", "## Bull flag: close below the stop sold at the next open, emergency stop variants (net R a trade)", "",
          "| variant | 2011-19 avg R (t) | vs live 3R (t) | worst R | % below -1.5R | 2020-26 avg R (t) | vs live 3R (t) | worst R | % below -1.5R |", "|---|---|---|---|---|---|---|---|---|"]
    for v in [n for n, _ in HARD] + ["touch"]:
        cells = []
        for p in ("2011-2019", "2020-2026"):
            o = res["bull_flag"][p][v]
            cells += [f"{o['avg_R']} ({o['t']})", f"{o.get('vs_live_R', '-')} ({o.get('vs_live_t', '-')})", str(o["worst_R"]), str(o["share_below_-1.5R"])]
        L.append(f"| {v} | " + " | ".join(cells) + " |")
    L += ["", f"Trades: {res['bull_flag']['2011-2019']['trades']} (2011-19), {res['bull_flag']['2020-2026']['trades']} (2020-26)."]
    if "time_exit" in res:
        L += ["", "## Live-rule time exits: selling at another time of the last day vs the close (% , + = better)", "", "| when | 2014-21 n / avg % / t | 2022-26 n / avg % / t |", "|---|---|---|"]
        for k, v in res["time_exit"].items():
            f = lambda o: f"{o.get('n')} / {o.get('avg_pct', '-')} / {o.get('t', '-')}"
            L.append(f"| {k} | {f(v.get('2014-2021', {}))} | {f(v.get('2022-2026', {}))} |")
    md = "\n".join(L); (out / "exit_check.md").write_text(md); print(md)
    if os.environ.get("GITHUB_STEP_SUMMARY"):
        with open(os.environ["GITHUB_STEP_SUMMARY"], "a") as fh: fh.write(md + "\n")
