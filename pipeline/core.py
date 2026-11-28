"""Shared logic for the TrendLens momentum screener (rule: 12-month return up to now).
The rank is evidence for the ideas pipeline (trading/arena). Nothing here opens or closes an idea: a decision-maker approves and closes."""
import json, datetime as dt, requests
from concurrent.futures import ThreadPoolExecutor

ENTRY_RANK, EXIT_RANK, UNIVERSE = 20, 50, 500
UNIVERSE_LABEL = "NIFTY 500"
MIN_TURNOVER_CR, MIN_PRICE = 10, 50     # ranked only if 20-day median traded value >= Rs 10 cr a day and price >= Rs 50 (Nov 2026 audit)
STATS = {"rule": "NIFTY 500 stocks ranked by 12-month return divided by 12-month volatility. Enter at the top 20 (next open), exit below #50, 6x ATR safety stop, at most 3 per industry and 6 per broad sector. Re-tested Nov 2026 with pipeline/backtest.py: liquidity floor scaled to the market level, Indian delivery charges and 0.1% slippage each side. The stock list is today's NIFTY 500, so stocks dropped or delisted since are missing, which flatters the results. Live, stocks on NSE ASM/GSM surveillance are skipped; the back-test cannot apply that (no free historical lists). Realistic expectation: about 18-25% a year, with drops of 30-40%",
         "realistic": "18-25% a year, with drops of 30-40%", "source": "web/backtest.json", "benchmark": "NIFTY 500 (price index)", "periods": [
    {"period": "2011–2019", "cagr": 21.2, "nifty": 7.9, "maxdd": -28.1, "trades": 495, "win": 47.9, "avg_win": 31.4, "avg_loss": -10.0, "med_days": 44},
    {"period": "2020–2026", "cagr": 45.4, "nifty": 13.8, "maxdd": -37.9, "trades": 341, "win": 50.4, "avg_win": 55.6, "avg_loss": -12.7, "med_days": 67}]}

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

def momentum_score(past, price):
    """THE momentum score, shared by the live ranking and pipeline/backtest.py (audit fix #9: one function, no drift).
    past: daily closes up to the previous session (None allowed for missing days); price: today's price.
    Returns (12-month return, score) or None. 12-month return = price / close 252 sessions ago - 1;
    score = that return / annualised volatility of the last 252 daily returns (including today's move)."""
    if price is None or len(past) < 252 or not past[-252]:
        return None
    cc = [x for x in list(past[-252:]) + [price] if x]
    rets = [cc[i] / cc[i - 1] - 1 for i in range(1, len(cc))]
    if len(rets) < 200:
        return None
    m = sum(rets) / len(rets); vol = (sum((r - m) ** 2 for r in rets) / (len(rets) - 1)) ** 0.5 * 252 ** 0.5
    if vol <= 0:
        return None
    mom = price / past[-252] - 1
    return mom, mom / vol


def ranks_now(hist, prices, today):
    """Risk-adjusted momentum rank on today's prices, via momentum_score (the back-tested function)."""
    dates = hist["dates"]; today_in = dates[-1] == today        # if today's bar is in history, the past ends the session before
    mom, score = {}, {}
    for s in hist.get("rank_universe") or hist["universe"]:
        c = hist["closes"].get(s)
        if not c: continue
        r = momentum_score(c[:-1] if today_in else c, prices.get(s))
        if r: mom[s], score[s] = r
    order = sorted(score, key=lambda s: (-score[s], s))
    return {s: i + 1 for i, s in enumerate(order)}, mom

def rank_exit_prices(hist, prices, today, syms, exit_rank=None):
    """Nov 2026: for each momentum idea, the estimated closing price below which it would drop out of the top `exit_rank`
    (the rank exit), with every other stock at its current price. Shown on the site as one "Exit below" price instead of the rank
    rule. The level moves when other stocks move, so it is recomputed after every close. {sym: price or None}."""
    exit_rank = exit_rank or EXIT_RANK
    dates = hist["dates"]; today_in = dates[-1] == today
    past = lambda s: (lambda c: (c[:-1] if today_in else c) if c else None)(hist["closes"].get(s))
    score = {}
    for s in hist.get("rank_universe") or hist["universe"]:
        c = past(s); r = momentum_score(c, prices.get(s)) if c else None
        if r: score[s] = r[1]
    out = {}
    for sym in syms:
        others = sorted((v for s, v in score.items() if s != sym), reverse=True)
        c, p0 = past(sym), prices.get(sym)
        if len(others) < exit_rank or not c or not p0: out[sym] = None; continue
        thr = others[exit_rank - 1]                        # the stock must score at least as high as the 50th of the others
        f = lambda p: (momentum_score(c, p) or (None, None))[1]
        hi, lo = float(p0), float(p0) * 0.3
        if f(hi) is None or f(hi) < thr: out[sym] = round(hi, 2); continue          # already below: exit level = today's price
        if f(lo) is not None and f(lo) >= thr: out[sym] = None; continue           # no level within 70% of the price
        for _ in range(40):
            mid = (hi + lo) / 2; v = f(mid)
            if v is not None and v >= thr: hi = mid
            else: lo = mid
        out[sym] = round(hi, 2)
    return out


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


def refresh_live(state, rk, prices, nifty_now, now, blocked=None, results=None, n500_now=None):
    """Updates price, rank, NIFTY comparison and NSE flags on live ideas. Opens and closes nothing."""
    date = now.date().isoformat()
    for e in state["live"]:
        s = e["sym"]
        e["results_date"] = (results or {}).get(s) if (results or {}).get(s, "") >= date else None
        e["surveillance"] = (blocked or {}).get(s)
        e["rank_now"] = rk.get(s); e["price_now"] = round(prices.get(s, e.get("price_now", e["entry_price"])), 2)
        if nifty_now and e.get("nifty_entry"):
            e["nifty_pct"] = round((nifty_now / e["nifty_entry"] - 1) * 100, 2)
        if n500_now and e.get("n500_entry"):
            e["n500_pct"] = round((n500_now / e["n500_entry"] - 1) * 100, 2)


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
            if s and s != "nan":
                results.setdefault(s, []).append(d)
    today = ist_now().date().isoformat()
    # the NEXT results date (earliest one today or later); a stock with only past dates keeps its latest past date
    results = {s: (min([x for x in v if x >= today]) if any(x >= today for x in v) else max(v)) for s, v in results.items()}
    return blocked, results
