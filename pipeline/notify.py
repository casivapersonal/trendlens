"""Send web-push notifications when an idea is posted (approved) or closed by the CEO agent (alerts_today in picks.json).

Env: VAPID_PRIVATE_KEY, VAPID_SUBJECT (mailto:you@example.com), UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN
Each alert is pushed once (tracked in Redis set trendlens:notified). Dead subscriptions are removed.
"""
import json, os, sys, requests
from pywebpush import webpush, WebPushException

URL, TOK = os.environ["UPSTASH_REDIS_REST_URL"], os.environ["UPSTASH_REDIS_REST_TOKEN"]
SITE = os.environ.get("SITE_URL", "/")
def redis(*cmd):
    r = requests.post(URL, headers={"Authorization": f"Bearer {TOK}"}, json=list(cmd), timeout=15); r.raise_for_status()
    return r.json().get("result")

d = json.load(open(sys.argv[1]))
subs = redis("SMEMBERS", "trendlens:subs") or []
sent = 0
KEY = lambda a: f'{a["type"]}:{a["sym"]}:{a["date"]}' + (":intraday" if a.get("term") == "intraday" else "")   # intraday and swing alerts on one stock do not collide
todo = [a for a in d.get("alerts_today", []) if not redis("SISMEMBER", "trendlens:notified", KEY(a))]
watch = []; todo = [a for a in todo if a["type"] != "watch"]     # Nov 2026: the evening watch list is no longer sent (intraday ideas are announced only when posted)
if len(todo) > 3:      # a batch from one CEO run: one notification, not ten
    lab = lambda a: a["sym"] + (" (intraday short)" if a.get("side") == "short" else "")
    en = [lab(a) for a in todo if a["type"] == "entry"]; ex = [lab(a) for a in todo if a["type"] == "exit"]
    summary = {"type": "batch", "sym": f"{len(en)} new, {len(ex)} closed", "date": todo[0]["date"],
               "title": f"TrendLens: {len(en)} new idea{'s' if len(en) != 1 else ''}" + (f", {len(ex)} closed" if ex else ""),
               "body": ("New: " + ", ".join(en) + ". " if en else "") + ("Closed: " + ", ".join(ex) + "." if ex else "")}
    for a in todo: redis("SADD", "trendlens:notified", KEY(a))
    todo = [summary]
todo = watch + todo
for a in todo:
    key = KEY(a)
    if a["type"] == "batch":
        title, body = a["title"], a["body"]
    elif a["type"] == "watch":
        title = f'Intraday setups for {a.get("for") or "the next session"}'
        body = "If they open 1.5-8% above yesterday's close, intraday short at 9:20: " + ", ".join(a.get("list") or []) + ". Stop, target and trigger band on TrendLens."
    elif a["type"] == "entry" and a.get("side") == "short":
        title = f'Intraday short: {a["sym"]}'
        body = f'Triggered: short at Rs {a["price"]:,.2f} (9:20 open). Stop-loss Rs {a["stop"]:,.2f}, target Rs {a["target"]:,.2f}, squared off by 3:15 PM. Approved by the CEO.'
    elif a["type"] == "entry":
        title = f'New idea: {a["sym"]}'
        lv = f' Stop-loss Rs {a["stop"]:,.2f}, target Rs {a["target"]:,.2f}.' if a.get("stop") and a.get("target") else ''
        body = f'Bullish view posted at about Rs {a["price"]:,.2f}.{lv} Approved by the CEO.'
    else:
        title = f'{"Intraday short closed" if a.get("term") == "intraday" else "Idea closed"}: {a["sym"]}'
        body = f'{a.get("reason") or "Closed by the CEO"}. ' + (("No result counted." if a.get("term") == "intraday" else "Never entered, so no result.") if a.get("ret_pct") is None else (f'Result {a["ret_pct"]:+.2f}% after costs.' if a.get("term") == "intraday" else f'Result {a["ret_pct"]:+.2f}%.')) + ' Moved to Closed ideas.'
    payload = json.dumps({"title": title, "body": body, "tag": key, "url": SITE})
    for s in subs:
        try:
            webpush(subscription_info=json.loads(s), data=payload, vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                    vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:alerts@example.com")}, ttl=6 * 3600)
            sent += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                redis("SREM", "trendlens:subs", s)
    redis("SADD", "trendlens:notified", key)
print("subscribers", len(subs), "notifications sent", sent)
