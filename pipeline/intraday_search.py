"""Full combination search for intraday rules, chosen on one period and checked on another (walk-forward, no peeking).

  python pipeline/intraday_search.py --train hist_data --test hf_data --train-trades hist_res/trades.csv.gz \
         --test-trades hf_res/trades.csv.gz --out search_results

Part A - gap fade, every setting: gap size from 1/1.5/2/3% (up to 8%, so split days drop out), confirmation after 1/2/3
five-minute bars, stop at the opening low/high or 0.5/1 daily ATR, target gap fill / 1R / 2R / none, exit 12:00 / 13:30 /
15:15 (3x3x4x3 = 108 trade settings x 4 gap sizes), each also with every combination of up to three filters, including
signals from the other strategies (VWAP side, NIFTY moving the same way, early volume, 15-minute range break).
Part B - every other setup (ORB, Stocks in Play, VWAP pullback, z-score) from the research trade lists, with every
combination of up to three filters.
Selection uses the train period only: at least --min-n trades, profitable in 80%+ of train years, ranked by the lower
edge of the train mean (mean - 2 standard errors). The top --top rules are then measured on the test period, which they
have never seen. Costs: Indian intraday charges + 0.05% slippage each side, same as intraday_research.py."""
from __future__ import annotations

import argparse, itertools, json, sys, warnings
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
warnings.filterwarnings("ignore", category=RuntimeWarning)
sys.path.insert(0, str(HERE))
import intraday_research as R                       # noqa: E402  (load, cost_frac, SLIP, pool helpers)

CONF, STOPS, TGTS, EXITS = (1, 2, 3), ("orl", "atr05", "atr1"), ("fill", "1R", "2R", "none"), {"12:00": 33, "13:30": 51, "15:15": 72}
VARIANTS = [(k, s, t, x) for k in CONF for s in STOPS for t in TGTS for x in EXITS]
GAPS = (1.0, 1.5, 2.0, 3.0)
_S = {}


def walk(o, h, l, e, entry, stop, long, tgt, xb):
    for j in range(e, xb):
        if long:
            if j > e and o[j] <= stop: return o[j]
            if l[j] <= stop: return stop
            if tgt is not None and h[j] >= tgt: return max(o[j], tgt) if j > e else tgt
        else:
            if j > e and o[j] >= stop: return o[j]
            if h[j] >= stop: return stop
            if tgt is not None and l[j] <= tgt: return min(o[j], tgt) if j > e else tgt
    return o[xb]


def gap_events(sym):
    """All gap-fade candidates of one stock: features + net % for each of the 108 settings (NaN = setting not tradable)."""
    per, N = _S["D"][sym], _S["N"]
    days = sorted(per)
    cl = pd.Series({d: per[d][3][-1] for d in days}); hi = pd.Series({d: np.nanmax(per[d][1]) for d in days}); lo = pd.Series({d: np.nanmin(per[d][2]) for d in days})
    tr = np.maximum(hi - lo, np.maximum((hi - cl.shift(1)).abs(), (lo - cl.shift(1)).abs()))
    atr = tr.rolling(14, min_periods=10).mean().shift(1); sma20 = cl.rolling(20, min_periods=15).mean().shift(1)
    r5 = (cl / cl.shift(5) - 1).shift(1) * 100
    feats, res = [], []
    for i in range(20, len(days)):
        d, pd_ = days[i], days[i - 1]
        o, h, l, c, v = per[d]; pc = per[pd_][3][-1]
        if not pc > 0 or o[0] < 50: continue
        g0 = (o[0] / pc - 1) * 100
        if not 1.0 <= abs(g0) <= 8.0: continue
        prev = [per[days[k]] for k in range(i - 14, i)]
        tv = np.median([np.nansum(x[3] * x[4]) for x in prev])
        A = atr.get(d)
        if tv < 1e8 or not A == A or A <= 0: continue
        long = g0 < 0
        vavg = np.mean([x[4] for x in prev], axis=0)
        vw = np.cumsum((h + l + c) / 3 * v) / np.maximum(np.cumsum(v), 1)
        nd = N.get(d); npc = N[pd_][3][-1] if pd_ in N else None
        row = np.full(len(VARIANTS), np.nan, dtype=np.float32)
        f = {"sym": sym, "day": d, "year": d.year, "side": "long" if long else "short", "gapabs": abs(g0),
             "trend": bool(pc > sma20.get(d, np.inf)), "r5": r5.get(d), "atrp": A / pc * 100, "tvcr": tv / 1e7,
             "wd": pd.Timestamp(d).day_name()[:3], "ngap": (nd[0][0] / npc - 1) * 100 if nd is not None and npc else np.nan}
        conf_info = {}
        for vi, (k, sk, tk, xk) in enumerate(VARIANTS):
            if (long and not c[k - 1] > o[0]) or (not long and not c[k - 1] < o[0]): continue
            entry = o[k] * (1 + R.SLIP if long else 1 - R.SLIP)
            if (long and entry >= pc) or (not long and entry <= pc): continue          # gap already filled
            stop = {"orl": (l[:k].min() if long else h[:k].max()), "atr05": entry - 0.5 * A if long else entry + 0.5 * A,
                    "atr1": entry - A if long else entry + A}[sk]
            risk = abs(entry - stop)
            if risk <= 0 or risk / entry > 0.04 or risk / entry < 0.001: continue
            tgt = {"fill": pc, "1R": entry + risk if long else entry - risk, "2R": entry + 2 * risk if long else entry - 2 * risk, "none": None}[tk]
            ex = walk(o, h, l, k, entry, stop, long, tgt, EXITS[xk]) * (1 - R.SLIP if long else 1 + R.SLIP)
            g = ex / entry - 1 if long else entry / ex - 1
            row[vi] = (g - R.cost_frac(entry)) * 100
            if k not in conf_info:
                sgn = 1 if long else -1
                conf_info[k] = {f"vwap_ok{k}": bool(sgn * (c[k - 1] - vw[k - 1]) > 0),
                                f"nifty_same{k}": (bool(sgn * (nd[3][k - 1] - nd[0][0]) > 0) if nd is not None else None),
                                f"rv{k}": float(v[:k].sum() / max(vavg[:k].sum(), 1)),
                                f"orbreak{k}": bool((c[k - 1] > h[:3].max()) if long else (c[k - 1] < l[:3].min())) if k == 3 else None}
        if np.isnan(row).all(): continue
        for k in CONF: f.update(conf_info.get(k, {}))
        feats.append(f); res.append(row)
    return feats, res


def masks_for(F):
    """Filter conditions (name -> boolean array) built from the event / trade features that exist in F."""
    M = {}
    def add(name, arr):
        arr = np.asarray(arr, dtype=bool)
        if 0 < arr.sum() < len(arr): M[name] = arr
    if "trend" in F: add("uptrend", F.trend == True); add("downtrend", F.trend == False)   # noqa: E712
    if "r5" in F:
        for lo_, hi_ in ((-99, -5), (-5, -2), (-2, 0), (0, 2), (2, 5), (5, 99)): add(f"r5 {lo_}..{hi_}", (F.r5 > lo_) & (F.r5 <= hi_))
    if "atrp" in F:
        for lo_, hi_ in ((0, 1.5), (1.5, 2.5), (2.5, 4), (4, 99)): add(f"atr% {lo_}..{hi_}", (F.atrp > lo_) & (F.atrp <= hi_))
    if "tvcr" in F:
        for lo_, hi_ in ((0, 30), (30, 100), (100, 300), (300, 1e9)): add(f"value Cr {lo_}..{hi_:g}", (F.tvcr > lo_) & (F.tvcr <= hi_))
    if "wd" in F:
        for w in ("Mon", "Tue", "Wed", "Thu", "Fri"): add(f"day {w}", F.wd == w)
    if "ngap" in F:
        for lo_, hi_ in ((-99, -0.5), (-0.5, -0.15), (-0.15, 0.15), (0.15, 0.5), (0.5, 99)): add(f"NIFTY gap {lo_}..{hi_}", (F.ngap > lo_) & (F.ngap <= hi_))
    if "rk" in F:
        for lo_, hi_ in ((0, 20), (20, 50), (50, 150), (150, 999)): add(f"momentum rank {lo_}..{hi_}", (F.rk > lo_) & (F.rk <= hi_))
    if "rv" in F:
        for lo_, hi_ in ((0, 0.8), (0.8, 1.2), (1.2, 2), (2, 3), (3, 999)): add(f"early volume {lo_}..{hi_}x", (F.rv > lo_) & (F.rv <= hi_))
    if "t" in F:
        for lo_, hi_ in ((0, 6), (6, 12), (12, 22), (22, 80)): add(f"entry bar {lo_}..{hi_}", (F.t > lo_) & (F.t <= hi_))
    if "nvw" in F: add("NIFTY with trade", F.nvw > 0.1); add("NIFTY against trade", F.nvw < -0.1)
    for k in CONF:
        if f"vwap_ok{k}" in F: add(f"right side of VWAP (c{k})", F[f"vwap_ok{k}"] == True)                  # noqa: E712
        if f"nifty_same{k}" in F: add(f"NIFTY moving same way (c{k})", F[f"nifty_same{k}"] == True)       # noqa: E712
        if f"rv{k}" in F: add(f"early volume >1.5x (c{k})", F[f"rv{k}"] > 1.5)
        if f"orbreak{k}" in F: add(f"15-min range broken (c{k})", F[f"orbreak{k}"] == True)                # noqa: E712
    return M


def _combo_eval(job):
    """Every 1-3 filter combination for one base set: returns rows of (base, filters, variant, n, mean, se, years+, years)."""
    base, idx = job
    ids = np.where(idx)[0]                                   # work on this base's rows only (much faster)
    X, years, names, min_n, vnames = _S["X"][ids], _S["years"][ids], _S["names"], _S["min_n"], _S["vnames"]
    M = {k: v[ids] for k, v in _S["M"].items()}
    idx = np.ones(len(ids), dtype=bool)
    out = []
    uy = np.unique(years)
    def score(sel, label):
        x = X[sel]; n = np.sum(~np.isnan(x), axis=0)
        ok = n >= min_n
        if not ok.any(): return False
        mean = np.nanmean(np.where(ok, x, np.nan), axis=0) if ok.any() else None
        sd = np.nanstd(x, axis=0)
        yp, yn = np.zeros(x.shape[1]), np.zeros(x.shape[1])
        for y in uy:
            xy = x[years[sel] == y]
            if len(xy) == 0: continue
            cnt = np.sum(~np.isnan(xy), axis=0); m = np.where(cnt > 0, np.nansum(xy, axis=0) / np.maximum(cnt, 1), np.nan)
            yn += cnt >= 5; yp += (cnt >= 5) & (m > 0)
        for vi in np.where(ok)[0]:
            out.append((base, label, vnames[vi], int(n[vi]), float(mean[vi]), float(sd[vi] / np.sqrt(n[vi])), int(yp[vi]), int(yn[vi])))
        return True
    if not score(idx, "-"): return out
    def fam(nm):
        if nm in ("uptrend", "downtrend"): return "trend"
        if nm.startswith("NIFTY with") or nm.startswith("NIFTY against"): return "NIFTY vs VWAP"
        last = nm.rsplit(" ", 1)[-1]
        return nm.rsplit(" ", 1)[0] if (".." in last or last in ("Mon", "Tue", "Wed", "Thu", "Fri")) else nm
    fam = {nm: fam(nm) for nm in names}
    live1 = []
    for a in names:
        s1 = idx & M[a]
        if s1.sum() >= min_n and score(s1, a): live1.append(a)
    live2 = []
    for a, b in itertools.combinations(live1, 2):
        if fam[a] == fam[b]: continue
        s2 = idx & M[a] & M[b]
        if s2.sum() >= min_n and score(s2, f"{a} + {b}"): live2.append((a, b))
    for a, b in live2:
        for c in live1:
            if c <= b or fam[c] in (fam[a], fam[b]): continue
            s3 = idx & M[a] & M[b] & M[c]
            if s3.sum() >= min_n: score(s3, f"{a} + {b} + {c}")
    return out


def search(X, F, vnames, bases, min_n):
    M = masks_for(F)
    _S.update(X=X, years=F.year.values, M=M, names=sorted(M), min_n=min_n, vnames=vnames)
    rows = []
    for part in R._pool_map(_combo_eval, bases): rows += part
    return pd.DataFrame(rows, columns=["base", "filters", "setting", "n", "mean", "se", "years_pos", "years"])


def apply_rule(X, F, vnames, base_masks, rule):
    """Measure one chosen rule (base, filters, setting) on another period's events."""
    M = masks_for(F)
    sel = base_masks.get(rule["base"])
    if sel is None: return None
    for nm in ([] if rule["filters"] == "-" else rule["filters"].split(" + ")):
        if nm not in M: return {"n": 0}
        sel = sel & M[nm]
    if rule["setting"] not in vnames: return {"n": 0}
    x = X[sel, vnames.index(rule["setting"])]; yrs = F.year.values[sel]
    ok = ~np.isnan(x); x, yrs = x[ok], yrs[ok]
    if len(x) == 0: return {"n": 0}
    by = pd.Series(x).groupby(yrs).mean()
    return {"n": int(len(x)), "mean": round(float(x.mean()), 4), "win": round(float((x > 0).mean() * 100), 1),
            "by_year": {int(k): round(float(v), 3) for k, v in by.items()}, "years_pos": f"{int((by > 0).sum())}/{len(by)}"}


def gap_table(data_dir):
    D = {}
    for name, per in R._pool_map(R._load_one, sorted(Path(data_dir).glob("*.csv.gz"))):
        if per is not None: D[name] = per
    _S.update(D=D, N=D.get("NIFTY50", {}))
    F, X = [], []
    for fs, rs in R._pool_map(gap_events, [s for s in D if s != "NIFTY50"]): F += fs; X += rs
    _S.pop("D", None)
    F = pd.DataFrame(F); X = np.array(X, dtype=np.float32).reshape(len(F), len(VARIANTS))
    print(f"{data_dir}: {len(D)} files, {len(F)} gap events", flush=True)
    return F, X


def gap_bases(F):
    return {f"gap fade {sd} {g:g}-8%": ((F.side == sd) & (F.gapabs >= g)).values for sd in ("long", "short") for g in GAPS}


def trade_table(path):
    T = pd.read_csv(path)
    T["base"] = T.setup + " " + T.side
    keys = sorted(T.base.unique())
    X = np.full((len(T), 1), np.nan, dtype=np.float32); X[:, 0] = T.net.values
    return T, X, {k: (T.base == k).values for k in keys}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--train"); ap.add_argument("--test"); ap.add_argument("--train-trades"); ap.add_argument("--test-trades")
    ap.add_argument("--out", default="search_results"); ap.add_argument("--min-n", type=int, default=150); ap.add_argument("--top", type=int, default=25)
    ap.add_argument("--min-years", type=int, default=3)
    a = ap.parse_args(); out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    vn = [f"confirm {k} bar, stop {s}, target {t}, exit {x}" for k, s, t, x in VARIANTS]
    report, picks = {}, []
    # Part A: gap fade, every setting x every filter combination
    Ftr, Xtr = gap_table(a.train); Fte, Xte = gap_table(a.test)
    Btr = gap_bases(Ftr)
    G = search(Xtr, Ftr, vn, list(Btr.items()), a.min_n)
    report["gap_fade_rules_tested"] = int(len(G))
    G["part"] = "gap fade"
    # Part B: other setups, every filter combination
    if a.train_trades and a.test_trades:
        Ttr, XTtr, BTtr = trade_table(a.train_trades); Tte, XTte, BTte = trade_table(a.test_trades)
        BTtr = {k: v for k, v in BTtr.items() if not k.startswith("gapfade")}
        H = search(XTtr, Ttr, ["as researched"], list(BTtr.items()), a.min_n)
        H["part"] = "other setups"; report["other_rules_tested"] = int(len(H))
        G = pd.concat([G, H], ignore_index=True)
    G["share_pos"] = G.years_pos / G.years.clip(lower=1)
    G["lower"] = G["mean"] - 2 * G["se"]
    good = G[(G.share_pos >= 0.8) & (G.years >= a.min_years) & (G["mean"] > 0)].sort_values("lower", ascending=False)
    report["rules_tested_total"] = int(len(G)); report["passing_train"] = int(len(good))
    report["luck_note"] = (f"{len(good)} of {len(G)} rules passed on the train period. With no real edge, roughly "
                           f"{int(len(G) * 0.02)} would pass by luck (rough), so the test period decides.")
    G.sort_values("lower", ascending=False).head(5000).to_csv(out / "train_rules_top5000.csv", index=False)
    # diversify: best rule per (base, filters) only
    seen = set()
    for _, r in good.iterrows():
        key = (r.base, r.filters)
        if key in seen: continue
        seen.add(key)
        if r.part == "gap fade": te = apply_rule(Xte, Fte, vn, gap_bases(Fte), r)
        else: te = apply_rule(XTte, Tte, ["as researched"], BTte, r)
        picks.append({"part": r.part, "base": r.base, "filters": r.filters, "setting": r.setting,
                      "train": {"n": int(r.n), "mean": round(float(r["mean"]), 4), "years_pos": f"{int(r.years_pos)}/{int(r.years)}"}, "test": te})
        if len(picks) >= a.top: break
    report["top_rules"] = picks
    report["test_positive"] = f"{sum(1 for p in picks if p['test'] and p['test'].get('n', 0) >= 30 and p['test']['mean'] > 0)}/{len(picks)}"
    (out / "search.json").write_text(json.dumps(report, indent=1, default=str))
    L = ["# Intraday combination search (chosen on train, checked on test)", "", report["luck_note"],
         f"Test period: {report['test_positive']} of the top rules made money", "",
         "| # | strategy | filters | setting | train n | train %/trade | train yrs+ | test n | test %/trade | test yrs+ |", "|---|---|---|---|---|---|---|---|---|---|"]
    for i, p in enumerate(picks, 1):
        t = p["test"] or {}
        L.append(f"| {i} | {p['base']} | {p['filters']} | {p['setting']} | {p['train']['n']} | {p['train']['mean']:+.3f} | {p['train']['years_pos']} | "
                 f"{t.get('n', 0)} | {t.get('mean', float('nan')):+.3f} | {t.get('years_pos', '-')} |")
    (out / "search.md").write_text("\n".join(L)); print("\n".join(L))


if __name__ == "__main__":
    main()
