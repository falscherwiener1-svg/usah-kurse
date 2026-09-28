#!/usr/bin/env python3
"""
Pflegt data/prices.json aus den kostenlosen IEX-HIST-Dateien.

  python update.py                  # alle fehlenden Handelstage (max. 5) nachtragen
  python update.py --month 2024-06  # Monatsschluss eines Monats nachtragen (für 5-Jahres-Chart)
  python update.py --backfill 60 --part 0/6   # Monatsschlüsse der letzten 60 Monate, Teil 1 von 6

Ticker stehen in tickers.txt (einer pro Zeile, z. B. GOOGL, BRK.B).
"""
import argparse, json, os, sys, urllib.request
from datetime import date
from iex_close import scan, HIST_API

HERE = os.path.dirname(os.path.abspath(__file__))
OUT = os.path.join(HERE, "data", "prices.json")
PARTS = os.path.join(HERE, "parts")


def load_tickers():
    with open(os.path.join(HERE, "tickers.txt")) as f:
        t = [l.split("#")[0].strip().upper() for l in f]
    return [x for x in t if x]


def iex_syms(t):            # Aktiengattungen: beide Schreibweisen prüfen ("BRK.B" und "BRK B")
    return sorted({t, t.replace(".", " ").replace("-", " ")})


def hist_index():
    req = urllib.request.Request(HIST_API, headers={"User-Agent": "usah-iex-close"})
    with urllib.request.urlopen(req, timeout=120) as r:
        d = json.load(r)
    return {day: next((f for f in files if f.get("feed") == "TOPS"), None) for day, files in d.items()}


def load(path=None):
    path = path or OUT
    if os.path.exists(path):
        with open(path) as f:
            return json.load(f)
    return {"source": "IEX HIST (TOPS) – letzter Trade der regulären Sitzung auf IEX", "tickers": {}}


def save(db, path=None):
    path = path or db.get("_path") or OUT
    os.makedirs(os.path.dirname(path), exist_ok=True)
    db["updated"] = date.today().isoformat()
    for t, v in db["tickers"].items():
        v["daily"] = sorted({d[0]: d for d in v.get("daily", [])}.values())[-400:]
        v["monthly"] = sorted({m[0]: m for m in v.get("monthly", [])}.values())[-72:]
    out = {k: v for k, v in db.items() if k != "_path"}
    tmp = path + ".tmp"
    with open(tmp, "w") as f:
        json.dump(out, f, separators=(",", ":"))
    os.replace(tmp, path)


def merge_into(db, part):
    for t, v in part.get("tickers", {}).items():
        cur = db["tickers"].setdefault(t, {"daily": [], "monthly": []})
        have = {m[0]: m for m in cur.get("monthly", [])}
        for m in v.get("monthly", []):
            if m[0] not in have or have[m[0]][2] <= m[2]:
                have[m[0]] = m
        cur["monthly"] = list(have.values())
        days = {d[0]: d for d in cur.get("daily", [])}
        days.update({d[0]: d for d in v.get("daily", [])})
        cur["daily"] = list(days.values())


def merge_parts(files=None):
    """Teil-Ergebnisse (paralleler Backfill oder eigener Tageslauf) in prices.json übernehmen."""
    db = load()
    if files is None:
        files = [os.path.join(PARTS, f) for f in sorted(os.listdir(PARTS))] if os.path.isdir(PARTS) else []
    for fn in files:
        if fn.endswith(".json") and os.path.exists(fn):
            with open(fn) as f:
                merge_into(db, json.load(f))
            print(f"übernommen: {fn}", file=sys.stderr)
    save(db, OUT)


def process_day(db, day, meta, tickers, daily=True):
    syms = {v: t for t in tickers for v in iex_syms(t)}
    print(f"{day}: {int(meta.get('size', 0))/1e9:.1f} GB", file=sys.stderr, flush=True)
    res, _ = scan(meta["link"], day, list(syms))
    iso = f"{day[:4]}-{day[4:6]}-{day[6:]}"
    for t in tickers:
        found = [res[v] for v in iex_syms(t) if res[v]["last"] is not None]
        if not found:
            print(f"  {t}: kein regulärer Trade auf IEX", file=sys.stderr)
            continue
        r = max(found, key=lambda x: x["n"])
        v = db["tickers"].setdefault(t, {"daily": [], "monthly": []})
        if daily:
            v["daily"].append([iso, r["last"], r["hi"], r["lo"], r["vol"]])
        # Monatsschluss: letzter bekannter Handelstag des Monats gewinnt
        ym = iso[:7]
        cur = [m for m in v["monthly"] if m[0] == ym]
        if not cur or cur[0][2] <= iso:
            v["monthly"] = [m for m in v["monthly"] if m[0] != ym] + [[ym, r["last"], iso]]
    save(db)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--month")
    ap.add_argument("--backfill", type=int)
    ap.add_argument("--part", default="0/1")
    ap.add_argument("--max-days", type=int, default=5)
    ap.add_argument("--merge", action="store_true")
    ap.add_argument("--merge-file")
    a = ap.parse_args()
    if a.merge:
        return merge_parts()
    if a.merge_file:
        return merge_parts([a.merge_file])

    tickers = load_tickers()
    idx = {d: m for d, m in hist_index().items() if m}
    days = sorted(idx)
    db = load()

    if a.month or a.backfill:
        if a.month:
            months = [a.month]
        else:
            months = sorted({d[:4] + "-" + d[4:6] for d in days})[-a.backfill - 1:-1]
            k, n = map(int, a.part.split("/"))
            months = months[k::n]
            db = {"source": db["source"], "tickers": {}, "_path": os.path.join(PARTS, f"part_{k}.json")}
        for ym in months:
            last = [d for d in days if d.startswith(ym.replace("-", ""))]
            if last:
                process_day(db, last[-1], idx[last[-1]], tickers, daily=False)
        return

    have = {d[0] for v in db["tickers"].values() for d in v.get("daily", [])}
    todo = [d for d in days[-a.max_days:] if f"{d[:4]}-{d[4:6]}-{d[6:]}" not in have]
    # neue Ticker ohne Daten: letzten Handelstag sicher mitnehmen
    if not todo and any(t not in db["tickers"] for t in tickers):
        todo = [days[-1]]
    for d in todo:
        process_day(db, d, idx[d], tickers)
    if not todo:
        print("Nichts zu tun – alles aktuell.", file=sys.stderr)


if __name__ == "__main__":
    main()
