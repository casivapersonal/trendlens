"""Every 10 minutes in market hours: check live prices of the Live ideas and push price warnings.

Warnings (each sent once per stock per day):
  - down 5% or more today        - up 5% or more today
  - new 20-day low               - crossed below its 50-day average
Env: VAPID_PRIVATE_KEY, VAPID_SUBJECT, UPSTASH_REDIS_REST_URL, UPSTASH_REDIS_REST_TOKEN, SITE_URL
Usage: python pipeline/warnings_push.py web/picks.json
"""
import json, os, sys, datetime as dt, requests
from pywebpush import webpush, WebPushException

URL, TOK = os.environ["UPSTASH_REDIS_REST_URL"], os.environ["UPSTASH_REDIS_REST_TOKEN"]
SITE = os.environ.get("SITE_URL", "/")
def redis(*cmd):
    r = requests.post(URL, headers={"Authorization": f"Bearer {TOK}"}, json=list(cmd), timeout=15); r.raise_for_status()
    return r.json().get("result")

def quote(sym):
    y = sym + ".NS"
    r = requests.get(f"https://query2.finance.yahoo.com/v8/finance/chart/{requests.utils.quote(y)}?interval=1m&range=1d",
                     headers={"User-Agent": "Mozilla/5.0 TrendLens"}, timeout=15)
    m = r.json()["chart"]["result"][0]["meta"]
    return m["regularMarketPrice"], m.get("chartPreviousClose") or m.get("previousClose")

d = json.load(open(sys.argv[1]))
today = (dt.datetime.utcnow() + dt.timedelta(hours=5, minutes=30)).date().isoformat()
subs = redis("SMEMBERS", "trendlens:subs") or []
warn = []
for idea in d.get("live", []):
    s = idea["sym"]; bars = d["bars"].get(s, [])
    hist = [b[4] for b in bars if b[0] < today]          # closes before today
    if len(hist) < 51:
        continue
    try:
        p, pc = quote(s)
    except Exception as e:
        print("quote failed", s, e); continue
    pc = pc or hist[-1]
    day = (p / pc - 1) * 100
    low20 = min(hist[-20:]); sma50 = sum(hist[-50:]) / 50
    if day <= -5: warn.append((s, "fall", f"Down {day:.1f}% today at Rs {p:,.2f}"))
    if day >= 5: warn.append((s, "rise", f"Up {day:.1f}% today at Rs {p:,.2f}"))
    if p < low20: warn.append((s, "low20", f"New 20-day low at Rs {p:,.2f}"))
    if p < sma50 and hist[-1] >= sma50: warn.append((s, "sma50", f"Fell below its 50-day average (Rs {sma50:,.2f}), now Rs {p:,.2f}"))
sent = 0
for s, kind, text in warn:
    key = f"warn:{kind}:{s}:{today}"
    if redis("SISMEMBER", "trendlens:notified", key):
        continue
    payload = json.dumps({"title": f"Price warning: {s}", "body": text + ". Live idea, exit rule unchanged.", "tag": key, "url": SITE})
    for sub in subs:
        try:
            webpush(subscription_info=json.loads(sub), data=payload, vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                    vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:alerts@example.com")}, ttl=3600)
            sent += 1
        except WebPushException as e:
            if e.response is not None and e.response.status_code in (404, 410):
                redis("SREM", "trendlens:subs", sub)
    redis("SADD", "trendlens:notified", key)
print("live ideas", len(d.get("live", [])), "warnings found", len(warn), "notifications sent", sent)
