"""Time Machine: replays the rest of the session 10,000 times from a stock's own 5-minute bars, then repeats the same trade
(same time of day, same stop and target %) on every past session. A simulation, not a prediction."""
from __future__ import annotations

import random

from . import charges as ch


def sessions(df):
    """DataFrame (IST index, open/high/low/close/volume) -> {date: [(minute_of_day, o, h, l, c), ...]}"""
    out: dict = {}
    for ts, r in df.iterrows():
        out.setdefault(ts.date(), []).append((ts.hour * 60 + ts.minute, float(r["open"]), float(r["high"]), float(r["low"]), float(r["close"])))
    return {d: sorted(v) for d, v in sorted(out.items())}


def _pool(sess):
    pool: dict = {}
    for bars in sess.values():
        for i in range(1, len(bars)):
            pc = bars[i - 1][4]
            if pc > 0:
                m, o, h, l, c = bars[i]
                pool.setdefault(m, []).append((c / pc - 1, h / pc - 1, l / pc - 1))
    return pool


def simulate(pool, start_min, end_min, entry, stop, target, n=10000, seed=19):
    slots = list(range(start_min + 5, end_min + 1, 5)); rnd = random.Random(seed); res = {"target": 0, "stop": 0, "open": 0}
    for _ in range(n):
        p, out = entry, "open"
        for m in slots:
            opts = pool.get(m)
            if not opts:
                continue
            cr, hr, lr = rnd.choice(opts)
            if p * (1 + lr) <= stop: out = "stop"; break        # stop first inside a bar: conservative
            if p * (1 + hr) >= target: out = "target"; break
            p *= 1 + cr
        res[out] += 1
    return {k: round(v / n * 100, 1) for k, v in res.items()}


def replay_same_trade(sess, start_min, end_min, stop_pct, target_pct, qty, product, rates, skip=None):
    w = l = f = 0; pnl = 0.0
    for d, bars in sess.items():
        if d == skip:
            continue
        i = next((k for k, b in enumerate(bars) if b[0] == start_min), None)
        if i is None:
            continue
        entry = bars[i][4]; stop = entry * (1 - stop_pct / 100); target = entry * (1 + target_pct / 100)
        exit_px, kind = entry, "flat"
        for b in bars[i + 1:]:
            if b[0] > end_min: break
            exit_px = b[4]
            if b[3] <= stop: exit_px, kind = min(b[1], stop), "stop"; break      # gap-aware
            if b[2] >= target: exit_px, kind = max(b[1], target) if b[1] > target else target, "target"; break
        net = qty * (exit_px - entry) - ch.compute("BUY", qty, entry, product, rates).total - ch.compute("SELL", qty, exit_px, product, rates).total
        pnl += net; w += kind == "target"; l += kind == "stop"; f += kind == "flat"
    n = w + l + f
    return {"sessions": n, "target": w, "stop": l, "neither": f, "net_pnl": round(pnl, 2), "avg_net": round(pnl / n, 2) if n else None}


def verdict(sim, real):
    if real["sessions"] < 20:
        return "too few past sessions to judge (fewer than 20)"
    edge = sim["target"] - sim["stop"]
    if real["net_pnl"] < 0 and edge <= 5:
        return "past sessions lost money after charges: unfavourable"
    return "close to a coin flip" if abs(edge) <= 5 else ("tilted toward the target" if edge > 0 else "tilted toward the stop")


def run(df, now, entry, stop_pct, target_pct, qty, cfg, n=10000, seed=19):
    sess = sessions(df); today = now.date(); past = {d: b for d, b in sess.items() if d != today}
    st_m = now.hour * 60 + now.minute - now.minute % 5
    en_m = int(cfg["strategy"]["square_off_time"][:2]) * 60 + int(cfg["strategy"]["square_off_time"][3:])
    sim = simulate(_pool(past), st_m, en_m, entry, entry * (1 - stop_pct / 100), entry * (1 + target_pct / 100), n, seed)
    real = replay_same_trade(past, st_m, en_m, stop_pct, target_pct, max(qty, 1), cfg["strategy"]["product"], cfg.get("charges", {}))
    return {"replays": n, "sim": sim, "real": real, "verdict": verdict(sim, real), "note": "A simulation, not a prediction."}


# ---------------------------------------------------------------- daily-bar version (used by the ideas flow)
def _daily_pool(d):
    """Day-over-day (close, high, low) moves as fractions of the previous close, from a daily OHLC frame."""
    c, h, l = d["close"].values, d["high"].values, d["low"].values
    return [(float(c[i] / c[i - 1] - 1), float(h[i] / c[i - 1] - 1), float(l[i] / c[i - 1] - 1)) for i in range(1, len(c)) if c[i - 1] > 0]


def simulate_daily(pool, entry, stop, target, days, n=2000, seed=19):
    """Replays the idea's holding period n times, drawing each day from the stock's own daily moves. Stop first inside a day: conservative."""
    rnd = random.Random(seed); res = {"target": 0, "stop": 0, "open": 0}
    if not pool:
        return {k: None for k in res}
    for _ in range(n):
        p, out = entry, "open"
        for _d in range(days):
            cr, hr, lr = rnd.choice(pool)
            if p * (1 + lr) <= stop: out = "stop"; break
            if p * (1 + hr) >= target: out = "target"; break
            p *= 1 + cr
        res[out] += 1
    return {k: round(v / n * 100, 1) for k, v in res.items()}


def replay_daily(d, stop_pct, target_pct, days, cfg, notional=50000.0):
    """Re-runs the SAME stop/target (as % of entry) from every past close in the frame, holding at most `days` sessions. Gap-aware, charges included (delivery).
    Overlapping starts, so the samples are not independent."""
    rates = cfg.get("charges", {}); o, h, l, c = (d[k].values for k in ("open", "high", "low", "close"))
    w = s = t = 0; net_pct = 0.0; last = len(c) - 1
    for i in range(0, last - days + 1 if last - days + 1 > 0 else 0):
        entry = float(c[i]); stop = entry * (1 - stop_pct / 100); target = entry * (1 + target_pct / 100); px, kind = float(c[i + days]), "time"
        for j in range(i + 1, i + days + 1):
            if o[j] <= stop: px, kind = float(o[j]), "stop"; break
            if l[j] <= stop: px, kind = stop, "stop"; break
            if h[j] >= target: px, kind = max(float(o[j]), target), "target"; break
        qty = max(int(notional // entry), 1)
        cost = ch.compute("BUY", qty, entry, "delivery", rates).total + ch.compute("SELL", qty, px, "delivery", rates).total
        net_pct += ((px - entry) * qty - cost) / (qty * entry) * 100
        w += kind == "target"; s += kind == "stop"; t += kind == "time"
    n = w + s + t
    return {"sessions": n, "target": w, "stop": s, "neither": t, "net_pnl": round(net_pct, 2), "avg_net": round(net_pct / n, 3) if n else None, "unit": "sum of % returns after charges"}


def verdict_daily(sim, real, r):
    """Compares the decided runs in R-multiples (target pays r, stop costs 1), so a 2.5:1 idea is not judged on raw hit rates. Runs still open count as 0."""
    if real["sessions"] < 20:
        return "too few past sessions to judge (fewer than 20)"
    edge = (sim["target"] * r - sim["stop"]) / 100
    if real["net_pnl"] < 0 and edge <= 0.15:
        return "past sessions lost money after charges: unfavourable"
    if edge > 0.15 and real["net_pnl"] >= 0:
        return "tilted toward the target"
    return "tilted toward the stop" if edge < -0.15 else "close to a coin flip"


def _demean(pool):
    """Removes the stock's average daily drift from its moves, keeping their size and shape. A momentum stock is chosen BECAUSE it
    rose; replaying that rise back at it is circular. Drift-neutral moves ask only: given how this stock swings, how often does
    price reach the target before the stop?"""
    if not pool: return pool
    mu = sum(x[0] for x in pool) / len(pool)
    return [(c - mu, h - mu, l - mu) for c, h, l in pool]


def _mirror(pool):
    """No-edge baseline (audit fix #1): the stock's own day shapes plus each day mirrored (up becomes down), so up and down moves
    are exactly balanced. Same size of moves, zero drift, zero skew: what a stock with no edge at all would do."""
    return list(pool) + [(-c, -l, -h) for c, h, l in pool]


def run_daily(d, entry, stop, target, days, cfg, n=None, seed=19, lookback=252):
    """Time Machine for a SETUP idea (not used on momentum ideas: audit fix #10).
    1. simulation on DRIFT-NEUTRAL daily moves (the stock's volatility and skew, not its recent trend);
    2. the same simulation on a NO-EDGE baseline (the moves mirrored): the verdict is the difference, so the stop-first rule
       inside a day (which favours the stop) and the reward-to-risk shape cancel out (audit fix #1);
    3. replay of the same stop and target on history BEFORE the last `lookback` sessions, when 120+ such sessions exist.
    A simulation, not a prediction; not back-tested as a filter."""
    n = n or int(cfg.get("timemachine", {}).get("replays", 2000))
    pool = _demean(_daily_pool(d))
    sim = simulate_daily(pool, entry, stop, target, days, n, seed)
    base = simulate_daily(_mirror(pool), entry, stop, target, days, n, seed)
    before = d.iloc[:-lookback] if len(d) > lookback + 120 else None
    stop_pct, tgt_pct = (1 - stop / entry) * 100, (target / entry - 1) * 100
    real = replay_daily(before, stop_pct, tgt_pct, days, cfg) if before is not None else {"sessions": 0, "target": 0, "stop": 0, "neither": 0, "net_pnl": 0.0, "avg_net": None, "unit": "no history before the selection year"}
    r = (target - entry) / (entry - stop) if entry > stop else 1.0
    edge = None
    if sim["target"] is None: verdict = "no history"
    else:
        edge = round(((sim["target"] * r - sim["stop"]) - (base["target"] * r - base["stop"])) / 100, 3)   # in R vs the no-edge baseline
        th = 0.10
        if real["sessions"] >= 20 and real["net_pnl"] < 0 and edge <= th: verdict = "past sessions lost money after charges: unfavourable"
        elif edge > th and (real["sessions"] < 20 or real["net_pnl"] >= 0): verdict = "tilted toward the target"
        elif edge < -th: verdict = "tilted toward the stop"
        else: verdict = "close to a coin flip"
    return {"replays": n, "sim": sim, "baseline": base, "edge_vs_baseline": edge, "real": real, "verdict": verdict,
            "method": "drift-neutral simulation compared with a no-edge (mirrored) baseline; replay on the period before the selection year",
            "note": "A simulation, not a prediction. Not back-tested as a filter."}
