"""Intraday research on years of 5-minute bars (Angel One files from pipeline/angel_download.py: SYMBOL.csv.gz, NIFTY50.csv.gz).

  python pipeline/intraday_research.py --data intraday_data --out intraday_results

One pass over every stock-day builds every candidate trade (long and short) for four setups, tags it with what was known at
entry, and records the result after Indian intraday costs (brokerage Rs 20 cap, STT, exchange, GST, stamp) and 0.05% slippage
each side. Then: results by setup, by year, by factor, and every one- or two-factor group, kept only if it made money in
(nearly) every year. Setups:
  orb      : 15-minute opening range breakout (long above the high / short below the low), stop at the other side
  orb5_sip : 5-minute opening range on Stocks in Play (top 20 by first-5-minute relative volume), direction of the first candle,
             stop 10% of daily ATR (as published by Zarattini, Barbon and Aziz) and a wider 25% version
  vwap     : pullback to VWAP in the direction of the day after 10:00, stop beyond the pullback bar
  gapfade  : fade a gap of more than 1.5% when the first 15 minutes move back toward the previous close
All exits by 15:15 at the latest. Writes summary.json, summary.md and trades.csv.gz."""
from __future__ import annotations

import argparse, gzip, itertools, json, sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import core                                  # noqa: E402
from arena import charges                    # noqa: E402

SLIP, NOTIONAL, SLOTS, EXIT_BAR = 0.0005, 100_000, 75, 72


def cost_frac(px):
    q = max(1, int(NOTIONAL / px))
    return (charges.compute("BUY", q, px, "intraday").total + charges.compute("SELL", q, px, "intraday").total) / (q * px)


def load(path):
    x = pd.read_csv(path)
    t = pd.to_datetime(x["time"].astype(str).str.slice(0, 19))
    x.index = t; x = x.drop(columns=["time"]).astype(float)
    x = x[(x.index.time >= pd.Timestamp("09:15").time()) & (x.index.time <= pd.Timestamp("15:25").time())]
    per = {}
    for d, g in x.groupby(x.index.normalize()):
        if len(g) < 70: continue
        slot = ((g.index.hour * 60 + g.index.minute) - 555) // 5
        a = np.full((5, SLOTS), np.nan, dtype=np.float32)
        for k, col in enumerate(("open", "high", "low", "close", "volume")): a[k, slot] = g[col].values
        c = pd.Series(a[3]).ffill().bfill().values
        for k in range(3): a[k] = np.where(np.isnan(a[k]), c, a[k])
        a[3] = c; a[4] = np.nan_to_num(a[4])
        per[d.date()] = a
    return per


def exit_walk(o, h, l, e, entry, stop, long, tgt=None):
    for j in range(e, EXIT_BAR):
        if long:
            if j > e and o[j] <= stop: return o[j]
            if l[j] <= stop: return stop
            if tgt and h[j] >= tgt: return max(o[j], tgt) if j > e else tgt
        else:
            if j > e and o[j] >= stop: return o[j]
            if h[j] >= stop: return stop
            if tgt and l[j] <= tgt: return min(o[j], tgt) if j > e else tgt
    return o[EXIT_BAR]


def trade(sym, day, setup, long, o, h, l, e, entry_raw, stop, feats, tgt_r=None):
    entry = entry_raw * (1 + SLIP if long else 1 - SLIP)
    risk = abs(entry - stop)
    if risk <= 0 or risk / entry > 0.04 or risk / entry < 0.001: return None
    tgt = (entry + tgt_r * risk if long else entry - tgt_r * risk) if tgt_r else None
    ex = exit_walk(o, h, l, e, entry, stop, long, tgt) * (1 - SLIP if long else 1 + SLIP)
    g = ex / entry - 1 if long else entry / ex - 1
    net = g - cost_frac(entry)
    return {"sym": sym, "day": day, "year": day.year, "setup": setup, "side": "long" if long else "short", "net": net * 100,
            "gross": (net + cost_frac(entry)) * 100, "r": net * entry / risk, **feats}


def research(D, out):
    syms = [s for s in D if s != "NIFTY50"]
    N = D.get("NIFTY50", {})
    # daily facts from the 5-minute bars themselves (known at the previous close)
    daily = pd.DataFrame({s: pd.Series({d: a[3][-1] for d, a in D[s].items()}) for s in syms}).sort_index()
    hi = pd.DataFrame({s: pd.Series({d: np.nanmax(a[1]) for d, a in D[s].items()}) for s in syms}).reindex(daily.index)
    lo = pd.DataFrame({s: pd.Series({d: np.nanmin(a[2]) for d, a in D[s].items()}) for s in syms}).reindex(daily.index)
    tr = np.maximum(hi - lo, np.maximum((hi - daily.shift(1)).abs(), (lo - daily.shift(1)).abs()))
    atr = tr.rolling(14, min_periods=10).mean().shift(1)
    sma20 = daily.rolling(20, min_periods=15).mean().shift(1); r5 = (daily / daily.shift(5) - 1).shift(1) * 100
    days = list(daily.index)
    ranks = {}
    C = daily.values
    for i, d in enumerate(days):
        if i < 253: continue
        sc = {}
        for j, s in enumerate(syms):
            col = C[i - 253:i, j]
            if col[-1] == col[-1]:
                r = core.momentum_score([None if x != x else float(x) for x in col[:-1]], float(col[-1]))
                if r: sc[s] = r[1]
        ranks[d] = {s: k + 1 for k, s in enumerate(sorted(sc, key=lambda s: -sc[s]))}
    rows = []
    for di, day in enumerate(days):
        if di < 20: continue
        prevd = days[di - 1]
        nd = N.get(day); npc = N[prevd][3][-1] if prevd in N else None
        ngap = (nd[0][0] / npc - 1) * 100 if nd is not None and npc else None
        sip = []
        for j, s in enumerate(syms):
            per = D[s]
            if day not in per or prevd not in per: continue
            o, h, l, c, v = per[day]; pc = per[prevd][3][-1]
            if not (pc > 0) or o[0] < 50: continue
            prev = [per[days[k]] for k in range(di - 14, di) if days[k] in per]
            if len(prev) < 10: continue
            tv = np.median([np.nansum(x[3] * x[4]) for x in prev])
            if tv < 1e8: continue
            vavg = np.mean([x[4] for x in prev], axis=0)
            A = atr[s].get(day)
            feats = {"gap": (o[0] / pc - 1) * 100, "ngap": ngap, "rk": ranks.get(day, {}).get(s, 999),
                     "trend": bool(pc > sma20[s].get(day, np.inf)), "r5": r5[s].get(day), "atrp": (A / pc * 100) if A == A else None,
                     "tvcr": tv / 1e7, "wd": pd.Timestamp(day).day_name()[:3]}
            vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1)
            # 1) 15-minute ORB, both sides
            orh, orl = h[:3].max(), l[:3].min()
            if 0.002 <= (orh - orl) / orh <= 0.03:
                for long in (True, False):
                    for i in range(3, 22):
                        if (long and c[i] > orh >= c[i - 1]) or (not long and c[i] < orl <= c[i - 1]):
                            f = {**feats, "t": i, "rv": float(v[:i + 1].sum() / max(vavg[:i + 1].sum(), 1)),
                                 "nvw": (1 if long else -1) * ((nd[3][i] / (np.cumsum((nd[1] + nd[2] + nd[3]) / 3) / np.arange(1, SLOTS + 1))[i] - 1) * 100) if nd is not None else None}
                            for R in (None, 2.0):
                                t = trade(s, day, "orb" + ("" if R is None else "_2R"), long, o, h, l, i + 1, o[i + 1], orl if long else orh, f, R)
                                if t: rows.append(t)
                            break
            # 2) VWAP pullback in the day's direction
            for i in range(9, 60):
                up = c[i] > pc and vw[i] > vw[i - 6]; dn = c[i] < pc and vw[i] < vw[i - 6]
                if up and l[i] <= vw[i] * 1.001 and c[i] > vw[i] and c[i - 1] > vw[i - 1]:
                    t = trade(s, day, "vwap", True, o, h, l, i + 1, o[i + 1], min(l[i], l[i - 1]) * 0.9995, {**feats, "t": i, "rv": float(v[:i + 1].sum() / max(vavg[:i + 1].sum(), 1)), "nvw": None}, 2.0)
                    if t: rows.append(t)
                    break
                if dn and h[i] >= vw[i] * 0.999 and c[i] < vw[i] and c[i - 1] < vw[i - 1]:
                    t = trade(s, day, "vwap", False, o, h, l, i + 1, o[i + 1], max(h[i], h[i - 1]) * 1.0005, {**feats, "t": i, "rv": float(v[:i + 1].sum() / max(vavg[:i + 1].sum(), 1)), "nvw": None}, 2.0)
                    if t: rows.append(t)
                    break
            # 3) gap fade: gap > 1.5% that starts filling in the first 15 minutes
            g0 = o[0] / pc - 1
            if abs(g0) > 0.015 and A == A:
                long = g0 < 0
                if (long and c[2] > o[0]) or (not long and c[2] < o[0]):
                    t = trade(s, day, "gapfade", long, o, h, l, 3, o[3], (l[:3].min() if long else h[:3].max()), {**feats, "t": 3, "rv": None, "nvw": None}, None)
                    if t: rows.append(t)
            # 4) Stocks in Play candidates (5-minute opening range)
            v0 = vavg[0]
            if v0 > 0 and c[0] != o[0] and A == A: sip.append((v[0] / v0, s, A, feats))
        sip.sort(reverse=True, key=lambda x: x[0])
        for rv, s, A, feats in [x for x in sip if x[0] >= 1][:20]:
            o, h, l, c, v = D[s][day]; long = c[0] > o[0]; lvl = h[0] if long else l[0]
            for j in range(1, EXIT_BAR - 6):
                if (long and h[j] > lvl) or (not long and l[j] < lvl):
                    px = max(o[j], lvl) if long else min(o[j], lvl)
                    for k, nm in ((0.10, "sip_10atr"), (0.25, "sip_25atr")):
                        t = trade(s, day, nm, long, o, h, l, j, px, px - k * A if long else px + k * A, {**feats, "t": j, "rv": rv, "nvw": None}, None)
                        if t: rows.append(t)
                    break
    T = pd.DataFrame(rows)
    out.mkdir(parents=True, exist_ok=True)
    with gzip.open(out / "trades.csv.gz", "wt") as f: T.to_csv(f, index=False)
    return T


def summarise(T, out):
    rng = np.random.default_rng(1)
    def st(x):
        p = x.net.values
        if len(p) < 30: return None
        ms = [float(np.mean(rng.choice(p, len(p)))) for _ in range(500)]
        by = x.groupby("year").net.mean()
        return {"n": int(len(p)), "net": round(float(p.mean()), 4), "ci95": [round(float(np.percentile(ms, 2.5)), 4), round(float(np.percentile(ms, 97.5)), 4)],
                "gross": round(float(x.gross.mean()), 4), "win": round(float((p > 0).mean() * 100), 1),
                "years_positive": f"{int((by > 0).sum())}/{len(by)}", "by_year": {int(k): round(float(v), 3) for k, v in by.items()}}
    S = {"period": [str(T.day.min()), str(T.day.max())], "trades": int(len(T)), "stocks": int(T.sym.nunique()), "setups": {}}
    for (su, sd), g in T.groupby(["setup", "side"]): S["setups"][f"{su} {sd}"] = st(g)
    B = {"t": pd.cut(T.t, [0, 6, 12, 22, 80]), "gap": pd.cut(T.gap, [-99, -1, -0.3, 0.3, 1, 99]), "ngap": pd.cut(T.ngap, [-99, -0.5, -0.15, 0.15, 0.5, 99]),
         "rk": pd.cut(T.rk, [0, 20, 50, 150, 999]), "trend": T.trend, "r5": pd.cut(T.r5, [-99, -5, -2, 0, 2, 5, 99]), "atrp": pd.cut(T.atrp, [0, 1.5, 2.5, 4, 99]),
         "tvcr": pd.cut(T.tvcr, [0, 30, 100, 300, 1e7]), "rv": pd.cut(T.rv, [0, 0.8, 1.2, 2, 3, 999]), "nvw": pd.cut(T.nvw, [-99, -0.1, 0.1, 99]), "wd": T.wd}
    groups = []
    for (su, sd), g in T.groupby(["setup", "side"]):
        for k in (1, 2):
            for cols in itertools.combinations(B, k):
                key = [B[c][g.index] for c in cols]
                for grp, x in g.groupby(key, observed=True):
                    if len(x) < 300: continue
                    by = x.groupby("year").net.mean()
                    if len(by) < 3: continue
                    groups.append({"setup": su, "side": sd, "factors": "+".join(cols), "bucket": str(grp), "n": int(len(x)), "net": round(float(x.net.mean()), 4),
                                   "years_pos": int((by > 0).sum()), "years": int(len(by)), "worst_year": round(float(by.min()), 3)})
    G = pd.DataFrame(groups)
    S["groups_tested"] = int(len(G))
    if len(G):
        G["share_pos_years"] = G.years_pos / G.years
        good = G[(G.share_pos_years >= 0.8) & (G.net > 0)].sort_values(["share_pos_years", "net"], ascending=False)
        S["consistent_groups"] = good.head(40).to_dict("records")
        S["chance_note"] = f"{len(good)} of {len(G)} groups made money in 80%+ of years; with no edge, about {round(len(G) * float((G.net > 0).mean()) * 0.1)} would by luck (rough)"
        G.to_csv(out / "groups.csv", index=False)
    (out / "summary.json").write_text(json.dumps(S, indent=1, default=str))
    L = [f"# Intraday research {S['period'][0]} to {S['period'][1]}", f"{S['trades']} trades, {S['stocks']} stocks, after costs and slippage", "", "| setup | trades | net % / trade | 95% range | before costs | win % | years positive |", "|---|---|---|---|---|---|---|"]
    for k, v in S["setups"].items():
        if v: L.append(f"| {k} | {v['n']} | {v['net']:+.3f} | {v['ci95'][0]:+.3f} to {v['ci95'][1]:+.3f} | {v['gross']:+.3f} | {v['win']} | {v['years_positive']} |")
    L += ["", "Consistent groups (positive in 80%+ of years):", ""] + [f"- {g['setup']} {g['side']} | {g['factors']} = {g['bucket']} | n {g['n']} | {g['net']:+.3f}% | {g['years_pos']}/{g['years']} years | worst year {g['worst_year']:+.3f}" for g in S.get("consistent_groups", [])[:25]]
    L += ["", S.get("chance_note", "")]
    (out / "summary.md").write_text("\n".join(L))
    print("\n".join(L))


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--data", default="intraday_data"); ap.add_argument("--out", default="intraday_results"); a = ap.parse_args()
    D = {}
    for f in sorted(Path(a.data).glob("*.csv.gz")):
        try: D[f.name[:-7]] = load(f)
        except Exception as e: print("skip", f.name, e)
    print("loaded", len(D), "files")
    summarise(research(D, Path(a.out)), Path(a.out))
