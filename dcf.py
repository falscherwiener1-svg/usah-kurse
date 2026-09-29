#!/usr/bin/env python3
"""
USAktienHub – automatisches DCF-Modell (nur kostenlose, veröffentlichbare Quellen)

Quellen
  SEC EDGAR companyfacts (XBRL)     Quartals-/Jahreszahlen, Bilanz, Aktien
  SEC EDGAR 8-K Item 2.02 (EX-99)   Prognose der Firma (Guidance), sofern mit Zahlen veröffentlicht
  US-Treasury Daily Yield Curve     risikoloser Zins (10 Jahre)
  Damodaran (NYU Stern) ERPbymonth  implizite Marktrisikoprämie
  data/prices.json (IEX HIST)       Kurs, 5-Jahres-Beta gegen SPY

Modell
  FCFF = operativer Cashflow − Investitionen − aktienbasierte Vergütung + Zinsaufwand × (1 − Steuer)
  10 Jahre: Umsatzwachstum startet bei Guidance bzw. Trend und läuft linear auf das ewige Wachstum zu,
  FCFF-Marge wandert bis Jahr 5 vom TTM-Wert zum 5-Jahres-Schnitt. Endwert nach Gordon, Halbjahreskonvention.
  WACC = CAPM (Beta nach Blume) + synthetisches Rating für Fremdkapital, Marktwert-Gewichte.
  Szenarien Bear / Base / Bull, Fair Value = 25 / 50 / 25.
  Reverse-DCF: welches Anfangswachstum rechtfertigt den aktuellen Kurs?

Aufruf: python3 dcf.py [--cache DIR] [--only TICKER,...]
"""
import argparse, datetime as dt, html, io, json, math, os, re, sys, time, urllib.request

UA = os.environ.get("SEC_UA", "USAktienHub Research kontakt@usaktienhub.de")
MODEL_VERSION = "1.0"
HERE = os.path.dirname(os.path.abspath(__file__))
D = lambda s: dt.date.fromisoformat(s)

# ---------------------------------------------------------------- HTTP
_last = [0.0]
def fetch(url, cache=None, binary=False, max_age_h=20):
    fn = None
    if cache:
        fn = os.path.join(cache, re.sub(r"[^A-Za-z0-9._-]+", "_", url)[-180:])
        if os.path.exists(fn) and time.time() - os.path.getmtime(fn) < max_age_h * 3600:
            return open(fn, "rb").read() if binary else open(fn, encoding="utf-8", errors="ignore").read()
    if "sec.gov" in url:                       # SEC: max. 10 Anfragen/Sekunde
        wait = 0.15 - (time.time() - _last[0])
        if wait > 0: time.sleep(wait)
        _last[0] = time.time()
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Encoding": "identity"})
            data = urllib.request.urlopen(req, timeout=60).read()
            break
        except Exception as e:
            if attempt == 3: raise
            time.sleep(2 + attempt * 3)
    if fn:
        open(fn, "wb").write(data)
    return data if binary else data.decode("utf-8", errors="ignore")

# ---------------------------------------------------------------- Makro
def risk_free(cache):
    y = dt.date.today().year
    for yr in (y, y - 1):
        url = ("https://home.treasury.gov/resource-center/data-chart-center/interest-rates/daily-treasury-rates.csv/"
               f"{yr}/all?type=daily_treasury_yield_curve&field_tdr_date_value={yr}&page&_format=csv")
        try:
            rows = fetch(url, cache).strip().splitlines()
            head = [h.strip('"') for h in rows[0].split(",")]
            i = head.index("10 Yr")
            best = None
            for r in rows[1:]:
                c = r.split(",")
                if len(c) > i and c[i]:
                    d = dt.datetime.strptime(c[0], "%m/%d/%Y").date()
                    if best is None or d > best[0]: best = (d, float(c[i]) / 100)
            if best: return {"rate": round(best[1], 4), "date": best[0].isoformat()}
        except Exception as e:
            print("Treasury-Fehler", yr, e, file=sys.stderr)
    return {"rate": 0.045, "date": None, "fallback": True}

def erp_damodaran(cache):
    try:
        import openpyxl
        data = fetch("https://pages.stern.nyu.edu/~adamodar/pc/implprem/ERPbymonth.xlsx", cache, binary=True, max_age_h=240)
        ws = openpyxl.load_workbook(io.BytesIO(data), data_only=True, read_only=True).worksheets[0]
        rows = list(ws.iter_rows(values_only=True))
        head = [str(h or "") for h in rows[0]]
        col = next(i for i, h in enumerate(head) if h.startswith("ERP (T12 m with sustainable"))
        last = None
        for r in rows[1:]:
            if isinstance(r[0], dt.datetime) and isinstance(r[col], (int, float)):
                last = (r[0].date(), float(r[col]))
        if last: return {"rate": round(last[1], 4), "month": last[0].strftime("%Y-%m")}
    except Exception as e:
        print("ERP-Fehler", e, file=sys.stderr)
    return {"rate": 0.043, "month": None, "fallback": True}

# ---------------------------------------------------------------- SEC-Fakten
TAGS = {
    "rev":    ["RevenueFromContractWithCustomerExcludingAssessedTax", "Revenues", "SalesRevenueNet",
               "RevenueFromContractWithCustomerIncludingAssessedTax"],
    "ebit":   ["OperatingIncomeLoss"],
    "pretax": ["IncomeLossFromContinuingOperationsBeforeIncomeTaxesExtraordinaryItemsNoncontrollingInterest",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesMinorityInterestAndIncomeLossFromEquityMethodInvestments",
               "IncomeLossFromContinuingOperationsBeforeIncomeTaxesDomestic"],
    "ni":     ["NetIncomeLoss", "ProfitLoss"],
    "tax":    ["IncomeTaxExpenseBenefit"],
    "cfo":    ["NetCashProvidedByUsedInOperatingActivities", "NetCashProvidedByUsedInOperatingActivitiesContinuingOperations"],
    "capex":  ["PaymentsToAcquirePropertyPlantAndEquipment", "PaymentsToAcquireProductiveAssets"],
    "sbc":    ["ShareBasedCompensation", "AllocatedShareBasedCompensationExpense"],
    "int":    ["InterestExpense", "InterestExpenseNonoperating", "InterestExpenseDebt", "InterestPaidNet"],
    "dil":    ["WeightedAverageNumberOfDilutedSharesOutstanding"],
    "cash":   ["CashAndCashEquivalentsAtCarryingValue"],
    "ms_c":   ["MarketableSecuritiesCurrent", "ShortTermInvestments", "AvailableForSaleSecuritiesDebtSecuritiesCurrent"],
    "ms_nc":  ["MarketableSecuritiesNoncurrent", "AvailableForSaleSecuritiesDebtSecuritiesNoncurrent"],
    "debt":   ["LongTermDebt", "LongTermDebtNoncurrent"],
    "debt_c": ["LongTermDebtCurrent", "DebtCurrent"],
    "cp":     ["CommercialPaper", "ShortTermBorrowings"],
}
INSTANT = {"cash", "ms_c", "ms_nc", "debt", "debt_c", "cp"}

def _entries(facts, tag):
    for ns in ("us-gaap", "dei"):
        node = facts.get(ns, {}).get(tag)
        if node:
            unit = "USD" if "USD" in node["units"] else ("shares" if "shares" in node["units"] else next(iter(node["units"])))
            return node["units"][unit]
    return None

def pick_series(facts, key):
    """Wählt das Tag mit den aktuellsten Daten; dedupliziert nach Zeitraum (neueste Einreichung gewinnt)."""
    best, best_end = None, ""
    for tag in TAGS[key]:
        e = _entries(facts, tag)
        if not e: continue
        last = max(x["end"] for x in e)
        if last > best_end: best, best_end = (tag, e), last
    if not best: return None, {}
    tag, e = best
    out = {}
    for x in sorted(e, key=lambda x: x.get("filed", "")):
        k = (x.get("start"), x["end"])
        out[k] = x["val"]
    return tag, out

def discrete_quarters(series):
    """Leitet Einzelquartale ab – auch aus kumulierten Werten (6M/9M/GJ), wie sie Kapitalflussrechnungen melden."""
    q = {}
    groups = {}
    for (s, e), v in series.items():
        if not s: continue
        d = (D(e) - D(s)).days
        if 75 <= d <= 115: q[e] = (s, v)
        groups.setdefault(s, []).append((e, v, d))
    for s, lst in groups.items():
        lst.sort()
        for (e1, v1, d1), (e2, v2, d2) in zip(lst, lst[1:]):
            if e2 not in q and 75 <= (D(e2) - D(e1)).days <= 115:
                q[e2] = ((D(e1) + dt.timedelta(days=1)).isoformat(), v2 - v1)
    return sorted((s, e, v) for e, (s, v) in q.items())

def annual(series):
    out = {}
    for (s, e), v in series.items():
        if s and 350 <= (D(e) - D(s)).days <= 380: out[e] = v
    return sorted(out.items())

def latest_instant(series):
    inst = [(e, v) for (s, e), v in series.items() if not s]
    return max(inst) if inst else (None, None)

def ttm(qs, end=None):
    qs = [x for x in qs if end is None or x[1] <= end]
    if len(qs) < 4: return None
    last4 = qs[-4:]
    if not (330 <= (D(last4[-1][1]) - D(last4[0][0])).days <= 380): return None
    return sum(v for _, _, v in last4)

def qmap(qs):
    return {e: v for _, e, v in qs}

def find_q(qs_map, end, back_days):
    """Wert des Quartals, das ~back_days vor 'end' endet (±20 Tage)."""
    t = D(end) - dt.timedelta(days=back_days)
    best = None
    for e, v in qs_map.items():
        diff = abs((D(e) - t).days)
        if diff <= 20 and (best is None or diff < best[0]): best = (diff, v)
    return best[1] if best else None

def pct(a, b):
    if a is None or b is None or b == 0 or (b < 0 < a) or (a < 0 < b and False): return None
    if b < 0: return None
    return round((a / b - 1) * 100, 1)

def diluted_shares_from_instance(cik, sub, cache):
    """Fallback (z. B. Visa): verwässerte Aktien Klasse A aus der XBRL-Instanz des letzten 10-Q/10-K."""
    r = sub["filings"]["recent"]
    for i, f in enumerate(r["form"]):
        if f not in ("10-Q", "10-K"): continue
        acc = r["accessionNumber"][i].replace("-", "")
        idx = json.loads(fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json", cache))
        xmls = [x["name"] for x in idx["directory"]["item"] if x["name"].endswith("_htm.xml")]
        if not xmls: return None
        x = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{xmls[0]}", cache)
        ctx = {}
        for m in re.finditer(r'<(?:\w+:)?context id="([^"]+)">(.*?)</(?:\w+:)?context>', x, re.S):
            b = m.group(2)
            sd = re.search(r"<(?:\w+:)?startDate>([^<]+)<", b); ed = re.search(r"<(?:\w+:)?endDate>([^<]+)<", b)
            dims = re.findall(r'dimension="([^"]+)">([^<]+)<', b)
            if sd and ed: ctx[m.group(1)] = (sd.group(1), ed.group(1), dims)
        best = None
        for m in re.finditer(r'<us-gaap:WeightedAverageNumberOfDilutedSharesOutstanding[^>]*contextRef="([^"]+)"[^>]*>([^<]+)<', x):
            c = ctx.get(m.group(1))
            if not c: continue
            s, e, dims = c
            if not (75 <= (D(e) - D(s)).days <= 115): continue
            if dims and not any("ClassA" in v.replace("Class A", "ClassA") or v.endswith("CommonClassAMember") for _, v in dims): continue
            v = float(m.group(2))
            if best is None or e > best[0] or (e == best[0] and v > best[1]): best = (e, v)
        return {"value": best[1], "end": best[0], "source": f"10-Q/10-K {acc} (Klasse A, verwässert)"} if best else None
    return None

def supplement_from_instance(facts, cik, sub, cache):
    """companyfacts wird bei manchen Filern verspätet aktualisiert. Ist der letzte 10-Q/10-K neuer,
    werden die nicht-dimensionalen Werte direkt aus dessen XBRL-Instanz ergänzt."""
    r = sub["filings"]["recent"]
    i = next((k for k, f in enumerate(r["form"]) if f in ("10-Q", "10-K")), None)
    if i is None: return None
    rep = r["reportDate"][i]
    e = _entries(facts, "NetCashProvidedByUsedInOperatingActivities") or []
    have = max((x["end"] for x in e), default="")
    if have >= rep: return None
    acc = r["accessionNumber"][i].replace("-", "")
    idx = json.loads(fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/index.json", cache))
    xmls = [x["name"] for x in idx["directory"]["item"] if x["name"].endswith("_htm.xml")]
    if not xmls: return None
    x = fetch(f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/{xmls[0]}", cache)
    ctx = {}
    for m in re.finditer(r'<(?:\w+:)?context id="([^"]+)">(.*?)</(?:\w+:)?context>', x, re.S):
        b = m.group(2)
        if "explicitMember" in b or "typedMember" in b: continue
        sd = re.search(r"<(?:\w+:)?startDate>([^<]+)<", b); ed = re.search(r"<(?:\w+:)?endDate>([^<]+)<", b)
        it = re.search(r"<(?:\w+:)?instant>([^<]+)<", b)
        ctx[m.group(1)] = (sd.group(1), ed.group(1)) if sd and ed else (None, it.group(1)) if it else None
    wanted = {t for lst in TAGS.values() for t in lst}
    n = 0
    for m in re.finditer(r'<us-gaap:(\w+)\b[^>]*contextRef="([^"]+)"[^>]*>([-\d.]+)</us-gaap:\1>', x):
        tag, c, val = m.group(1), m.group(2), m.group(3)
        if tag not in wanted or not ctx.get(c): continue
        st, en = ctx[c]
        node = facts.setdefault("us-gaap", {}).setdefault(tag, {"units": {}})
        unit = "shares" if "Shares" in tag and "Payments" not in tag else "USD"
        lst = node["units"].setdefault(unit, [])
        ent = {"end": en, "val": float(val), "filed": r["filingDate"][i], "form": r["form"][i], "accn": r["accessionNumber"][i]}
        if st: ent["start"] = st
        lst.append(ent); n += 1
    return {"report": rep, "facts": n, "accession": r["accessionNumber"][i]}

# ---------------------------------------------------------------- Guidance
Q_TOKEN = re.compile(r"(?i)\b(first|second|third|fourth|next) quarter(?: (?:of )?(?:fiscal )?(?:year )?(?:FY)?\s?\d{2,4})?|\bQ[1-4](?:\s?(?:FY)?\s?'?\d{2,4})?\b")
FY_TOKEN = re.compile(r"(?i)\bfull[- ]year\b|\bfiscal (?:year )?20\d\d\b|\bFY\s?'?\d{2,4}\b|\b20\d\d (?:guidance|outlook|targets?)\b|\bannual\b")
ANCHOR = re.compile(r"(?i)\b(outlook|guidance|targets?|expects?|expected|forecast)\b")
MULT = {"billion": 1e9, "million": 1e6, "b": 1e9, "m": 1e6}
NUM = r"(\d[\d,]*(?:\.\d+)?)"
REV_RANGE = re.compile(r"(?i)\b(?:total )?(?:revenue|net sales|sales)s?\b[^$]{0,70}?\$\s?" + NUM + r"\s?(billion|million|B|M)?\s?(?:to|-|–|and)\s?\$\s?" + NUM + r"\s?(billion|million|B|M)\b")
REV_POINT = re.compile(r"(?i)\b(?:estimated|expected|projected|guidance of|outlook of)\b[^$.]{0,40}?\b(?:revenue|sales)\b[^$]{0,20}?\bof (?:approximately |about )?\$\s?" + NUM + r"\s?(billion|million)\b")
EPS_RANGE = re.compile(r"(?i)\b(non-gaap |adjusted operational |adjusted |gaap )?(?:diluted )?(?:earnings per share|EPS)\*?\b[^$]{0,90}?(?:\bby \$\s?\d+(?:\.\d+)?\s?to\s?\$\s?(\d+(?:\.\d+)?)|\$\s?(\d+(?:\.\d+)?)\s?(?:to|and|-|–)\s?\$\s?(\d+(?:\.\d+)?))")

def _period(text, pos):
    window = text[max(0, pos - 400):pos]
    qs = [(m.start(), m.end()) for m in Q_TOKEN.finditer(window)]
    fys = [m.start() for m in FY_TOKEN.finditer(window) if not any(a <= m.start() < b for a, b in qs)]
    lq = max((a for a, _ in qs), default=-1); lf = max(fys, default=-1)
    if lq < 0 and lf < 0: return None
    return "Q" if lq > lf else "FY"

def _num(s): return float(s.replace(",", ""))

def parse_guidance(text):
    items = []
    for am in ANCHOR.finditer(text):
        seg_start = am.start()
        seg = text[seg_start:seg_start + 700]
        for rx, kind in ((REV_RANGE, "rev"), (REV_POINT, "rev"), (EPS_RANGE, "eps")):
            for m in rx.finditer(seg):
                pos = seg_start + m.start()
                per = _period(text, pos + 1) or _period(text, seg_start + m.end())
                if kind == "rev":
                    before = text[max(0, pos - 45):pos + 25].lower()
                    if re.search(r"subscription|segment|professionals|consumers|cloud|services revenue|products revenue|arr", before): continue
                    if rx is REV_RANGE:
                        u2 = MULT[m.group(4).lower()]; u1 = MULT[(m.group(2) or m.group(4)).lower()]
                        lo, hi = _num(m.group(1)) * u1, _num(m.group(3)) * u2
                    else:
                        lo = hi = _num(m.group(1)) * MULT[m.group(2).lower()]
                    if lo > hi: continue
                    items.append({"metric": "Umsatz", "period": per, "low": lo, "high": hi, "pos": pos,
                                  "text": text[max(0, pos - 60):pos + len(m.group(0)) + 20].strip()})
                else:
                    basis = (m.group(1) or "").strip().lower()
                    if m.group(2): lo = hi = float(m.group(2))
                    else: lo, hi = float(m.group(3)), float(m.group(4))
                    if lo > hi or hi > 1000: continue
                    label = {"non-gaap": " (bereinigt)", "adjusted": " (bereinigt)", "adjusted operational": " (bereinigt, konstante Währungen)", "gaap": " (GAAP)"}.get(basis, "")
                    items.append({"metric": "EPS" + label,
                                  "period": per, "low": lo, "high": hi, "pos": pos,
                                  "text": text[max(0, pos - 60):pos + len(m.group(0)) + 20].strip()})
    seen, out = set(), []
    for it in sorted(items, key=lambda x: x["pos"]):
        k = (it["metric"], it["period"], round(it["low"], 2), round(it["high"], 2))
        if k in seen: continue
        seen.add(k); it.pop("pos"); out.append(it)
    return out

def latest_guidance(cik, sub, cache):
    r = sub["filings"]["recent"]
    for i, f in enumerate(r["form"]):
        if f != "8-K" or "2.02" not in (r["items"][i] or ""): continue
        acc = r["accessionNumber"][i].replace("-", "")
        base = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc}/"
        idx = json.loads(fetch(base + "index.json", cache))
        names = [x["name"] for x in idx["directory"]["item"]]
        docs = [n for n in names if n.lower().endswith((".htm", ".html")) and n != r["primaryDocument"][i] and not n.startswith("R")]
        docs.sort(key=lambda n: (0 if re.search(r"99[._-]?1|991", n) else 1, n))
        text, used = "", []
        for n in docs[:3]:
            h = fetch(base + n, cache)
            text += " " + html.unescape(re.sub(r"\s+", " ", re.sub(r"<[^>]+>", " ", h)))
            used.append(base + n)
        return {"filed": r["filingDate"][i], "report": r["reportDate"][i], "urls": used, "items": parse_guidance(text)}
    return {"filed": None, "urls": [], "items": []}

# ---------------------------------------------------------------- Beta
def beta_from_prices(prices, ticker, bench="SPY"):
    try:
        a = {m[0]: m[1] for m in prices["tickers"][ticker]["monthly"]}
        b = {m[0]: m[1] for m in prices["tickers"][bench]["monthly"]}
    except KeyError:
        return None
    months = sorted(set(a) & set(b))
    ra, rb = [], []
    for m0, m1 in zip(months, months[1:]):
        ra.append(a[m1] / a[m0] - 1); rb.append(b[m1] / b[m0] - 1)
    if len(ra) < 36: return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    cov = sum((x - ma) * (y - mb) for x, y in zip(ra, rb)) / (len(ra) - 1)
    var = sum((y - mb) ** 2 for y in rb) / (len(rb) - 1)
    raw = cov / var
    return {"raw": round(raw, 2), "adjusted": round(0.67 * raw + 0.33, 2), "months": len(ra)}

SPREADS = [(8.5, 0.0075, "AAA"), (6.5, 0.0100, "AA"), (5.5, 0.0120, "A+"), (4.25, 0.0140, "A"), (3.0, 0.0180, "A-/BBB"),
           (2.5, 0.0220, "BBB"), (2.25, 0.0260, "BB+"), (2.0, 0.0310, "BB"), (1.75, 0.0400, "B+"), (1.5, 0.0500, "B"),
           (1.25, 0.0600, "B-"), (-1e9, 0.0800, "CCC")]

# ---------------------------------------------------------------- DCF-Kern
def project(rev0, g1, g_term, m0, m_norm, wacc, years=10):
    """Gibt (Unternehmenswert, Barwert Endwert, Pfad) zurück. Halbjahreskonvention."""
    rev, pv, path = rev0, 0.0, []
    for t in range(1, years + 1):
        g = g1 + (g_term - g1) * (t - 1) / (years - 1)
        rev *= 1 + g
        m = m0 + (m_norm - m0) * min(t, 5) / 5
        fcf = rev * m
        pv += fcf / (1 + wacc) ** (t - 0.5)
        path.append({"j": t, "g": round(g * 100, 1), "marge": round(m * 100, 1), "umsatz": round(rev / 1e9, 2), "fcff": round(fcf / 1e9, 2)})
    tv = rev * (1 + g_term) * m_norm / (wacc - g_term)
    pv_tv = tv / (1 + wacc) ** (years - 0.5)
    return pv + pv_tv, pv_tv, path

def per_share(ev, cash, debt, shares):
    return (ev + cash - debt) / shares

def value_company(t, facts, sub, cik, prices, rf, erp, cache, sic):
    warn, notes = [], []
    res = {"ticker": t, "name": sub.get("name"), "cik": cik, "sic": sic, "model": MODEL_VERSION}
    # Methodische Eignung
    if sic and (6000 <= sic <= 6799):
        res["status"] = "ungeeignet"
        res["reason"] = ("Finanz-/Versicherungskonzern (SIC %d): Ein Cashflow-DCF ist hier methodisch nicht aussagekräftig, "
                         "weil Versicherungsfloat und Kapitalanlagen den operativen Cashflow dominieren. Bewertung über Buchwert/"
                         "Sum-of-the-Parts – bitte manuell." % sic)
        return res

    sup = supplement_from_instance(facts, cik, sub, cache)
    if sup: notes.append(f"SEC-Datenbank noch ohne Bericht zum {sup['report']} – {sup['facts']} Werte direkt aus der Einreichung {sup['accession']} ergänzt.")
    S, TG = {}, {}
    for k in TAGS:
        TG[k], S[k] = pick_series(facts, k)
    Q = {k: discrete_quarters(S[k]) for k in TAGS if k not in INSTANT and k != "dil"}
    A = {k: annual(S[k]) for k in ("rev", "cfo", "capex", "sbc", "int")}
    if not Q["rev"] or not Q["cfo"]:
        res["status"] = "daten_fehlen"; res["reason"] = "Umsatz oder operativer Cashflow in SEC-Daten nicht auffindbar."
        return res

    last_end = Q["rev"][-1][1]
    stale = (dt.date.today() - D(last_end)).days
    if stale > 150: warn.append(f"Letzte Quartalszahlen vom {last_end} – älter als 150 Tage.")

    # Aktien (verwässert)
    dil = [(e, v) for (s, e), v in S["dil"].items() if s and 75 <= (D(e) - D(s)).days <= 115]
    shares, shares_src = None, None
    if dil:
        e, v = max(dil); shares, shares_src = v, f"SEC {TG['dil']} ({e})"
    if (not dil) or (dt.date.today() - D(max(dil)[0])).days > 200:
        inst = diluted_shares_from_instance(cik, sub, cache)
        if inst: shares, shares_src = inst["value"], "SEC " + inst["source"] + f" ({inst['end']})"
    if not shares:
        res["status"] = "daten_fehlen"; res["reason"] = "Verwässerte Aktienanzahl nicht auffindbar."; return res

    # Kennzahlen-Tabelle (8 Quartale, YoY/QoQ)
    qm = {k: qmap(Q[k]) for k in Q}
    ebit_key = "ebit" if Q["ebit"] and Q["ebit"][-1][1] == last_end else "pretax"
    if ebit_key == "pretax": notes.append("Kein operatives Ergebnis in XBRL – Vorsteuerergebnis als Näherung.")
    dil_q = {e: v for e, v in dil}
    rows = []
    for s, e, rev in Q["rev"][-8:]:
        row = {"ende": e, "umsatz": rev}
        for k in ("ni", "cfo", "capex", "sbc"):
            row[k] = qm[k].get(e)
        row["ebit"] = qm[ebit_key].get(e)
        row["fcf"] = (row["cfo"] - row["capex"]) if row["cfo"] is not None and row["capex"] is not None else None
        sh = dil_q.get(e) or next((v for e2, v in sorted(dil, reverse=True) if 0 <= (D(e) - D(e2)).days <= 200), None)
        row["eps"] = round(row["ni"] / sh, 2) if row["ni"] is not None and sh else None
        for k in ("umsatz", "ebit", "ni", "fcf"):
            cur = row[k]
            src = {"umsatz": qm["rev"], "ebit": qm[ebit_key], "ni": qm["ni"]}.get(k)
            if k == "fcf":
                py = find_q(qm["cfo"], e, 364); pc = find_q(qm["capex"], e, 364)
                prev_y = (py - pc) if py is not None and pc is not None else None
                pq = find_q(qm["cfo"], e, 91); pqc = find_q(qm["capex"], e, 91)
                prev_q = (pq - pqc) if pq is not None and pqc is not None else None
            else:
                prev_y, prev_q = find_q(src, e, 364), find_q(src, e, 91)
            row[k + "_yoy"] = pct(cur, prev_y); row[k + "_qoq"] = pct(cur, prev_q)
        if row["eps"] is not None:
            e_prev = None
            e_py = next((e2 for e2 in qm["ni"] if abs((D(e2) - (D(e) - dt.timedelta(days=364))).days) <= 20), None)
            if e_py:
                sh2 = dil_q.get(e_py) or next((v for e2, v in sorted(dil, reverse=True) if 0 <= (D(e_py) - D(e2)).days <= 200), None)
                if sh2: e_prev = qm["ni"][e_py] / sh2
            row["eps_yoy"] = pct(row["eps"], e_prev)
        rows.append(row)

    margins = sorted(r_["ni"] / r_["umsatz"] for r_ in rows if r_["ni"] is not None and r_["umsatz"])
    med = margins[len(margins) // 2] if margins else None
    for r_ in rows[-4:]:
        if med and r_["ni"] is not None and r_["umsatz"] and r_["ni"] / r_["umsatz"] > max(1.5 * med, med + 0.12):
            warn.append(f"Quartal {r_['ende']}: Nettomarge {r_['ni']/r_['umsatz']*100:.0f} % statt üblich ~{med*100:.0f} % – vermutlich "
                        "Sondereffekte (z. B. Beteiligungsgewinne). KGV/EPS verzerrt; der DCF rechnet mit Cashflows und ist davon nicht betroffen.")

    # TTM-Größen
    T = {k: ttm(Q[k]) for k in ("rev", "cfo", "capex", "sbc", "ni", "tax", "pretax", "int")}
    T["ebit"] = ttm(Q[ebit_key])
    if T["rev"] is None or T["cfo"] is None or T["capex"] is None:
        res["status"] = "daten_fehlen"; res["reason"] = "TTM-Werte unvollständig (Quartalsreihe lückenhaft)."; return res
    prev_ttm_rev = ttm(Q["rev"], (D(last_end) - dt.timedelta(days=360)).isoformat())
    g_ttm = (T["rev"] / prev_ttm_rev - 1) if prev_ttm_rev else None
    g_lastq = (rows[-1]["umsatz_yoy"] or 0) / 100 if rows[-1].get("umsatz_yoy") is not None else None
    fy_rev = A["rev"]
    cagr3 = ((fy_rev[-1][1] / fy_rev[-4][1]) ** (1 / 3) - 1) if len(fy_rev) >= 4 and fy_rev[-4][1] > 0 else None

    tax_rate = (T["tax"] / T["pretax"]) if T.get("tax") and T.get("pretax") and T["pretax"] > 0 else 0.21
    tax_rate = min(max(tax_rate, 0.12), 0.28)

    # Bilanz
    cash = sum((latest_instant(S[k])[1] or 0) for k in ("cash", "ms_c", "ms_nc"))
    debt_lt = latest_instant(S["debt"])[1] or 0
    debt = debt_lt + (latest_instant(S["cp"])[1] or 0)
    if TG["debt"] == "LongTermDebtNoncurrent": debt += latest_instant(S["debt_c"])[1] or 0
    bal_date = latest_instant(S["cash"])[0]

    # Zinsen / Fremdkapitalkosten
    interest = T.get("int")
    if not interest:
        interest = None; notes.append("Zinsaufwand nicht separat in XBRL – über synthetisches Rating geschätzt.")
    coverage = (T["ebit"] / interest) if interest and T.get("ebit") else (20 if debt < 0.5 * (T["ebit"] or 1) else 6)
    spread, rating = next((s, r) for lim, s, r in SPREADS if coverage > lim)
    kd = rf["rate"] + spread
    if interest is None: interest = debt * kd

    # Kurs, Beta, WACC
    pt = t.replace("-", ".")
    try:
        daily = prices["tickers"][pt]["daily"]; price, price_date = daily[-1][1], daily[-1][0]
    except KeyError:
        res["status"] = "daten_fehlen"; res["reason"] = f"Kein Kurs für {pt} in prices.json."; return res
    beta = beta_from_prices(prices, pt)
    b = beta["adjusted"] if beta else 1.0
    b = min(max(b, 0.6), 2.0)
    ke = rf["rate"] + b * erp["rate"]
    E = price * shares; Dv = debt
    wacc = (E * ke + Dv * kd * (1 - tax_rate)) / (E + Dv)

    # FCFF und Margen
    def fcff(cfo, capex, sbc, intr): return cfo - capex - (sbc or 0) + (intr or 0) * (1 - tax_rate)
    f_ttm = fcff(T["cfo"], T["capex"], T["sbc"], interest)
    m_ttm = f_ttm / T["rev"]
    hist = []
    amap = {k: dict(A[k]) for k in A}
    for e, rv in fy_rev[-5:]:
        c, cx = amap["cfo"].get(e), amap["capex"].get(e)
        if c is None or cx is None or rv <= 0: continue
        hist.append(fcff(c, cx, amap["sbc"].get(e), amap["int"].get(e) or interest) / rv)
    m_norm = (sum(hist) / len(hist)) if hist else m_ttm
    m_norm = 0.5 * m_norm + 0.5 * m_ttm if hist and len(hist) < 3 else m_norm
    if m_ttm <= 0 and m_norm <= 0:
        res["status"] = "ungeeignet"; res["reason"] = "Negativer freier Cashflow – DCF nicht belastbar."; return res

    # Guidance
    g = latest_guidance(cik, sub, cache)
    g1, g1_src = None, None
    last_fy_end, last_fy_rev = (fy_rev[-1] if fy_rev else (None, None))
    q_prev_year = find_q(qm["rev"], last_end, 273)            # Vorjahresquartal zum kommenden Quartal
    for it in g["items"]:
        if it["metric"] != "Umsatz": continue
        mid = (it["low"] + it["high"]) / 2
        if it["period"] == "FY" and last_fy_rev and 0.7 < mid / T["rev"] < 1.6:
            fy_g = mid / last_fy_rev - 1
            if D(last_end) > D(last_fy_end) and ((D(last_end) - D(last_fy_end)).days > 300): continue
            g1, g1_src = fy_g, f"Guidance Geschäftsjahr: Umsatz {it['low']/1e9:.2f}–{it['high']/1e9:.2f} Mrd. $ (Mitte {fy_g*100:+.1f} % ggü. Vorjahr)"
            break
        if it["period"] == "Q" and q_prev_year and 0.6 < mid / (T["rev"] / 4) < 1.7 and g1 is None:
            q_g = mid / q_prev_year - 1
            blend = 0.6 * q_g + 0.4 * (g_ttm if g_ttm is not None else q_g)
            g1, g1_src = blend, (f"Guidance nächstes Quartal: Umsatz {it['low']/1e9:.2f}–{it['high']/1e9:.2f} Mrd. $ "
                                 f"({q_g*100:+.1f} % ggü. Vorjahresquartal), 60/40 mit TTM-Wachstum gewichtet")
    if g1 is None:
        parts = [(0.5, g_ttm), (0.3, g_lastq), (0.2, cagr3)]
        parts = [(w, v) for w, v in parts if v is not None]
        g1 = sum(w * v for w, v in parts) / sum(w for w, _ in parts) if parts else 0.04
        g1_src = "Keine Zahlen-Guidance im Quartalsbericht – Trend: 50 % TTM, 30 % letztes Quartal (YoY), 20 % 3-J.-CAGR"
    g1 = min(max(g1, -0.10), 0.35)

    g_term = min(0.025, rf["rate"] - 0.005)
    scen = {
        "bear": dict(g1=g1 * 0.5 if g1 > 0 else g1 - 0.03, gt=g_term - 0.005, m=0.85, w=wacc + 0.0075, weight=0.25),
        "base": dict(g1=g1, gt=g_term, m=1.00, w=wacc, weight=0.50),
        "bull": dict(g1=min(g1 * 1.35, g1 + 0.06) if g1 > 0 else g1 + 0.03, gt=g_term + 0.005, m=1.10, w=wacc - 0.005, weight=0.25),
    }
    out_s = {}
    for name, p in scen.items():
        ev, pv_tv, path = project(T["rev"], p["g1"], p["gt"], m_ttm * p["m"], m_norm * p["m"], p["w"])
        v = per_share(ev, cash, debt, shares)
        out_s[name] = {"wert": round(v, 2), "g1": round(p["g1"] * 100, 1), "g_ewig": round(p["gt"] * 100, 2),
                       "marge_norm": round(m_norm * p["m"] * 100, 1), "wacc": round(p["w"] * 100, 2),
                       "anteil_endwert": round(pv_tv / ev * 100, 1), "pfad": path if name == "base" else None}
    fv = sum(out_s[n]["wert"] * scen[n]["weight"] for n in scen)
    if out_s["base"]["anteil_endwert"] > 85: warn.append("Endwert > 85 % des Unternehmenswerts – Ergebnis sehr sensitiv.")

    # Reverse-DCF
    def val_at(g):
        ev, _, _ = project(T["rev"], g, g_term, m_ttm, m_norm, wacc); return per_share(ev, cash, debt, shares)
    lo, hi = -0.20, 0.60
    implied = None
    if val_at(lo) <= price <= val_at(hi):
        for _ in range(60):
            mid = (lo + hi) / 2
            if val_at(mid) < price: lo = mid
            else: hi = mid
        implied = round((lo + hi) / 2 * 100, 1)

    # Sensitivität (Base): WACC ±1 pp × ewiges Wachstum ±0,5 pp
    sens = []
    for dw in (-0.01, -0.005, 0, 0.005, 0.01):
        row = []
        for dg in (-0.005, 0, 0.005):
            ev, _, _ = project(T["rev"], g1, g_term + dg, m_ttm, m_norm, wacc + dw)
            row.append(round(per_share(ev, cash, debt, shares), 0))
        sens.append({"wacc": round((wacc + dw) * 100, 2), "werte": row})

    res.update({
        "status": "ok",
        "kurs": price, "kurs_datum": price_date,
        "fair_value": round(fv, 2), "bear": out_s["bear"]["wert"], "base": out_s["base"]["wert"], "bull": out_s["bull"]["wert"],
        "abschlag_pct": round((fv / price - 1) * 100, 1),
        "kgv_ttm": round(price * shares / T["ni"], 1) if T.get("ni") and T["ni"] > 0 else None,
        "fcf_rendite_pct": round((T["cfo"] - T["capex"]) / (price * shares) * 100, 2),
        "implizites_wachstum_pct": implied,
        "szenarien": out_s,
        "sensitivitaet": {"g_ewig": [round((g_term + d) * 100, 2) for d in (-0.005, 0, 0.005)], "zeilen": sens},
        "annahmen": {
            "umsatz_ttm": T["rev"], "fcff_ttm": round(f_ttm), "fcff_marge_ttm": round(m_ttm * 100, 1),
            "fcff_marge_norm": round(m_norm * 100, 1), "fcff_margen_hist": [round(x * 100, 1) for x in hist],
            "wachstum_j1": round(g1 * 100, 1), "wachstum_quelle": g1_src,
            "wachstum_ttm": round(g_ttm * 100, 1) if g_ttm is not None else None,
            "cagr_3j": round(cagr3 * 100, 1) if cagr3 is not None else None,
            "steuer": round(tax_rate * 100, 1), "beta": beta, "beta_genutzt": b,
            "rf": rf, "erp": erp, "ke": round(ke * 100, 2), "kd": round(kd * 100, 2), "rating_synth": rating,
            "zinsdeckung": round(coverage, 1), "wacc": round(wacc * 100, 2),
            "cash": cash, "schulden": debt, "bilanz_datum": bal_date, "aktien_verw": shares, "aktien_quelle": shares_src,
            "sbc_abgezogen": True, "tags": {k: v for k, v in TG.items() if v},
        },
        "quartale": rows,
        "guidance": g,
        "stand_zahlen": last_end,
        "warnungen": warn, "hinweise": notes,
    })
    return res

# ---------------------------------------------------------------- Main
def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--cache", default=None)
    ap.add_argument("--only", default="")
    ap.add_argument("--prices", default=os.path.join(HERE, "data", "prices.json"))
    ap.add_argument("--out", default=os.path.join(HERE, "data", "dcf.json"))
    a = ap.parse_args()
    if a.cache: os.makedirs(a.cache, exist_ok=True)
    prices = json.load(open(a.prices))
    tick = [l.split("#")[0].strip() for l in open(os.path.join(HERE, "tickers.txt")) if l.split("#")[0].strip()]
    tick = [t for t in tick if t not in ("SPY", "QQQ")]
    if a.only: tick = [t for t in tick if t in a.only.split(",")]
    cikmap = {v["ticker"].upper(): v["cik_str"] for v in json.loads(fetch("https://www.sec.gov/files/company_tickers.json", a.cache, max_age_h=168)).values()}
    rf, erp = risk_free(a.cache), erp_damodaran(a.cache)
    print(f"rf {rf}  ERP {erp}")
    old = {}
    if os.path.exists(a.out):
        try: old = {c["ticker"]: c for c in json.load(open(a.out)).get("companies", [])}
        except Exception: pass
    out = []
    for t in tick:
        sec_t = t.replace(".", "-")
        cik = cikmap.get(sec_t)
        if not cik: print(t, "kein CIK"); continue
        try:
            facts = json.loads(fetch(f"https://data.sec.gov/api/xbrl/companyfacts/CIK{cik:010d}.json", a.cache))["facts"]
            sub = json.loads(fetch(f"https://data.sec.gov/submissions/CIK{cik:010d}.json", a.cache))
            sic = int(sub.get("sic") or 0) or None
            r = value_company(t, facts, sub, cik, prices, rf, erp, a.cache, sic)
            r["ticker"] = t
        except Exception as e:
            import traceback; traceback.print_exc()
            r = old.get(t) or {"ticker": t, "status": "fehler", "reason": str(e)[:200]}
            r.setdefault("warnungen", []).append(f"Neuberechnung fehlgeschlagen ({str(e)[:80]}) – vorheriger Stand.")
        out.append(r)
        if r.get("status") == "ok":
            print(f"{t:6} Kurs {r['kurs']:>8.2f}  FV {r['fair_value']:>8.2f} (Bear {r['bear']:.0f} / Base {r['base']:.0f} / Bull {r['bull']:.0f})"
                  f"  Abschlag {r['abschlag_pct']:+.1f} %  WACC {r['annahmen']['wacc']} %  g1 {r['annahmen']['wachstum_j1']} %"
                  f"  implizit {r['implizites_wachstum_pct']} %  TV {r['szenarien']['base']['anteil_endwert']} %")
        else:
            print(f"{t:6} {r.get('status')}: {r.get('reason')}")
    doc = {"generated": dt.datetime.now(dt.timezone.utc).replace(microsecond=0).strftime("%Y-%m-%dT%H:%M:%SZ"), "model": MODEL_VERSION,
           "rf": rf, "erp": erp,
           "methodik": ("FCFF-DCF, 10 Jahre, Halbjahreskonvention; FCFF = operativer Cashflow − Investitionen − aktienbasierte "
                        "Vergütung + Zinsen × (1 − Steuer); WACC über CAPM (Beta 5 J. monatlich vs. SPY, Blume-bereinigt), "
                        "Marktrisikoprämie Damodaran, Fremdkapital über synthetisches Rating; Szenarien 25/50/25."),
           "quellen": "SEC EDGAR (XBRL, 8-K), U.S. Treasury, Damodaran/NYU Stern, IEX HIST",
           "companies": out}
    json.dump(doc, open(a.out, "w"), ensure_ascii=False, indent=1)
    print("geschrieben:", a.out)

if __name__ == "__main__":
    main()
