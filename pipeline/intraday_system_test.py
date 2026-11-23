"""Gap-rebound finding + the conditions TrendLens already uses, measured on two separate periods.

  python pipeline/intraday_system_test.py --data hist_data --label 2014-2021 --out sys_old
  python pipeline/intraday_system_test.py --data new_data  --label 2022-2026 --out sys_new
  python pipeline/intraday_system_test.py --compare sys_old sys_new --out sys_compare

Events: stocks that open 1.5-8% below (long) or above (short) the previous close, liquid by the system's own rule
(price >= 20, 20-day average traded value >= 5 Cr). Entry at the 9:20 open (after the first five-minute candle).
Everything a filter uses is known before 9:20: daily facts up to the previous close, the 9:15 gap, the first candle.
Conditions are the system's (trading/arena/judge.py, committee.technical_daily, scanner.daily_setups, config.toml):
momentum rank top 20 / top 50, bull flag, 20-day breakout, above 50 / 200-day average, 50 above 200, MACD above signal,
RSI 50-75, within 10% of the 52-week high, extension / chasing penalties, ATR > 7% reject, macro cautious (NIFTY down
>6% in a month; India VIX is not in the data), and the judge score / verdict built from them.
Exits: the system's intraday defaults (stop 1.5%, target 3%, square-off 15:15) and the settings found in the search.
Costs: Indian intraday charges + 0.05% slippage each side. No rule is searched here: a fixed list is measured on both
periods, and only rules that make money in both count."""
from __future__ import annotations

import argparse, sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import intraday_research as R                       # noqa: E402
from arena.scanner import daily_setups              # noqa: E402

EXITS = {"system: stop 1.5%, target 3%, out 15:15": ("pct", 1.5, 3.0, 72),
         "found: stop 0.5 ATR, no target, out 15:15": ("atr", 0.5, None, 72),
         "stop 1 ATR, target 2R, out 15:15": ("atr", 1.0, 2.0, 72),
         "stop 1.5%, no target, out 15:15": ("pct", 1.5, None, 72)}
_S = {}


def walk(o, h, l, e, entry, stop, long, tgt, xb):
    for j in range(e, xb):
        if long:
            if j > e and o[j] <= stop: return o[j]
            if l[j] <= stop: return stop
            if tgt is not None and h[j] >= tgt: return max(o[j], tgt) if j > e else tgt
        else:
            if j > e and o[j] >= stop: return o[j]
            if h[j] >= stop: return stop
            if tgt is not None and l[j] <= tgt: return min(o[j], tgt) if j > e else tgt
    return o[xb]


def daily(per):
    days = sorted(per)
    d = pd.DataFrame({"open": [per[x][0][0] for x in days], "high": [np.nanmax(per[x][1]) for x in days],
                      "low": [np.nanmin(per[x][2]) for x in days], "close": [per[x][3][-1] for x in days],
                      "volume": [np.nansum(per[x][4]) for x in days]}, index=days)
    c, h, l = d.close, d.high, d.low
    d["sma50"] = c.rolling(50).mean(); d["sma200"] = c.rolling(200).mean(); d["sma20"] = c.rolling(20).mean()
    e12, e26 = c.ewm(span=12, adjust=False).mean(), c.ewm(span=26, adjust=False).mean(); macd = e12 - e26
    d["macd_up"] = macd > macd.ewm(span=9, adjust=False).mean()
    dl = c.diff(); up = dl.clip(lower=0).ewm(alpha=1 / 14, adjust=False).mean(); dn = (-dl.clip(upper=0)).ewm(alpha=1 / 14, adjust=False).mean()
    d["rsi"] = 100 - 100 / (1 + up / dn.clip(lower=1e-9))
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    d["atr"] = tr.rolling(14).mean(); d["atr_pct"] = d.atr / c * 100
    d["hi52"] = h.rolling(252, min_periods=120).max(); d["ret5"] = c / c.shift(5) - 1; d["ext50"] = c / d.sma50 - 1
    d["value_cr"] = (c * d.volume).rolling(20).mean() / 1e7
    d["bo20"] = (c > h.shift(1).rolling(20).max()) & (d.volume > 1.5 * d.volume.shift(1).rolling(50).mean())
    return d


def events(sym):
    per, N, ranks = _S["D"][sym], _S["N"], _S["ranks"]
    d = daily(per); days = list(d.index); out = []
    for i in range(_S.get("warmup", 201), len(days)):
        day, p = days[i], d.iloc[i - 1]                    # p = facts at the previous close (morning scan)
        o, h, l, c, v = per[day]; pc = p.close
        if not pc > 0: continue
        g = (o[0] / pc - 1) * 100
        if not 1.5 <= abs(g) <= 8 or pc < 20 or not p.value_cr >= 5 or not p.atr > 0: continue
        long = g < 0
        flag = bool(daily_setups(d.iloc[i - 60:i][["open", "high", "low", "close", "volume"]], ("bull_flag",)))
        rk = ranks.get(days[i - 1], {}).get(sym)
        nd = N.get(day); npd = N.get(days[i - 1]) if days[i - 1] in N else None
        ng = (nd[0][0] / npd[3][-1] - 1) * 100 if nd is not None and npd is not None else np.nan
        n1 = (nd[3][0] > nd[0][0]) if nd is not None else None
        nifty_1m = _S["n1m"].get(day, np.nan)               # NIFTY change over the month up to the previous close
        f = {"sym": sym, "day": day, "year": day.year, "side": "long" if long else "short", "gap": abs(g),
             "c1_with": bool(c[0] > o[0]) if long else bool(c[0] < o[0]),
             "nifty_gap_with": (ng <= -0.5) if long else (ng >= 0.5), "nifty_c1_with": (bool(n1) if long else (n1 is not None and not n1)) if n1 is not None else False,
             "rank": rk if rk is not None else 999, "bull_flag": flag, "breakout20": bool(p.bo20),
             "above50": bool(pc > p.sma50), "above200": bool(pc > p.sma200), "s50_over_200": bool(p.sma50 > p.sma200),
             "macd_up": bool(p.macd_up), "rsi50_75": bool(50 <= p.rsi <= 75), "near_hi52": bool(pc >= 0.9 * p.hi52),
             "ext50": float(p.ext50), "ret5": float(p.ret5), "atr_pct": float(p.atr_pct), "macro_cautious": bool(nifty_1m < -6)}
        top = f["rank"] <= 20
        sc = (2 if top else 0) + (2 if flag else 0)
        if not top:
            sc += f["above50"] + f["above200"] + f["s50_over_200"] + f["macd_up"] + f["rsi50_75"] + f["near_hi52"]
            sc += -2 if f["ext50"] > 0.40 else -1 if f["ext50"] > 0.25 else 0
            sc += -1 if f["ret5"] > 0.20 else 0
            sc += -1 if f["macro_cautious"] else 0
        hard = f["atr_pct"] > 7 and not top
        if f["atr_pct"] > 7 and top: sc -= 1
        f["score"] = sc
        f["verdict"] = "REJECT" if hard else ("APPROVE" if top or sc >= 6 else "WATCH" if sc >= 4 else "REJECT")
        entry = o[1] * (1 + R.SLIP if long else 1 - R.SLIP)
        for name, (kind, s, t, xb) in EXITS.items():
            risk = entry * s / 100 if kind == "pct" else s * p.atr
            stop = entry - risk if long else entry + risk
            tgt = None if t is None else (entry * (1 + t / 100) if long else entry * (1 - t / 100)) if kind == "pct" else (entry + t * risk if long else entry - t * risk)
            ex = walk(o, h, l, 1, entry, stop, long, tgt, xb) * (1 - R.SLIP if long else 1 + R.SLIP)
            f[name] = ((ex / entry - 1) if long else (entry / ex - 1)) * 100 - R.cost_frac(entry) * 100
        out.append(f)
    return out


def build(data_dir):
    D = {}
    for name, per in R._pool_map(R._load_one, sorted(Path(data_dir).glob("*.csv.gz"))):
        if per is not None: D[name] = per
    syms = [s for s in D if s != "NIFTY50"]
    close = pd.DataFrame({s: pd.Series({d: a[3][-1] for d, a in D[s].items()}) for s in syms}).sort_index()
    days = list(close.index)
    R._S.update(syms=syms, days=days, C=close.values)
    ranks = {}
    for part in R._pool_map(R._ranks, R._spans(253, len(days))): ranks.update(part)
    N = D.get("NIFTY50", {})
    nc = pd.Series({d: a[3][-1] for d, a in N.items()}).sort_index()
    n1m = ((nc / nc.shift(21) - 1) * 100).shift(1).to_dict() if len(nc) else {}
    _S.update(D=D, N=N, ranks=ranks, n1m=n1m)
    rows = []
    for part in R._pool_map(events, syms): rows += part
    print(f"{data_dir}: {len(D)} files, NIFTY 50 {'found' if _S['N'] else 'MISSING'}, {len(rows)} gap events", flush=True)
    return pd.DataFrame(rows)


def rules():
    """Fixed list, decided before looking at results: base x system condition."""
    base = {"all gaps 1.5-8%": lambda E: E.gap >= 1.5,
            "first candle with the trade": lambda E: E.c1_with,
            "FOUND: market gap + both first candles with the trade": lambda E: E.c1_with & E.nifty_gap_with & E.nifty_c1_with}
    cond = {"-": lambda E: E.gap > 0,
            "judge APPROVE": lambda E: E.verdict == "APPROVE", "judge APPROVE or WATCH": lambda E: E.verdict != "REJECT",
            "judge REJECT": lambda E: E.verdict == "REJECT", "score >= 6": lambda E: E.score >= 6, "score <= 3": lambda E: E.score <= 3,
            "momentum top 20": lambda E: E["rank"] <= 20, "momentum top 50": lambda E: E["rank"] <= 50, "rank below 150": lambda E: E["rank"] > 150,
            "bull flag": lambda E: E.bull_flag, "20-day breakout": lambda E: E.breakout20,
            "above 50-day": lambda E: E.above50, "below 50-day": lambda E: ~E.above50,
            "above 200-day": lambda E: E.above200, "below 200-day": lambda E: ~E.above200,
            "50-day above 200-day": lambda E: E.s50_over_200, "MACD above signal": lambda E: E.macd_up,
            "RSI 50-75": lambda E: E.rsi50_75, "within 10% of 52-week high": lambda E: E.near_hi52,
            "extended >25% above 50-day": lambda E: E.ext50 > 0.25, "up >20% in 5 days": lambda E: E.ret5 > 0.20,
            "ATR <= 7%": lambda E: E.atr_pct <= 7, "macro cautious": lambda E: E.macro_cautious, "macro calm": lambda E: ~E.macro_cautious}
    return base, cond


def measure(E, out, label):
    base, cond = rules(); rows = []
    for side in ("long", "short"):
        S = E[E.side == side]
        for bn, bf in base.items():
            B = S[bf(S)]
            for cn, cf in cond.items():
                X = B[cf(B)]
                for ex in EXITS:
                    x = X[ex]; by = x.groupby(X.year).mean()
                    rows.append({"side": side, "base": bn, "condition": cn, "exit": ex, "n": int(len(x)),
                                 "net": round(float(x.mean()), 4) if len(x) else None, "win": round(float((x > 0).mean() * 100), 1) if len(x) else None,
                                 "years_pos": int((by > 0).sum()), "years": int(len(by)), "by_year": {int(k): round(float(v), 3) for k, v in by.items()}})
    T = pd.DataFrame(rows); out.mkdir(parents=True, exist_ok=True)
    T.to_json(out / "rules.json", orient="records", indent=1)
    E.to_csv(out / "events.csv.gz", index=False)
    print(f"{label}: measured {len(T)} rule/exit combinations", flush=True)


def compare(a, b, out):
    A = pd.read_json(Path(a) / "rules.json"); B = pd.read_json(Path(b) / "rules.json")
    k = ["side", "base", "condition", "exit"]
    M = A.merge(B, on=k, suffixes=("_old", "_new"))
    M["both_positive"] = (M.net_old > 0) & (M.net_new > 0) & (M.n_old >= 30) & (M.n_new >= 30)
    M["min_net"] = M[["net_old", "net_new"]].min(axis=1)
    out.mkdir(parents=True, exist_ok=True); M.to_json(out / "compare.json", orient="records", indent=1)
    keep = M[M.both_positive].sort_values("min_net", ascending=False)
    L = ["# TrendLens conditions on gap trades: 2014-2021 vs 2022-2026 (after costs)", "",
         f"{len(M)} fixed rule/exit combinations measured on both periods; {len(keep)} made money in BOTH with 30+ trades each.", "",
         "| side | base | condition | exit | old n | old %/trade | old yrs+ | new n | new %/trade | new yrs+ |", "|---|---|---|---|---|---|---|---|---|---|"]
    for _, r in keep.head(40).iterrows():
        L.append(f"| {r.side} | {r.base} | {r.condition} | {r.exit} | {r.n_old} | {r.net_old:+.3f} | {r.years_pos_old}/{r.years_old} | {r.n_new} | {r.net_new:+.3f} | {r.years_pos_new}/{r.years_new} |")
    L += ["", "## Reference: base rules with no extra condition", "", "| side | base | exit | old n | old %/trade | new n | new %/trade |", "|---|---|---|---|---|---|---|"]
    for _, r in M[M.condition == "-"].iterrows():
        L.append(f"| {r.side} | {r.base} | {r.exit} | {r.n_old} | {r.net_old:+.3f} | {r.n_new} | {r.net_new:+.3f} |")
    L += ["", "## Reference: judge verdicts on the FOUND rule, system exit", "", "| side | condition | old n | old %/trade | new n | new %/trade |", "|---|---|---|---|---|---|"]
    for _, r in M[(M.base.str.startswith("FOUND")) & (M.exit.str.startswith("system")) & (M.condition.isin(["-", "judge APPROVE", "judge APPROVE or WATCH", "judge REJECT", "score >= 6", "score <= 3"]))].iterrows():
        L.append(f"| {r.side} | {r.condition} | {r.n_old} | {r.net_old:+.3f} | {r.n_new} | {r.net_new:+.3f} |")
    (out / "compare.md").write_text("\n".join(L)); print("\n".join(L))


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--data"); ap.add_argument("--label", default=""); ap.add_argument("--out", default="sys_results")
    ap.add_argument("--compare", nargs=2); a = ap.parse_args()
    if a.compare: compare(a.compare[0], a.compare[1], Path(a.out)); return
    measure(build(a.data), Path(a.out), a.label)


if __name__ == "__main__":
    main()
