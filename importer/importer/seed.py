"""Test data: Odoo (companies, contacts, leads with chatter e-mails) and IMAP mailboxes.

The IMAP mails are built so that every rule in the shipped rules.yaml is exercised:
new inquiries, replies within the import, replies to existing Odoo leads, contact form,
newsletter, auto-reply, bounce, duplicates, lead reference in subject, internal mail ...
"""
from __future__ import annotations

import logging
import mailbox
import random
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from email.utils import format_datetime, make_msgid
from pathlib import Path

from imapclient import IMAPClient

from .fetcher import connect
from .odoo import OdooClient
from .settings import Account, Settings

log = logging.getLogger(__name__)

OWN = "meinefirma.de"
SEED_HEADER = "X-Importer-Seed"
LEAD1_MSGID = "<seed-lead1-angebot@meinefirma.de>"


# ------------------------------------------------------------------------------------------
# Odoo
# ------------------------------------------------------------------------------------------
def seed_odoo(odoo: OdooClient) -> dict:
    """Idempotent: existing records (by e-mail / name) are reused."""
    tag_test = odoo.tag_ids(["Testdaten"])[0]

    def partner(name, email=None, company=None, phone=None, website=None, is_company=False):
        if email:
            pid = odoo.partner_by_email(email)
            if pid:
                return pid
        else:
            ids = odoo.search_ids("res.partner", [("name", "=", name), ("is_company", "=", is_company)], limit=1)
            if ids:
                return ids[0]
        vals = {"name": name, "is_company": is_company}
        for k, v in (("email", email), ("parent_id", company), ("phone", phone), ("website", website)):
            if v:
                vals[k] = v
        return odoo.create("res.partner", vals)

    def lead(name, partner_id, email, **extra):
        ids = odoo.search_ids("crm.lead", [("name", "=", name)], limit=1, context={"active_test": False})
        if ids:
            return ids[0], False
        vals = {"name": name, "type": "opportunity", "partner_id": partner_id, "email_from": email,
                "tag_ids": [(6, 0, [tag_test])]}
        vals.update(extra)
        return odoo.create("crm.lead", vals), True

    mueller_co = partner("Müller Maschinenbau GmbH", is_company=True, website="mueller-maschinenbau.de",
                         phone="+49 711 123450")
    hans = partner("Hans Müller", "hans.mueller@mueller-maschinenbau.de", company=mueller_co, phone="+49 711 123451")
    schmidt_co = partner("Schmidt & Partner Architekten", is_company=True, website="schmidt-architekten.de")
    anna = partner("Anna Schmidt", "a.schmidt@schmidt-architekten.de", company=schmidt_co)
    weber_co = partner("Bäckerei Weber", is_company=True, website="baeckerei-weber.de")
    peter = partner("Peter Weber", "info@baeckerei-weber.de", company=weber_co)
    becker_co = partner("Becker Logistik KG", is_company=True, website="becker-logistik.de")
    thomas = partner("Thomas Becker", "t.becker@becker-logistik.de", company=becker_co)
    lisa = partner("Lisa Klein", "lisa.klein@gmail.com")

    lead1, new1 = lead("Neue CNC-Fräse – Angebot", hans, "hans.mueller@mueller-maschinenbau.de",
                       expected_revenue=85000, probability=40)
    if new1:
        # E-Mail im Chatter mit fester Message-ID: die IMAP-Testmail antwortet darauf
        odoo.create("mail.message", {
            "model": "crm.lead", "res_id": lead1, "message_type": "email",
            "subtype_id": odoo.xmlid("mail", "mt_comment"), "message_id": LEAD1_MSGID,
            "subject": "Ihr Angebot CNC-Fräse FX-500",
            "body": "<p>Sehr geehrter Herr Müller,</p><p>anbei unser Angebot für die FX-500.</p>",
            "email_from": f'"Vertrieb" <vertrieb@{OWN}>', "author_id": False,
            "date": (datetime.utcnow() - timedelta(days=20)).strftime("%Y-%m-%d %H:%M:%S"),
        })
    lead2, _ = lead("Bürogebäude Umbau – Beleuchtung", anna, "a.schmidt@schmidt-architekten.de",
                    expected_revenue=42000, probability=20)
    lead3, new3 = lead("Ladeneinrichtung Filiale 2", peter, "info@baeckerei-weber.de", expected_revenue=18000)
    if new3:
        odoo.call("crm.lead", "action_set_won", [lead3])
    lead4, new4 = lead("Anfrage Gartenhaus (privat)", lisa, "lisa.klein@gmail.com")
    if new4:
        odoo.call("crm.lead", "write", [lead4], vals={"active": False})

    result = {"lead_cnc": lead1, "lead_buero": lead2, "lead_won": lead3, "lead_archived": lead4,
              "partners": {"hans": hans, "anna": anna, "peter": peter, "thomas": thomas, "lisa": lisa}}
    log.info("Odoo-Testdaten: %s", result)
    return result


# ------------------------------------------------------------------------------------------
# IMAP
# ------------------------------------------------------------------------------------------
@dataclass
class SeedMail:
    account: str
    folder: str
    msg: EmailMessage
    flags: tuple = (b"\\Seen",)


@dataclass
class Builder:
    base: datetime = field(default_factory=lambda: datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=14))
    mails: list[SeedMail] = field(default_factory=list)

    def make(self, account, folder, frm, to, subject, body, minutes=0, reply_to: EmailMessage | None = None,
             msgid_domain=None, headers=None, html=None, attachments=(), cc=None) -> EmailMessage:
        m = EmailMessage()
        m["From"] = frm
        m["To"] = to
        if cc:
            m["Cc"] = cc
        m["Subject"] = subject
        m["Date"] = format_datetime(self.base + timedelta(minutes=minutes))
        domain = msgid_domain or frm.rpartition("@")[2].strip(">")
        m["Message-ID"] = make_msgid(domain=domain)
        if reply_to is not None:
            m["In-Reply-To"] = reply_to["Message-ID"]
            refs = (reply_to.get("References", "") + " " + reply_to["Message-ID"]).strip()
            m["References"] = refs
        m[SEED_HEADER] = "1"
        for k, v in (headers or {}).items():
            m[k] = v
        m.set_content(body)
        if html:
            m.add_alternative(html, subtype="html")
        for name, ctype, payload in attachments:
            maintype, subtype = ctype.split("/")
            m.add_attachment(payload, maintype=maintype, subtype=subtype, filename=name)
        self.mails.append(SeedMail(account, folder, m))
        return m


FAKE_PDF = b"%PDF-1.4\n1 0 obj<<>>endobj\ntrailer<<>>\n%%EOF\n"


def build_scenarios(odoo_ids: dict | None) -> list[SeedMail]:
    b = Builder()
    v = f"Vertrieb Meine Firma <vertrieb@{OWN}>"
    julia = "Julia Hoffmann <julia.hoffmann@hoffmann-solar.de>"

    # 1-3: neue Anfrage + Antwort von uns (Gesendet) + Rückantwort mit Anhang -> 1 Lead, 2 angehängt
    m1 = b.make("archiv", "INBOX", julia, v, "Anfrage: Photovoltaik für unsere Lagerhalle",
                "Guten Tag,\n\nwir planen eine PV-Anlage (ca. 120 kWp) auf unserer Lagerhalle in Ulm.\n"
                "Können Sie uns ein Angebot machen?\n\nViele Grüße\nJulia Hoffmann\nHoffmann Solar GmbH\nTel. 0731 998877",
                minutes=0)
    m2 = b.make("archiv", "Sent", v, julia, "AW: Anfrage: Photovoltaik für unsere Lagerhalle",
                "Hallo Frau Hoffmann,\n\ngerne! Können Sie uns die Dachpläne schicken?\n\nGruß\nIhr Vertriebsteam",
                minutes=180, reply_to=m1)
    b.make("archiv", "INBOX", julia, v, "AW: AW: Anfrage: Photovoltaik für unsere Lagerhalle",
           "Anbei die Dachpläne.\n\nJulia Hoffmann", minutes=600, reply_to=m2,
           attachments=[("Dachplan_Halle3.pdf", "application/pdf", FAKE_PDF)])

    # 4: Antwort auf eine E-Mail, die in Odoo am Lead "CNC-Fräse" hängt
    seed_offer = EmailMessage()
    seed_offer["Message-ID"] = LEAD1_MSGID
    hans_reply = b.make("archiv", "INBOX", "Hans Müller <hans.mueller@mueller-maschinenbau.de>", v,
           "AW: Ihr Angebot CNC-Fräse FX-500",
           "Hallo,\n\ndanke für das Angebot. Ist bei 2 Stück ein Rabatt möglich?\n\nHans Müller", minutes=60,
           reply_to=seed_offer)

    # 5: Kunde mit offenem Lead schreibt neue Mail (ohne Thread) -> Vorschlag anhängen, prüfen
    b.make("vertrieb", "INBOX", "Anna Schmidt <a.schmidt@schmidt-architekten.de>", v,
           "Rückfrage Lichtplanung 2. OG",
           "Hallo zusammen,\n\nkönnen wir die Lichtplanung fürs 2. OG nochmal besprechen?\n\nAnna Schmidt", minutes=120)

    # 6: Kontaktformular der Website (Absender noreply, Daten im Text)
    b.make("archiv", "INBOX", f"Website <noreply@{OWN}>", v, "Kontaktanfrage über Website",
           "Neue Kontaktanfrage:\n\nName: Markus Wagner\nFirma: Wagner Elektrotechnik\n"
           "E-Mail: m.wagner@wagner-elektro.de\nTelefon: 0711 445566\n\nNachricht:\n"
           "Wir suchen einen Partner für die Wartung unserer Schaltschränke.", minutes=240)

    # 7: Newsletter
    b.make("archiv", "INBOX", "Fachverband Elektro <newsletter@fachverband-elektro.de>", v,
           "Newsletter Oktober: Neue Normen", "Die wichtigsten Neuerungen im Überblick ...", minutes=300,
           headers={"List-Unsubscribe": "<mailto:unsubscribe@fachverband-elektro.de>", "Precedence": "bulk"})

    # 8: Abwesenheitsnotiz
    b.make("archiv", "INBOX", "Klaus Meier <k.meier@meier-bau.de>", v, "Abwesenheitsnotiz: Ihre Nachricht",
           "Ich bin bis 20.10. nicht im Büro.", minutes=310, headers={"Auto-Submitted": "auto-replied"})

    # 9: Unzustellbar
    b.make("archiv", "INBOX", "Mail Delivery System <MAILER-DAEMON@mail.meinefirma.de>", v,
           "Undelivered Mail Returned to Sender", "This is the mail system ... user unknown", minutes=320)

    # 10: bekannter Kontakt ohne offenen Lead (Buchhaltungsfrage) -> nur Kontakt, prüfen
    b.make("vertrieb", "INBOX", "Thomas Becker <t.becker@becker-logistik.de>", v, "Frage zu Rechnung Nr. 4711",
           "Hallo,\n\nauf der Rechnung fehlt die Bestellnummer. Bitte korrigieren.\n\nThomas Becker", minutes=360)

    # 11: Lead-Referenz im Betreff (Kollegin der Architektin, andere Adresse)
    if odoo_ids:
        b.make("archiv", "INBOX", "Jonas Schmidt <j.schmidt@schmidt-architekten.de>", v,
               f"[Lead#{odoo_ids['lead_buero']}] Pläne Beleuchtung als PDF",
               "Hallo,\n\nanbei wie besprochen die Pläne.\n\nJonas Schmidt", minutes=400,
               attachments=[("Beleuchtungsplan_OG2.pdf", "application/pdf", FAKE_PDF)])

    # 12: Duplikat (gleiche Mail zusätzlich im Archiv-Ordner)
    dup = SeedMail("archiv", "Archiv/2024", m1)
    b.mails.append(dup)

    # 13: Kunde mit gewonnenem (geschlossenem) Lead hat neues Interesse
    b.make("archiv", "INBOX", "Peter Weber <info@baeckerei-weber.de>", v, "Neue Filiale – wieder Interesse",
           "Hallo,\n\nwir eröffnen 2027 eine dritte Filiale und brauchen wieder eine Ladeneinrichtung.\n\nPeter Weber",
           minutes=480)

    # 14: HTML-only Mail mit Umlauten
    b.make("archiv", "INBOX", "Dr. Jürgen Groß <juergen.gross@gross-consulting.at>", v,
           "Größere Ausschreibung – Energieberatung für 12 Standorte",
           "Sehr geehrte Damen und Herren,\n\nwir bereiten eine Ausschreibung vor ...",
           html="<html><body><p>Sehr geehrte Damen und Herren,</p><p>wir bereiten eine <b>Ausschreibung</b> "
                "für 12 Standorte vor. Hätten Sie Interesse?</p><p>Mit freundlichen Grüßen<br>Dr. Jürgen Groß</p></body></html>",
           minutes=540)

    # 15: interne Mail zwischen Kollegen
    b.make("archiv", "Sent", v, f"Chefin <chefin@{OWN}>", "Wochenbericht Vertrieb",
           "Hallo Chefin,\n\nanbei der Wochenbericht.\n", minutes=700)

    # 16: Antwort von uns an Hans Müller (Gesendet), Thread mit Odoo-Lead
    b.make("archiv", "Sent", v, "Hans Müller <hans.mueller@mueller-maschinenbau.de>",
           "AW: AW: Ihr Angebot CNC-Fräse FX-500",
           "Hallo Herr Müller,\n\nbei 2 Stück gewähren wir 5 % Rabatt.\n\nGruß", minutes=720,
           reply_to=hans_reply)
    return b.mails


FIRST = ["Max", "Sophie", "Lukas", "Marie", "Felix", "Emma", "Paul", "Mia", "Jonas", "Lena", "Tim", "Laura",
         "Jan", "Sarah", "David", "Hannah", "Niklas", "Julia", "Erik", "Nina"]
LAST = ["Fischer", "Weber", "Wagner", "Becker", "Schulz", "Hoffmann", "Koch", "Richter", "Klein", "Wolf",
        "Neumann", "Schwarz", "Zimmermann", "Braun", "Krüger", "Hartmann", "Lange", "Werner", "Krause", "Lehmann"]
BRANCHE = ["Metallbau", "Logistik", "Elektro", "Haustechnik", "Immobilien", "Druckerei", "Kanzlei", "Autohaus",
           "Hotel", "Software", "Möbel", "Landtechnik"]
TOPICS = ["Angebot Wartungsvertrag", "Anfrage Ersatzteile", "Termin vor Ort", "Preisliste 2027", "Rückruf erbeten",
          "Projekt Neubau", "Kostenvoranschlag", "Rahmenvertrag", "Lieferzeit", "Reklamation"]


def build_bulk(count: int, seed: int = 42) -> list[SeedMail]:
    """Many plausible mails for load tests: ~30 % are replies inside a thread."""
    rnd = random.Random(seed)
    b = Builder(base=datetime.now(timezone.utc).replace(microsecond=0) - timedelta(days=3 * 365))
    v = f"Vertrieb Meine Firma <vertrieb@{OWN}>"
    threads: list[tuple[EmailMessage, str, str]] = []
    step = max(1, (3 * 365 * 24 * 60) // max(1, count))
    for i in range(count):
        minutes = i * step + rnd.randint(0, step // 2)
        if threads and rnd.random() < 0.3:
            parent, contact, parent_body = rnd.choice(threads)
            outgoing = rnd.random() < 0.5
            frm, to = (v, contact) if outgoing else (contact, v)
            subj = "AW: " + str(parent["Subject"]).removeprefix("AW: ")
            body = f"Antwort {i}\n\n> " + parent_body[:200].replace("\n", "\n> ")
            m = b.make("archiv", "Sent" if outgoing else "INBOX", frm, to, subj, body,
                       minutes=minutes, reply_to=parent)
            threads.append((m, contact, body))
            continue
        first, last, br = rnd.choice(FIRST), rnd.choice(LAST), rnd.choice(BRANCHE)
        dom = f"{last.lower().replace('ü', 'ue').replace('ö', 'oe').replace('ä', 'ae')}-{br.lower().replace('ö', 'oe')}.de"
        contact = f"{first} {last} <{first.lower()}.{dom.split('-')[0]}@{dom}>"
        topic = rnd.choice(TOPICS)
        attachments = [("Unterlagen.pdf", "application/pdf", FAKE_PDF)] if rnd.random() < 0.15 else ()
        body = f"Guten Tag,\n\n{topic.lower()} für unseren Betrieb ({br}).\n\nGruß\n{first} {last}"
        m = b.make("archiv", "INBOX", contact, v, f"{topic} – {last} {br}", body,
                   minutes=minutes, attachments=attachments)
        threads.append((m, contact, body))
    return b.mails


def _ensure_folder(client: IMAPClient, folder: str) -> None:
    if not client.folder_exists(folder):
        client.create_folder(folder)
        client.subscribe_folder(folder)


def append_mails(settings: Settings, mails: list[SeedMail], force: bool = False) -> dict[str, int]:
    by_account: dict[str, list[SeedMail]] = {}
    for m in mails:
        by_account.setdefault(m.account, []).append(m)
    result = {}
    for acc_name, items in by_account.items():
        acc = settings.account(acc_name)
        client = connect(acc)
        try:
            if not force:
                client.select_folder("INBOX", readonly=True)
                if client.search(["HEADER", SEED_HEADER, "1"]):
                    log.info("%s: Testdaten bereits vorhanden - übersprungen (--force zum erneuten Einspielen)", acc_name)
                    result[acc_name] = 0
                    continue
            for m in items:
                _ensure_folder(client, m.folder)
                when = datetime.strptime(str(m.msg["Date"]), "%a, %d %b %Y %H:%M:%S %z")
                client.append(m.folder, m.msg.as_bytes(), flags=m.flags, msg_time=when)
            result[acc_name] = len(items)
        finally:
            client.logout()
    return result


def upload_files(acc: Account, folder: str, paths: list[Path]) -> int:
    """Upload .eml files, directories of .eml files or .mbox files into an IMAP folder."""
    client = connect(acc)
    count = 0
    try:
        _ensure_folder(client, folder)
        for path in paths:
            files = sorted(p for p in path.rglob("*") if p.is_file()) if path.is_dir() else [path]
            for f in files:
                if f.suffix.lower() in (".mbox", ".mbx") or f.name == "mbox":
                    for msg in mailbox.mbox(str(f)):
                        client.append(folder, msg.as_bytes(), flags=(b"\\Seen",))
                        count += 1
                elif f.suffix.lower() in (".eml", ".msg_rfc822", ".txt"):
                    client.append(folder, f.read_bytes(), flags=(b"\\Seen",))
                    count += 1
    finally:
        client.logout()
    return count
