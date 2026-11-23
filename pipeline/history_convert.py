"""Converts the community 1-minute NSE archive (github.com/gsidhu/nse-intraday-data, a copy of aeron7/nifty-banknifty-intraday-data:
about 100-200 F&O stocks plus NIFTY, 2012 to January 2023, one folder per day) into 5-minute files in the same format as
pipeline/upstox_download.py (SYMBOL.csv.gz with time, open, high, low, close, volume; NIFTY.txt -> NIFTY50.csv.gz), so
pipeline/intraday_research.py reads them.

Lines look like "SBIN,20190401,09:16,322.00,323.60,322.00,323.15,384957,0": each bar is stamped with the minute it ENDS, so
09:16 is 09:15-09:16. Bars from 09:16 to 15:30 are kept and grouped into 5-minute bars starting 09:15 ... 15:25. Futures files
(_F1, _F2) and other indices are skipped. Prices are as traded (not adjusted for splits or bonuses): fine inside a day; the few
split days show up as large gaps. The archive is unofficial and has missing days; the summary lists days per year.

  python pipeline/history_convert.py --src nse-intraday-data --out intraday_data --from-year 2012 --to-year 2021"""
from __future__ import annotations

import argparse, datetime as dt, gzip, json, re
from collections import Counter
from pathlib import Path

MON = {m: i + 1 for i, m in enumerate("JAN FEB MAR APR MAY JUN JUL AUG SEP OCT NOV DEC".split())}


def day_of(path: Path):
    """Date from the folder names: .../2019/APR/01APR/... -> 2019-04-01 (None if the path does not follow that layout)."""
    y = next((p for p in path.parts if re.fullmatch(r"\d{4}", p)), None)
    d = next((p for p in path.parts if re.fullmatch(r"\d{2}[A-Z]{3}", p)), None)
    if not y or not d or d[2:] not in MON: return None
    try: return dt.date(int(y), MON[d[2:]], int(d[:2]))
    except ValueError: return None


def bars5(lines):
    """1-minute lines of one stock-day -> {slot start 'HH:MM': [o, h, l, c, v]} for 09:15 ... 15:25."""
    out = {}
    for ln in lines:
        p = ln.strip().split(",")
        if len(p) < 8: continue
        try:
            hh, mm = int(p[2][:2]), int(p[2][3:5]); o, h, l, c = (float(x) for x in p[3:7]); v = float(p[7])
        except ValueError:
            continue
        m = hh * 60 + mm - 1                           # bar start minute
        if m < 555 or m > 929 or not (o > 0 and h > 0 and l > 0 and c > 0): continue
        s = m - (m - 555) % 5
        k = f"{s // 60:02d}:{s % 60:02d}"
        b = out.get(k)
        if b is None: out[k] = [o, h, l, c, v]
        else: b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = c; b[4] += v
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--src", default="nse-intraday-data"); ap.add_argument("--out", default="intraday_data")
    ap.add_argument("--from-year", type=int, default=2012); ap.add_argument("--to-year", type=int, default=2021); a = ap.parse_args()
    src, out = Path(a.src), Path(a.out); out.mkdir(parents=True, exist_ok=True)
    files = []
    for f in src.rglob("*.txt"):
        if any(x.endswith("_files") or x == "Upto_2011" for x in f.parts): continue
        nm = f.stem
        if re.search(r"_F\d$", nm) or "-" in nm or "&" in nm: continue
        d = day_of(f)
        if d and a.from_year <= d.year <= a.to_year: files.append((d, "NIFTY50" if nm == "NIFTY" else nm, f))
    files.sort()
    print("files:", len(files), flush=True)
    W, seen, days, bars = {}, set(), Counter(), Counter()
    skip_idx = {"BANKNIFTY", "NIFTYIT", "NIFTYBANK", "NIFTYAUTO", "NIFTYMETAL", "NIFTYINFRA", "NIFTYBEES", "NIFTYPHARMA", "NIFTYFMCG",
                "NIFTYENERGY", "NIFTYREALTY", "NIFTYMEDIA", "NIFTYPSE", "NIFTYPSUBANK", "NIFTYFINSERVICE", "NIFTYMIDCAP", "INDIAVIX", "SENSEX"}
    for n, (d, sym, f) in enumerate(files, 1):
        if sym in skip_idx or (d, sym) in seen: continue
        seen.add((d, sym))
        try: b = bars5(f.read_text(errors="ignore").splitlines())
        except Exception: continue
        if len(b) < 60: continue
        w = W.get(sym)
        if w is None:
            w = W[sym] = gzip.open(out / f"{sym}.csv.gz", "wt"); w.write("time,open,high,low,close,volume\n")
        for k in sorted(b):
            o, h, l, c, v = b[k]; w.write(f"{d}T{k}:00,{o},{h},{l},{c},{int(v)}\n")
        days[d.year] += sym == "NIFTY50"; bars[sym] += len(b)
        if n % 50000 == 0: print(n, "files read", d, flush=True)
    for w in W.values(): w.close()
    info = {"source": "github.com/gsidhu/nse-intraday-data (unofficial 1-minute archive), 5-minute bars, prices not adjusted",
            "years": f"{a.from_year}-{a.to_year}", "stocks": len(W) - ("NIFTY50" in W), "nifty_days_per_year": dict(sorted(days.items()))}
    (out / "_info.json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info, indent=1))
    print("stocks with the most data:", bars.most_common(5))


if __name__ == "__main__":
    main()
