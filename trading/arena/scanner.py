"""Stage 1 of the funnel: scan the whole universe (Nifty 500) and hand the runner a short list it can afford to watch.
Daily setups from the Day Trader skill (20-day breakout on volume, bull flag, oversold bounce) plus TrendLens-style
risk-adjusted 12-month momentum. During the session the pool is re-ranked on live strength. Long-only."""
from __future__ import annotations

import logging
import math

import pandas as pd

from . import universe
from .indicators import rsi

log = logging.getLogger("arena")


def liquid(d: pd.DataFrame, cfg: dict) -> bool:
    u = cfg.get("universe", {})
    if len(d) < 25:
        return False
    val = float((d["close"] * d["volume"]).iloc[-20:].mean()) / 1e7        # crore rupees traded a day
    return float(d["close"].iloc[-1]) >= u.get("min_price", 20) and val >= u.get("min_avg_value_cr", 5)


SETUPS_ENABLED = ("bull_flag",)   # Nov 2026 setup back-test (pipeline/backtest.py --setups, 2011-2026, after costs):
# bull flag +0.07R a trade (44% win, 2,588 trades, 95% range +0.02 to +0.13); 20-day breakout -0.08R (37% win, 11,374): breakouts are kept as
# information only and no longer become ideas. Override with [ideas] setups = ["bull_flag", "breakout20"] in config.toml.


def daily_setups(d: pd.DataFrame, enabled=None) -> list[dict]:
    """Definitions (audit fix #18, back-tested): breakout = close above the prior 20-day high on volume above 1.5x the 50-day average;
    bull flag = a rise of 15%+ over 10 sessions, then a 3-10 session pause whose high-low range is under 1.5x ATR(14), close above
    the 20-day average."""
    c, h, l, v = d["close"], d["high"], d["low"], d["volume"]
    enabled = SETUPS_ENABLED if enabled is None else enabled
    out = []
    if len(d) < 30: return out
    if "breakout20" in enabled and c.iloc[-1] > h.iloc[-21:-1].max() and v.iloc[-1] > 1.5 * v.iloc[-51:-1].mean():
        out.append({"setup": "breakout20", "why": "closed above the 20-day high on 1.5x the 50-day average volume"})
    if "bull_flag" in enabled:
        pc = c.shift(1)
        atr = float(pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1).iloc[-14:].mean())
        for k in range(3, 11):
            if len(d) < k + 12: break
            rng = float(h.iloc[-k:].max() - l.iloc[-k:].min()); base = len(d) - 1 - k
            run = float(c.iloc[base] / c.iloc[base - 10] - 1)
            if rng < 1.5 * atr and run >= 0.15 and c.iloc[-1] > c.iloc[-20:].mean():
                out.append({"setup": "bull_flag", "why": f"{run*100:.0f}% rise in 10 sessions, then a {k}-session pause within {rng/atr:.1f}x ATR"}); break
    # oversold bounce removed (Nov 2026 audit): it bets against the trend the rest of the system follows
    return out


def momentum(d: pd.DataFrame) -> float | None:
    c = d["close"]
    if len(c) < 200:
        return None
    r = c.pct_change().dropna().iloc[-252:]
    vol = float(r.std()) * math.sqrt(252)
    k = min(252, len(c) - 1)
    return float(c.iloc[-1] / c.iloc[-1 - k] - 1) / vol if vol > 0 else None


class Shortlister:
    def __init__(self, cfg: dict, feed):
        u = cfg.get("universe", {})
        self.cfg, self.feed = cfg, feed
        self.n, self.pool_n, self.batch = u.get("shortlist_size", 15), u.get("pool_size", 40), u.get("batch", 100)
        self.pool: list[str] = []
        self.why: dict[str, str] = {}
        self.frames: dict[str, pd.DataFrame] = {}
        self.mom_pct: dict[str, float] = {}
        self.setups: dict[str, list] = {}
        self.day = None

    def open_day(self, now, fallback: list[str]) -> list[str]:
        """Daily scan of the universe. Falls back to the static watchlist if the feed cannot give daily history."""
        if self.day == now.date() and self.pool:
            return self.pool[: self.n]
        hist = getattr(self.feed, "history", None)
        syms = universe.load(self.cfg)
        if not hist or not syms:
            return fallback
        frames: dict[str, pd.DataFrame] = {}
        for i in range(0, len(syms), self.batch):
            try:
                frames.update(hist(syms[i:i + self.batch], period="2y", interval="1d"))
            except Exception as e:
                log.warning("daily scan batch failed: %s", e)
        rows = []
        for s, d in frames.items():
            if d is None or d.empty or not liquid(d, self.cfg):
                continue
            rows.append((s, daily_setups(d, self.cfg.get("ideas", {}).get("setups")), momentum(d), float(d["close"].iloc[-1] > d["close"].iloc[-50:].mean())))
        if not rows:
            return fallback
        moms = sorted(m for _, _, m, _ in rows if m is not None)
        pct = lambda m: (moms.index(m) + 1) / len(moms) if m is not None else 0
        scored = sorted(((2 * len(su) + pct(m) + 0.5 * t, s, su, m) for s, su, m, t in rows), reverse=True)[: self.pool_n]
        self.pool = [s for _, s, _, _ in scored]
        self.why = {s: "; ".join([x["why"] for x in su] + ([f"momentum rank top {100 - pct(m)*100:.0f}%"] if m is not None else [])) for _, s, su, m in scored}
        self.frames = {s: frames[s] for s in self.pool}
        self.mom_pct = {s: pct(m) for _, s, _, m in scored}
        self.setups = {s: su for _, s, su, _ in scored}
        self.day = now.date()
        log.info("Universe scan: %d tradable stocks -> pool of %d, watching %d", len(rows), len(self.pool), self.n)
        return self.pool[: self.n]

    def intraday(self, now, quotes_fn) -> list[str]:
        """Re-rank the pool on live strength: up from the open and near the day's high."""
        if not self.pool:
            return []
        q = quotes_fn(self.pool)
        sc = []
        for s in self.pool:
            x = q.get(s)
            if x and x.open:
                sc.append(((x.ltp / x.open - 1) * 100 + (0.5 if x.ltp >= 0.995 * x.high else 0), s))
        return [s for _, s in sorted(sc, reverse=True)[: self.n]] or self.pool[: self.n]
