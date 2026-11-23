"""How much the exhaustion fade (short) loses if the entry comes later than 9:20 (GitHub schedules can run late).

  python pipeline/intraday_delay_test.py --old hist_data --new new_data --out delay_results

Same trades and exit as the live rule (F&O stocks, gap up 1.5-8% after a run-up; stop 1 ATR, target 2R, out 15:15;
costs + 0.10% slippage), entered at the open of the 9:20, 9:25, 9:30, 9:40, 9:50 or 10:00 candle. A late entry is
skipped if price is already below the 2R target or above the stop."""
from __future__ import annotations

import argparse, json, sys
from pathlib import Path

import numpy as np
import pandas as pd

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE)); sys.path.insert(0, str(HERE.parent / "trading"))
import intraday_research as R                       # noqa: E402
import intraday_system_test as T                    # noqa: E402
import intraday_exhaustion_test as X                # noqa: E402

SLIP = 0.001
ENTRIES = {"9:20": 1, "9:25": 2, "9:30": 3, "9:40": 5, "9:50": 7, "10:00": 9}


def short_walk(o, h, l, e, entry, stop, tgt, xb=72):
    for j in range(e, xb):
        if j > e and o[j] >= stop: return o[j]
        if h[j] >= stop: return stop
        if l[j] <= tgt: return min(o[j], tgt) if j > e else tgt
    return o[xb]


def run(sym):
    per = T._S["D"][sym]; d = T.daily(per); days = list(d.index); out = []
    for i in range(201, len(days)):
        day, p = days[i], d.iloc[i - 1]
        o, h, l, c, v = per[day]; pc = p.close
        if not pc > 0 or pc < 20 or not p.value_cr >= 5 or not p.atr > 0: continue
        g = (o[0] / pc - 1) * 100
        if not 1.5 <= g <= 8 or not ((p.ret5 > 0.20) or (p.ext50 > 0.25) or bool(p.bo20)): continue
        A = float(p.atr); f = {"year": day.year}
        stop0 = o[1] * (1 - SLIP) + A; tgt0 = o[1] * (1 - SLIP) - 2 * A          # levels fixed at 9:20 (as posted)
        for name, e in ENTRIES.items():
            entry = o[e] * (1 - SLIP)
            if entry >= stop0 or entry <= tgt0 or (h[1:e].max() >= stop0 if e > 1 else False) or (l[1:e].min() <= tgt0 if e > 1 else False):
                f[name] = np.nan; continue                                       # idea already done before we could enter
            px = short_walk(o, h, l, e, entry, stop0, tgt0) * (1 + SLIP)
            f[name] = (entry / px - 1) * 100 - R.cost_frac(entry) * 100
        out.append(f)
    return out


def table(data_dir, fno=None):
    D = {}
    for name, per in R._pool_map(R._load_one, sorted(Path(data_dir).glob("*.csv.gz"))):
        if per is not None and name != "NIFTY50" and (fno is None or name in fno): D[name] = per
    T._S.update(D=D); rows = []
    for part in R._pool_map(run, list(D)): rows += part
    return pd.DataFrame(rows)


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--out", default="delay_results"); a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True); fno, _ = X.fno_list()
    res, L = {}, ["# Exhaustion fade: effect of a late entry (levels fixed at 9:20; costs + 0.10% slippage)", "",
                 "| entry | old trades | old %/trade | old yrs+ | new trades | new %/trade | new yrs+ |", "|---|---|---|---|---|---|---|"]
    Eo, En = table(a.old), table(a.new, fno)
    for name in ENTRIES:
        r = []
        for E in (Eo, En):
            x = E[name].dropna(); by = x.groupby(E.loc[x.index, "year"]).mean()
            r.append((len(x), round(float(x.mean()), 3), f"{int((by > 0).sum())}/{len(by)}"))
        res[name] = r
        L.append(f"| {name} | {r[0][0]} | {r[0][1]:+.3f} | {r[0][2]} | {r[1][0]} | {r[1][1]:+.3f} | {r[1][2]} |")
    (out / "delay.json").write_text(json.dumps(res, indent=1)); (out / "delay.md").write_text("\n".join(L)); print("\n".join(L))


if __name__ == "__main__":
    main()
