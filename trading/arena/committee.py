"""Investment committee. Analysts report to a decision-maker, like a fund's research desk:
  technical analyst   -> trend, momentum, VWAP, daily setups (the only part that has a back-test behind it)
  fundamentals analyst-> valuation, quality, leverage, growth from web/fundamentals.json, priced on the LIVE price
  news analyst        -> last 3 days of Google News headlines for the company (keyword flags, not a sentiment model)
  macro analyst       -> India VIX, USD/INR, Brent, S&P 500, US 10-year, Nifty
  risk officer        -> deterministic: surveillance list, trades-a-day, loss-per-trade limit
Our back-test found that technical bull/bear arguments did not improve the rank rule, so NOTHING here auto-vetoes a trade except
the risk officer. The other analysts' views go to the decision-maker (Claude or you) as evidence, labelled with how much proof exists."""
from __future__ import annotations

import json
import logging
import time
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import quote

from . import riskplan, universe
from .safety import SafeHTTP

log = logging.getLogger("arena")
FUND_FILE = universe.REPO / "web" / "fundamentals.json"
import re

# Headline flags. Whole-word patterns: a bare substring test made "ban" fire on "bank"/"urban" and "ed " on any word ending in -ed.
NEG = (("fraud", r"\bfraud"), ("probe", r"\bprobe[sd]?\b"), ("sebi order", r"\bsebi order"), ("raid", r"\braids?\b"), ("downgrade", r"\bdowngrad"),
       ("resigns", r"\bresign"), ("default", r"\bdefault"), ("penalty", r"\bpenalt"), ("ban", r"\b(?:ban|bans|banned)\b"), ("loss widens", r"\bloss widens"),
       ("plunge", r"\bplunge"), ("cuts guidance", r"\bcuts? guidance"), ("insolvency", r"\binsolvenc"), ("ED probe", r"\bED\b|enforcement directorate"))
POS = (("order win", r"\border wins?\b"), ("bags order", r"\bbags? order"), ("upgrade", r"\bupgrade[sd]?\b"), ("record profit", r"\brecord profit"), ("profit jumps", r"\bprofit jumps"),
       ("acquisition", r"\bacquisition\b|\bacquires\b"), ("buyback", r"\bbuyback"), ("dividend", r"\bdividend"), ("beats estimates", r"\bbeats? estimates"), ("wins contract", r"\bwins? contract"))
_LEGAL = {"limited", "ltd", "pvt", "private", "inc", "corp", "corporation"}
_norm = lambda t: re.sub(r"[^a-z0-9]", "", t.lower())


def _hit(label_pat, text):
    label, pat = label_pat
    return label if re.search(pat, text, 0 if label == "ED probe" else re.I) else None


PROOF = {"technical": "partly back-tested (rank rule only)", "fundamentals": "not back-tested", "news": "not back-tested", "macro": "not back-tested"}


class Committee:
    def __init__(self, cfg: dict, fund_path: Path | None = None):
        self.cfg = cfg
        self.fund_path = fund_path or FUND_FILE
        self._fund = None
        self._news: dict[str, tuple[float, list]] = {}
        self._macro: tuple[float, dict] | None = None
        self.http = SafeHTTP(timeout=10)

    # ---- fundamentals -----------------------------------------------
    def _funds(self) -> dict:
        if self._fund is None:
            try:
                self._fund = json.loads(self.fund_path.read_text())
            except Exception:
                self._fund = {"stocks": {}}
        return self._fund

    def fundamentals(self, sym: str, ltp: float) -> dict:
        f = self._funds().get("stocks", {}).get(sym)
        if not f:
            return {"stance": "no data", "points": ["fundamentals file has no entry for this stock"]}
        pts, bull, bear = [], 0, 0
        eps = f.get("eps_ttm")
        if eps is not None:
            if eps <= 0: bear += 2; pts.append("loss-making over the last 12 months")
            else:
                pe = ltp / eps; pts.append(f"P/E {pe:.1f}x on the live price")
                if pe > 60: bear += 1; pts.append("rich valuation (P/E above 60)")
                elif pe < 25: bull += 1
        for k, lab, good, bad in (("roe_pct", "ROE", 15, 8), ("earn_growth_pct", "earnings growth", 15, 0), ("rev_growth_pct", "revenue growth", 12, 0)):
            v = f.get(k)
            if v is not None:
                pts.append(f"{lab} {v:.0f}%"); bull += v >= good; bear += v < bad
        de = f.get("de_ratio")
        if de is not None:
            pts.append(f"debt/equity {de:.1f}")
            if de > 2: bear += 1; pts.append("high leverage")
        d = bull - bear
        return {"stance": "supportive" if d >= 2 else "concerning" if d <= -2 else "mixed", "points": pts[:6], "as_of": self._funds().get("asof")}

    # ---- news ---------------------------------------------------------
    def news(self, sym: str) -> dict:
        t0, items = self._news.get(sym, (0, None))
        if items is None or time.time() - t0 > 3600:
            name = (self._funds().get("stocks", {}).get(sym) or {}).get("name") or sym
            items = []
            try:
                r = self.http.get("https://news.google.com/rss/search", params={"q": f'"{name}" stock when:3d', "hl": "en-IN", "gl": "IN", "ceid": "IN:en"})
                r.raise_for_status()
                for it in ET.fromstring(r.content).iter("item"):
                    items.append(((it.findtext("title") or "")[:110], (it.findtext("pubDate") or "")[:16]))
                    if len(items) >= 6: break
            except Exception as e:
                log.debug("news for %s failed: %s", sym, e)
                items = None
            self._news[sym] = (time.time(), items)
        if items is None:
            return {"stance": "no data", "headlines": [], "flags": []}
        return self.score_headlines(items, (self._funds().get("stocks", {}).get(sym) or {}).get("name") or sym, sym)

    @staticmethod
    def score_headlines(items, name: str, sym: str) -> dict:
        """Keeps only headlines that mention the company (its first two name words, or the symbol) and flags them with whole-word patterns."""
        ws = [w for w in re.split(r"\s+", name) if w.strip("&.,") and w.lower().strip("&.,") not in _LEGAL]
        ws2 = [w for w in ws if w.lower() not in ("of", "the", "and", "&")]
        keys = {k for k in (_norm("".join(ws)), _norm("".join(ws[:2])), _norm("".join(ws2[:2]))) if len(k) >= 8}   # too-short keys ("bankof") match other companies
        if len(_norm(sym)) >= 3: keys.add(_norm(sym))
        rel = [(t, d) for t, d in items if any(k in _norm(t) for k in keys)]
        text = " | ".join(t for t, _ in rel)
        neg = [x for x in (_hit(p, text) for p in NEG) if x]; pos = [x for x in (_hit(p, text) for p in POS) if x]
        return {"stance": "negative flags" if neg else "positive flags" if pos else "quiet", "flags": neg + pos, "neg_flags": neg, "pos_flags": pos, "headlines": [t for t, _ in rel[:4]]}

    # ---- macro --------------------------------------------------------
    def macro(self) -> dict:
        if self._macro and time.time() - self._macro[0] < 1800:
            return self._macro[1]
        out = {"stance": "no data"}
        try:
            import yfinance as yf
            tk = {"india_vix": "^INDIAVIX", "usd_inr": "INR=X", "brent": "BZ=F", "sp500": "^GSPC", "us10y": "^TNX", "nifty": "^NSEI"}
            raw = yf.download(list(tk.values()), period="3mo", interval="1d", progress=False, group_by="ticker", auto_adjust=False)
            data = {}
            for k, t in tk.items():
                c = raw[t]["Close"].dropna()
                if len(c) > 21: data[k] = {"last": round(float(c.iloc[-1]), 2), "chg_1m_pct": round(float(c.iloc[-1] / c.iloc[-22] - 1) * 100, 1)}
            vix = data.get("india_vix", {}).get("last")
            why = []
            if vix and vix > 22: why.append(f"India VIX {vix} is elevated")
            if data.get("nifty", {}).get("chg_1m_pct", 0) < -6: why.append("Nifty is down more than 6% in a month")
            if data.get("brent", {}).get("chg_1m_pct", 0) > 15: why.append("Brent is up more than 15% in a month")
            if data.get("usd_inr", {}).get("chg_1m_pct", 0) > 2: why.append("the rupee has weakened more than 2% in a month")
            out = {"stance": "cautious" if why else "calm", "reasons": why, "data": data}
        except Exception as e:
            log.debug("macro failed: %s", e)
        self._macro = (time.time(), out)
        return out

    # ---- technical + risk officer ---------------------------------------
    @staticmethod
    def technical(snap: dict, setup_text: str = "") -> dict:
        pts, s = [], 0
        if "sma9" in snap:
            s += 1 if snap["sma9"] > snap["sma21"] else -1; pts.append(f"SMA9 {'above' if snap['sma9'] > snap['sma21'] else 'below'} SMA21")
            s += 1 if snap.get("above_vwap") else -1; pts.append(f"price {'above' if snap.get('above_vwap') else 'below'} VWAP")
            if snap["rsi14"] > 75: s -= 1; pts.append(f"RSI {snap['rsi14']} stretched")
            if snap["mom_30m_pct"] > 0.25: s += 1; pts.append(f"30m momentum {snap['mom_30m_pct']}%")
        if setup_text: pts.append("daily scan: " + setup_text[:120])
        return {"stance": "bullish" if s >= 2 else "bearish" if s <= -2 else "neutral", "points": pts}

    @staticmethod
    def technical_daily(d, setups=None, mom_pct=None) -> dict:
        """Technical analyst on daily bars (what an idea is built from). Returns stance, points and the numbers the judge scores."""
        import pandas as pd
        c, h, l = d["close"], d["high"], d["low"]
        m = {"close": float(c.iloc[-1])}
        for n in (20, 50, 200):
            m[f"sma{n}"] = float(c.iloc[-n:].mean()) if len(c) >= n else None
        e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean(); macd = e12 - e26
        m["macd_above_signal"] = bool(macd.iloc[-1] > macd.ewm(span=9, adjust=False).mean().iloc[-1])
        dl = c.diff(); up = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
        m["rsi"] = float(100 - 100 / (1 + up.iloc[-1] / max(dn.iloc[-1], 1e-9)))
        tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
        m["atr"] = float(tr.rolling(14).mean().iloc[-1]); m["atr_pct"] = m["atr"] / m["close"] * 100
        m["hi52"] = float(h.iloc[-252:].max()); m["ret5"] = float(c.iloc[-1] / c.iloc[-6] - 1) if len(c) > 6 else 0.0
        m["ext50"] = float(c.iloc[-1] / m["sma50"] - 1) if m["sma50"] else 0.0
        m["setups"] = [x["setup"] for x in (setups or [])]; m["mom_pct"] = mom_pct
        pts = []
        if m["sma50"]: pts.append(f"price {'above' if m['close'] > m['sma50'] else 'below'} 50-day average")
        if m["sma200"]: pts.append(f"price {'above' if m['close'] > m['sma200'] else 'below'} 200-day average")
        pts.append(f"RSI {m['rsi']:.0f}"); pts += [x["why"] for x in (setups or [])]
        up_n = bool(m["sma50"] and m["close"] > m["sma50"]) + bool(m["sma200"] and m["close"] > m["sma200"]) + m["macd_above_signal"]
        return {"stance": "bullish" if up_n >= 3 else "bearish" if up_n == 0 else "neutral", "points": pts[:6], "metrics": m}

    def risk_officer(self, sym: str, j, run_id: int, now) -> dict:
        why = []
        bl = universe.blocked()
        if sym in bl: why.append(f"{bl[sym]} (NSE surveillance)")
        g = riskplan.gate(self.cfg, j, run_id, now)
        if g: why.append(g)
        return {"verdict": "BLOCK" if why else "PASS", "reasons": why}

    # ---- the memo -----------------------------------------------------
    def memo(self, sym: str, snap: dict, setup_text: str, j, run_id: int, now) -> dict:
        a = {"technical": self.technical(snap, setup_text), "fundamentals": self.fundamentals(sym, snap.get("ltp", 0)), "news": self.news(sym)}
        stances = [a["technical"]["stance"], a["fundamentals"]["stance"], a["news"]["stance"]]
        good = sum(s in ("bullish", "supportive", "positive flags") for s in stances); bad = sum(s in ("bearish", "concerning", "negative flags") for s in stances)
        agree = "analysts agree (supportive)" if good >= 2 and not bad else "analysts agree (concerns)" if bad >= 2 and not good else "analysts disagree" if good and bad else "no strong view"
        return {"analysts": a, "agreement": agree, "risk_officer": self.risk_officer(sym, j, run_id, now), "evidence": PROOF}
