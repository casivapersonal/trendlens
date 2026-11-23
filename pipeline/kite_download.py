"""Downloads years of 5-minute bars from Zerodha Kite Connect for the intraday research (read-only market data; no orders).

Run by .github/workflows/kite_data.yml. GitHub secrets: KITE_API_KEY, KITE_API_SECRET and KITE_REQUEST_TOKEN (the
request_token from today's Kite login; it is single-use and expires within minutes, so run the workflow right after adding it).
Output: one CSV.gz per stock (time, open, high, low, close, volume) in --out, plus NIFTY50.csv.gz, the same format as
pipeline/angel_download.py, so pipeline/intraday_research.py reads both. Kite serves up to 100 days of 5-minute bars per request
and allows 3 requests a second; requests are paced just under that."""
from __future__ import annotations

import argparse, csv, datetime as dt, gzip, hashlib, io, json, os, sys, time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
API = "https://api.kite.trade"
NIFTY = ("NIFTY50", 256265)                      # NSE:NIFTY 50 instrument token
GAP = 0.36                                       # seconds between requests (Kite: 3 a second)


def session():
    need = ["KITE_API_KEY", "KITE_API_SECRET", "KITE_REQUEST_TOKEN"]
    miss = [k for k in need if not os.environ.get(k)]
    if miss: sys.exit("missing GitHub secrets: " + ", ".join(miss))
    key, sec, req = (os.environ[k].strip() for k in need)
    chk = hashlib.sha256((key + req + sec).encode()).hexdigest()
    r = requests.post(API + "/session/token", data={"api_key": key, "request_token": req, "checksum": chk},
                      headers={"X-Kite-Version": "3"}, timeout=30)
    j = r.json()
    if j.get("status") != "success":
        sys.exit("Kite login failed: " + str(j.get("message")) + " (the request token works once and only for a few minutes: log in again, update KITE_REQUEST_TOKEN, run straight away)")
    print("logged in to Kite Connect (market data only)", flush=True)
    return {"X-Kite-Version": "3", "Authorization": f"token {key}:{j['data']['access_token']}"}


def instruments():
    r = requests.get(API + "/instruments/NSE", timeout=120); r.raise_for_status()
    return {row["tradingsymbol"]: int(row["instrument_token"]) for row in csv.DictReader(io.StringIO(r.text))
            if row.get("segment") == "NSE" and row.get("instrument_type") == "EQ"}


def liquid(symbols, n):
    import yfinance as yf
    tv = {}
    for k in range(0, len(symbols), 100):
        d = yf.download([s + ".NS" for s in symbols[k:k + 100]], period="3mo", progress=False, group_by="ticker", auto_adjust=False, threads=True)
        for s in symbols[k:k + 100]:
            try: x = d[s + ".NS"].dropna(); tv[s] = float((x["Close"] * x["Volume"]).median())
            except Exception: pass
    return sorted(tv, key=lambda s: -tv[s])[:n]


def chunks(start, end, days=95):
    a = start
    while a <= end:
        b = min(a + dt.timedelta(days=days), end)
        yield a, b
        a = b + dt.timedelta(days=1)


LAST = [0.0]; ERRORS = {}


def candles(hdr, token, a, b):
    url = f"{API}/instruments/historical/{token}/5minute"
    for k in range(6):
        d = LAST[0] + GAP - time.time()
        if d > 0: time.sleep(d)
        LAST[0] = time.time()
        try:
            r = requests.get(url, params={"from": f"{a} 09:15:00", "to": f"{b} 15:30:00"}, headers=hdr, timeout=60)
            j = r.json()
            if r.status_code == 200 and j.get("status") == "success": return j["data"]["candles"]
            msg = f"HTTP {r.status_code} {j.get('error_type', '')} {j.get('message', '')}"
        except Exception as e:
            msg = "error: " + str(e)[:150]
        ERRORS[msg] = ERRORS.get(msg, 0) + 1
        if ERRORS[msg] <= 3: print(f"  {token} {a}..{b}: {msg}", flush=True)
        if "TokenException" in msg or "PermissionException" in msg:
            sys.exit("Kite refused the request (" + msg + "): the Connect plan with historical data must be active, and the login must be today's")
        if "InputException" in msg: return []                       # e.g. dates before the instrument existed
        time.sleep(2 + 3 * k)
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--from-date", default="2015-02-01"); ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--out", default="intraday_data"); ap.add_argument("--minutes", type=float, default=330); a = ap.parse_args(); t0 = time.time()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    hdr = session()                                                   # first: the request token expires within minutes
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    pick = liquid(syms, a.stocks); ins = instruments()
    tok = [(s, ins[s]) for s in pick if s in ins]
    print("stocks picked by traded value:", len(pick), "| Kite tokens found:", len(tok), "| missing:", [s for s in pick if s not in ins][:20], flush=True)
    start, end = dt.date.fromisoformat(a.from_date), dt.date.today() - dt.timedelta(days=1)
    failed = []
    for n, (s, t) in enumerate([NIFTY] + tok, 1):
        if (time.time() - t0) / 60 > a.minutes: print(f"time limit reached after {n - 1} files: stopping cleanly", flush=True); break
        rows = []
        for x, y in chunks(start, end):
            d = candles(hdr, t, x, y)
            if d is None: failed.append(f"{s} {x}..{y}"); continue
            rows += d
        if rows:
            with gzip.open(out / f"{s}.csv.gz", "wt") as f:
                f.write("time,open,high,low,close,volume\n")
                for r in sorted({r[0]: r for r in rows}.values()): f.write(",".join(str(v) for v in r[:6]) + "\n")
        print(f"{n}/{len(tok) + 1} {s}: {len(rows)} bars from {rows[0][0][:10] if rows else '-'} ({time.strftime('%H:%M:%S')})", flush=True)
        if n == 1 and not rows: sys.exit("no bars for NIFTY 50: stopping (see the messages above)")
    (out / "_info.json").write_text(json.dumps({"source": "Zerodha Kite Connect", "from": str(start), "to": str(end), "interval": "5 minute",
                                                "files": len(list(out.glob("*.csv.gz"))), "failed_chunks": failed, "errors": ERRORS}, indent=1))
    print("done; failed chunks:", len(failed))


if __name__ == "__main__":
    main()
