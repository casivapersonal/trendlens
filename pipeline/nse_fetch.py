"""Refreshes the NSE watch lists every morning (audit fix #14): ASM and GSM surveillance lists, the board-meeting /
results calendar and the trading-holiday list (nse/holidays.csv, read by trading/arena/config.py), written to ../nse/*_auto.csv where core.load_nse reads them. NSE's site needs a browser-like session;
if NSE refuses (it sometimes blocks cloud servers), the previous files are kept and the run says so. Hand-uploaded CSVs in
nse/ keep working alongside these."""
import csv, datetime as dt, os, sys, time

import requests

OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "nse")
H = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
     "Accept": "application/json, text/plain, */*", "Accept-Language": "en-US,en;q=0.9", "Referer": "https://www.nseindia.com/"}


def rows(obj):
    """Every dict with a 'symbol' key anywhere in NSE's JSON (the layouts differ between endpoints and change over time)."""
    if isinstance(obj, dict):
        if any(k.lower() == "symbol" for k in obj): yield obj
        for v in obj.values(): yield from rows(v)
    elif isinstance(obj, list):
        for v in obj: yield from rows(v)


def get(s, url):
    for k in range(3):
        try:
            r = s.get(url, timeout=25)
            if r.status_code == 200: return r.json()
        except Exception:
            pass
        time.sleep(2 + 2 * k)
    return None


def write(name, header, data):
    path = os.path.join(OUT, name); tmp = path + ".tmp"
    with open(tmp, "w", newline="") as f:
        w = csv.writer(f); w.writerow(header); w.writerows(data)
    os.replace(tmp, path)
    print(f"{name}: {len(data)} rows")


def main():
    os.makedirs(OUT, exist_ok=True)
    s = requests.Session(); s.headers.update(H)
    try: s.get("https://www.nseindia.com/", timeout=25)
    except Exception as e: print("NSE home page not reachable:", e)
    ok = 0
    j = get(s, "https://www.nseindia.com/api/reportASM")
    if j is not None:
        d = [(r.get("symbol") or r.get("Symbol"), r.get("asmSurvIndicator") or r.get("stage") or "ASM") for r in rows(j)]
        d = [x for x in d if x[0]]
        if d: write("asm_auto.csv", ["SYMBOL", "STAGE"], d); ok += 1
    j = get(s, "https://www.nseindia.com/api/reportGSM")
    if j is not None:
        d = [(r.get("symbol") or r.get("Symbol"), r.get("gsmStage") or r.get("stage") or "GSM") for r in rows(j)]
        d = [x for x in d if x[0]]
        if d: write("gsm_auto.csv", ["SYMBOL", "STAGE"], d); ok += 1
    today = dt.date.today(); a, b = today.strftime("%d-%m-%Y"), (today + dt.timedelta(days=45)).strftime("%d-%m-%Y")
    j = get(s, f"https://www.nseindia.com/api/event-calendar?index=equities&from_date={a}&to_date={b}") or get(s, "https://www.nseindia.com/api/event-calendar")
    if j is not None:
        d = []
        for r in rows(j):
            sym = r.get("symbol"); date = r.get("date") or r.get("bm_date") or r.get("meetingDate")
            purpose = " ".join(str(r.get(k) or "") for k in ("purpose", "bm_purpose", "bm_desc", "desc"))
            if sym and date: d.append((sym, date, purpose.strip() or "Results"))
        if d: write("results_auto.csv", ["SYMBOL", "DATE", "PURPOSE"], d); ok += 1
    j = get(s, "https://www.nseindia.com/api/holiday-master?type=trading")
    if isinstance(j, dict) and j.get("CM"):          # equity segment; kept across years (the API returns the current year only)
        old = {}
        try:
            with open(os.path.join(OUT, "holidays.csv"), newline="") as f: old = {r["DATE"]: r["DESCRIPTION"] for r in csv.DictReader(f)}
        except Exception: pass
        for r in j["CM"]:
            try: old[dt.datetime.strptime(r["tradingDate"], "%d-%b-%Y").date().isoformat()] = (r.get("description") or "").strip()
            except Exception: pass
        if old: write("holidays.csv", ["DATE", "DESCRIPTION"], sorted(old.items())); ok += 1
    if ok:
        with open(os.path.join(OUT, "asof.txt"), "w") as f: f.write(dt.date.today().isoformat())
    if not ok:
        print("NSE refused every request: the previous nse/*.csv files are kept (upload them by hand if this keeps happening)")
    return 0                                   # never fail the daily job over this


if __name__ == "__main__":
    sys.exit(main())
