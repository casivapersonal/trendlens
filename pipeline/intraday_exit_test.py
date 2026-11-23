"""Exit rules for the exhaustion gap fade (short), chosen on 2014-2021 and checked on 2022-2026.

  python pipeline/intraday_exit_test.py --old hist_data --new new_data --out exit_results

Trades (fixed, from pipeline/intraday_exhaustion_test.py): F&O stocks that open 1.5-8% above the previous close after
running up (up >20% in 5 days, or >25% above the 50-day average, or a 20-day breakout on volume the day before).
Short at the 9:20 open. Costs: Indian intraday charges + 0.10% slippage each side.
About 40 exit rules: stop (0.5-1.5 daily ATR, first-candle high, 1.5%), target (none, 1.5R, 2R, 3R, gap fill to the
previous close), management (breakeven after 1R, trail behind the last candle's high after 1R, half off at 1R),
time (out 11:30 if not in profit, out 13:30, out 15:15). An exit is adopted only if it ranks well on 2014-2021 AND
beats the current rule (stop 1 ATR, target 2R, out 15:15) on 2022-2026."""
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
BAR = {"11:30": 27, "13:30": 51, "15:15": 72}


def rules():
    out = {}
    for s in ("atr0.5", "atr0.75", "atr1", "atr1.25", "atr1.5", "c1high", "pct1.5"):
        for t in ("none", "1.5R", "2R", "3R", "fill"):
            out[f"stop {s}, target {t}, out 15:15"] = dict(stop=s, tgt=t, mgmt=None, xb="15:15")
    for s in ("atr0.75", "atr1", "atr1.25"):
        for m in ("breakeven", "trail", "half"):
            out[f"stop {s}, target 2R, {m} after 1R, out 15:15"] = dict(stop=s, tgt="2R", mgmt=m, xb="15:15")
            out[f"stop {s}, no target, {m} after 1R, out 15:15"] = dict(stop=s, tgt="none", mgmt=m, xb="15:15")
        out[f"stop {s}, target 2R, out 11:30 if not in profit"] = dict(stop=s, tgt="2R", mgmt="timestop", xb="15:15")
        out[f"stop {s}, target 2R, out 13:30"] = dict(stop=s, tgt="2R", mgmt=None, xb="13:30")
    return out


def sim_short(o, h, l, c, entry, stop, tgt, mgmt, xb):
    """Short from bar 1. Returns average exit price (half-off rule averages two exits)."""
    risk = stop - entry; one_r = entry - risk; parts, part_px = [], None
    st = stop
    for j in range(1, xb):
        if j > 1 and o[j] >= st: parts.append(o[j]); break                                  # gap through the stop
        if h[j] >= st: parts.append(st); break
        if tgt is not None and l[j] <= tgt: parts.append(min(o[j], tgt) if j > 1 else tgt); break
        if mgmt in ("breakeven", "trail", "half") and l[j] <= one_r:
            if mgmt == "half" and part_px is None: part_px = one_r
            st = min(st, entry) if mgmt in ("breakeven", "half") else st
        if mgmt == "trail" and l[1:j + 1].min() <= one_r: st = min(st, h[j])                 # trail behind this candle's high
        if mgmt == "timestop" and j == BAR["11:30"] - 1 and c[j] >= entry: parts.append(c[j]); break
    else:
        parts.append(o[xb])
    if not parts: parts.append(o[xb])
    px = parts[0]
    return (part_px + px) / 2 if part_px is not None else px


def run_events(sym):
    """Exhaustion short events of one stock with the net % of every exit rule."""
    per = T._S["D"][sym]; d = T.daily(per); days = list(d.index); RU = _RULES; out = []
    for i in range(T._S.get("warmup", 201), len(days)):
        day, p = days[i], d.iloc[i - 1]
        o, h, l, c, v = per[day]; pc = p.close
        if not pc > 0 or pc < 20 or not p.value_cr >= 5 or not p.atr > 0: continue
        g = (o[0] / pc - 1) * 100
        if not 1.5 <= g <= 8: continue
        ex_ = (p.ret5 > 0.20) or (p.ext50 > 0.25) or bool(p.bo20)
        if not ex_: continue
        entry = o[1] * (1 - SLIP); A = float(p.atr)
        f = {"sym": sym, "day": day, "year": day.year, "ret5": float(p.ret5)}
        for name, r in RU.items():
            s = r["stop"]
            stop = entry + (float(s[3:]) * A if s.startswith("atr") else entry * 0.015 if s == "pct1.5" else 0)
            if s == "c1high": stop = max(h[0] * 1.001, entry * 1.002)
            risk = stop - entry
            if risk <= 0 or risk / entry > 0.08: f[name] = np.nan; continue
            t = r["tgt"]
            tgt = None if t == "none" else pc if t == "fill" else entry - float(t[:-1]) * risk
            if tgt is not None and tgt >= entry: f[name] = np.nan; continue
            px = sim_short(o, h, l, c, entry, stop, tgt, r["mgmt"], BAR[r["xb"]]) * (1 + SLIP)
            f[name] = (entry / px - 1) * 100 - R.cost_frac(entry) * 100
        out.append(f)
    return out


_RULES = rules()


def table(data_dir, fno=None):
    D = {}
    for name, per in R._pool_map(R._load_one, sorted(Path(data_dir).glob("*.csv.gz"))):
        if per is not None and name != "NIFTY50" and (fno is None or name in fno): D[name] = per
    T._S.update(D=D)
    rows = []
    for part in R._pool_map(run_events, list(D)): rows += part
    E = pd.DataFrame(rows)
    print(f"{data_dir}: {len(D)} stocks, {len(E)} exhaustion short trades", flush=True)
    return E


def summary(E):
    out = {}
    for name in _RULES:
        x = E[name].dropna(); by = x.groupby(E.loc[x.index, "year"]).mean()
        out[name] = {"n": int(len(x)), "net": round(float(x.mean()), 3), "win": round(float((x > 0).mean() * 100), 1),
                     "years_pos": f"{int((by > 0).sum())}/{len(by)}", "by_year": {int(k): round(float(v), 2) for k, v in by.items()}}
    return out


def main():
    ap = argparse.ArgumentParser(); ap.add_argument("--old"); ap.add_argument("--new"); ap.add_argument("--out", default="exit_results"); a = ap.parse_args()
    out = Path(a.out); out.mkdir(parents=True, exist_ok=True)
    fno, src = X.fno_list()
    So, Sn = summary(table(a.old)), summary(table(a.new, fno))
    base = "stop atr1, target 2R, out 15:15"
    ranked = sorted(_RULES, key=lambda k: -So[k]["net"])
    top = ranked[:8]
    adopt = [k for k in top if Sn[k]["net"] > Sn[base]["net"] and So[k]["net"] > So[base]["net"]]
    L = ["# Exit rules for the exhaustion gap fade (short), F&O stocks, after costs + 0.10% slippage", "",
         f"Chosen on 2014-2021 (top 8 by net %/trade), checked on 2022-2026. Current rule: {base}.",
         f"Old: {So[base]['net']:+.3f}%/trade ({So[base]['n']} trades). New: {Sn[base]['net']:+.3f}%/trade ({Sn[base]['n']} trades).", "",
         "**Adopt:** " + (", ".join(adopt) if adopt else "none - keep the current rule"), "",
         "| rank (old) | exit rule | old %/trade | old win % | old yrs+ | new %/trade | new win % | new yrs+ |", "|---|---|---|---|---|---|---|---|"]
    for i, k in enumerate(ranked, 1):
        L.append(f"| {i} | {k}{' (current)' if k == base else ''}{' (top 8)' if k in top else ''} | {So[k]['net']:+.3f} | {So[k]['win']} | {So[k]['years_pos']} | "
                 f"{Sn[k]['net']:+.3f} | {Sn[k]['win']} | {Sn[k]['years_pos']} |")
    (out / "exits.json").write_text(json.dumps({"old": So, "new": Sn, "top8_old": top, "adopt": adopt, "fno_source": src}, indent=1))
    (out / "exits.md").write_text("\n".join(L)); print("\n".join(L))


if __name__ == "__main__":
    main()
