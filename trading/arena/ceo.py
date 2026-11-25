"""The CEO: final decision-maker for ideas, in plain code (no AI needed, fully auditable).

Modelled on how an investment firm runs its committee (and on the TradingAgents layout: analysts -> bull/bear
researchers -> risk team -> fund manager). Everything below the CEO only gives evidence; the CEO alone posts or closes.

ENTRY (after every scan): each idea in the queue is read with every input placed before the CEO
  - risk officer verdict (hard: a BLOCK is never overruled)
  - coded judge verdict and score
  - analysts' agreement (technical / fundamentals / news), bull vs bear lean, macro backdrop
  - Time Machine verdict (simulation of the same levels on the stock's own history)
  - house limits: max live ideas, max new per day, max ideas per sector, entry still valid
and gets POST or DECLINE with a written memo and a conviction (High / Medium / Low).
Only the back-tested evidence (momentum rank) can carry an idea past soft doubts; untested evidence can only hold one back.

EXIT (after every monitor): a flagged exit signal (stop, target, time up, momentum rank faded) is closed by the CEO.
Stop and target are price facts; a faded rank is the back-tested exit rule; time up is the idea's own horizon.

Optional: with ANTHROPIC_API_KEY set and [ceo].ai_review = true, Claude reads the same packet and may DECLINE a
POST (never the reverse). Its reasoning is stored with the memo. Leave it off to keep the decision fully rule-based.
"""
from __future__ import annotations

import json
import logging
import os

log = logging.getLogger("arena")
NAME = "CEO"


def _sector(sym: str, funds: dict) -> str:
    """Industry (e.g. "Banks - Regional", "Drug Manufacturers"), not the broad sector: a cap of 3 per broad sector would shut out
    most of the momentum leaders when one theme leads."""
    f = (funds.get("stocks", {}) or {}).get(sym) or {}
    return f.get("industry") or f.get("sector") or "Unknown"


def _conviction(q: dict) -> tuple[str, list[str]]:
    """High / Medium / Low with the reasons, from the evidence on the idea."""
    j, rv, cm = q["judge"], q.get("review") or {}, q.get("committee") or {}
    pts, why = 0, []
    rk = q.get("entry_rank")
    tm = (rv.get("time_machine") or {}).get("verdict", "")
    if q.get("term") == "medium" and rk is not None:
        # momentum ideas: the back-tested rank, plus the (drift-neutral) Time Machine; untested trend points are not counted twice
        pts = 3 if rk <= 5 else 2 if rk <= 12 else 1
        why.append(f"momentum rank #{rk} (the back-tested rule)")
        if tm == "tilted toward the target": pts += 1; why.append("Time Machine tilted toward the target")
        elif tm.startswith(("tilted toward the stop", "past sessions lost money")): pts -= 1; why.append("Time Machine: " + tm)
        return ("High" if pts >= 3 else "Medium" if pts == 2 else "Low"), why
    # setup ideas: conviction is the judge's final score, which already holds every piece of evidence once (audit fix #4)
    sc = j["score"]; why.append(f"judge score {sc}: " + "; ".join(f"{r['points']:+d} {r['rule']}" for r in j.get("rules", [])[:6]))
    level = "High" if sc >= 10 else "Medium" if sc >= 8 else "Low"
    return level, why


def review_entry(q: dict, ctx: dict, cfg: dict) -> dict:
    """POST or DECLINE one queued idea. ctx: live count, posted today, sector counts, fundamentals."""
    c = cfg.get("ceo", {}); ic = cfg.get("ideas", {})
    rv, cm, j = q.get("review") or {}, q.get("committee") or {}, q["judge"]
    no, notes = [], []
    # ---- hard limits: never overruled ----
    if (q.get("risk") or {}).get("verdict") != "PASS": no.append("risk officer blocked it: " + "; ".join(q["risk"].get("reasons", [])))
    momentum0 = q.get("entry_rank") is not None and q["entry_rank"] <= ic.get("entry_rank", 20)
    if j["verdict"] == "REJECT" or (j["verdict"] != "APPROVE" and not momentum0): no.append(f"judge verdict {j['verdict']} (score {j['score']})")
    # audit fix #3: separate books. Momentum has its own 20 slots (as back-tested), setups their own few; one never takes the other's slots
    mom = q.get("term") == "medium"
    if mom and ctx.get("momentum_live", 0) >= ic.get("max_live", 20): no.append(f"momentum book is full ({ctx['momentum_live']} of {ic.get('max_live', 20)})")
    if not mom and ctx.get("short_live", 0) >= c.get("max_live_short", 4):
        no.append(f"setup book is full ({ctx['short_live']} of {c.get('max_live_short', 4)})")
    if not mom and ctx.get("setups_today", 0) >= ic.get("max_new_per_day", 4): no.append(f"already posted {ctx['setups_today']} setup ideas today (limit)")
    sec = _sector(q["sym"], ctx["funds"]); broad = ((ctx["funds"].get("stocks", {}) or {}).get(q["sym"]) or {}).get("sector") or "Unknown"
    book = "m" if mom else "all"          # momentum caps count momentum ideas only (as back-tested); setups count the whole book
    if broad != "Unknown" and ctx.get("broad_" + book, {}).get(broad, 0) >= c.get("max_per_broad_sector", 6):
        no.append(f"sector limit: already {ctx['broad_' + book][broad]} live ideas in {broad}")
    if sec != "Unknown" and ctx.get("sectors_" + book, {}).get(sec, 0) >= c.get("max_per_sector", 3):
        no.append(f"industry limit: already {ctx['sectors_' + book][sec]} live ideas in {sec}")
    if q["sym"] in ctx.get("closed_today", set()): no.append("an idea on this stock was closed today; no re-entry the same day")
    if mom and ic.get("reentry_block", False) and q["sym"] in ctx.get("reentry_block", {}):
        no.append("stopped out earlier: re-entry waits until the rank has reset (below the exit rank) and 10 sessions have passed")
    # ---- soft doubts: the back-tested momentum rank can outweigh them, untested evidence cannot ----
    level, why = _conviction(q)
    momentum = q.get("entry_rank") is not None and q["entry_rank"] <= ic.get("entry_rank", 20)
    # audit fix #4: the doubts below are already inside the judge's score, so they are shown, never counted a second time as vetoes
    doubts = []
    if rv.get("lean") == "bear": doubts.append("bear case outweighs the bull case")
    if (rv.get("time_machine") or {}).get("verdict", "").startswith(("past sessions lost money", "tilted toward the stop")):
        doubts.append("Time Machine: " + rv["time_machine"]["verdict"])
    if (cm.get("analysts", {}).get("technical") or {}).get("stance") == "bearish": doubts.append("technical analyst is bearish")
    if (cm.get("analysts", {}).get("fundamentals") or {}).get("stance") == "concerning": doubts.append("fundamentals concerning")
    if cm.get("macro") == "cautious": doubts.append("economy is cautious")
    notes += [d + (" (noted; the momentum rule carries the idea)" if momentum else " (already in the judge score)") for d in doubts]
    notes += list(j.get("warnings", []))
    decision = "DECLINE" if no else "POST"
    memo = (f"{decision} {q['sym']} ({q['term']} term, conviction {level}). "
            + (("Reasons against: " + "; ".join(no) + ".") if no else ("Case: " + "; ".join(why) + "." if why else "Judge approved."))
            + ((" Watch: " + "; ".join(notes) + ".") if notes else ""))
    return {"decision": decision, "conviction": level, "for": why, "against": no, "noted": notes, "sector": sec, "memo": memo, "by": NAME + " (coded rules)"}


def _ai_second_look(q: dict, verdict: dict, cfg: dict) -> dict | None:
    """Claude may DECLINE a POST it considers clearly unsafe; it can never post on its own."""
    key = os.environ.get("ANTHROPIC_API_KEY")
    if not key or not cfg.get("ceo", {}).get("ai_review"): return None
    try:
        import requests
        packet = {k: q.get(k) for k in ("sym", "name", "term", "entry_price", "entry_lo", "entry_hi", "stop", "target", "days", "entry_rank", "mom_pct", "judge", "review", "committee", "risk")}
        prompt = ("You are the CEO of an Indian equity research desk making the final call on a swing idea that the coded rules want to POST. "
                  "Only DECLINE for a clear, specific risk in the evidence (fraud, regulator action, results in 2 days, broken trend). "
                  'Answer only JSON: {"decision":"POST"|"DECLINE","reason":"one sentence"}.\n' + json.dumps({"idea": packet, "ceo_rules": verdict}, default=str)[:12000])
        r = requests.post("https://api.anthropic.com/v1/messages", timeout=60,
                          headers={"x-api-key": key, "anthropic-version": "2023-06-01", "content-type": "application/json"},
                          json={"model": cfg.get("ceo", {}).get("model") or os.environ.get("ANTHROPIC_MODEL", "claude-sonnet-5-5"), "max_tokens": 200,
                                "messages": [{"role": "user", "content": prompt}]})
        r.raise_for_status()
        t = r.json()["content"][0]["text"]; out = json.loads(t[t.find("{"): t.rfind("}") + 1])
        return {"decision": out.get("decision", "POST"), "reason": out.get("reason", "")}
    except Exception as e:
        log.warning("CEO AI second look failed (rules decision stands): %s", e); return None


def _count(ctx: dict, sym: str, mom: bool, funds: dict) -> None:
    sec = _sector(sym, funds); b = ((funds.get("stocks", {}) or {}).get(sym) or {}).get("sector") or "Unknown"
    for book in (("m", "all") if mom else ("all",)):
        ctx["sectors_" + book][sec] = ctx["sectors_" + book].get(sec, 0) + 1; ctx["broad_" + book][b] = ctx["broad_" + book].get(b, 0) + 1


def decide_entries(cfg: dict, now, ideas_mod, frames_for=None, pending_path=None, public_path=None) -> list[dict]:
    """Runs after a scan: posts what the CEO approves, records every decision on the pending file."""
    from .committee import Committee
    pen, pub = ideas_mod.pending(pending_path), ideas_mod.public(public_path)
    funds = Committee(cfg)._funds(); today = now.date().isoformat()
    ctx = {"live": len(pub["live"]), "momentum_live": sum(1 for i in pub["live"] if i.get("term") == "medium"),
           "short_live": sum(1 for i in pub["live"] if i.get("term") != "medium"),
           "setups_today": sum(1 for i in pub["live"] + pub["closed"] if i.get("posted") == today and i.get("approved_by") == NAME and i.get("term") != "medium"),
           "sectors_m": {}, "broad_m": {}, "sectors_all": {}, "broad_all": {}, "funds": funds,
           "reentry_block": pub.get("reentry_block") or {},
           "closed_today": {c["sym"] for c in pub["closed"] if c.get("closed_at", "")[:10] == today}}
    for i in pub["live"]: _count(ctx, i["sym"], i.get("term") == "medium", funds)
    er = cfg.get("ideas", {}).get("entry_rank", 20)
    out = []
    waiting = [q for q in pen["queue"] if q["status"] == "awaiting decision"]
    # back-tested momentum ideas first (best rank first), then setups by judge score
    waiting.sort(key=lambda x: (not ((x.get("entry_rank") or 999) <= er), (x.get("entry_rank") or 999) if (x.get("entry_rank") or 999) <= er else 0, -x["judge"]["score"]))
    for q in waiting:
        v = review_entry(q, ctx, cfg)
        ai = _ai_second_look(q, v, cfg) if v["decision"] == "POST" else None
        if ai:
            v["ai"] = ai
            if ai["decision"] == "DECLINE": v["decision"] = "DECLINE"; v["against"].append("AI second look: " + ai["reason"]); v["memo"] = f"DECLINE {q['sym']}: AI second look: {ai['reason']}"
        rec = {"date": today, "time": now.strftime("%I:%M %p"), "sym": q["sym"], "id": q["id"], **{k: v[k] for k in ("decision", "conviction", "memo")}}
        if v["decision"] == "POST":
            p = ideas_mod.pending(pending_path)
            at_open = bool(cfg.get("ideas", {}).get("post_at_open"))
            for x in p["queue"]:
                if x["id"] == q["id"]:
                    x["ceo"] = v
                    if at_open: x["status"] = "scheduled for open"; rec["decision"] = "POST AT OPEN"; rec["memo"] += " Posted at the next open only if the opening price is inside the entry range."
            ideas_mod._save(pending_path or ideas_mod.PENDING, p)
            if not at_open:
                ideas_mod.approve(q["id"], NAME, now, pending_path, public_path, frames=(frames_for([q["sym"]]) if frames_for else None))
            ctx["live"] += 1; _count(ctx, q["sym"], q.get("term") == "medium", funds)
            if q.get("term") == "medium": ctx["momentum_live"] += 1
            else: ctx["short_live"] += 1; ctx["setups_today"] += 1
        else:
            p = ideas_mod.pending(pending_path)
            for x in p["queue"]:
                if x["id"] == q["id"]: x["status"] = "watch"; x["ceo"] = v
            ideas_mod._save(pending_path or ideas_mod.PENDING, p)
        out.append(rec)
    p = ideas_mod.pending(pending_path)
    p["ceo_log"] = (p.get("ceo_log", []) + out)[-120:]
    ideas_mod._save(pending_path or ideas_mod.PENDING, p)
    return out


def decide_exits(cfg: dict, now, ideas_mod, public_path=None) -> list[dict]:
    """Runs after monitor: closes every live idea with a flagged exit signal, with the CEO's memo."""
    pub = ideas_mod.public(public_path); out = []
    for i in [x for x in pub["live"] if x.get("exit_signal") and not x["exit_signal"].get("awaiting")]:   # circuit-locked: wait for a tradable price
        sig = i["exit_signal"]
        if sig.get("not_entered"):
            memo = "the price never traded inside the entry range, so the idea was never entered; it carries no result"
        elif sig.get("note"): memo = sig["note"]
        else: memo = {"Stop-loss hit": "the stop level was reached; the idea's risk limit is respected without exception",
                      "Target hit": "the target was reached; the idea did its job",
                      "Time up": "the idea's time horizon is over; capital is freed for a fresh idea"}.get(sig["reason"],
                     "momentum rank faded past the exit level (the back-tested exit rule)")
        done = ideas_mod.close(i["sym"], NAME, now, public_path=public_path)
        pub2 = ideas_mod.public(public_path)
        for c in pub2["closed"]:
            if c.get("id") == done.get("id"): c["ceo_exit"] = f"CLOSE {i['sym']}: {sig['reason']} — {memo}."; break
        ideas_mod._publish(pub2, now, public_path)
        out.append({"sym": i["sym"], "reason": sig["reason"], "price": done["exit_price"], "ret_pct": done["ret_pct"], "memo": memo})
    return out
