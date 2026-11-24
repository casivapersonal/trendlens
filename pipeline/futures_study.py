"""Stock-futures version of the intraday exhaustion short, on 1-minute data (Nov 2026).

Rule (unchanged from the live intraday segment): an F&O stock that ran up hard (up >20% in 5 sessions, or >25% above its
50-day average, or a 20-day high on 1.5x volume the day before) opens 1.5-8% above the previous close. Short at the open of
the 9:20 candle, stop = entry + 1 daily ATR(14), target = entry - 2 ATR, at most 3 a day (biggest 5-day run-up first).
Variants:  hold 0 = square off 15:15 the same day (live rule); hold 1 / 2 = keep the short (futures only) until 15:15 one or
two sessions later, stop and target still active (a gap through the stop at the next open fills at that open).
Costs as % of contract value, short = sell first, buy back later:
  futures: STT 0.05% on the sell (Budget 2026, from 1 Apr 2026), NSE 0.00173% + SEBI 0.0001% each side, stamp 0.002% on the buy,
           brokerage Rs 20 an order on a Rs 7.5 lakh lot, GST 18%;   cash intraday (reference): STT 0.025% on the sell,
           NSE 0.00297%, stamp 0.003% on the buy, Rs 20 an order on Rs 2 lakh, GST.   Slippage 0.05% each side, and 0.10%.
Data: 2022-2026 1-minute candles (Hugging Face, Upstox), the current NSE F&O list (small look-ahead), also re-run on 5-minute
bars built from the same minutes to show what the finer data changes; 2014-2021 5-minute F&O archive. Within one bar the stop
is assumed to come before the target (conservative). Futures follow the stock within the day; the small basis and its
overnight drift (which slightly favours a short) are ignored. Writes futures_summary.json / futures_summary.md."""
from __future__ import annotations

import argparse, gzip, json, re, subprocess, sys, time
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
HF = "https://huggingface.co/datasets/bsakash137/indian-stock-market-minute-data/resolve/main/"
HOLDS = (0, 1, 2)
SLIPS = (0.05, 0.10)


# ------------------------------------------------------------------ costs (% of contract value, round trip of a short)
def cost_pct(kind: str, slip: float) -> float:
    if kind == "futures":
        brk = 20 / 750000 * 100; ex = 0.00173 + 0.0001
        return 0.05 + 2 * ex + 0.002 + 2 * brk + 0.18 * (2 * brk + 2 * ex) + 2 * slip
    brk = 20 / 200000 * 100; ex = 0.00297 + 0.0001
    return 0.025 + 2 * ex + 0.003 + 2 * brk + 0.18 * (2 * brk + 2 * ex) + 2 * slip


# ------------------------------------------------------------------ data
def hf_minutes(fno: set, work: Path, shards=range(10)) -> Path:
    """1-minute candles for the F&O stocks, one parquet per symbol in work/min (09:15-15:29 IST)."""
    import pyarrow.parquet as pq
    parts = work / "parts"; parts.mkdir(parents=True, exist_ok=True); dest = work / "min"; dest.mkdir(exist_ok=True)
    for f in shards:
        p = work / "shard.parquet"; t0 = time.time()
        subprocess.run(["curl", "-sSfL", "--retry", "5", "--retry-delay", "10", "-o", str(p), HF + f"minute/train-{f:05d}.parquet"], check=True)
        t = pq.read_table(p, filters=[("symbol", "in", sorted(fno))], columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"]).to_pandas()
        p.unlink()
        if t.empty: continue
        ts = pd.to_datetime(t["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        m = ts.dt.hour * 60 + ts.dt.minute; t["timestamp"] = ts; t = t[(m >= 555) & (m <= 929)]
        for s, x in t.groupby("symbol"):
            x.drop(columns="symbol").astype({"open": "float32", "high": "float32", "low": "float32", "close": "float32", "volume": "float64"}).to_parquet(parts / f"{s}.{f:02d}.parquet")
        print(f"shard {f}: {len(t):,} rows, {t.symbol.nunique()} symbols ({time.time() - t0:.0f}s)", flush=True)
    by = {}
    for q in parts.glob("*.parquet"): by.setdefault(q.name.rsplit(".", 2)[0], []).append(q)
    for s, qs in by.items():
        d = pd.concat([pd.read_parquet(q) for q in qs]).drop_duplicates("timestamp").sort_values("timestamp")
        d.to_parquet(dest / f"{s}.parquet")
        for q in qs: q.unlink()
    return dest


def bars_from(path: Path) -> pd.DataFrame:
    if path.suffix == ".parquet":
        d = pd.read_parquet(path).rename(columns={"timestamp": "time"})
    else:
        d = pd.read_csv(path, parse_dates=["time"])
    return d[["time", "open", "high", "low", "close", "volume"]].dropna().sort_values("time").reset_index(drop=True)


def to5(d: pd.DataFrame) -> pd.DataFrame:
    g = d.assign(b=d.time.dt.floor("5min")).groupby("b")
    return pd.DataFrame({"time": g.time.first().index, "open": g.open.first().values, "high": g.high.max().values,
                         "low": g.low.min().values, "close": g.close.last().values, "volume": g.volume.sum().values})


# ------------------------------------------------------------------ events and trade walks
def daily_facts(d: pd.DataFrame) -> pd.DataFrame:
    g = d.groupby(d.time.dt.date)
    D = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(), "close": g.close.last(), "volume": g.volume.sum(),
                      "t0": g.time.first()})
    D.index = pd.to_datetime(D.index)
    pc = D.close.shift(1)
    tr = np.maximum(D.high - D.low, np.maximum((D.high - pc).abs(), (D.low - pc).abs()))
    D["atr"] = tr.rolling(14, min_periods=10).mean()
    D["sma50"] = D.close.rolling(50, min_periods=40).mean()
    D["ret5"] = D.close / D.close.shift(5) - 1
    D["hi20"] = D.high.rolling(20, min_periods=15).max().shift(1)
    D["v50"] = D.volume.rolling(50, min_periods=40).mean().shift(1)
    D["val20"] = (D.close * D.volume).rolling(20, min_periods=15).mean()
    D["setup"] = (D.ret5 > 0.20) | (D.close / D.sma50 - 1 > 0.25) | ((D.close >= D.hi20) & (D.volume >= 1.5 * D.v50))
    return D


def events(sym: str, d: pd.DataFrame) -> list[dict]:
    D = daily_facts(d); out = []
    t = d.time.values; o, h, l = d.open.values, d.high.values, d.low.values
    day = d.time.dt.normalize().values; mins = (d.time.dt.hour * 60 + d.time.dt.minute).values
    starts = np.flatnonzero(np.r_[True, day[1:] != day[:-1]]); dkeys = day[starts]
    pos = {pd.Timestamp(k): i for i, k in enumerate(dkeys)}
    dates = list(D.index)
    for k in range(60, len(dates)):
        p, dd = D.iloc[k - 1], D.iloc[k]
        if not p.setup or not (p.close >= 20) or not (p.val20 >= 5e7) or not (p.atr > 0): continue
        if d.time.iloc[starts[pos[dates[k]]]].hour * 60 + d.time.iloc[starts[pos[dates[k]]]].minute != 555: continue   # needs the 9:15 bar
        gap = dd.open / p.close - 1
        if not 0.015 <= gap <= 0.08: continue
        i0 = starts[pos[dates[k]]]; i1 = starts[pos[dates[k]] + 1] if pos[dates[k]] + 1 < len(starts) else len(t)
        e = next((j for j in range(i0, i1) if mins[j] >= 560), None)
        if e is None or mins[e] > 565: continue
        entry = float(o[e]); stop = entry + float(p.atr); tgt = entry - 2 * float(p.atr)
        ev = {"sym": sym, "day": dates[k], "gap": gap, "ret5": float(p.ret5), "entry": entry, "risk_pct": (stop - entry) / entry * 100}
        for H in HOLDS:
            di = pos[dates[k]] + H
            if di >= len(starts): ev[f"x{H}"] = None; continue
            j0 = starts[di]; j1 = starts[di + 1] if di + 1 < len(starts) else len(t)
            sq = next((j for j in range(j0, j1) if mins[j] >= 915), j1 - 1)
            px = None
            for j in range(e, sq + 1):
                if j == sq and px is None: px = float(o[j]); break
                if h[j] >= stop: px = float(max(o[j], stop)) if j > e else stop; break
                if l[j] <= tgt: px = float(min(o[j], tgt)) if j > e else tgt; break
            ev[f"x{H}"] = px
        out.append(ev)
    return out


# ------------------------------------------------------------------ summary
def table(E: pd.DataFrame, label: str) -> dict:
    E = E.sort_values(["day", "ret5"], ascending=[True, False]).groupby("day").head(3).copy()      # live limit: 3 a day
    res = {}
    for H in HOLDS:
        X = E.dropna(subset=[f"x{H}"])
        gross = (X.entry - X[f"x{H}"]) / X.entry * 100
        for kind in ("futures", "cash") if H == 0 else ("futures",):
            for s in SLIPS:
                net = gross - cost_pct(kind, s); R = net / X.risk_pct
                g = net.groupby(X.day).mean(); se = g.std(ddof=1) / np.sqrt(len(g)) if len(g) > 2 else np.nan
                yr = net.groupby(X.day.dt.year).mean()
                eq = (1 + net.groupby(X.day).sum() * 0.20 / 100).cumprod()                         # 20% of capital per trade, no leverage
                res[f"hold {H} | {kind} | slip {s}"] = {"trades": int(len(X)), "net_pct": round(float(net.mean()), 3), "avg_R": round(float(R.mean()), 3),
                    "win_pct": round(float((net > 0).mean() * 100), 1), "t_by_day": round(float(g.mean() / se), 2) if se == se and se > 0 else None,
                    "years_pos": f"{int((yr > 0).sum())}/{len(yr)}", "by_year": {int(k): round(float(v), 2) for k, v in yr.items()},
                    "worst_trade_pct": round(float(net.min()), 2), "max_drawdown_pct_20pct_each": round(float((eq / eq.cummax() - 1).min() * 100), 1)}
    print(label, json.dumps({k: (v["trades"], v["net_pct"], v["t_by_day"]) for k, v in res.items()}), flush=True)
    return res


def run(files, label, five=False):
    ev = []; t0 = time.time()
    for n, f in enumerate(files):
        try:
            d = bars_from(f)
            if five: d = to5(d)
            ev += events(f.name.split(".")[0], d)
        except Exception as e:
            print("skip", f.name, str(e)[:100], flush=True)
        if n % 50 == 0: print(f"{label}: {n}/{len(files)} files, {len(ev)} events ({time.time() - t0:.0f}s)", flush=True)
    return pd.DataFrame(ev)


def report(res, out):
    (out / "futures_summary.json").write_text(json.dumps(res, indent=1, default=str))
    L = ["# Exhaustion short as stock futures", "", f"F&O list: {res.get('fno_count')} from {res.get('fno_source')}. Costs round trip (% of contract value): {json.dumps(res['costs_pct_round_trip'])}", ""]
    for per in [k for k in res if k[:2] == "20"]:
        L += [f"## {per}", "", "| variant | trades | net %/trade | avg R | win % | t (by day) | years + | worst trade % | max drawdown (20% each) |", "|---|---|---|---|---|---|---|---|---|"]
        for k, v in res[per].items():
            L.append(f"| {k} | {v['trades']} | {v['net_pct']} | {v['avg_R']} | {v['win_pct']} | {v['t_by_day']} | {v['years_pos']} | {v['worst_trade_pct']} | {v['max_drawdown_pct_20pct_each']} |")
        L.append("")
    (out / "futures_summary.md").write_text("\n".join(L)); print("\n".join(L), flush=True)


def main():
    """Parallel: each part job builds its shard of 1-minute files and its slice of the 2014-21 files and writes events;
    --collect reads every part's events and writes the summary."""
    ap = argparse.ArgumentParser(); ap.add_argument("--old"); ap.add_argument("--work", default="/tmp/fut"); ap.add_argument("--out", default="futures_results")
    ap.add_argument("--shards", default="0,1,2,3,4,5,6,7,8,9"); ap.add_argument("--part", type=int, default=0); ap.add_argument("--parts", type=int, default=1)
    ap.add_argument("--collect"); a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    from intraday_exhaustion_test import fno_list
    fno, src = fno_list(); print("F&O stocks:", len(fno), src, flush=True)
    res = {"fno_source": src, "fno_count": len(fno), "costs_pct_round_trip": {k: {s: round(cost_pct(k, s), 4) for s in SLIPS} for k in ("futures", "cash")}}
    names = {"2022-2026 (1-minute)": "events_2022_1min", "2022-2026 (5-minute, same data)": "events_2022_5min", "2014-2021 (5-minute)": "events_2014_5min"}
    if a.collect:
        for label, nm in names.items():
            fs = sorted(Path(a.collect).rglob(f"{nm}*.csv.gz"))
            E = pd.concat([pd.read_csv(f, parse_dates=["day"]) for f in fs if f.stat().st_size > 40], ignore_index=True) if fs else pd.DataFrame()
            if len(E): res[label] = table(E, label)
        report(res, out); return
    work = Path(a.work); work.mkdir(parents=True, exist_ok=True)
    tag = f"_p{a.part}"
    mdir = hf_minutes(fno, work, [int(x) for x in a.shards.split(",") if x != ""])
    files = sorted(mdir.glob("*.parquet"))
    run(files, "2022-26 1-minute").to_csv(out / f"events_2022_1min{tag}.csv.gz", index=False)
    run(files, "2022-26 5-minute", five=True).to_csv(out / f"events_2022_5min{tag}.csv.gz", index=False)
    if a.old:
        olds = sorted(Path(a.old).glob("*.csv.gz"))[a.part::a.parts]
        run(olds, "2014-21 5-minute").to_csv(out / f"events_2014_5min{tag}.csv.gz", index=False)


if __name__ == "__main__":
    main()
