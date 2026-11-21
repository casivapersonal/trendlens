"""Idea review layer: the pieces of the Day Trader skill (entry timer, 1R exit, risk check) and the committee's bull / bear debate,
packaged as plain functions on one idea. Everything here is deterministic and informational; the decision-maker still approves and closes.
Only the risk check can stop an idea from being approved (unless forced); the debate and the Time Machine are evidence."""
from __future__ import annotations



# ---------------------------------------------------------------- committee debate (bull vs bear vs economic)
def debate(memo: dict, macro: dict | None) -> dict:
    """Bull analyst and bear analyst each list their case from the same evidence the judge scores; the economic analyst reports the macro backdrop."""
    a = memo["analysts"]; m = a["technical"]["metrics"]; bull, bear = [], []
    if m.get("rank") is not None and m["rank"] <= 20: bull.append(f"momentum rank #{m['rank']}")
    if m["setups"]: bull.append("fresh daily setup: " + ", ".join(m["setups"]))
    if m["sma50"] and m["sma200"] and m["close"] > m["sma50"] > m["sma200"]: bull.append("price above a rising 50/200-day structure")
    if m["macd_above_signal"]: bull.append("MACD above signal")
    if m["close"] >= 0.9 * m["hi52"]: bull.append("within 10% of the 52-week high")
    f, n = a["fundamentals"]["stance"], a["news"]["stance"]
    if f == "supportive": bull.append("fundamentals supportive")
    if n == "positive flags": bull.append("positive headline flags")
    if m["rsi"] > 75: bear.append(f"RSI {m['rsi']:.0f}: stretched")
    if m["ext50"] > 0.25: bear.append(f"{m['ext50']*100:.0f}% above the 50-day average: extended")
    if m["ret5"] > 0.15: bear.append(f"up {m['ret5']*100:.0f}% in 5 days: chasing risk")
    if m["sma50"] and m["close"] < m["sma50"]: bear.append("below the 50-day average")
    if m["sma200"] and m["close"] < m["sma200"]: bear.append("below the 200-day average")
    if m["atr_pct"] > 4: bear.append(f"volatile (daily ATR {m['atr_pct']:.1f}%)")
    if f == "concerning": bear.append("fundamentals concerning")
    if n == "negative flags": bear.append("negative headline flags: " + ", ".join(a["news"].get("flags", [])))
    econ = macro or {}
    if econ.get("stance") == "cautious": bear.append("economic backdrop cautious: " + "; ".join(econ.get("reasons", [])[:2]))
    lean = "bull" if len(bull) - len(bear) >= 2 else "bear" if len(bear) - len(bull) >= 2 else "balanced"
    return {"bull": bull[:6], "bear": bear[:6], "lean": lean,
            "economic": {"stance": econ.get("stance", "no data"), "reasons": econ.get("reasons", []), "data": econ.get("data", {})}}


# ---------------------------------------------------------------- entry timer (trader-entry)
def entry_state(idea: dict, price: float | None = None) -> str:
    """Where the price is against the idea's entry range. Informational: nothing is bought by this system."""
    p = float(price if price is not None else idea.get("price_now") or idea["entry_price"])
    lo, hi, stop = idea.get("entry_lo"), idea.get("entry_hi"), idea.get("stop")
    if stop and p <= stop: return "invalidated: price is at or below the stop level"
    if hi and p > hi: return "waiting: above the entry range (do not chase; a pullback into the range is the entry)"
    if lo and p < lo: return "waiting: below the entry range (let it recover into the range first)"
    return "triggered: price is inside the entry range"


# ---------------------------------------------------------------- 1R exit rule (trader-exit)
def one_r_advice(idea: dict, high_seen: float | None) -> dict | None:
    """Day Trader's 1R idea on a swing idea: once price has gained one stop-distance, advise moving the stop to the entry price. Advice only; the stop level is unchanged."""
    e, s = idea.get("entry_price"), idea.get("stop")
    if not e or not s or e <= s or high_seen is None: return None
    r = e - s
    return {"move_stop_to": round(e, 2), "reason": "price has gained 1R (one stop distance); consider protecting the position"} if high_seen >= e + r else None


# ---------------------------------------------------------------- budget (trader-budget) and risk check (trader-risk)
def risk_check(idea: dict, cfg: dict, live_count: int) -> dict:
    """PASS/BLOCK with reasons, on the idea itself (no account, capital or position size is assumed):
    reward-to-risk for short-term ideas, a stop that is not absurdly wide, and no signal on a one-day jump that may be a
    split or bonus the data has not adjusted for yet."""
    ic, why = cfg.get("ideas", {}), []
    term, e, st = idea.get("term"), idea.get("entry_price"), idea.get("stop")
    if term != "medium" and idea.get("r") is not None and idea["r"] < ic.get("min_rr", 2.0): why.append(f"reward-to-risk {idea['r']} is below {ic.get('min_rr', 2.0)}")
    if live_count >= ic.get("max_live", 20): why.append(f"already {live_count} live ideas (max {ic.get('max_live', 20)})")
    if term == "short" and e and st and (1 - st / e) * 100 > ic.get("max_stop_pct_short", 12): why.append(f"stop-loss is {(1 - st / e) * 100:.1f}% away (max {ic.get('max_stop_pct_short', 12)}% for short-term ideas)")
    if idea.get("ret1") is not None and abs(idea["ret1"]) > ic.get("max_day_move", 0.35): why.append(f"moved {idea['ret1'] * 100:.0f}% in one day: check for a split, bonus or other corporate action first")
    return {"verdict": "BLOCK" if why else "PASS", "reasons": why}
