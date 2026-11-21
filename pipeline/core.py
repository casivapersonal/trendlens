"""Shared logic for the TrendLens momentum screener (rule: 12-month return up to now).
The rank is evidence for the ideas pipeline (trading/arena). Nothing here opens or closes an idea: a decision-maker approves and closes."""
import json, datetime as dt, requests
from concurrent.futures import ThreadPoolExecutor

ENTRY_RANK, EXIT_RANK, UNIVERSE = 20, 50, 500
UNIVERSE_LABEL = "NIFTY 500"
STATS = {"rule": "NIFTY 500 stocks ranked by 12-month return divided by 12-month volatility. Back-test rule: enter at top 20, exit below #50", "periods": [
    {"period": "2011–2019", "cagr": 34.9, "nifty": 8.1, "maxdd": -21.7, "trades": 980, "win": 43.0, "avg_win": 34.7, "avg_loss": -6.8, "med_days": 60},
    {"period": "2020–2026", "cagr": 50.7, "nifty": 11.4, "maxdd": -35.5, "trades": 673, "win": 42.5, "avg_win": 51.0, "avg_loss": -9.1, "med_days": 65}]}

def ist_now():
    return dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)

def market_open(t=None):
    t = t or ist_now()
    return t.weekday() < 5 and dt.time(9, 15) <= t.time() <= dt.time(15, 30)

def quote(sym):
    y = {"NIFTY 50": "^NSEI", "NIFTY BANK": "^NSEBANK"}.get(sym, sym + ".NS")
    try:
        r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(y)}?interval=1m&range=1d",
                         headers={"User-Agent": "Mozilla/5.0 TrendLens"}, timeout=15)
        m = r.json()["chart"]["result"][0]["meta"]
        return sym, {"p": m["regularMarketPrice"], "pc": m.get("chartPreviousClose") or m.get("previousClose"), "t": m.get("regularMarketTime")}
    except Exception:
        return sym, None

def day_meta(ysym):
    """Today's/last session close, high and low from Yahoo's chart meta. Yahoo sometimes leaves the last daily bar's close
    empty for a day or more (seen 20 Nov 2026: volume present, close NaN); the meta still has the final price."""
    try:
        r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(ysym)}?interval=1d&range=5d",
                         headers={"User-Agent": "Mozilla/5.0 TrendLens"}, timeout=15)
        m = r.json()["chart"]["result"][0]["meta"]
        return ysym, {"close": m["regularMarketPrice"], "high": m.get("regularMarketDayHigh"), "low": m.get("regularMarketDayLow"), "t": m.get("regularMarketTime")}
    except Exception:
        return ysym, None

def fill_last_close(frames, cols=("Open", "High", "Low", "Close", "Volume")):
    """frames: {yahoo_symbol: DataFrame}. Fills an empty close on the LAST bar (only when that bar traded) from the chart meta.
    The meta's date must match the bar's date, so a stale price is never written into a new day."""
    O, H, L, C, V = cols
    need = [t for t, x in frames.items() if len(x) and x[C].isna().iloc[-1] and (x[V].iloc[-1] or 0) > 0]
    if not need: return 0
    with ThreadPoolExecutor(16) as ex: metas = dict(ex.map(day_meta, need))
    n = 0
    for t in need:
        m = metas.get(t); x = frames[t]
        if not m or not m["close"] or not m["t"]: continue
        if (dt.datetime.utcfromtimestamp(m["t"]) + dt.timedelta(hours=5, minutes=30)).date() != x.index[-1].date(): continue
        i = x.index[-1]
        x.loc[i, C] = m["close"]
        if x[H].isna().iloc[-1] and m["high"]: x.loc[i, H] = m["high"]
        if x[L].isna().iloc[-1] and m["low"]: x.loc[i, L] = m["low"]
        if x[O].isna().iloc[-1]: x.loc[i, O] = m["close"]
        n += 1
    return n

def quotes(syms):
    with ThreadPoolExecutor(16) as ex:
        return {s: q for s, q in ex.map(quote, syms) if q}

def ranks_now(hist, prices, today):
    """Risk-adjusted momentum: (price now / close 252 sessions ago - 1) / annualised volatility of daily returns."""
    dates = hist["dates"]; k = 253 if dates[-1] == today else 252   # if today's bar is in history, skip it
    mom, score = {}, {}
    for s in hist["universe"]:
        c = hist["closes"].get(s)
        p = prices.get(s)
        if not c or len(c) < k or p is None or not c[-k]:
            continue
        cc = [x for x in c[-253:] if x]
        rets = [cc[i] / cc[i - 1] - 1 for i in range(1, len(cc))]
        if len(rets) < 200:
            continue
        m = sum(rets) / len(rets); vol = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * 252 ** 0.5
        if vol <= 0:
            continue
        mom[s] = p / c[-k] - 1; score[s] = mom[s] / vol
    order = sorted(score, key=lambda s: -score[s])
    return {s: i + 1 for i, s in enumerate(order)}, mom

RANK_EXIT_TERMS = ("momentum", "medium")      # only ideas built on momentum rank are flagged when the rank fades


def rank_exit_flags(state, rk, now):
    """Flags (never closes) live ideas whose momentum rank has faded. The decision-maker closes ideas.
    Short-term setup ideas (breakout, flag, bounce) are not judged on rank: a bounce idea starts from a weak rank by design."""
    date = now.date().isoformat(); flagged = []
    for e in state["live"]:
        if e.get("term", "momentum") not in RANK_EXIT_TERMS:
            e.pop("rank_exit", None); continue
        r = rk.get(e["sym"])
        if r is None or r > EXIT_RANK:
            if not e.get("rank_exit"): flagged.append(e["sym"])
            e["rank_exit"] = dict(date=date, rank=r, reason=("Left the NIFTY 500 ranking" if r is None
                                  else f"Momentum rank fell to #{r} (flag level: below #{EXIT_RANK})"))
        else:
            e.pop("rank_exit", None)
    return flagged


def refresh_live(state, rk, prices, nifty_now, now, blocked=None, results=None):
    """Updates price, rank, NIFTY comparison and NSE flags on live ideas. Opens and closes nothing."""
    date = now.date().isoformat()
    for e in state["live"]:
        s = e["sym"]
        e["results_date"] = (results or {}).get(s) if (results or {}).get(s, "") >= date else None
        e["surveillance"] = (blocked or {}).get(s)
        e["rank_now"] = rk.get(s); e["price_now"] = round(prices.get(s, e.get("price_now", e["entry_price"])), 2)
        if nifty_now and e.get("nifty_entry"):
            e["nifty_pct"] = round((nifty_now / e["nifty_entry"] - 1) * 100, 2)


def fallback_bars(hist, s):
    return [[d, c, c, c, c, 0] for d, c in zip(hist["dates"], hist["closes"][s]) if c]

# ---------------- NSE files uploaded by hand to the repo's nse/ folder ----------------
def _norm(cols):
    return {c: str(c).strip().upper().replace("\n", " ") for c in cols}

def load_nse(folder):
    """Returns (blocked: {sym: reason}, results: {sym: 'YYYY-MM-DD'}) from any CSVs in nse/.
    Surveillance files (ASM/GSM) are recognised by a stage column or 'asm'/'gsm' in the file name;
    board-meeting / event-calendar files by a date column (results only when a purpose column mentions results)."""
    import os, glob, pandas as pd
    blocked, results = {}, {}
    for f in glob.glob(os.path.join(folder, "*.csv")):
        try:
            df = pd.read_csv(f, dtype=str, encoding_errors="ignore")
        except Exception:
            continue
        df = df.rename(columns=_norm(df.columns))
        sym = next((c for c in df.columns if c == "SYMBOL" or c.startswith("SYMBOL")), None)
        if not sym:
            continue
        name = os.path.basename(f).lower()
        stage = next((c for c in df.columns if "STAGE" in c), None)
        if "asm" in name or "gsm" in name or stage:
            tag = "GSM" if "gsm" in name else "ASM"
            for _, r in df.iterrows():
                s = str(r[sym]).strip()
                if s and s != "nan":
                    blocked[s] = f"{tag} surveillance" + (f" ({str(r[stage]).strip()})" if stage else "")
            continue
        dcol = next((c for c in df.columns if "DATE" in c), None)
        pcol = next((c for c in df.columns if "PURPOSE" in c or "SUBJECT" in c or "DESC" in c), None)
        if not dcol:
            continue
        for _, r in df.iterrows():
            if pcol and "RESULT" not in str(r[pcol]).upper():
                continue
            try:
                d = pd.to_datetime(str(r[dcol]).strip(), dayfirst=True).date().isoformat()
            except Exception:
                continue
            s = str(r[sym]).strip()
            if s and s != "nan" and (s not in results or d < results[s]):
                results[s] = d
    return blocked, results
