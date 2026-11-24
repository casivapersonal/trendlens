"""Widely used futures trading rules, long and short, tested on NSE stock futures after futures costs (Nov 2026).

A fixed list (nothing is searched): a rule counts only if its average result is positive after costs in BOTH periods.
INTRADAY (5-minute bars of F&O stocks; 2014-2021 archive and 2022-2026 built from 1-minute data; one trade per stock per rule a
day; square-off 15:15; within a bar the stop comes before the target; fill-bar target not counted):
  orb15 / orb30    opening-range breakout (9:15-9:30 / 9:15-9:45 range), stop = other side of the range
  pdhl             break of the previous day's high (long) / low (short), stop 0.5 ATR
  gap_go           gap 1.5-8% and the first 15 minutes in the gap's direction: enter 9:30 with the gap, stop = 9:15-9:30 extreme
  gap_fade         gap 1.5-8%, enter 9:20 against the gap, stop 1 ATR  (the live exhaustion short adds a run-up filter)
  vwap_trend       10:00, price above VWAP and VWAP rising -> long (mirror short), stop 0.5 ATR
  vwap_revert      after 10:00, a 5-minute close 0.75 ATR away from VWAP -> fade it, target VWAP, stop 0.5 ATR beyond
  ema_cross        9/21 EMA cross on 5-minute bars after 9:45, stop 0.5 ATR
  supertrend       Supertrend (10, 3) flip on 5-minute bars after 9:45, stop = the Supertrend line
  pivot_r1s1       classic floor pivot: break of R1 long / S1 short, stop = pivot P
  camarilla_h4l4   Camarilla H4 breakout long / L4 breakdown short, stop 0.5 ATR
  rsi_extreme      RSI(14) 5-minute below 20 -> long, above 80 -> short (after 10:00), stop 0.5 ATR
  inside_day       yesterday inside the day before: break of yesterday's high / low, stop = the other side
  nr7_orb          yesterday the narrowest range of 7 days: 15-minute opening-range breakout
  Exits per rule: 2R target or hold to 15:15 (and VWAP for vwap_revert).
SWING (daily bars 2011-2026, NIFTY 500 stocks that are in F&O today; enter at the next open; futures costs per round trip plus
one roll cost per 21 sessions held; 2011-2019 vs 2020-2026):
  donchian20 (exit 10-day opposite), donchian55 (exit 20-day), ma50_200 (cross, exit on the opposite cross), ema20_50,
  inside_bar (breakout, stop the other side, 2R or 10 days), nr7 (same), rsi2 (Connors: with the 200-day trend, RSI(2) < 10,
  exit close beyond the 5-day average), hi52 (52-week high / low breakout, exit 20-day opposite), boll (close outside the
  20,2 band, exit at the middle band), supertrend_d (10,3 flips, hold to the next flip), pullback20 (trend up, touch of the
  20 EMA, stop 1.5 ATR, 2R or 10 days) - long and short.
Costs: futures STT 0.05% on the sell, exchange, SEBI, stamp, Rs 20 brokerage, GST, slippage 0.05% a side (~0.17% round trip).
Results are net % of contract value and R (multiples of the stop distance) per trade, t-stat clustered by day (intraday) or
month (swing). Writes rules_summary.json / rules_summary.md."""
from __future__ import annotations

import argparse, json, pickle, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
from futures_study import cost_pct, bars_from, to5        # noqa: E402

COST = cost_pct("futures", 0.05)
SQ = 15 * 60 + 15


# ------------------------------------------------------------------ helpers
def ema(x, n):
    a = 2 / (n + 1); out = np.empty_like(x); out[0] = x[0]
    for i in range(1, len(x)): out[i] = a * x[i] + (1 - a) * out[i - 1]
    return out


def rsi(c, n=14):
    d = np.diff(c, prepend=c[0]); up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au, ad = ema(up, 2 * n - 1), ema(dn, 2 * n - 1)
    return 100 - 100 / (1 + au / np.where(ad == 0, 1e-12, ad))


def supertrend(h, l, c, n=10, m=3.0):
    tr = np.maximum(h - l, np.maximum(abs(h - np.r_[c[0], c[:-1]]), abs(l - np.r_[c[0], c[:-1]])))
    atr = pd.Series(tr).rolling(n, min_periods=1).mean().to_numpy(); mid = (h + l) / 2
    ub, lb = mid + m * atr, mid - m * atr; fu, fl = ub.copy(), lb.copy(); d = np.ones(len(c)); line = np.empty(len(c))
    for i in range(len(c)):
        if i:
            fu[i] = ub[i] if (ub[i] < fu[i - 1] or c[i - 1] > fu[i - 1]) else fu[i - 1]
            fl[i] = lb[i] if (lb[i] > fl[i - 1] or c[i - 1] < fl[i - 1]) else fl[i - 1]
            d[i] = 1 if c[i] > fu[i - 1] else -1 if c[i] < fl[i - 1] else d[i - 1]
        line[i] = fl[i] if d[i] > 0 else fu[i]
    return d, line


def walk(o, h, l, e, side, entry, stop, tgt, last, fill_bar_stop=True):
    """Exit price from bar e (fill bar) to bar `last` (square-off bar, exit at its open)."""
    for j in range(e, last + 1):
        if j == last and j > e: return o[j]
        if side > 0:
            if (j > e or fill_bar_stop) and l[j] <= stop: return min(o[j], stop) if j > e else stop
            if j > e and tgt is not None and h[j] >= tgt: return max(o[j], tgt)
        else:
            if (j > e or fill_bar_stop) and h[j] >= stop: return max(o[j], stop) if j > e else stop
            if j > e and tgt is not None and l[j] <= tgt: return min(o[j], tgt)
    return o[last]


def rec(out, rule, side, day, entry, stop, exits):
    risk = abs(entry - stop)
    if risk <= 0 or risk / entry > 0.08: return
    for ex, px in exits.items():
        net = side * (px - entry) / entry * 100 - COST
        out.append((rule, "long" if side > 0 else "short", ex, day, net, net / (risk / entry * 100)))


# ------------------------------------------------------------------ intraday rules on one stock's 5-minute bars
def intraday_stock(path_five):
    path, five = path_five
    try:
        d = bars_from(Path(path))
        if not five: d = to5(d)
    except Exception:
        return []
    d["date"] = d.time.dt.normalize(); m = (d.time.dt.hour * 60 + d.time.dt.minute).to_numpy()
    o, h, l, c, v = (d[k].to_numpy(dtype=float) for k in ("open", "high", "low", "close", "volume"))
    starts = np.flatnonzero(np.r_[True, d.date.values[1:] != d.date.values[:-1]]); ends = np.r_[starts[1:], len(d)]
    # daily facts
    dO = o[starts]; dH = np.array([h[a:b].max() for a, b in zip(starts, ends)]); dL = np.array([l[a:b].min() for a, b in zip(starts, ends)])
    dC = c[ends - 1]; dV = np.array([v[a:b].sum() for a, b in zip(starts, ends)])
    pc = np.r_[np.nan, dC[:-1]]; tr = np.maximum(dH - dL, np.maximum(abs(dH - pc), abs(dL - pc)))
    atr = pd.Series(tr).rolling(14, min_periods=10).mean().shift(1).to_numpy()            # known before the day
    val20 = pd.Series(dC * dV).rolling(20, min_periods=15).mean().shift(1).to_numpy()
    rng = dH - dL; nr7 = pd.Series(rng).rolling(7).apply(lambda x: float(x[-1] == x.min()), raw=True).shift(1).to_numpy()
    IND = (ema(c, 9), ema(c, 21), supertrend(h, l, c), rsi(c))
    out = []
    for k in range(20, len(starts)):
        a, b = starts[k], ends[k]
        if b - a < 60 or m[a] != 555 or not (atr[k] > 0) or not (val20[k] >= 5e7) or not (dC[k - 1] >= 20): continue
        O, H, L, C, V, M = o[a:b], h[a:b], l[a:b], c[a:b], v[a:b], m[a:b]
        A = atr[k]; day = d.date.iloc[a]; P = dC[k - 1]
        last = int(np.searchsorted(M, SQ)); last = min(last, len(M) - 1)
        if last < 20: continue
        tp = (H + L + C) / 3; vw = np.cumsum(tp * V) / np.maximum(np.cumsum(V), 1)
        def go(rule, side, e, entry, stop, tgt2=True, extra=None):
            if e >= last: return
            ex = {"eod": walk(O, H, L, e, side, entry, stop, None, last)}
            if tgt2: ex["2R"] = walk(O, H, L, e, side, entry, stop, entry + side * 2 * abs(entry - stop), last)
            if extra: ex.update(extra)
            rec(out, rule, side, day, entry, stop, ex)
        def breakout(rule, hi, lo, after, stop_hi_side, stop_lo_side, until=14 * 60):
            for side, lvl, st in ((1, hi, stop_hi_side), (-1, lo, stop_lo_side)):
                idx = np.flatnonzero((M >= after) & (M < until) & ((H >= lvl) if side > 0 else (L <= lvl)))
                if len(idx):
                    e = int(idx[0]); entry = max(O[e], lvl) if side > 0 else min(O[e], lvl)
                    if (entry - st) * side > 0: go(rule, side, e, entry, st)
        # opening ranges
        for nm, mins in (("orb15", 570), ("orb30", 585)):
            q = M < mins; rh, rl = H[q].max(), L[q].min()
            breakout(nm, rh, rl, mins, rl, rh)
        q15 = M < 570; r15h, r15l = H[q15].max(), L[q15].min()
        if nr7[k] == 1: breakout("nr7_orb", r15h, r15l, 570, r15l, r15h)
        breakout("pdhl", dH[k - 1], dL[k - 1], 570, dH[k - 1] - 0.5 * A, dL[k - 1] + 0.5 * A)
        if dH[k - 1] <= dH[k - 2] and dL[k - 1] >= dL[k - 2]: breakout("inside_day", dH[k - 1], dL[k - 1], 555, dL[k - 1], dH[k - 1])
        pv = (dH[k - 1] + dL[k - 1] + dC[k - 1]) / 3; r1, s1 = 2 * pv - dL[k - 1], 2 * pv - dH[k - 1]
        breakout("pivot_r1s1", r1, s1, 570, pv, pv)
        cr = dH[k - 1] - dL[k - 1]; h4, l4 = dC[k - 1] + cr * 1.1 / 2, dC[k - 1] - cr * 1.1 / 2
        breakout("camarilla_h4l4", h4, l4, 570, h4 - 0.5 * A, l4 + 0.5 * A)
        # gaps
        gap = O[0] / P - 1
        if 0.015 <= abs(gap) <= 0.08:
            sd = 1 if gap > 0 else -1
            e20 = int(np.searchsorted(M, 560)); go("gap_fade", -sd, e20, O[e20], O[e20] + sd * A)
            e30 = int(np.searchsorted(M, 570))
            if (C[e30 - 1] - O[0]) * sd > 0: go("gap_go", sd, e30, O[e30], (L[:e30].min() if sd > 0 else H[:e30].max()))
        # VWAP trend at 10:00
        e10 = int(np.searchsorted(M, 600))
        if e10 < last:
            i30 = int(np.searchsorted(M, 570))
            if C[e10 - 1] > vw[e10 - 1] > vw[i30]: go("vwap_trend", 1, e10, O[e10], O[e10] - 0.5 * A)
            if C[e10 - 1] < vw[e10 - 1] < vw[i30]: go("vwap_trend", -1, e10, O[e10], O[e10] + 0.5 * A)
            dev = (C - vw) / A
            for side, cond in ((-1, dev > 0.75), (1, dev < -0.75)):
                idx = np.flatnonzero(cond & (M >= 600) & (M < 14 * 60))
                if len(idx) and idx[0] + 1 < last:
                    e = int(idx[0]) + 1; entry = O[e]; stop = entry - side * 0.5 * A
                    tgtv = vw[e - 1]
                    ex = {"vwap": walk(O, H, L, e, side, entry, stop, tgtv, last) if (tgtv - entry) * side > 0 else O[e]}
                    go("vwap_revert", side, e, entry, stop, extra=ex)
        # EMA cross, Supertrend, RSI on 5-minute bars (indicators run across days)
        _intraday_indicators(out, (a, b, M, O, H, L, last, A, day), IND, go)
    return out


def _intraday_indicators(out, kk, IND, go):
    a, b, M, O, H, L, last, A, day = kk
    e9, e21, (sd, sl), r = IND[0][a:b], IND[1][a:b], (IND[2][0][a:b], IND[2][1][a:b]), IND[3][a:b]
    cross = np.sign(e9 - e21); flips = np.flatnonzero((np.r_[cross[0], cross[:-1]] != cross) & (M >= 585) & (M < 14 * 60))
    if len(flips) and flips[0] + 1 < last:
        e = int(flips[0]) + 1; s = int(cross[e - 1]); go("ema_cross", s, e, O[e], O[e] - s * 0.5 * A)
    fl = np.flatnonzero((np.r_[sd[0], sd[:-1]] != sd) & (M >= 585) & (M < 14 * 60))
    if len(fl) and fl[0] + 1 < last:
        e = int(fl[0]) + 1; s = int(sd[e - 1])
        if (O[e] - sl[e - 1]) * s > 0: go("supertrend", s, e, O[e], sl[e - 1])
    for s, cond in ((1, r < 20), (-1, r > 80)):
        idx = np.flatnonzero(cond & (M >= 600) & (M < 14 * 60))
        if len(idx) and idx[0] + 1 < last:
            e = int(idx[0]) + 1; go("rsi_extreme", s, e, O[e], O[e] - s * 0.5 * A)


# ------------------------------------------------------------------ swing rules on daily bars
def swing_stock(args):
    sym, df = args; out = []
    df = df.dropna(subset=["open", "high", "low", "close"])
    if len(df) < 260: return out
    o, h, l, c = (df[k].to_numpy(float) for k in ("open", "high", "low", "close")); idx = df.index
    pc = np.r_[c[0], c[:-1]]; tr = np.maximum(h - l, np.maximum(abs(h - pc), abs(l - pc))); A = pd.Series(tr).rolling(14).mean().to_numpy()
    s5, s50, s200 = (pd.Series(c).rolling(n).mean().to_numpy() for n in (5, 50, 200)); e20, e50 = ema(c, 20), ema(c, 50)
    r2 = rsi(c, 2); hi = lambda n: pd.Series(h).rolling(n).max().to_numpy(); lo = lambda n: pd.Series(l).rolling(n).min().to_numpy()
    h20, l20, h55, l55, h10, l10, h252, l252 = hi(20), lo(20), hi(55), lo(55), hi(10), lo(10), hi(252), lo(252)
    sd_, _ = supertrend(h, l, c); bm = pd.Series(c).rolling(20).mean().to_numpy(); bs = pd.Series(c).rolling(20).std().to_numpy()
    rngs = h - l; nr7 = pd.Series(rngs).rolling(7).apply(lambda x: float(x[-1] == x.min()), raw=True).to_numpy()
    n = len(c)

    def trade(rule, side, i, exit_fn, stop=None, tgt=None, maxd=250):
        """Signal at close i, enter open i+1; exit_fn(j) True at close j -> exit next open; stop / target intraday."""
        e = i + 1
        if e >= n - 1: return n
        entry = o[e]
        if stop is not None and (entry - stop) * side <= 0: return e
        for j in range(e, min(n - 1, e + maxd)):
            if stop is not None and ((l[j] <= stop) if side > 0 else (h[j] >= stop)):
                px = (min(o[j], stop) if side > 0 else max(o[j], stop)) if j > e else stop; break
            if tgt is not None and j > e and ((h[j] >= tgt) if side > 0 else (l[j] <= tgt)):
                px = max(o[j], tgt) if side > 0 else min(o[j], tgt); break
            if exit_fn(j): px = o[j + 1]; j += 1; break
        else:
            j = min(n - 1, e + maxd); px = o[j]
        held = j - e; net = side * (px - entry) / entry * 100 - COST * (1 + held // 21)
        risk = abs(entry - stop) / entry * 100 if stop is not None else 2 * A[i] / entry * 100
        out.append((rule, "long" if side > 0 else "short", "rule", idx[i], net, net / risk if risk > 0 else np.nan))
        return j

    for side in (1, -1):
        L_ = side > 0
        nxt = {}
        for i in range(252, n - 2):
            if not A[i] > 0: continue
            def sig(rule, cond, exit_fn, stop=None, tgt=None, maxd=250):
                if cond and i >= nxt.get(rule, 0): nxt[rule] = trade(rule, side, i, exit_fn, stop, tgt, maxd)
            sig("donchian20", c[i] > h20[i - 1] if L_ else c[i] < l20[i - 1], lambda j: c[j] < l10[j - 1] if L_ else c[j] > h10[j - 1], c[i] - side * 2 * A[i])
            sig("donchian55", c[i] > h55[i - 1] if L_ else c[i] < l55[i - 1], lambda j: c[j] < l20[j - 1] if L_ else c[j] > h20[j - 1], c[i] - side * 2 * A[i])
            sig("ma50_200", (s50[i] > s200[i] and s50[i - 1] <= s200[i - 1]) if L_ else (s50[i] < s200[i] and s50[i - 1] >= s200[i - 1]),
                lambda j: (s50[j] < s200[j]) if L_ else (s50[j] > s200[j]), c[i] - side * 3 * A[i])
            sig("ema20_50", (e20[i] > e50[i] and e20[i - 1] <= e50[i - 1]) if L_ else (e20[i] < e50[i] and e20[i - 1] >= e50[i - 1]),
                lambda j: (e20[j] < e50[j]) if L_ else (e20[j] > e50[j]), c[i] - side * 3 * A[i])
            for nm, cond in (("inside_bar", h[i] <= h[i - 1] and l[i] >= l[i - 1]), ("nr7", nr7[i] == 1)):
                if cond and i >= nxt.get(nm, 0) and i + 1 < n - 1:
                    lvl, st = (h[i], l[i]) if L_ else (l[i], h[i])
                    if (h[i + 1] >= lvl) if L_ else (l[i + 1] <= lvl):        # breakout next day: fill at the level (or the open)
                        e = i + 1; entry = max(o[e], lvl) if L_ else min(o[e], lvl); risk = abs(entry - st)
                        if risk > 0:
                            tg = entry + side * 2 * risk; px = None
                            for j in range(e, min(n - 1, e + 10)):
                                if (l[j] <= st) if L_ else (h[j] >= st): px = (min(o[j], st) if L_ else max(o[j], st)) if j > e else st; break
                                if j > e and ((h[j] >= tg) if L_ else (l[j] <= tg)): px = max(o[j], tg) if L_ else min(o[j], tg); break
                            if px is None: j = min(n - 1, e + 10); px = c[j]
                            net = side * (px - entry) / entry * 100 - COST
                            out.append((nm, "long" if L_ else "short", "2R/10d", idx[i], net, net / (risk / entry * 100))); nxt[nm] = j
            sig("rsi2", (c[i] > s200[i] and r2[i] < 10) if L_ else (c[i] < s200[i] and r2[i] > 90), lambda j: c[j] > s5[j] if L_ else c[j] < s5[j],
                c[i] - side * 3 * A[i], maxd=20)
            sig("hi52", c[i] > h252[i - 1] if L_ else c[i] < l252[i - 1], lambda j: c[j] < l20[j - 1] if L_ else c[j] > h20[j - 1], c[i] - side * 3 * A[i])
            sig("boll", c[i] > bm[i] + 2 * bs[i] if L_ else c[i] < bm[i] - 2 * bs[i], lambda j: c[j] < bm[j] if L_ else c[j] > bm[j], c[i] - side * 2 * A[i])
            sig("supertrend_d", sd_[i] == side and sd_[i - 1] != side, lambda j: sd_[j] != side, c[i] - side * 3 * A[i])
            sig("pullback20", ((c[i] > s50[i] and s50[i] > s200[i] and l[i] <= e20[i] < c[i]) if L_ else (c[i] < s50[i] and s50[i] < s200[i] and h[i] >= e20[i] > c[i])),
                lambda j: False, c[i] - side * 1.5 * A[i], c[i] + side * 3 * A[i], maxd=10)
    return out


# ------------------------------------------------------------------ summary
def summarise(T: pd.DataFrame, periods, cluster) -> dict:
    res = {}
    for (rule, side, ex), g in T.groupby(["rule", "side", "exit"]):
        row = {}
        for name, a, b in periods:
            x = g[(g.day >= a) & (g.day <= b)]
            if len(x) < 30: row[name] = {"n": int(len(x))}; continue
            cl = x.groupby(x.day.dt.to_period(cluster)).net.mean(); se = cl.std(ddof=1) / np.sqrt(len(cl))
            yr = x.groupby(x.day.dt.year).net.mean()
            row[name] = {"n": int(len(x)), "net_pct": round(float(x.net.mean()), 3), "avg_R": round(float(x.R.mean()), 3), "win": round(float((x.net > 0).mean() * 100), 1),
                         "t": round(float(cl.mean() / se), 2) if se > 0 else None, "years_pos": f"{int((yr > 0).sum())}/{len(yr)}"}
        ok = all(row.get(p[0], {}).get("net_pct", -1) > 0 for p in periods)
        strong = ok and all((row[p[0]].get("t") or 0) >= 2 for p in periods)
        row["verdict"] = "PASS (both periods, t >= 2)" if strong else "positive both, weak" if ok else "fails"
        res[f"{rule} | {side} | {ex}"] = row
    return res


def md(title, res, periods):
    L = [f"## {title}", "", "| rule | side | exit | " + " | ".join(f"{p[0]} net % / R / t / n / years+" for p in periods) + " | verdict |", "|---" * (4 + len(periods)) + "|"]
    order = sorted(res.items(), key=lambda kv: (kv[1]["verdict"] != "PASS (both periods, t >= 2)", kv[1]["verdict"] != "positive both, weak", kv[0]))
    for k, row in order:
        cells = []
        for p in periods:
            x = row.get(p[0], {}); cells.append(f"{x.get('net_pct', '-')} / {x.get('avg_R', '-')} / {x.get('t', '-')} / {x.get('n', 0)} / {x.get('years_pos', '-')}")
        r, s, e = k.split(" | "); L.append(f"| {r} | {s} | {e} | " + " | ".join(cells) + f" | {row['verdict']} |")
    return L + [""]


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--old"); ap.add_argument("--minutes"); ap.add_argument("--cache", default="daily_cache.pkl")
    ap.add_argument("--out", default="rules_results"); a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True); t0 = time.time()
    from intraday_exhaustion_test import fno_list
    fno, src = fno_list(); res = {"fno": len(fno), "fno_source": src, "cost_round_trip_pct": round(COST, 4)}
    rows = []
    jobs = []
    if a.old: jobs += [(str(f), True) for f in sorted(Path(a.old).glob("*.csv.gz"))]
    if a.minutes: jobs += [(str(f), False) for f in sorted(Path(a.minutes).glob("*.parquet"))]
    with ProcessPoolExecutor() as ex:
        for k, r in enumerate(ex.map(intraday_stock, jobs, chunksize=2)):
            rows += r
            if k % 50 == 0: print(f"intraday {k}/{len(jobs)} files, {len(rows):,} trades ({(time.time() - t0) / 60:.1f} min)", flush=True)
    T = pd.DataFrame(rows, columns=["rule", "side", "exit", "day", "net", "R"]); T["day"] = pd.to_datetime(T.day)
    T.to_parquet(out / "intraday_trades.parquet")
    P1 = [("2014-2021", "2014-01-01", "2021-12-31"), ("2022-2026", "2022-01-01", "2026-12-31")]
    res["intraday"] = summarise(T, P1, "D")
    import backtest as B
    syms = sorted({s.strip() for s in open(HERE / "nifty500.txt") if s.strip()} & fno)
    data = pickle.load(open(a.cache, "rb")) if Path(a.cache).exists() else B.download(syms, a.cache)
    frames = [(s, data[s]) for s in syms if data.get(s) is not None]
    rows = []
    with ProcessPoolExecutor() as ex:
        for r in ex.map(swing_stock, frames, chunksize=4): rows += r
    S = pd.DataFrame(rows, columns=["rule", "side", "exit", "day", "net", "R"]); S["day"] = pd.to_datetime(S.day).dt.tz_localize(None)
    S.to_parquet(out / "swing_trades.parquet")
    P2 = [("2011-2019", "2011-01-01", "2019-12-31"), ("2020-2026", "2020-01-01", "2026-12-31")]
    res["swing"] = summarise(S, P2, "M")
    (out / "rules_summary.json").write_text(json.dumps(res, indent=1, default=str))
    L = ["# Widely used futures rules on NSE stock futures (after futures costs)", "",
         f"F&O stocks {len(fno)} ({src}); round-trip cost {COST:.3f}% of contract value. PASS = positive after costs in both periods with t >= 2 in each.", ""]
    L += md("Intraday (5-minute bars)", res["intraday"], P1) + md("Swing (daily bars)", res["swing"], P2)
    (out / "rules_summary.md").write_text("\n".join(L)); print("\n".join(L), flush=True)
    print(f"done in {(time.time() - t0) / 60:.1f} min")


if __name__ == "__main__":
    main()
