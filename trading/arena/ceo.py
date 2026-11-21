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
    if rk is not None and rk <= 20: pts += 2; why.append(f"momentum rank #{rk} (the back-tested rule)")
    if j["score"] >= 10: pts += 2; why.append(f"judge score {j['score']} (strong)")
    elif j["score"] >= 8: pts += 1; why.append(f"judge score {j['score']}")
    if cm.get("agreement", "").startswith("analysts agree"): pts += 1; why.append("analysts agree")
    if rv.get("lean") == "bull": pts += 1; why.append("bull case outweighs the bear case")
    tm = (rv.get("time_machine") or {}).get("verdict", "")
    if tm == "tilted toward the target": pts += 1; why.append("Time Machine tilted toward the target")
    level = "High" if pts >= 5 else "Medium" if pts >= 3 else "Low"
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
    if ctx["live"] >= ic.get("max_live", 10): no.append(f"book is full ({ctx['live']} live ideas)")
    if ctx["posted_today"] >= ic.get("max_new_per_day", 10): no.append(f"already posted {ctx['posted_today']} ideas today (limit)")
    if q.get("term") == "short" and ctx.get("short_live", 0) >= c.get("max_live_short", 8):
        no.append(f"short-term setup ideas are capped at {c.get('max_live_short', 8)} live (not back-tested, kept from crowding out the momentum rule)")
    sec = _sector(q["sym"], ctx["funds"])
    if sec != "Unknown" and ctx["sectors"].get(sec, 0) >= c.get("max_per_sector", 3):
        no.append(f"industry limit: already {ctx['sectors'][sec]} live ideas in {sec}")
    if q["sym"] in ctx.get("closed_today", set()): no.append("an idea on this stock was closed today; no re-entry the same day")
    # ---- soft doubts: the back-tested momentum rank can outweigh them, untested evidence cannot ----
    level, why = _conviction(q)
    momentum = q.get("entry_rank") is not None and q["entry_rank"] <= ic.get("entry_rank", 20)
    doubts = []
    if rv.get("lean") == "bear": doubts.append("bear case outweighs the bull case")
    if (rv.get("time_machine") or {}).get("verdict", "").startswith(("past sessions lost money", "tilted toward the stop")):
        doubts.append("Time Machine: " + rv["time_machine"]["verdict"])
    if (cm.get("analysts", {}).get("technical") or {}).get("stance") == "bearish": doubts.append("technical analyst is bearish")
    if (cm.get("analysts", {}).get("fundamentals") or {}).get("stance") == "concerning": doubts.append("fundamentals concerning")
    if cm.get("macro") == "cautious" and level != "High": doubts.append("economy is cautious and conviction is not High")
    if level == "Low" and not momentum: doubts.append("low conviction without the momentum rule behind it")
    if doubts and not momentum: no += doubts
    elif doubts: notes += [d + " (noted; the momentum rule carries the idea)" for d in doubts]
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


def decide_entries(cfg: dict, now, ideas_mod, frames_for=None, pending_path=None, public_path=None) -> list[dict]:
    """Runs after a scan: posts what the CEO approves, records every decision on the pending file."""
    from .committee import Committee
    pen, pub = ideas_mod.pending(pending_path), ideas_mod.public(public_path)
    funds = Committee(cfg)._funds(); today = now.date().isoformat()
    sectors: dict[str, int] = {}
    for i in pub["live"]: s = _sector(i["sym"], funds); sectors[s] = sectors.get(s, 0) + 1
    ctx = {"live": len(pub["live"]), "posted_today": sum(1 for i in pub["live"] + pub["closed"] if i.get("posted") == today and i.get("approved_by") == NAME),
           "short_live": sum(1 for i in pub["live"] if i.get("term") == "short"), "sectors": sectors, "funds": funds,
           "closed_today": {c["sym"] for c in pub["closed"] if c.get("closed_at", "")[:10] == today}}
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
            for x in p["queue"]:
                if x["id"] == q["id"]: x["ceo"] = v
            ideas_mod._save(pending_path or ideas_mod.PENDING, p)
            ideas_mod.approve(q["id"], NAME, now, pending_path, public_path, frames=(frames_for([q["sym"]]) if frames_for else None))
            ctx["live"] += 1; ctx["posted_today"] += 1; ctx["sectors"][v["sector"]] = ctx["sectors"].get(v["sector"], 0) + 1
            ctx["short_live"] += q.get("term") == "short"
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
    for i in [x for x in pub["live"] if x.get("exit_signal")]:
        sig = i["exit_signal"]
        if sig.get("not_entered"):
            memo = "the price never traded inside the entry range, so the idea was never entered; it carries no result"
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
