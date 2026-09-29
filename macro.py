#!/usr/bin/env python3
"""Makrodaten von FRED (Federal Reserve Bank of St. Louis) -> data/macro.json

Key kommt ausschließlich aus der Umgebungsvariable FRED_API_KEY (GitHub Secret).
Pro Reihe: letzter Wert, Wert vor ca. 1 Monat und 1 Jahr, Wochenverlauf 2 Jahre (für Minigrafiken).
"""
import datetime as dt, json, os, sys, time, urllib.parse, urllib.request

KEY = os.environ.get("FRED_API_KEY", "").strip()
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data", "macro.json")
SERIES = {
    "DGS10":     ("US Staatsanleihe 10 Jahre", "%", 2),
    "DFII10":    ("US Staatsanleihe 10 Jahre real", "%", 2),
    "DGS2":      ("US Staatsanleihe 2 Jahre", "%", 2),
    "DTB3":      ("US T Bill 3 Monate", "%", 2),
    "T10YIE":    ("Inflationserwartung 10 Jahre", "%", 2),
    "DFEDTARU":  ("Fed Leitzins Obergrenze", "%", 2),
    "SP500":     ("S&P 500", "Punkte", 2),
    "NASDAQ100": ("Nasdaq 100", "Punkte", 2),
    "VIXCLS":    ("VIX", "Punkte", 2),
    "DEXUSEU":   ("US Dollar je Euro", "USD", 4),
    "CPIAUCSL":  ("US Verbraucherpreise", "Index", 3),
    "UNRATE":    ("US Arbeitslosenquote", "%", 1),
}

def fetch(sid, start):
    q = urllib.parse.urlencode({"series_id": sid, "api_key": KEY, "file_type": "json",
                                "observation_start": start, "sort_order": "asc"})
    req = urllib.request.Request("https://api.stlouisfed.org/fred/series/observations?" + q,
                                 headers={"User-Agent": "usah-macro"})
    for i in range(4):
        try:
            with urllib.request.urlopen(req, timeout=40) as r:
                d = json.load(r)
            return [(o["date"], float(o["value"])) for o in d.get("observations", []) if o["value"] not in (".", "")]
        except Exception as e:
            if i == 3: raise
            time.sleep(3 * (i + 1))

def at_or_before(obs, day):
    best = None
    for d, v in obs:
        if d <= day: best = (d, v)
        else: break
    return best

def main():
    if not KEY:
        print("FRED_API_KEY fehlt, nichts zu tun"); return 0
    today = dt.date.today()
    start = (today - dt.timedelta(days=800)).isoformat()
    old = {}
    if os.path.exists(OUT):
        try: old = json.load(open(OUT)).get("series", {})
        except Exception: old = {}
    out = {"updated": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%MZ"),
           "source": "FRED, Federal Reserve Bank of St. Louis", "series": {}}
    fails = 0
    for sid, (name, unit, dec) in SERIES.items():
        try:
            obs = fetch(sid, start)
            if not obs: raise ValueError("leer")
        except Exception as e:
            print(f"{sid}: Fehler {e}", file=sys.stderr); fails += 1
            if sid in old: out["series"][sid] = old[sid]
            continue
        d, v = obs[-1]
        last = dt.date.fromisoformat(d)
        m1 = at_or_before(obs, (last - dt.timedelta(days=30)).isoformat())
        y1 = at_or_before(obs, (last - dt.timedelta(days=365)).isoformat())
        weekly, seen = [], set()
        for od, ov in obs:
            wk = dt.date.fromisoformat(od).isocalendar()[:2]
            if wk in seen: weekly[-1] = [od, round(ov, dec)]
            else: seen.add(wk); weekly.append([od, round(ov, dec)])
        out["series"][sid] = {"name": name, "unit": unit, "date": d, "value": round(v, dec),
                              "m1": [m1[0], round(m1[1], dec)] if m1 else None,
                              "y1": [y1[0], round(y1[1], dec)] if y1 else None,
                              "weekly": weekly[-105:]}
        print(f"{sid:10} {d} {v}")
    # abgeleitete Werte
    s = out["series"]
    if "CPIAUCSL" in s and s["CPIAUCSL"].get("y1"):
        c = s["CPIAUCSL"]; out["derived"] = {"cpi_yoy": round((c["value"] / c["y1"][1] - 1) * 100, 1), "cpi_date": c["date"]}
    if fails == len(SERIES):
        print("alle Reihen fehlgeschlagen", file=sys.stderr); return 1
    os.makedirs(os.path.dirname(OUT), exist_ok=True)
    json.dump(out, open(OUT, "w"), separators=(",", ":"), ensure_ascii=False)
    print("geschrieben", OUT, os.path.getsize(OUT), "Bytes")
    return 0

if __name__ == "__main__":
    sys.exit(main())
