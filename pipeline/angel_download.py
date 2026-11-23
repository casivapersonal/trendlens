"""Downloads years of 5-minute bars from Angel One SmartAPI for the intraday research (read-only market data; no orders).

Run by .github/workflows/intraday_data.yml with the account details kept in GitHub secrets:
  ANGEL_API_KEY, ANGEL_CLIENT_CODE, ANGEL_PIN, ANGEL_TOTP_SECRET
Output: one CSV.gz per stock (time, open, high, low, close, volume) in --out, plus NIFTY 50.
Angel allows up to 100 days of 5-minute bars per request and about 3 requests a second, so requests are chunked and paced."""
from __future__ import annotations

import argparse, datetime as dt, gzip, json, os, sys, time
from pathlib import Path

import requests

HERE = Path(__file__).resolve().parent
MASTER = "https://margincalculator.angelbroking.com/OpenAPI_File/files/OpenAPIScripMaster.json"
NIFTY_TOKEN = ("NIFTY50", "99926000")


def login():
    from SmartApi import SmartConnect
    import pyotp
    need = ["ANGEL_API_KEY", "ANGEL_CLIENT_CODE", "ANGEL_PIN", "ANGEL_TOTP_SECRET"]
    miss = [k for k in need if not os.environ.get(k)]
    if miss: sys.exit("missing GitHub secrets: " + ", ".join(miss))
    api = SmartConnect(api_key=os.environ["ANGEL_API_KEY"])
    s = api.generateSession(os.environ["ANGEL_CLIENT_CODE"], os.environ["ANGEL_PIN"], pyotp.TOTP(os.environ["ANGEL_TOTP_SECRET"]).now())
    if not s or not s.get("status"): sys.exit("Angel One login failed: " + str((s or {}).get("message")))
    print("logged in to Angel One (market data only)")
    return api


def tokens(symbols):
    m = requests.get(MASTER, timeout=120).json()
    eq = {x["symbol"][:-3]: x["token"] for x in m if x.get("exch_seg") == "NSE" and str(x.get("symbol", "")).endswith("-EQ")}
    return {s: eq[s] for s in symbols if s in eq}


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


def chunks(start, end, days=95):
    a = start
    while a <= end:
        b = min(a + dt.timedelta(days=days), end)
        yield a, b
        a = b + dt.timedelta(days=1)


def candles(api, token, a, b, exch="NSE"):
    p = {"exchange": exch, "symboltoken": token, "interval": "FIVE_MINUTE", "fromdate": a.strftime("%Y-%m-%d 09:15"), "todate": b.strftime("%Y-%m-%d 15:30")}
    for k in range(6):
        try:
            r = api.getCandleData(p)
            if r and r.get("status"): return r.get("data") or []
            msg = str((r or {}).get("message", "")) + str((r or {}).get("errorcode", ""))
            if "AB1021" in msg or "many" in msg.lower(): time.sleep(5 + 5 * k); continue
            if "AB1004" in msg or "no data" in msg.lower(): return []
            time.sleep(2 + 2 * k)
        except Exception as e:
            print("  retry after error:", str(e)[:120]); time.sleep(3 + 3 * k)
    return None


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--years", default="max", help="number of years, or max (Angel One has NSE 5-minute data from 2016)"); ap.add_argument("--stocks", type=int, default=200)
    ap.add_argument("--out", default="intraday_data"); a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    syms = [s.strip() for s in open(HERE / "nifty500.txt") if s.strip()]
    pick = liquid(syms, a.stocks); print("stocks picked by traded value:", len(pick))
    tok = tokens(pick); print("Angel tokens found:", len(tok), "missing:", [s for s in pick if s not in tok][:20])
    api = login()
    end = dt.date.today() - dt.timedelta(days=1)
    start = dt.date(2016, 1, 1) if str(a.years).lower() == "max" else max(dt.date(2016, 1, 1), end - dt.timedelta(days=int(365 * float(a.years))))
    todo = [NIFTY_TOKEN] + list(tok.items()); failed = []
    for n, (s, t) in enumerate(todo, 1):
        rows = []
        for x, y in chunks(start, end):
            d = candles(api, t, x, y)
            if d is None: failed.append(f"{s} {x}..{y}"); continue
            rows += d; time.sleep(0.4)
        if rows:
            with gzip.open(out / f"{s}.csv.gz", "wt") as f:
                f.write("time,open,high,low,close,volume\n")
                for r in sorted({r[0]: r for r in rows}.values()): f.write(",".join(str(v) for v in r[:6]) + "\n")
        print(f"{n}/{len(todo)} {s}: {len(rows)} bars", flush=True)
    (out / "_info.json").write_text(json.dumps({"from": str(start), "to": str(end), "interval": "5 minute", "stocks": len(todo), "failed_chunks": failed}, indent=1))
    print("done; failed chunks:", len(failed))


if __name__ == "__main__":
    main()
