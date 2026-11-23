"""Real-world check of the exhaustion gap fade (short) found by pipeline/intraday_system_test.py.

  python pipeline/intraday_exhaustion_test.py --old sys_old/events.csv.gz --new sys_new/events.csv.gz --out exh

Rule (fixed before this test): a stock opens 1.5-8% ABOVE the previous close after running up hard:
  up >20% in 5 days, or >25% above its 50-day average, or a 20-day breakout on volume the day before ("any").
Short at the 9:20 open, stop 1 daily ATR above entry, target 2R, square off 15:15 (and the system's 1.5% / 3% exit).
Real-world filters:
  F&O stocks only: no circuit limits, never in ASM / GSM / trade-to-trade, always shortable intraday (2014-2021 data is
  F&O stocks already; for 2022-2026 the current NSE F&O list is used, a small look-ahead noted in the report).
  Slippage 0.10% each side instead of 0.05% (net is lowered by 0.10% a trade).
  At most 3 trades a day (the biggest 5-day run-ups first), 20% of capital each (config.toml max_position_pct).
Reports per trade, per year and as a portfolio (yearly return, worst drawdown)."""
from __future__ import annotations

import argparse, csv, gzip, io, json
from pathlib import Path

import pandas as pd
import requests

EX = {"stop 1 ATR, target 2R": "stop 1 ATR, target 2R, out 15:15", "system 1.5% / 3%": "system: stop 1.5%, target 3%, out 15:15"}
EXTRA_SLIP = 0.10                    # percent per trade: 0.05% more on each side


def fno_list():
    """Current NSE F&O stock list: NSE file, else Upstox's F&O instruments."""
    h = {"User-Agent": "Mozilla/5.0", "Accept": "*/*"}
    try:
        r = requests.get("https://nsearchives.nseindia.com/content/fo/fo_mktlots.csv", headers=h, timeout=60); r.raise_for_status()
        s = {row[1].strip() for row in csv.reader(io.StringIO(r.text)) if len(row) > 2 and row[1].strip() and row[1].strip() != "SYMBOL"}
        if len(s) > 100: return s, "NSE fo_mktlots.csv"
    except Exception as e:
        print("NSE F&O list not read:", str(e)[:120])
    r = requests.get("https://assets.upstox.com/market-quote/instruments/exchange/NSE.json.gz", timeout=120); r.raise_for_status()
    m = json.loads(gzip.decompress(r.content))
    s = {x.get("underlying_symbol") or x.get("asset_symbol") for x in m if x.get("segment") == "NSE_FO" and x.get("instrument_type") == "FUT"}
    return {x for x in s if x}, "Upstox NSE_FO futures"


def prep(path, fno=None):
    E = pd.read_csv(path)
    E = E[E.side == "short"].copy()
    E["day"] = pd.to_datetime(E.day)
    if fno is not None: E = E[E.sym.isin(fno)]
    E["c_ret5"] = E.ret5 > 0.20; E["c_ext50"] = E.ext50 > 0.25; E["c_bo20"] = E.breakout20.astype(str) == "True"
    E["any"] = E.c_ret5 | E.c_ext50 | E.c_bo20
    for k, col in EX.items(): E[k] = E[col] - EXTRA_SLIP
    return E


def stats(x, years):
    by = x.groupby(years).mean()
    return {"n": int(len(x)), "net": round(float(x.mean()), 3) if len(x) else None, "win": round(float((x > 0).mean() * 100), 1) if len(x) else None,
            "years_pos": f"{int((by > 0).sum())}/{len(by)}", "by_year": {int(k): round(float(v), 2) for k, v in by.items()}}


def portfolio(T, col, per_day=3, size=0.20):
    T = T.sort_values(["day", "ret5"], ascending=[True, False]).groupby("day").head(per_day)
    d = T.groupby("day")[col].sum() * size / 100                   # fraction of capital gained that day
    eq = (1 + d).cumprod(); dd = float((eq / eq.cummax() - 1).min() * 100)
    yr = d.groupby(d.index.year).apply(lambda x: float(((1 + x).prod() - 1) * 100))
    return {"trades": int(len(T)), "days": int(len(d)), "yearly_return_pct": {int(k): round(v, 1) for k, v in yr.items()},
            "worst_drawdown_pct": round(dd, 1), "worst_day_pct": round(float(d.min() * 100), 2)}


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--out", default="exh"); a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    fno, src = fno_list(); print("F&O stocks:", len(fno), "from", src, flush=True)
    P = {"2014-2021 (F&O stocks)": prep(a.old), "2022-2026 (F&O stocks)": prep(a.new, fno), "2022-2026 (all NIFTY 500, for reference)": prep(a.new)}
    R = {"fno_source": src, "fno_count": len(fno), "extra_slippage_pct": EXTRA_SLIP, "results": {}}
    L = ["# Exhaustion gap fade (short) - real-world check", "",
         f"F&O stocks only ({len(fno)} from {src}); slippage 0.10% each side; costs included. Short at 9:20, square off 15:15.", ""]
    conds = {"any exhaustion": "any", "up >20% in 5 days": "c_ret5", "> 25% above 50-day": "c_ext50", "20-day breakout yesterday": "c_bo20"}
    for pname, E in P.items():
        L += [f"## {pname}", "", "| condition | first candle red too | exit | trades | net %/trade | win % | years + | by year |", "|---|---|---|---|---|---|---|---|"]
        R["results"][pname] = {}
        for cn, cc in conds.items():
            for red in (False, True):
                X = E[E[cc] & (E.c1_with.astype(str) == "True" if red else True)]
                for ek in EX:
                    s = stats(X[ek], X.year)
                    R["results"][pname][f"{cn} | red1={red} | {ek}"] = s
                    L.append(f"| {cn} | {'yes' if red else 'no'} | {ek} | {s['n']} | {s['net'] if s['net'] is not None else '-'} | {s['win']} | {s['years_pos']} | " +
                             ", ".join(f"{k}: {v:+.2f}" for k, v in s["by_year"].items()) + " |")
        X = E[E["any"]]
        pf = {ek: portfolio(X, ek) for ek in EX}
        R["results"][pname]["portfolio_any_max3"] = pf
        L += ["", "Portfolio (any exhaustion, max 3 a day, 20% of capital each):", ""]
        for ek, p in pf.items():
            L.append(f"- {ek}: {p['trades']} trades on {p['days']} days; yearly return % " + ", ".join(f"{k}: {v:+.1f}" for k, v in p["yearly_return_pct"].items()) +
                     f"; worst drawdown {p['worst_drawdown_pct']}%; worst day {p['worst_day_pct']}%")
        L.append("")
    (out / "exhaustion.json").write_text(json.dumps(R, indent=1, default=str)); (out / "exhaustion.md").write_text("\n".join(L)); print("\n".join(L))


if __name__ == "__main__":
    main()
