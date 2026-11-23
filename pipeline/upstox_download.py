"""Downloads 5-minute bars from Upstox's public Historical Candle Data V3 API for the intraday research (no login, no keys,
read-only market data). Upstox keeps minute bars from January 2022; prices are adjusted for splits and bonuses.

Run by .github/workflows/upstox_data.yml. Output: one CSV.gz per stock (time, open, high, low, close, volume) in --out, plus
NIFTY50.csv.gz, the same format as pipeline/angel_download.py, so pipeline/intraday_research.py reads both.
Upstox serves one month of 5-minute bars per request; requests are paced and slowed down automatically if Upstox refuses them."""
from __future__ import annotations

import argparse, datetime as dt, gzip, json, sys, time
from pathlib import Path
from urllib.parse import quote

import requests

HERE = Path(__file__).resolve().parent
API = "https://api.upstox.com/v3/historical-candle/"
MASTER = "https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz"
NIFTY = ("NIFTY50", "NSE_INDEX|Nifty 50")
PACE = [0.9]                                     # seconds between requests (Upstox: 2000 per 30 minutes); grows if refused
S = requests.Session(); S.headers["Accept"] = "application/json"


def keys():
    """Symbol -> Upstox instrument key ("NSE_EQ|<ISIN>"): Upstox's instrument file, else the ISINs in NSE's NIFTY 500 list."""
    k = {}
    try:
        r = S.get(MASTER, timeout=120); r.raise_for_status()
        m = json.loads(gzip.decompress(r.content))
        k = {x["trading_symbol"]: x["instrument_key"] for x in m if x.get("segment") == "NSE_EQ" and x.get("instrument_type") == "EQ"}
    except Exception as e:
        print("Upstox instrument file not read:", str(e)[:150], flush=True)
    if len(k) < 400:
        import csv
        for url in ("https://archives.nseindia.com/content/indices/ind_nifty500list.csv", "https://www.niftyindices.com/IndexConstituent/ind_nifty500list.csv"):
            try:
                r = requests.get(url, timeout=60, headers={"User-Agent": "Mozilla/5.0"}); r.raise_for_status()
                for row in csv.DictReader(r.text.splitlines()):
                    row = {c.strip(): v.strip() for c, v in row.items() if c}
                    if row.get("Symbol") and row.get("ISIN Code"): k.setdefault(row["Symbol"], "NSE_EQ|" + row["ISIN Code"])
                print("ISINs from", url, flush=True); break
            except Exception as e:
                print("NSE list not read:", url, str(e)[:120], flush=True)
    return k


def liquid(symbols, n):
    """Top n NIFTY 500 stocks by median daily traded value over the last 3 months (Yahoo daily bars)."""
    import yfinance as yf
    tv = {}
    for k in range(0, len(symbols), 100):
        d = yf.download([s + ".NS" for s in symbols[k:k + 100]], period="3mo", progress=False, group_by="ticker", auto_adjust=False, threads=True)
        for s in symbols[k:k + 100]:
            try: x = d[s + ".NS"].dropna(); tv[s] = float((x["Close"] * x["Volume"]).median())
            except Exception: pass
    return sorted(tv, key=lambda s: -tv[s])[:n]


def months(start, end):
    a = start.replace(day=1)
    while a <= end:
        b = (a.replace(day=28) + dt.timedelta(days=4)).replace(day=1) - dt.timedelta(days=1)
        yield max(a, start), min(b, end)
        a = b + dt.timedelta(days=1)


LAST = [0.0]; ERRORS = {}


def candles(key, a, b):
    url = f"{API}{quote(key, safe='')}/minutes/5/{b}/{a}"
    for k in range(7):
        d = LAST[0] + PACE[0] - time.time()
        if d > 0: time.sleep(d)
        LAST[0] = time.time()
        try:
            r = S.get(url, timeout=60)
            j = r.json() if r.headers.get("content-type", "").startswith("application/json") else {}
            if r.status_code == 200 and j.get("status") == "success": return j["data"]["candles"]
            msg = f"HTTP {r.status_code} {str(j.get('errors') or r.text[:120])[:160]}"
        except Exception as e:
            msg = "error: " + str(e)[:150]
        ERRORS[msg] = ERRORS.get(msg, 0) + 1
        if ERRORS[msg] <= 3: print(f"  {key} {a}..{b}: {msg}", flush=True)
        if "HTTP 429" in msg: PACE[0] = min(PACE[0] * 1.5, 8.0); time.sleep(30 * (k + 1)); continue
        if msg.startswith("HTTP 400"): return []                      # e.g. dates before the stock listed
        time.sleep(3 + 5 * k)
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--from-date", default="2022-01-01"); ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--out", default="intraday_data"); ap.add_argument("--minutes", type=float, default=330); a = ap.parse_args(); t0 = time.time()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    pick = liquid(syms, a.stocks); ks = keys()
    todo = [(s, ks[s]) for s in pick if s in ks]
    print("stocks picked by traded value:", len(pick), "| Upstox keys found:", len(todo), "| missing:", [s for s in pick if s not in ks][:20], flush=True)
    start, end = dt.date.fromisoformat(a.from_date), dt.date.today() - dt.timedelta(days=1)
    failed = []
    for n, (s, key) in enumerate([NIFTY] + todo, 1):
        if (time.time() - t0) / 60 > a.minutes: print(f"time limit reached after {n - 1} files: stopping cleanly", flush=True); break
        rows = []
        for x, y in months(start, end):
            d = candles(key, x, y)
            if d is None: failed.append(f"{s} {x}..{y}"); continue
            rows += d
        if rows:
            with gzip.open(out / f"{s}.csv.gz", "wt") as f:
                f.write("time,open,high,low,close,volume\n")
                for r in sorted({r[0]: r for r in rows}.values()): f.write(",".join(str(v) for v in r[:6]) + "\n")
        first = min(r[0] for r in rows)[:10] if rows else "-"
        print(f"{n}/{len(todo) + 1} {s}: {len(rows)} bars from {first} (pace {PACE[0]:.1f}s, {time.strftime('%H:%M:%S')})", flush=True)
        if n == 1 and not rows: sys.exit("no bars for NIFTY 50: stopping (see the messages above)")
    (out / "_info.json").write_text(json.dumps({"source": "Upstox Historical Candle Data V3 (public)", "from": str(start), "to": str(end),
                                                "interval": "5 minute", "files": len(list(out.glob("*.csv.gz"))), "failed_chunks": failed,
                                                "errors": ERRORS}, indent=1))
    print("done; failed chunks:", len(failed))


if __name__ == "__main__":
    main()
