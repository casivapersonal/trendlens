"""Day-trader risk layer: size a trade from its stop so the loss at the stop stays inside a rupee limit (charges included),
and gate entries by trades-per-day. A setting of 0 switches that rule off."""
from __future__ import annotations

from . import charges as ch


def cap_alloc(cfg: dict, equity: float, entry: float, stop_pct: float, alloc_pct: float) -> float:
    """Lower allocation_pct (never raise it) so qty * stop distance + round-trip charges <= max_loss_per_trade."""
    lim = float(cfg.get("risk", {}).get("max_loss_per_trade", 0) or 0)
    if lim <= 0 or stop_pct <= 0 or entry <= 0 or equity <= 0:
        return alloc_pct
    prod, rates = cfg["strategy"]["product"], cfg.get("charges", {})
    per_share = entry * stop_pct / 100
    qty = int(lim // per_share)
    while qty > 0:
        loss = qty * per_share + ch.compute("BUY", qty, entry, prod, rates).total + ch.compute("SELL", qty, entry * (1 - stop_pct / 100), prod, rates).total
        if loss <= lim:
            break
        qty -= 1
    return min(alloc_pct, qty * entry / equity * 100)


def trades_today(j, run_id: int, day: str) -> int:
    return j.one("SELECT COUNT(*) n FROM orders WHERE run_id=? AND side='BUY' AND status='filled' AND substr(ts,1,10)=?", (run_id, day))["n"]


def gate(cfg: dict, j, run_id: int, now) -> str | None:
    """Reason string if a new entry must be refused, else None."""
    mx = int(cfg.get("risk", {}).get("max_trades_per_day", 0) or 0)
    if mx and trades_today(j, run_id, now.date().isoformat()) >= mx:
        return f"trades-a-day limit ({mx}) reached"
    return None
