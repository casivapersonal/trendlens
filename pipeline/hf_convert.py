"""Builds 5-minute files for every NIFTY 500 stock (2022 onwards) from the public Hugging Face dataset
bsakash137/indian-stock-market-minute-data (1-minute Upstox candles for ~3,200 NSE symbols, adjusted for splits and bonuses,
timestamps in UTC; stocks delisted before 2026 are not in it). Output is the same format as pipeline/upstox_download.py
(SYMBOL.csv.gz with time, open, high, low, close, volume; NIFTY 50 index -> NIFTY50.csv.gz) for pipeline/intraday_research.py.

The ten ~1.4 GB minute shards are downloaded one at a time and deleted after use, so disk stays small.

  python pipeline/hf_convert.py --out intraday_data"""
from __future__ import annotations

import argparse, gzip, json, re, subprocess, time
from pathlib import Path

import pandas as pd
import pyarrow.parquet as pq

HERE = Path(__file__).resolve().parent
BASE = "https://huggingface.co/datasets/bsakash137/indian-stock-market-minute-data/resolve/main/"


def get(name, dest):
    subprocess.run(["curl", "-sSfL", "--retry", "5", "--retry-delay", "10", "-o", str(dest), BASE + name], check=True)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--out", default="intraday_data"); ap.add_argument("--work", default="/tmp/hf")
    ap.add_argument("--shards", type=int, default=10); a = ap.parse_args(); t0 = time.time()
    out, work = Path(a.out), Path(a.work); out.mkdir(parents=True, exist_ok=True); (work / "parts").mkdir(parents=True, exist_ok=True)
    want = {s.strip() for s in open(HERE / "nifty500.txt") if s.strip()}
    nifty = None
    for f in range(a.shards):
        name = f"minute/train-{f:05d}.parquet"; p = work / "shard.parquet"
        get(name, p)
        if nifty is None:
            syms = set(pq.read_table(p, columns=["symbol"]).column(0).unique().to_pylist())
            nifty = next((s for s in syms if re.fullmatch(r"nifty ?50", s.strip(), re.I)), None)
            print("symbols in first shard:", len(syms), "| sample:", sorted(syms)[:8], "| NIFTY 50 index:", nifty, "| NIFTY 500 found:", len(want & syms), flush=True)
        keep = list(want | ({nifty} if nifty else set()))
        t = pq.read_table(p, filters=[("symbol", "in", keep)], columns=["symbol", "timestamp", "open", "high", "low", "close", "volume"]).to_pandas()
        p.unlink()
        if t.empty: print(name, "no NIFTY 500 rows", flush=True); continue
        ts = pd.to_datetime(t["timestamp"], utc=True).dt.tz_convert("Asia/Kolkata").dt.tz_localize(None)
        t["timestamp"] = ts
        mins = ts.dt.hour * 60 + ts.dt.minute
        t = t[(mins >= 555) & (mins <= 929)].sort_values(["symbol", "timestamp"])
        t["b"] = t["timestamp"].dt.floor("5min")
        g = t.groupby(["symbol", "b"], sort=True).agg(open=("open", "first"), high=("high", "max"), low=("low", "min"),
                                                         close=("close", "last"), volume=("volume", "sum")).reset_index()
        for s, x in g.groupby("symbol"):
            nm = "NIFTY50" if s == nifty else s
            x[["b", "open", "high", "low", "close", "volume"]].to_csv(work / "parts" / f"{nm}.{f:02d}.csv", index=False, header=False,
                                                                     date_format="%Y-%m-%dT%H:%M:%S", float_format="%.4f")
        print(f"{name}: {len(t):,} minute rows, {g.symbol.nunique()} symbols, {g.b.min()} .. {g.b.max()} ({(time.time() - t0) / 60:.1f} min)", flush=True)
    by = {}
    for f in sorted((work / "parts").glob("*.csv")): by.setdefault(f.name.rsplit(".", 2)[0], []).append(f)
    for s, fs in by.items():
        rows = {}
        for f in fs:
            for ln in f.read_text().splitlines(): rows[ln.split(",", 1)[0]] = ln
        with gzip.open(out / f"{s}.csv.gz", "wt") as w:
            w.write("time,open,high,low,close,volume\n")
            for k in sorted(rows): w.write(rows[k] + "\n")
    info = {"source": "huggingface.co/datasets/bsakash137/indian-stock-market-minute-data (Upstox 1-minute candles, adjusted)",
            "index_symbol": nifty, "files": len(by), "missing": sorted(want - set(by))[:60], "minutes": round((time.time() - t0) / 60, 1)}
    (out / "_info.json").write_text(json.dumps(info, indent=1)); print(json.dumps(info, indent=1))
    if not nifty: print("WARNING: NIFTY 50 index not found in the dataset; research will run without the index features")


if __name__ == "__main__":
    main()
