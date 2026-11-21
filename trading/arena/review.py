"""Idea review layer: the pieces of the Day Trader skill (entry timer, 1R exit, budget, risk check) and the committee's bull / bear debate,
packaged as plain functions on one idea. Everything here is deterministic and informational; the decision-maker still approves and closes.
Only the risk check can stop an idea from being approved (unless forced); the debate and the Time Machine are evidence."""
from __future__ import annotations

from . import charges as ch


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
def size_idea(cfg: dict, entry: float, stop: float) -> dict:
    """Quantity so the loss at the stop, charges included (delivery), stays within the loss limit ([risk].max_loss_pct of the account), and the position within
    [strategy].max_position_pct of [account].starting_capital. A worked example on the configured paper account, not advice."""
    from .riskplan import loss_limit
    lim = loss_limit(cfg); cap = float(cfg.get("account", {}).get("starting_capital", 0) or 0)
    pos_cap = cap * float(cfg.get("strategy", {}).get("max_position_pct", 100)) / 100 if cap else 0
    rates, per = cfg.get("charges", {}), entry - stop
    if per <= 0: return {"qty": 0, "note": "no valid stop distance"}
    qty = int(lim // per) if lim > 0 else (int(pos_cap // entry) if pos_cap else 0)
    over_cap = bool(pos_cap) and entry > pos_cap
    if pos_cap: qty = min(qty, int(pos_cap // entry)) if not over_cap else min(qty, 1)   # one share above the example cap is still shown
    while qty > 0 and lim > 0 and qty * per + ch.compute("BUY", qty, entry, "delivery", rates).total + ch.compute("SELL", qty, stop, "delivery", rates).total > lim: qty -= 1
    loss = qty * per + (ch.compute("BUY", qty, entry, "delivery", rates).total + ch.compute("SELL", qty, stop, "delivery", rates).total if qty else 0)
    return {"qty": qty, "value": round(qty * entry, 2), "loss_at_stop": round(loss, 2), "max_loss": lim, "capital": cap,
            "note": ("one share costs more than the example position cap; shown as 1 share" if over_cap else "example sizing on the configured paper account") + "; loss at the stop price, a gap can be worse"}


def risk_check(idea: dict, cfg: dict, live_count: int) -> dict:
    """PASS/BLOCK with reasons, like trader-risk: reward-to-risk, room for another idea, and a position that fits the loss limit."""
    ic, why = cfg.get("ideas", {}), []
    if idea["r"] < ic.get("min_rr", 2.0): why.append(f"reward-to-risk {idea['r']} is below {ic.get('min_rr', 2.0)}")
    if live_count >= ic.get("max_live", 10): why.append(f"already {live_count} live ideas (max {ic.get('max_live', 10)})")
    sz = idea.get("size", {})
    if sz.get("qty", 1) < 1:
        cap = float(cfg.get("account", {}).get("starting_capital", 0) or 0) * float(cfg.get("strategy", {}).get("max_position_pct", 100)) / 100
        why.append(f"one share costs more than the position cap (Rs {cap:,.0f})" if cap and idea.get("entry_price", 0) > cap
                   else "stop is too wide for the max loss per trade")
    return {"verdict": "BLOCK" if why else "PASS", "reasons": why}
