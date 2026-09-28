#!/usr/bin/env python3
"""
IEX-Schlusskurse aus den kostenlosen HIST-Dateien der Börse IEX.

Quelle: https://iextrading.com/api/1.0/hist  (öffentlich, kostenlos, ohne Registrierung, T+1)
Format: IEX TOPS 1.6 (pcap, gzip). Wir lesen ausschließlich "Trade Report"-Meldungen:

  Offset  Länge  Feld
  0       1      Message Type = 0x54 ('T')
  1       1      Sale Condition Flags
  2       8      Timestamp (ns seit Epoch, UTC)
  10      8      Symbol (ASCII, rechts mit Leerzeichen aufgefüllt)
  18      4      Size (Stück)
  22      8      Price (int64, 1/10.000 $)
  30      8      Trade ID
Jede Meldung steht im IEX-TP-Segment hinter einem 2-Byte-Längenfeld (38 = 0x26 0x00).

Statt jedes Paket einzeln zu zerlegen (Milliarden Meldungen pro Tag), suchen wir die
8-Byte-Symbolfolge direkt im entpackten Datenstrom (C-schnelles bytes.find) und prüfen
die Rahmenbytes davor. Das ist exakt, weil Länge+Typ (0x26 0x00 0x54) mitgeprüft werden.

Ergebnis je Symbol: letzter Trade der regulären Sitzung (9:30–16:00 New York),
ohne Extended-Hours- und Odd-Lot-Trades, dazu Tageshoch/-tief und Volumen auf IEX.
"""
import json, struct, subprocess, sys, time, urllib.request, zlib
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

HIST_API = "https://iextrading.com/api/1.0/hist"
NY = ZoneInfo("America/New_York")
FLAG_EXT, FLAG_ODD = 0x40, 0x20
CHUNK = 16 * 1024 * 1024
MSG = struct.Struct("<BBq8sIqq")   # 38 Bytes


def hist_link(day):
    with urllib.request.urlopen(urllib.request.Request(HIST_API + "?date=" + day, headers={"User-Agent": "usah-iex-close"}), timeout=60) as r:
        data = json.load(r)
    files = data if isinstance(data, list) else data.get(day, [])
    for f in files:
        if f.get("feed") == "TOPS":
            return f["link"], int(f.get("size", 0)), f.get("version")
    return None, 0, None


def stream_chunks(src, max_bytes=None):
    """Entpackter Datenstrom aus URL oder lokaler .gz-Datei (zlib läuft in C)."""
    if src.startswith("http"):
        cmd = ["curl", "-sL", "--retry", "5", "--fail"]
        if max_bytes:
            cmd += ["-r", f"0-{max_bytes}"]
        proc = subprocess.Popen(cmd + [src], stdout=subprocess.PIPE, bufsize=CHUNK)
        raw = proc.stdout
    else:
        proc, raw = None, open(src, "rb")
    d = zlib.decompressobj(16 + zlib.MAX_WBITS)
    while True:
        buf = raw.read(CHUNK)
        if not buf:
            break
        try:
            out = d.decompress(buf)
        except zlib.error:          # abgeschnittene Teil-Downloads (Tests) sauber beenden
            break
        if out:
            yield out
    if proc:
        proc.stdout.close(); proc.wait()


def session_bounds(day):
    y, m, d = int(day[:4]), int(day[4:6]), int(day[6:])
    o = datetime(y, m, d, 9, 30, tzinfo=NY).timestamp() * 1e9
    c = datetime(y, m, d, 16, 0, tzinfo=NY).timestamp() * 1e9
    return o, c


def scan(src, day, symbols, max_bytes=None):
    t_open, t_close = session_bounds(day)
    pats = {s: s.ljust(8).encode() for s in symbols}
    res = {s: {"last": None, "ts": 0, "hi": None, "lo": None, "vol": 0, "n": 0, "ext_last": None} for s in symbols}
    tail = b""
    total = 0
    for chunk in stream_chunks(src, max_bytes):
        total += len(chunk)
        buf = tail + chunk
        for s, p in pats.items():
            r = res[s]
            i = buf.find(p, 12)
            while i != -1:
                st = i - 12                       # Beginn Längenfeld
                if buf[st] == 0x26 and buf[st + 1] == 0x00 and buf[st + 2] == 0x54 and st + 40 <= len(buf):
                    _, flags, ts, _, size, price, _ = MSG.unpack_from(buf, st + 2)
                    px = price / 10000.0
                    if 0 < px < 1e6:
                        if flags & FLAG_EXT or not (t_open <= ts <= t_close):
                            r["ext_last"] = px
                        elif not flags & FLAG_ODD:
                            r["n"] += 1; r["vol"] += size
                            r["hi"] = px if r["hi"] is None else max(r["hi"], px)
                            r["lo"] = px if r["lo"] is None else min(r["lo"], px)
                            if ts >= r["ts"]:
                                r["ts"], r["last"] = ts, px
                i = buf.find(p, i + 1)
        tail = buf[-64:]
    for s in symbols:
        r = res[s]
        r["time"] = datetime.fromtimestamp(r["ts"] / 1e9, NY).strftime("%H:%M:%S") if r["ts"] else None
        del r["ts"]
    return res, total


if __name__ == "__main__":
    # Aufruf: iex_close.py JJJJMMTT SYM1,SYM2,... [lokale_datei.gz | url] [max_bytes]
    day, syms = sys.argv[1], sys.argv[2].split(",")
    src = sys.argv[3] if len(sys.argv) > 3 else None
    mx = int(sys.argv[4]) if len(sys.argv) > 4 else None
    if not src:
        src, size, ver = hist_link(day)
        if not src:
            sys.exit(f"Keine TOPS-Datei für {day}")
        print(f"TOPS {ver}, {size/1e9:.1f} GB", file=sys.stderr)
    t = time.time()
    res, total = scan(src, day, syms, mx)
    print(f"{total/1e9:.2f} GB entpackt in {time.time()-t:.0f} s", file=sys.stderr)
    print(json.dumps({"date": f"{day[:4]}-{day[4:6]}-{day[6:]}", "source": "IEX HIST (TOPS)", "quotes": res}, indent=1))
