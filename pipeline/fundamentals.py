"""Download company fundamentals for the NIFTY 500 universe and write web/fundamentals.json.

Source: Yahoo Finance via yfinance (free, no key, same provider as the price feed; research use only).
Usage:  python fundamentals.py ../web/history.json ../web/fundamentals.json

Safe by design: values Yahoo does not return stay None (Genie then says "not available");
symbols that fail keep their previous values; if fewer than half succeed the old file is kept.
Live-price items (P/E, P/B, market cap, dividend yield) are NOT stored; Genie derives them from
the live price at question time, using the EPS, book value, share count and dividend stored here.
"""
import sys, json, time, datetime as dt
from concurrent.futures import ThreadPoolExecutor
import yfinance as yf

def num(x):
    try:
        v = float(x)
        return None if v != v or v in (float("inf"), float("-inf")) else v
    except Exception:
        return None

def pct(x):
    v = num(x)
    return None if v is None else round(v * 100, 2)

def fetch(sym):
    for attempt in range(3):
        try:
            i = yf.Ticker(sym + ".NS").info or {}
            if not (i.get("longName") or i.get("shortName")):
                raise ValueError("empty")
            de = num(i.get("debtToEquity"))
            return sym, {
                "name": i.get("longName") or i.get("shortName"),
                "sector": i.get("sector"), "industry": i.get("industry"),
                "eps_ttm": num(i.get("trailingEps")), "eps_fwd": num(i.get("forwardEps")),
                "book_value": num(i.get("bookValue")), "shares": num(i.get("sharesOutstanding") or i.get("impliedSharesOutstanding") or ((i.get("marketCap") or 0) / i["currentPrice"] if i.get("marketCap") and i.get("currentPrice") else None)),
                "div_rate": num(i.get("dividendRate")),
                "roe_pct": pct(i.get("returnOnEquity")), "roa_pct": pct(i.get("returnOnAssets")),
                "de_ratio": None if de is None else round(de / 100, 2),   # Yahoo gives this in percent
                "rev_growth_pct": pct(i.get("revenueGrowth")), "earn_growth_pct": pct(i.get("earningsGrowth")),
                "net_margin_pct": pct(i.get("profitMargins")), "op_margin_pct": pct(i.get("operatingMargins")),
                "current_ratio": num(i.get("currentRatio")), "beta": num(i.get("beta")),
                "held_insiders_pct": pct(i.get("heldPercentInsiders")), "held_inst_pct": pct(i.get("heldPercentInstitutions")),
            }
        except Exception:
            time.sleep(1.5 * (attempt + 1))
    return sym, None

def main(hist_path, out_path):
    universe = json.load(open(hist_path))["universe"]
    try:
        old = json.load(open(out_path)).get("stocks", {})
    except Exception:
        old = {}
    with ThreadPoolExecutor(6) as ex:
        got = dict(ex.map(fetch, universe))
    ok = {s: v for s, v in got.items() if v}
    print(f"fetched {len(ok)}/{len(universe)}")
    if len(ok) < len(universe) / 2:
        print("too few results; keeping the previous file"); return
    stocks = {s: ok.get(s) or old.get(s) for s in universe if ok.get(s) or old.get(s)}
    out = {"asof": (dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)).strftime("%Y-%m-%d"),
           "source": "Yahoo Finance (yfinance), trailing twelve months / latest reported", "stocks": stocks}
    json.dump(out, open(out_path, "w"), separators=(",", ":"))

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
