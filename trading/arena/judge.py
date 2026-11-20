"""The judge: plain code, no AI. Scores a candidate from the committee's evidence and returns APPROVE / WATCH / REJECT with every rule that fired.
APPROVE only means "worth the decision-maker's time". A human decision-maker still approves before anything is posted.
Hard rejects are risk controls. The score weights are starting points and have NOT been back-tested as a whole."""
from __future__ import annotations


def judge(memo: dict, macro: dict, cfg: dict, live_ids: set[str] | None = None, sym: str = "", blocked: dict | None = None) -> dict:
    ic = cfg.get("ideas", {}); a = memo["analysts"]; m = a["technical"]["metrics"]
    score, hits, hard = 0, [], []

    def add(n, why):
        nonlocal score
        score += n; hits.append({"points": n, "rule": why})

    # ---- hard rejects (risk controls) ----
    if blocked and sym in blocked: hard.append(f"{blocked[sym]} (NSE surveillance)")
    if live_ids and sym in live_ids: hard.append("already a live idea")
    if m["atr_pct"] > ic.get("max_atr_pct", 7): hard.append(f"too volatile (daily ATR {m['atr_pct']:.1f}%)")
    if a["news"]["stance"] == "negative flags" and ic.get("news_hard_reject", True): hard.append("negative headline flags: " + ", ".join(a["news"].get("flags", [])))
    # ---- technical ----
    er, rk = ic.get("entry_rank", 20), m.get("rank")
    if rk is not None:
        if rk <= er: add(2, f"momentum rank #{rk} of the 300 most-traded stocks (the original entry rule: top {er})")
    elif (m.get("mom_pct") or 0) >= 0.95: add(2, "top 5% risk-adjusted 12-month momentum")
    if m["setups"]: add(2, "daily setup: " + ", ".join(m["setups"]))
    if m["sma50"] and m["close"] > m["sma50"]: add(1, "above 50-day average")
    if m["sma200"] and m["close"] > m["sma200"]: add(1, "above 200-day average")
    if m["sma50"] and m["sma200"] and m["sma50"] > m["sma200"]: add(1, "50-day above 200-day")
    if m["macd_above_signal"]: add(1, "MACD above signal")
    if 50 <= m["rsi"] <= 75: add(1, "RSI in the 50-75 trend zone")
    if m["close"] >= 0.9 * m["hi52"]: add(1, "within 10% of the 52-week high")
    if m["rsi"] > 85: add(-2, f"RSI {m['rsi']:.0f}: stretched")
    if m["ext50"] > 0.40: add(-2, f"{m['ext50']*100:.0f}% above the 50-day average: extended")
    elif m["ext50"] > 0.25: add(-1, f"{m['ext50']*100:.0f}% above the 50-day average")
    if m["ret5"] > 0.20: add(-1, f"up {m['ret5']*100:.0f}% in 5 days: chasing risk")
    # ---- Time Machine (evidence, small weight; not back-tested as a filter) ----
    tm = memo.get("timemachine")
    if tm and tm.get("real", {}).get("sessions", 0) >= 20:
        pts = ic.get("timemachine_points", 1)
        if tm["verdict"].startswith("past sessions lost money"):
            add(-pts, "Time Machine: " + tm["verdict"])
            if ic.get("timemachine_gate", False): hard.append("Time Machine gate: " + tm["verdict"])
        elif tm["verdict"] == "tilted toward the target": add(pts, "Time Machine: tilted toward the target")
    # ---- other analysts (evidence, small weights) ----
    if a["fundamentals"]["stance"] == "supportive": add(1, "fundamentals supportive")
    elif a["fundamentals"]["stance"] == "concerning": add(-1, "fundamentals concerning")
    if a["news"]["stance"] == "positive flags": add(1, "positive headline flags")
    if (macro or {}).get("stance") == "cautious": add(-1, "macro cautious: " + "; ".join(macro.get("reasons", [])[:2]))
    verdict = "REJECT" if hard else "APPROVE" if score >= ic.get("approve_score", 6) else "WATCH" if score >= ic.get("watch_score", 4) else "REJECT"
    if not hard and verdict == "REJECT": hard = [f"score {score} is below the watch level"]
    return {"verdict": verdict, "score": score, "rules": hits, "rejected_because": hard, "by": "coded judge v1 (no AI)"}
