#!/usr/bin/env python3
"""Schneller Ersatz für den IEX-Backfill: Monatsschlüsse über die freie Nasdaq-API.

Füllt nur Ticker auf, denen Monatshistorie fehlt (Standard: < 50 Monate).
Nasdaq liefert splitbereinigte Kurse; prices.json speichert Rohkurse und
bereinigt erst in dcf.py über prices["splits"]. Deshalb werden Kurse vor einem
bekannten Split hier mit dem Faktor multipliziert (zurück auf Rohkurs).

  python nasdaq_backfill.py                # alle Ticker mit Lücken
  python nasdaq_backfill.py --only PYPL,MO
  python nasdaq_backfill.py --min 50 --months 61
"""
import argparse, json, os, sys, time, datetime as dt, urllib.request

HERE = os.path.dirname(os.path.abspath(__file__))
PRICES = os.path.join(HERE, "data", "prices.json")
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0 Safari/537.36")
ETFS = {"SPY", "QQQ"}


def fetch(ticker, start, end):
    cls = "etf" if ticker in ETFS else "stocks"
    sym = ticker.replace(".", "%2F")
    url = (f"https://api.nasdaq.com/api/quote/{sym}/historical?assetclass={cls}"
           f"&fromdate={start}&todate={end}&limit=9999")
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=30) as r:
        js = json.load(r)
    rows = (((js or {}).get("data") or {}).get("tradesTable") or {}).get("rows") or []
    out = []
    for row in rows:
        try:
            m, d, y = row["date"].split("/")
            px = float(row["close"].replace("$", "").replace(",", ""))
            out.append((f"{y}-{m}-{d}", px))
        except Exception:
            continue
    return sorted(out)


def month_ends(daily):
    last = {}
    for day, px in daily:
        last[day[:7]] = (day, px)
    return last


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only")
    ap.add_argument("--min", type=int, default=50, help="auffüllen, wenn weniger Monate vorhanden")
    ap.add_argument("--months", type=int, default=61)
    ap.add_argument("--file", default=PRICES)
    a = ap.parse_args()

    db = json.load(open(a.file))
    splits = db.get("splits") or {}
    tick = db["tickers"]
    todo = a.only.split(",") if a.only else [t for t, x in tick.items() if len(x.get("monthly", [])) < a.min]
    today = dt.date.today()
    start = (today.replace(day=1) - dt.timedelta(days=31 * a.months)).isoformat()
    cur_month = today.strftime("%Y-%m")
    ok, fail = [], []
    for t in todo:
        try:
            daily = fetch(t, start, today.isoformat())
        except Exception as e:
            fail.append(f"{t}: {e}"); continue
        if not daily:
            fail.append(f"{t}: keine Daten"); continue
        me = month_ends(daily)
        have = {m[0]: m for m in tick.setdefault(t, {}).setdefault("monthly", [])}
        added = 0
        for mon, (day, px) in me.items():
            if mon in have or mon >= cur_month:
                continue            # IEX-Werte und den laufenden Monat nicht überschreiben
            raw = px
            for s_day, f in splits.get(t, []):
                if day < s_day and f:
                    raw *= f        # zurück auf Rohkurs, dcf.py teilt wieder
            have[mon] = [mon, round(raw, 4), day]
            added += 1
        tick[t]["monthly"] = [have[k] for k in sorted(have)]
        ok.append(f"{t}+{added}")
        time.sleep(0.4)
    json.dump(db, open(a.file, "w"), separators=(",", ":"))
    print("aufgefüllt:", " ".join(ok) or "-")
    if fail:
        print("fehlgeschlagen:", "; ".join(fail))
    return 0


if __name__ == "__main__":
    sys.exit(main())
