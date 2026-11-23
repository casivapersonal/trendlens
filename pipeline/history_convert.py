"""Converts the community 1-minute NSE archive (github.com/gsidhu/nse-intraday-data, a copy of aeron7/nifty-banknifty-intraday-data:
about 100-200 F&O stocks plus NIFTY, 2012 to January 2023, one folder per day) into 5-minute files in the same format as
pipeline/upstox_download.py (SYMBOL.csv.gz with time, open, high, low, close, volume; NIFTY.txt -> NIFTY50.csv.gz), so
pipeline/intraday_research.py reads them.

Lines look like "SBIN,20190401,09:16,322.00,323.60,322.00,323.15,384957,0": each bar is stamped with the minute it ENDS, so
09:16 is 09:15-09:16; older folders hold one file per month, so the date is read from each line. Bars from 09:16 to 15:30 are kept and grouped into 5-minute bars starting 09:15 ... 15:25. Futures files
(_F1, _F2) and other indices are skipped. Prices are as traded (not adjusted for splits or bonuses): fine inside a day; the few
split days show up as large gaps. The archive is unofficial and has missing days; the summary lists days per year.

  python pipeline/history_convert.py --src nse-intraday-data --out intraday_data --from-year 2012 --to-year 2021"""
from __future__ import annotations

import argparse, gzip, json, re
from collections import Counter
from pathlib import Path


def bars5(lines, y0, y1):
    """1-minute lines (one day or a whole month) -> {date: {slot start 'HH:MM': [o, h, l, c, v]}} for 09:15 ... 15:25.
    The date comes from each line (YYYYMMDD), because older folders hold one file per month."""
    out = {}
    for ln in lines:
        p = ln.strip().split(",")
        if len(p) < 8 or len(p[1]) != 8: continue
        try:
            y = int(p[1][:4])
            if y < y0 or y > y1: continue
            hh, mm = int(p[2][:2]), int(p[2][3:5]); o, h, l, c = (float(x) for x in p[3:7]); v = float(p[7])
        except ValueError:
            continue
        m = hh * 60 + mm - 1                           # bar start minute
        if m < 555 or m > 929 or not (o > 0 and h > 0 and l > 0 and c > 0): continue
        s = m - (m - 555) % 5
        d = out.setdefault(f"{p[1][:4]}-{p[1][4:6]}-{p[1][6:]}", {})
        k = f"{s // 60:02d}:{s % 60:02d}"
        b = d.get(k)
        if b is None: d[k] = [o, h, l, c, v]
        else: b[1] = max(b[1], h); b[2] = min(b[2], l); b[3] = c; b[4] += v
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--src", default="nse-intraday-data"); ap.add_argument("--out", default="intraday_data")
    ap.add_argument("--from-year", type=int, default=2012); ap.add_argument("--to-year", type=int, default=2021); a = ap.parse_args()
    src, out = Path(a.src), Path(a.out); out.mkdir(parents=True, exist_ok=True)
    work = out / "_parts"; work.mkdir(exist_ok=True)
    skip_idx = {"BANKNIFTY", "NIFTYIT", "NIFTYBANK", "NIFTYAUTO", "NIFTYMETAL", "NIFTYINFRA", "NIFTYBEES", "NIFTYPHARMA", "NIFTYFMCG",
                "NIFTYENERGY", "NIFTYREALTY", "NIFTYMEDIA", "NIFTYPSE", "NIFTYPSUBANK", "NIFTYFINSERVICE", "NIFTYMIDCAP", "INDIAVIX", "SENSEX",
                "FINNIFTY", "MIDCPNIFTY", "CRUDEOIL", "GOLD", "SILVER", "USDINR", "EURINR", "GBPINR", "JPYINR"}
    files = sorted(f for f in src.rglob("*.txt") if not re.search(r"_F\d$", f.stem) and "-" not in f.stem and "&" not in f.stem
                   and f.stem.upper() not in skip_idx and ".git" not in f.parts)
    print("files:", len(files), flush=True)
    seen, days, W = set(), Counter(), {}
    for n, f in enumerate(files, 1):
        sym = "NIFTY50" if f.stem.upper() in ("NIFTY", "NIFTY50", "NIFTY 50") else f.stem.upper()
        try: B = bars5(f.read_text(errors="ignore").splitlines(), a.from_year, a.to_year)
        except Exception: continue
        for d, b in B.items():
            if len(b) < 60 or (d, sym) in seen: continue
            seen.add((d, sym)); days[d[:4]] += sym == "NIFTY50"
            w = W.get(sym) or W.setdefault(sym, open(work / f"{sym}.txt", "a"))
            for k in b:
                o, h, l, c, v = b[k]; w.write(f"{d}T{k}:00,{o},{h},{l},{c},{int(v)}\n")
        if n % 20000 == 0: print(n, "files read", flush=True)
    for w in W.values(): w.close()
    bars = Counter()
    for f in work.glob("*.txt"):
        L = sorted(set(f.read_text().splitlines())); bars[f.stem] = len(L)
        with gzip.open(out / f"{f.stem}.csv.gz", "wt") as g: g.write("time,open,high,low,close,volume\n" + "\n".join(L) + "\n")
        f.unlink()
    work.rmdir()
    info = {"source": "github.com/gsidhu/nse-intraday-data (unofficial 1-minute archive), 5-minute bars, prices not adjusted",
            "years": f"{a.from_year}-{a.to_year}", "stocks": len(bars) - ("NIFTY50" in bars), "nifty_days_per_year": dict(sorted(days.items())),
            "stock_days_per_year": dict(sorted(Counter(d[:4] for d, s in seen if s != "NIFTY50").items()))}
    (out / "_info.json").write_text(json.dumps(info, indent=1))
    print(json.dumps(info, indent=1))
    print("stocks with the most data:", bars.most_common(5))


if __name__ == "__main__":
    main()
