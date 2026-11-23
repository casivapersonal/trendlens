"""Trading universe: Nifty 500 by default (Nifty 1000 later by changing one setting).
Looked up in order: [universe].file, ../universe/<name>.csv (column 'Symbol'), ../pipeline/<name>.txt (space/line separated),
then TrendLens's own pipeline/syms.txt. NSE surveillance names (ASM/GSM files in ../nse) are removed."""
from __future__ import annotations

import csv
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent          # trading/
REPO = ROOT.parent                                     # TrendLens repo root


def _read(p: Path) -> list[str]:
    if not p.exists():
        return []
    if p.suffix.lower() == ".csv":
        with open(p, newline="", encoding="utf-8-sig", errors="ignore") as f:
            rows = list(csv.DictReader(f))
        col = next((k for k in (rows[0] if rows else {}) if k.strip().lower() == "symbol"), None)
        return [r[col].strip() for r in rows if col and r[col].strip()]
    return p.read_text().split()


def load(cfg: dict | None = None) -> list[str]:
    u = (cfg or {}).get("universe", {})
    name = u.get("name", "nifty500")
    cands = []
    if u.get("file"):
        f = Path(u["file"]); cands.append(f if f.is_absolute() else (ROOT / f).resolve())
    cands += [REPO / "universe" / f"{name}.csv", REPO / "pipeline" / f"{name}.txt", REPO / "pipeline" / "syms.txt"]
    syms: list[str] = []
    for p in cands:
        syms = _read(p)
        if syms:
            break
    bl = blocked()
    return [s for s in dict.fromkeys(syms) if s not in bl]


def blocked() -> dict[str, str]:
    """{symbol: reason} from the repo's nse/*.csv (ASM / GSM). Empty if the files or pandas are missing."""
    try:
        import sys
        sys.path.insert(0, str(REPO / "pipeline"))
        from core import load_nse
        return load_nse(str(REPO / "nse"))[0]
    except Exception:
        return {}


def results() -> dict[str, str]:
    """{symbol: next results date 'YYYY-MM-DD'} from the repo's nse/*.csv board-meeting / event calendar (refreshed every morning
    by pipeline/nse_fetch.py). Empty if missing."""
    try:
        import sys
        sys.path.insert(0, str(REPO / "pipeline"))
        from core import load_nse
        return load_nse(str(REPO / "nse"))[1]
    except Exception:
        return {}
