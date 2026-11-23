"""Can five-minute data improve the momentum (medium-term) and bull-flag (short-term) entries and exits?

Trades are rebuilt exactly as the live rules (pipeline/backtest.py: momentum variant with the 6 x ATR stop and sector caps,
bull flag as setup_study(new_defs=True)). Then, for every trade day covered by the five-minute files, the entry / exit is moved
to another time of the same day and the change in the result is measured. Prices inside one day are used only as ratios to that
day's 9:15 open, so adjusted (Yahoo) and unadjusted (archive) prices mix safely.
Choose on 2014-2021 (archive), check on 2022-2026 (Hugging Face); a change is only worth making if it helps in both.
Writes summary.json and summary.md."""
from __future__ import annotations
import argparse, json, pickle, sys, time
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import backtest as B, core  # noqa: E402

SLOT = {f"{9 + (15 + 5 * k) // 60:02d}:{(15 + 5 * k) % 60:02d}": k for k in range(75)}     # 09:15 -> 0 ... 15:25 -> 74
NEED: dict = {}


def build_trades(cache):
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    data = pickle.load(open(cache, "rb")) if Path(cache).exists() else B.download(syms, cache)
    idx, syms, P = B.panel(data)
    bench = data["^CRSLDX"]["close"] if data.get("^CRSLDX") is not None else data["^NSEI"]["close"]
    R = B.daily_ranks(idx, syms, P, bench=bench)
    funds = json.loads((HERE.parent / "web" / "fundamentals.json").read_text()).get("stocks", {})
    eq, tr = B.simulate(idx, syms, P, R, start="2010-12-01", stop_atr=6, sector_caps={"map": funds, "broad": 6, "industry": 3})
    O, H, L, C = (P[f].to_numpy() for f in ("open", "high", "low", "close"))
    col = {s: j for j, s in enumerate(syms)}
    trh = np.maximum(H - L, np.maximum(abs(H - np.roll(C, 1, 0)), abs(L - np.roll(C, 1, 0))))
    ATR = pd.DataFrame(trh).rolling(14, min_periods=10).mean().to_numpy()
    M = []
    for t in tr:
        j = col[t["sym"]]; i = t["in"]
        M.append({"sym": t["sym"], "in": str(idx[i].date()), "out": str(idx[t["out"]].date()), "why": t["why"], "ret": t["ret"],
                  "gap": O[i, j] / C[i - 1, j] - 1, "ret5": C[i - 1, j] / C[i - 6, j] - 1})
    TV = (P["close"] * P["volume"]).rolling(20, min_periods=15).median().to_numpy(); lvl = bench.reindex(idx).ffill().bfill().to_numpy()
    up = (2.5 * 1.5 - 2.0 * 1.5) / 3.0; s0 = idx.searchsorted(pd.Timestamp("2011-01-01")); F = []
    V = P["volume"].to_numpy()
    for j, sym in enumerate(syms):
        c, h, l, o, a = C[:, j], H[:, j], L[:, j], O[:, j], ATR[:, j]; last = -99
        for t in range(max(s0, 60), len(idx) - 17):
            if t - last < 15 or c[t] != c[t] or a[t] != a[t] or c[t] < 50 or a[t] / c[t] > 0.07: continue
            if not TV[t, j] == TV[t, j] or TV[t, j] < core.MIN_TURNOVER_CR * 1e7 * lvl[t] / lvl[-1]: continue
            rk = R.get(t, {}).get(sym)
            if rk is not None and rk <= B.ENTRY: continue
            flag = False
            for k in range(3, 11):
                rng = np.nanmax(h[t - k + 1:t + 1]) - np.nanmin(l[t - k + 1:t + 1]); b0 = t - k
                if rng < 1.5 * a[t] and c[b0] / c[b0 - 10] - 1 >= 0.15 and c[t] > np.nanmean(c[t - 19:t + 1]): flag = True; break
            if not flag: continue
            stop, tgt = c[t] - 1.5 * a[t], c[t] + 2.5 * 1.5 * a[t]; fill = f0 = None; lo, hi = c[t] - 0.4 * a[t], c[t] + up * a[t]
            for u in range(t + 1, t + 4):
                if o[u] != o[u] or o[u] <= stop: break
                if l[u] <= hi: fill, f0 = (min(o[u], hi) if o[u] >= lo else o[u]), u; break
            if fill is None or fill <= stop or (tgt - fill) / (fill - stop) < 2.0: continue
            last = t; ex = None
            for u in range(f0, min(f0 + 15, len(idx))):
                oo = fill if u == f0 else o[u]
                if oo != oo: continue
                if oo >= tgt or oo <= stop: ex = oo; break
                if l[u] <= stop: ex = stop; break
                if h[u] >= tgt and u != f0: ex = tgt; break
            xd = u
            if ex is None: xd = min(f0 + 14, len(idx) - 1); ex = c[xd]
            if ex != ex: continue
            F.append({"sym": sym, "signal": str(idx[t].date()), "fill_day": str(idx[f0].date()), "fill": fill, "open": o[f0], "hi": hi, "lo": lo,
                      "stop": stop, "target": tgt, "exit_day": str(idx[xd].date()), "exit": ex, "gap": o[f0] / c[f0 - 1] - 1,
                      "exit_kind": "target" if ex >= tgt * 0.999 else "stop" if ex <= stop * 1.001 else "time"})
    return pd.DataFrame(M), pd.DataFrame(F)


def _load(args):
    f, days = args
    try:
        x = pd.read_csv(f, usecols=["time", "open", "high", "low", "close", "volume"], dtype={"time": str})
    except Exception as e:
        return f.name[:-7], {}
    d = x["time"].str.slice(0, 10); x = x[d.isin(days)]
    if x.empty: return f.name[:-7], {}
    hm = x["time"].str.slice(11, 16); out = {}
    for day, g in x.assign(hm=hm).groupby(d[x.index]):
        k = g["hm"].map(SLOT); g = g[k.notna()]; k = k[k.notna()].astype(int).to_numpy()
        if len(g) < 60 or 0 not in k: continue
        a = np.full((5, 75), np.nan)
        for r, cname in enumerate(("open", "high", "low", "close", "volume")): a[r, k] = g[cname].to_numpy(float)
        cc = pd.Series(a[3]).ffill().bfill().to_numpy()
        for r in range(3): a[r] = np.where(np.isnan(a[r]), cc, a[r])
        a[3] = cc; a[4] = np.nan_to_num(a[4]); out[day] = a
    return f.name[:-7], out


def load_bars(dirs, need):
    jobs = []
    for d in dirs:
        for f in sorted(Path(d).glob("*.csv.gz")):
            s = f.name[:-7]
            if s in need: jobs.append((f, sorted(need[s])))
    bars = {}
    with ProcessPoolExecutor() as ex:
        for s, per in ex.map(_load, jobs, chunksize=4):
            for day, a in per.items(): bars.setdefault(s, {}).setdefault(day, a)
    return bars


def price_at(a, how):
    """Price inside the day, as a ratio to the 9:15 open."""
    o, h, l, c, v = a; o0 = o[0]
    if how == "09:15": return 1.0
    if how in SLOT: return o[SLOT[how]] / o0
    if how == "close": return c[74] / o0
    if how == "vwap_1h": k = slice(0, 12); tp = (h[k] + l[k] + c[k]) / 3; w = v[k]; return (float((tp * w).sum() / w.sum()) if w.sum() > 0 else float(tp.mean())) / o0
    if how == "vwap_day": tp = (h + l + c) / 3; return (float((tp * v).sum() / v.sum()) if v.sum() > 0 else float(tp.mean())) / o0
    raise ValueError(how)


def summ(x):
    x = np.asarray(x, float); x = x[np.isfinite(x)]
    if len(x) < 20: return {"n": int(len(x))}
    se = x.std(ddof=1) / np.sqrt(len(x))
    return {"n": int(len(x)), "avg": round(float(x.mean()), 3), "t": round(float(x.mean() / se), 2) if se > 0 else None, "better_pct": round(100 * float((x > 0).mean()), 1)}


def period(day): return "2014-2021" if day < "2022-01-01" else "2022-2026"


TIMES = ["09:20", "09:30", "09:45", "10:00", "10:30", "11:00", "12:00", "13:00", "14:00", "15:00", "close", "vwap_1h", "vwap_day"]


def momentum_study(M, bars):
    res = {"entry": {}, "exit": {}, "entry_by_gap": {}}
    for how in TIMES:
        for P_ in ("2014-2021", "2022-2026"):
            d = [(1 / price_at(bars[r.sym][r.in_], how) - 1) * 100 for r in M.itertuples() if r.in_ >= "2014" and period(r.in_) == P_ and r.in_ in bars.get(r.sym, {})]
            res["entry"].setdefault(how, {})[P_] = summ(d)                 # + = buying at this time instead of the open adds this % to the trade
        for P_ in ("2014-2021", "2022-2026"):
            d = [(price_at(bars[r.sym][r.out], how) - 1) * 100 for r in M.itertuples() if r.why != "stop" and period(r.out) == P_ and r.out in bars.get(r.sym, {})]
            res["exit"].setdefault(how, {})[P_] = summ(d)                  # + = selling at this time instead of the open adds this %
    for lo, hi, lab in ((-1, 0, "opened lower"), (0, 0.01, "0-1% gap up"), (0.01, 0.03, "1-3% gap up"), (0.03, 1, "> 3% gap up")):
        for how in ("09:30", "10:00", "11:00", "close"):
            for P_ in ("2014-2021", "2022-2026"):
                d = [(1 / price_at(bars[r.sym][r.in_], how) - 1) * 100 for r in M.itertuples() if lo <= r.gap < hi and period(r.in_) == P_ and r.in_ >= "2014" and r.in_ in bars.get(r.sym, {})]
                res["entry_by_gap"].setdefault(lab, {}).setdefault(how, {})[P_] = summ(d)
    return res


def flag_study(F, bars):
    res = {"entry": {}, "first_30min_filter": {}, "gap_filter": {}, "baseline": {}}
    def R(fill, r): return (r.exit - fill) / (fill - r.stop)
    for P_ in ("2014-2021", "2022-2026"):
        res["baseline"][P_] = summ([R(r.fill, r) for r in F.itertuples() if period(r.fill_day) == P_ and r.fill_day >= "2014" and r.fill_day in bars.get(r.sym, {})])
    for how in ("09:30", "10:00", "11:00", "13:00", "close", "vwap_1h"):
        for P_ in ("2014-2021", "2022-2026"):
            d, sk = [], []
            for r in F.itertuples():
                if period(r.fill_day) != P_ or r.fill_day < "2014" or r.fill_day not in bars.get(r.sym, {}): continue
                a = bars[r.sym][r.fill_day]; px = r.open * price_at(a, how)
                lo_before = r.open * np.min(a[2][:SLOT.get(how, 74) + 1]) / a[0][0]
                if lo_before <= r.stop or px <= r.stop: sk.append(R(r.fill, r)); continue    # below the stop before this entry: the later entry is not taken
                d.append((r.fill / px - 1) * 100)
            res["entry"].setdefault(how, {})[P_] = summ(d)                      # % price improvement per trade vs the rule's fill (+ = cheaper)
            res.setdefault("entry_skipped", {}).setdefault(how, {})[P_] = summ(sk)  # rule's R on trades a later entry would have skipped
    for P_ in ("2014-2021", "2022-2026"):
        g = {"first 30 min up": [], "first 30 min down": []}
        for r in F.itertuples():
            if period(r.fill_day) != P_ or r.fill_day < "2014" or r.fill_day not in bars.get(r.sym, {}): continue
            a = bars[r.sym][r.fill_day]
            g["first 30 min up" if a[3][5] >= a[0][0] else "first 30 min down"].append(R(r.fill, r))
        for k, v in g.items(): res["first_30min_filter"].setdefault(k, {})[P_] = summ(v)
        for lo, hi, lab in ((-1, 0, "opened lower"), (0, 0.01, "0-1% gap up"), (0.01, 1, "> 1% gap up")):
            res["gap_filter"].setdefault(lab, {})[P_] = summ([R(r.fill, r) for r in F.itertuples() if lo <= r.gap < hi and period(r.fill_day) == P_
                                                               and r.fill_day >= "2014" and r.fill_day in bars.get(r.sym, {})])
    return res


def verdicts(res):
    out = []
    def walk(name, d):
        for k, v in d.items():
            if isinstance(v, dict) and "2014-2021" in v and "2022-2026" in v:
                a, b = v["2014-2021"], v["2022-2026"]
                if a.get("avg") is not None and b.get("avg") is not None and a["n"] >= 50 and b["n"] >= 50:
                    both = (a["avg"] > 0 and b["avg"] > 0) or (a["avg"] < 0 and b["avg"] < 0)
                    sig = (a.get("t") or 0) and abs(a["t"]) >= 2 and abs(b.get("t") or 0) >= 2
                    out.append({"test": f"{name} / {k}", "2014-2021": a, "2022-2026": b, "same sign both periods": both, "significant both (|t|>=2)": bool(both and sig)})
            elif isinstance(v, dict): walk(f"{name} / {k}", v)
    walk("momentum", res["momentum"]); walk("bull flag", res["flag"])
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--cache", default="daily_cache.pkl"); ap.add_argument("--out", default="swing_results")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(exist_ok=True); t0 = time.time()
    M, F = build_trades(a.cache); M = M.rename(columns={"in": "in_"})
    print("momentum trades", len(M), "flag trades", len(F), round(time.time() - t0), "s", flush=True)
    M.to_csv(out / "momentum_trades.csv", index=False); F.to_csv(out / "flag_trades.csv", index=False)
    need = {}
    for r in M.itertuples(): need.setdefault(r.sym, set()).update([r.in_, r.out])
    for r in F.itertuples(): need.setdefault(r.sym, set()).add(r.fill_day)
    bars = load_bars([a.old, a.new], need)
    print("five-minute days loaded", sum(len(v) for v in bars.values()), round(time.time() - t0), "s", flush=True)
    res = {"momentum": momentum_study(M, bars), "flag": flag_study(F, bars)}
    cov = {"momentum entries covered": int(sum(r.in_ in bars.get(r.sym, {}) for r in M.itertuples())), "momentum trades": len(M),
           "flag fills covered": int(sum(r.fill_day in bars.get(r.sym, {}) for r in F.itertuples())), "flag trades": len(F)}
    res["coverage"] = cov; res["verdicts"] = verdicts(res)
    (out / "summary.json").write_text(json.dumps(res, indent=1))
    lines = ["# Swing timing study (5-minute data)", "", f"Coverage: {cov}", "", "| test | 2014-21 avg (t, n) | 2022-26 avg (t, n) | same sign | significant both |", "|---|---|---|---|---|"]
    for v in res["verdicts"]:
        f = lambda s: f"{s['avg']:+.3f} (t {s.get('t')}, n {s['n']})"
        lines.append(f"| {v['test']} | {f(v['2014-2021'])} | {f(v['2022-2026'])} | {v['same sign both periods']} | {v['significant both (|t|>=2)']} |")
    (out / "summary.md").write_text("\n".join(lines)); print("\n".join(lines)); print("done", round(time.time() - t0), "s")
