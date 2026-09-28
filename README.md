# IEX-Schlusskurse – kostenlos, automatisch, legal

Holt jeden Morgen die Schlusskurse deiner Watchlist aus den **kostenlosen HIST-Dateien der US-Börse IEX**
und legt sie als kleine Datei `data/prices.json` ab. Das WordPress-Plugin „USAktienHub Research-Daten“ liest diese Datei.

- **Kosten:** 0 € (IEX HIST ist öffentlich und ohne Registrierung; GitHub Actions ist für öffentliche Repositories unbegrenzt kostenlos, für private 2.000 Minuten im Monat).
- **Kurs:** letzter Trade der regulären Sitzung (9:30–16:00 New York) auf IEX. Bei großen US-Werten weicht er in der Regel nur um Cent-Beträge vom offiziellen Schlusskurs ab. Auf der Website steht er als „Kurs (IEX)“.
- **Aktualität:** Folgetag (T+1), ca. 13:30 Uhr Wiener Zeit.

## Einrichtung (ca. 10 Minuten, einmalig)

1. Auf github.com kostenlos registrieren → **New repository** → Name `usah-kurse` → *Public* → Create.
2. **Add file → Upload files**: den Inhalt dieses Ordners hochladen (`iex_close.py`, `update.py`, `tickers.txt`, `.github/workflows/kurse.yml`).
   Hinweis: Der Ordner `.github` ist versteckt. Falls der Upload ihn nicht übernimmt, lege die Datei über *Add file → Create new file* mit dem Namen `.github/workflows/kurse.yml` an und füge den Inhalt ein.
3. **Settings → Actions → General → Workflow permissions → „Read and write permissions“** → Save.
4. **Actions → IEX-Schlusskurse → Run workflow** → im Feld „backfill“ `60` eintragen → Run.
   Das lädt die Monatsschlusskurse der letzten 5 Jahre (einmalig, dauert einige Stunden).
5. Danach nochmal **Run workflow** ohne Eingabe → holt die letzten Handelstage.
6. Die Datei-URL kopieren: `https://raw.githubusercontent.com/DEIN-NAME/usah-kurse/main/data/prices.json`
7. WordPress → Research-Daten → Datenquellen → **Kursdatei** einfügen → Speichern → „Alle Daten jetzt aktualisieren“.

Ab dann läuft alles automatisch (Di–Sa).

## Ticker ändern
`tickers.txt` auf GitHub bearbeiten (ein Ticker pro Zeile). Neue Ticker bekommen ab dem nächsten Lauf Tageskurse;
für ihre 5-Jahres-Historie den Backfill erneut starten.

## Grenzen
- Keine Dividenden (kommen aus den SEC-Daten oder dem Feld „Dividende manuell“).
- Aktiensplits werden nicht automatisch bereinigt: nach einem Split den Backfill neu starten
  bzw. die Monatswerte vor dem Split in `prices.json` teilen.
- IEX stellt die Dateien für die Vergangenheit bereit (aktuell bis Dezember 2016 zurück).
