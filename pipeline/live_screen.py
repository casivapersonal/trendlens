"""Every 10 minutes in market hours: rank the 300 most-traded NSE stocks on live prices,
raise entry/exit alerts, check price warnings on live ideas, and push notifications.
Usage: python pipeline/live_screen.py web/picks.json web/history.json
"""
import json, os, sys, requests, datetime as dt
from core import ist_now, market_open, quotes, ranks_now, apply_rules, fallback_bars, load_nse

PICKS, HIST = sys.argv[1], sys.argv[2]
state = json.load(open(PICKS)); hist = json.load(open(HIST))
now = ist_now(); today = now.date().isoformat()
if not market_open(now) and os.environ.get("FORCE") != "1":
    print("market closed, nothing to do"); sys.exit(0)
syms = sorted(set(hist["universe"]) | {e["sym"] for e in state["live"]})
q = quotes(syms + ["NIFTY 50"])
prices = {s: v["p"] for s, v in q.items() if v and v["p"]}
nifty = prices.pop("NIFTY 50", None)
if len(prices) < 0.8 * len(syms):
    print("too few quotes", len(prices), "of", len(syms)); sys.exit(0)
was_seeded = state.get("seeded", False)
rk, mom = ranks_now(hist, prices, today)
blocked, results = load_nse(os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nse"))
before = json.dumps([(e["sym"], e.get("results_date"), e.get("surveillance")) for e in state["live"]])
alerts = apply_rules(state, rk, mom, prices, nifty, now, blocked, results)
flags_changed = before != json.dumps([(e["sym"], e.get("results_date"), e.get("surveillance")) for e in state["live"]])
for a in alerts:                                   # make sure new ideas have chart data
    if a["sym"] not in state["bars"] and a["sym"] in hist["closes"]:
        state["bars"][a["sym"]] = fallback_bars(hist, a["sym"])
state["updated_ist"] = now.strftime("%d %b %Y, %I:%M %p IST")

# price warnings on published live ideas
warn = []
for e in state["live"]:
    s = e["sym"]; c = [x for x in hist["closes"].get(s, []) if x]
    if len(c) < 51 or s not in q: continue
    p, pc = q[s]["p"], q[s]["pc"] or c[-1]; day = (p / pc - 1) * 100
    if day <= -5: warn.append((s, "fall", f"Down {day:.1f}% today at Rs {p:,.2f}"))
    if day >= 5: warn.append((s, "rise", f"Up {day:.1f}% today at Rs {p:,.2f}"))
    if p < min(c[-20:]): warn.append((s, "low20", f"New 20-day low at Rs {p:,.2f}"))
    s50 = sum(c[-50:]) / 50
    if p < s50 <= c[-1]: warn.append((s, "sma50", f"Fell below its 50-day average (Rs {s50:,.2f}), now Rs {p:,.2f}"))
    if len(c) >= 200:
        s200 = sum(c[-200:]) / 200
        if p < s200 <= c[-1]: warn.append((s, "sma200", f"Fell below its 200-day average (Rs {s200:,.2f}), now Rs {p:,.2f}: long-term trend support broken"))

for e in state["live"]:
    if e.get("surveillance"): warn.append((e["sym"], "asm", f"Now under NSE {e['surveillance']}: higher margins and trading limits apply"))
    if e.get("results_date"):
        days = (dt.date.fromisoformat(e["results_date"]) - now.date()).days
        if 0 <= days <= 7: warn.append((e["sym"], "results", f"Quarterly results on {e['results_date']} ({days} days): expect a large price move"))
msgs = []
for a in alerts:
    if a["type"] == "entry":
        rd = next((e.get('results_date') for e in state['live'] if e['sym'] == a['sym']), None)
        msgs.append((f'entry:{a["sym"]}:{a["date"]}', f'Entry alert: {a["sym"]}', f'Ranked #{a["rank"]} by risk-adjusted 12-month momentum at {a["time"]}. Added to Live ideas at Rs {a["price"]:,.2f}.' + (f' Results due {rd}.' if rd else '')))
    else:
        msgs.append((f'exit:{a["sym"]}:{a["date"]}', f'Exit alert: {a["sym"]}', f'Rank fell to #{a["rank"]} at {a["time"]}. Result {a["ret_pct"]:+.2f}%. Moved to Closed ideas.'))
for s, kind, text in warn:
    msgs.append((f"warn:{kind}:{s}:{today}", f"Price warning: {s}", text + "."))

URL, TOK = os.environ.get("UPSTASH_REDIS_REST_URL"), os.environ.get("UPSTASH_REDIS_REST_TOKEN")
sent = 0
if URL and TOK and os.environ.get("VAPID_PRIVATE_KEY") and msgs:
    from pywebpush import webpush, WebPushException
    def redis(*cmd):
        r = requests.post(URL, headers={"Authorization": f"Bearer {TOK}"}, json=list(cmd), timeout=15); r.raise_for_status()
        return r.json().get("result")
    subs = redis("SMEMBERS", "trendlens:subs") or []
    for key, title, body in msgs:
        if redis("SISMEMBER", "trendlens:notified", key): continue
        payload = json.dumps({"title": title, "body": body, "tag": key, "url": os.environ.get("SITE_URL", "/")})
        for sub in subs:
            try:
                webpush(subscription_info=json.loads(sub), data=payload, vapid_private_key=os.environ["VAPID_PRIVATE_KEY"],
                        vapid_claims={"sub": os.environ.get("VAPID_SUBJECT", "mailto:alerts@example.com")}, ttl=3600)
                sent += 1
            except WebPushException as ex:
                if ex.response is not None and ex.response.status_code in (404, 410): redis("SREM", "trendlens:subs", sub)
        redis("SADD", "trendlens:notified", key)
if alerts or not was_seeded or flags_changed:
    json.dump(state, open(PICKS, "w"), separators=(",", ":"))
print(now.strftime("%H:%M"), "quotes", len(prices), "alerts", [(a["type"], a["sym"], a["rank"]) for a in alerts], "warnings", len(warn), "pushed", sent)
