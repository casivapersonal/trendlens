"""Send web-push notifications for new entry/exit alerts in picks.json.

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
for a in d.get("alerts_today", []):
    key = f'{a["type"]}:{a["sym"]}:{a["date"]}'
    if redis("SISMEMBER", "trendlens:notified", key):
        continue
    if a["type"] == "entry":
        title = f'Entry alert: {a["sym"]}'
        body = f'Ranked #{a["rank"]} by 12-month momentum. Added to Live ideas at about Rs {a["price"]:,.2f}.'
    else:
        title = f'Exit alert: {a["sym"]}'
        body = f'Momentum rank fell to #{a["rank"]}. Result {a["ret_pct"]:+.2f}%. Moved to Closed ideas.'
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
