# Odoo Mail-Importer

Massenimport von E-Mails (IMAP) nach Odoo CRM – **halbautomatisch**: Regeln machen Vorschläge, du prüfst und ordnest
in einer Weboberfläche zu (neuer Lead / an bestehenden Lead anhängen / nur Kontakt / ignorieren), danach werden die
Mails samt Anhängen in Odoo angelegt.

```
IMAP-Postfächer ──① abrufen──▶ Staging-DB (Postgres) ──② Regeln──▶ Vorschläge
                                                                     │  ③ prüfen & zuordnen (Weboberfläche)
                                                                     ▼
                                                      ④ nach Odoo übertragen (Kontakte, Leads, Chatter + Anhänge)
```

## Bestandteile

| Container  | Aufgabe |
|------------|---------|
| `db`       | PostgreSQL (Odoo + Staging-Datenbank des Importers) |
| `odoo`     | Odoo-Testinstanz (18.0, per `ODOO_VERSION=19.0` auch 19) mit CRM; `odoo-init` installiert `crm`+`contacts` |
| `imap`     | Dovecot-IMAP-Server mit Testpostfächern `archiv` und `vertrieb` (zum Üben und als Archiv-Ziel für `imapsync`) |
| `importer` | Weboberfläche + Worker (FastAPI) – Port 8090 |
| `tools/Test-OdooApi.ps1` | PowerShell-Skript: prüft das **Produktivsystem** auf verfügbare APIs (siehe unten) |

## Schnellstart

```bash
cp .env.example .env
docker compose up -d --build
docker compose exec importer python -m importer seed         # Testdaten in Odoo + IMAP
docker compose exec importer python -m importer cycle        # abrufen + Regeln + freigegebene übertragen
```

* Importer: <http://localhost:8090> (Login `admin` / `admin`, über `UI_USER`/`UI_PASSWORD`)
* Odoo: <http://localhost:8069> (Datenbank `odoo`, Login `admin` / `admin`)
* IMAP: `localhost:1143` (`archiv`/`archiv`, `vertrieb`/`vertrieb`, ohne TLS – nur Test!)

Lasttest mit generierten Mails: `python -m importer seed --imap-only --bulk 5000`.

## Arbeiten in der Weboberfläche

1. **Abrufen** holt neue Mails (nur lesend, `BODY.PEEK` – im Postfach wird nichts als gelesen markiert; inkrementell je Ordner über UIDVALIDITY + letzte UID).
2. **Regeln anwenden** erzeugt Vorschläge. Eindeutige Fälle (Newsletter, Auto-Replies, Antworten in bekannten Threads …) werden
   mit `auto: true` direkt freigegeben, alles andere landet unter **Zu prüfen**.
3. In der Mail-Ansicht entscheidest du:
   * **Neuer Lead** – optional eigener Titel, Kontakt/Firma korrigierbar
   * **An Lead anhängen** – per **Lead-Suche in Odoo** (Titel, Kontakt, Firma, E-Mail oder ID) oder Klick auf einen Lead des Kontakts
   * **Nur Kontakt** – Kontakt anlegen und Mail am Kontakt ablegen
   * **Ignorieren** / **Manuell entscheiden**
   * „Für die Zukunft merken“ erzeugt eine Regel für diese Adresse/Domain (optional ohne Rückfrage)
   * Nach jeder Entscheidung springt die Ansicht zur **nächsten offenen Mail**.
4. In der Liste gibt es Sammelaktionen (markierte oder alle gefilterten Mails freigeben/ignorieren/Aktion setzen), Filter nach Status, Aktion, Regel, Konto, Volltextsuche.
5. **Nach Odoo übertragen** schreibt alle freigegebenen Mails. Antworten warten automatisch auf ihre Ursprungs-Mail.

Was in Odoo entsteht: Kontakt (ggf. Firma aus der Domain, außer bei Freemailern), `crm.lead`, und die Original-Mail als
Chatter-Nachricht (Originaldatum, Original-`Message-ID`, Anhänge) – ohne Benachrichtigungen/Follower-Mails. Weil die
`Message-ID` erhalten bleibt, ordnet Odoo spätere Antworten dem Lead richtig zu. Wiederholte Läufe legen nichts doppelt an
(Duplikate werden über `Message-ID` und Hash erkannt, auch gegen bereits in Odoo vorhandene Mails).

## Regeln (`importer/config/rules.yaml`)

Wird bei jedem Lauf neu geladen; die ausführliche Syntax steht als Kommentar im Kopf der Datei. Beispiele sind enthalten für
Auto-Replies, Bounces, Newsletter, Threads, `[Lead#123]` im Betreff, Website-Kontaktformular (Felder aus dem Text extrahieren),
interne Mails und „Kunde hat offenen Lead“. `AUTO_APPROVE=false` schaltet jede automatische Freigabe ab.

## Postfächer (Seite „Postfächer“, `importer/config/importer.yaml`)

* **„Abrufen ab“:** Pro Postfach wird nur ab diesem Datum abgerufen; ohne Datum passiert nichts (`fetch.require_since`). So wird nie
  versehentlich ein ganzes Archiv importiert. **Anzahl prüfen** zeigt vorab, wie viele Mails es wären (liest nichts, speichert nichts).
  Ein früheres Datum später nachtragen holt den älteren Bestand nach, ohne etwas doppelt anzulegen.
* **Externe Postfächer:** drei Plätze `extern1`–`extern3` sind vorbereitet. In `.env` setzen: `MAIL1_ENABLED=true`, `MAIL1_HOST`, `MAIL1_PORT` (993),
  `MAIL1_SSL=true`, `MAIL1_USER`, `MAIL1_PASSWORD` (für Nr. 2/3 `MAIL2_…`, `MAIL3_…`), dann `docker compose up -d importer`.
  Weitere Postfächer: Block in `importer.yaml` kopieren. Microsoft 365/Gmail erlauben IMAP meist nur mit App-Passwort/OAuth.
  Der Importer liest nur (`BODY.PEEK`), im Postfach wird nichts verändert.
* Bestehende Archive (mbox/eml) lädst du in das Dovecot-Archiv:

```bash
docker compose exec importer python -m importer upload /import/export.mbox --account archiv --folder Archiv/2023
```
(Dateien dazu nach `./import-drop/` legen.) Für ganze Postfächer eignet sich `imapsync` gegen `localhost:1143`.

## Das Produktivsystem prüfen (PowerShell)

```powershell
# nur anonym: welche APIs gibt es?
.\tools\Test-OdooApi.ps1 -Url https://erp.firma.de -NoAuth
# mit Zugang (API-Schlüssel unter Mein Profil → Kontosicherheit): Rechte, Module, Mengengerüst, Latenz
.\tools\Test-OdooApi.ps1 -Url https://erp.firma.de -Database prod -User importer@firma.de
```
Rein lesend. Prüft XML-RPC, JSON-RPC, JSON-2 (Odoo 19+), Version, Login, Gruppen, Module (`crm`, `mail`, `contacts`), Rechte auf
`res.partner`, `crm.lead`, `mail.message`, `ir.attachment`, Mengen und Antwortzeiten; schreibt einen JSON-Bericht ohne Geheimnisse.
Der Importer wählt die API selbst (`ODOO_API=auto`: JSON-2, sonst XML-RPC). Für das Produktivsystem `ODOO_URL`, `ODOO_DB`,
`ODOO_USER`, `ODOO_PASSWORD` (= API-Schlüssel) in `.env` setzen. Odoo Online (SaaS) hat die externe API nur im Tarif „Custom“.

## Tests

```bash
cd importer && pip install -r requirements.txt pytest httpx && pytest
```
Die Tests laufen ohne Docker und ohne Odoo (eingebauter Fake-IMAP-Server `tests/fake_imap.py`).

## Hinweise

* Test-Zugangsdaten (`admin/admin`, ohne IMAP-TLS) sind nur für lokale Tests – vor produktivem Einsatz ändern, Odoo-Admin-Passwort setzen und nicht öffentlich exponieren.
* Vor dem ersten Massenimport auf dem Produktivsystem: zuerst Testinstanz/Kopie, zuerst mit `fetch --limit` kleine Mengen prüfen.
* Anhänge >`max_size_mb` (Standard 40 MB) werden nicht abgerufen und als Fehler markiert.

## Browser-Tests (Playwright)

Die Oberfläche hat stabile Test-Hooks (`data-testid`, z. B. `job-fetch`, `mail-row`, `action-new_lead`, `lead-search`,
`submit-approve`) und ARIA-Labels. Während ein Lauf aktiv ist, steht `data-running` am `<body>` (Zustand abwarten:
`expect(page.locator("body")).to_have_attribute("data-running", "")`). Beispiel: `importer/tests/test_e2e.py`.
