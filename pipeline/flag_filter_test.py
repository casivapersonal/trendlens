"""Bull flag: does "skip it if the first 30 minutes fall, otherwise buy at 9:45" beat the current rule?

Uses the trade list from the swing timing study (flag_trades.csv, live rules) and the five-minute files. For every trade whose fill
day has five-minute data:
  rule      the current daily rule (entry-range fill), result R = (exit - fill) / (fill - stop)
  filter A  first 30 minutes down (9:40 candle closes below the 9:15 open): no trade (0 R); otherwise buy at the 9:45 open
  filter B  as A, but only if the 9:45 price is still inside the entry range (<= top of the range); otherwise no trade
Both filters keep the live checks: no entry at or below the stop, and at least 2:1 reward to risk at the actual price.
After the 9:45 entry the fill day is walked on five-minute candles (stop first); later days use the rule's own exit.
Choose on 2014-2021, check on 2022-2026. Writes filter_summary.json / .md."""
from __future__ import annotations
import argparse, json, sys
from pathlib import Path
import numpy as np, pandas as pd

HERE = Path(__file__).resolve().parent; sys.path.insert(0, str(HERE))
from swing_timing_study import load_bars, SLOT, period  # noqa: E402

E = SLOT["09:45"]


def run(F, bars):
    rows = []
    for r in F.itertuples():
        a = bars.get(r.sym, {}).get(r.fill_day)
        if a is None or r.fill_day < "2014": continue
        o, h, l, c = a[0], a[1], a[2], a[3]; k = r.open / o[0]            # five-minute prices scaled to the daily open
        R0 = (r.exit - r.fill) / (r.fill - r.stop)
        up = c[E - 1] >= o[0]
        res = {"sym": r.sym, "day": r.fill_day, "period": period(r.fill_day), "rule": R0, "up": bool(up)}
        for name, need_range in (("A", False), ("B", True)):
            R = 0.0; took = False
            if up:
                px = o[E] * k
                ok = px > r.stop and (r.target - px) / (px - r.stop) >= 2.0 and np.min(l[:E]) * k > r.stop
                if need_range and px > r.hi: ok = False
                if ok:
                    took = True; ex = None
                    for j in range(E, 75):                                     # rest of the fill day, stop first
                        if l[j] * k <= r.stop: ex = r.stop; break
                        if h[j] * k >= r.target: ex = r.target; break
                    if ex is None:
                        ex = r.exit if r.exit_day > r.fill_day else c[74] * k    # later days: the rule's exit; same-day rule exit: today's close
                    R = (ex - px) / (px - r.stop)
            res[name] = R; res[name + "_took"] = took
        rows.append(res)
    return pd.DataFrame(rows)


def stats(D):
    out = {}
    for P_, g in D.groupby("period"):
        o = {"signals": int(len(g)), "rule_avg_R": round(float(g.rule.mean()), 3), "rule_total_R": round(float(g.rule.sum()), 1),
             "rule_win_pct": round(100 * float((g.rule > 0).mean()), 1)}
        for v in ("A", "B"):
            t = g[g[v + "_took"]]; d = g[v] - g.rule
            se = d.std(ddof=1) / np.sqrt(len(d))
            o[v] = {"trades": int(len(t)), "avg_R_per_trade": round(float(t[v].mean()), 3) if len(t) else None,
                    "total_R": round(float(g[v].sum()), 1), "win_pct": round(100 * float((t[v] > 0).mean()), 1) if len(t) else None,
                    "change_per_signal_R": round(float(d.mean()), 3), "t": round(float(d.mean() / se), 2) if se > 0 else None}
        out[P_] = o
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(); ap.add_argument("--trades"); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--out", default="filter_results")
    a = ap.parse_args(); out = Path(a.out); out.mkdir(exist_ok=True)
    F = pd.read_csv(a.trades)
    need = {}
    for r in F.itertuples(): need.setdefault(r.sym, set()).add(r.fill_day)
    bars = load_bars([a.old, a.new], need)
    D = run(F, bars); S = stats(D)
    D.to_csv(out / "filter_trades.csv", index=False); (out / "filter_summary.json").write_text(json.dumps(S, indent=1))
    L = ["# Bull flag: first-30-minute filter", "", "| period | signals | rule: avg R, total R, win% | A: trades, avg R, total R, win%, change/signal (t) | B: same |", "|---|---|---|---|---|"]
    for P_, o in S.items():
        f = lambda v: f"{o[v]['trades']}, {o[v]['avg_R_per_trade']}, {o[v]['total_R']}, {o[v]['win_pct']}%, {o[v]['change_per_signal_R']:+} (t {o[v]['t']})"
        L.append(f"| {P_} | {o['signals']} | {o['rule_avg_R']}, {o['rule_total_R']}, {o['rule_win_pct']}% | {f('A')} | {f('B')} |")
    (out / "filter_summary.md").write_text("\n".join(L)); print("\n".join(L))
