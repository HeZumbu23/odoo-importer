"""Applies approved decisions in Odoo (partners, leads, chatter messages with attachments)."""
from __future__ import annotations

import logging
import xmlrpc.client
from dataclasses import dataclass

from sqlalchemy import select

from .mailparse import RawStore, parse, text_to_html
from .models import Action, Mail, Status
from .odoo import OdooClient
from .settings import Settings

log = logging.getLogger(__name__)

FREEMAIL = {
    "gmail.com", "googlemail.com", "gmx.de", "gmx.net", "gmx.at", "gmx.ch", "web.de", "t-online.de",
    "yahoo.com", "yahoo.de", "outlook.com", "outlook.de", "hotmail.com", "hotmail.de", "live.com", "live.de",
    "icloud.com", "me.com", "aol.com", "aol.de", "freenet.de", "posteo.de", "mailbox.org", "proton.me",
    "protonmail.com", "online.de", "arcor.de", "bluewin.ch", "gmx.com",
}


class Deferred(Exception):
    """Mail depends on another mail that is not imported yet."""


class NeedsReview(Exception):
    """Mail cannot be applied automatically any more and goes back to review."""


class _Fmt(dict):
    def __missing__(self, key):
        return ""


@dataclass
class Contact:
    email: str
    name: str
    phone: str = ""
    partner_name: str = ""


def contact_for(mail: Mail) -> Contact:
    ex = (mail.options or {}).get("extract") or {}
    email = (ex.get("email") or mail.counterpart_email or "").strip().lower()
    name = ex.get("contact_name") or (mail.counterpart_name if email == mail.counterpart_email else "") or ""
    if not name and email:
        name = email.split("@")[0].replace(".", " ").title()
    return Contact(email=email, name=name.strip(), phone=ex.get("phone", ""), partner_name=ex.get("partner_name", ""))


class Executor:
    def __init__(self, odoo: OdooClient, store: RawStore, settings: Settings):
        self.odoo, self.store, self.settings = odoo, store, settings

    # ------------------------------------------------------------------------------
    def ensure_partner(self, c: Contact, opts: dict) -> int | None:
        if not c.email:
            return None
        pid = self.odoo.partner_by_email(c.email)
        if pid:
            return pid
        vals = {"name": c.name or c.email, "email": c.email}
        if c.phone:
            vals["phone"] = c.phone
        domain = c.email.rpartition("@")[2]
        if c.partner_name:
            vals["parent_id"] = self.odoo.company_partner(c.partner_name)
        elif opts.get("company_from_domain") and domain not in FREEMAIL and domain not in self.settings.own_domains:
            vals["parent_id"] = self.odoo.company_partner(domain, website=domain)
        return self.odoo.create("res.partner", vals)

    def lead_vals(self, mail: Mail, c: Contact, partner_id: int | None) -> dict:
        opts = mail.options or {}
        lo = opts.get("lead") or {}
        fields = _Fmt({"subject": mail.subject or "", "from_name": mail.from_name, "from_email": mail.from_email,
                       "email": c.email, "contact_name": c.name, "partner_name": c.partner_name,
                       "folder": mail.folder, "account": mail.account, **(opts.get("extract") or {})})
        name = opts.get("lead_name") or (lo.get("name") or "{subject}").format_map(fields).strip()
        vals = {
            "name": (name or f"Anfrage von {c.name or c.email or 'unbekannt'}")[:250],
            "type": lo.get("type", "opportunity"),
            "email_from": c.email or False,
            "contact_name": c.name or False,
            "partner_name": c.partner_name or False,
            "phone": c.phone or False,
            "description": text_to_html((mail.body_text or "")[:10000]),
        }
        if partner_id:
            vals["partner_id"] = partner_id
        if lo.get("tags"):
            vals["tag_ids"] = [(6, 0, self.odoo.tag_ids(list(lo["tags"])))]
        if lo.get("team"):
            team = self.odoo.team_id(lo["team"])
            if team:
                vals["team_id"] = team
        if "user" in lo:
            vals["user_id"] = (self.odoo.user_id(lo["user"]) if lo["user"] else False) or False
        if lo.get("priority") is not None:
            vals["priority"] = str(lo["priority"])
        vals.update(lo.get("vals") or {})
        return vals

    # ------------------------------------------------------------------------------
    def apply(self, session, mail: Mail) -> None:
        if mail.action == Action.IGNORE:
            mail.status = Status.IGNORED
            return
        if mail.action not in (Action.NEW_LEAD, Action.ATTACH, Action.CONTACT):
            raise NeedsReview("keine Aktion gewählt")

        raw_path = mail.raw_path
        if not raw_path and mail.duplicate_of:
            raw_path = session.get(Mail, mail.duplicate_of).raw_path
        parsed = parse(self.store.load(raw_path))

        # Idempotenz: bei Abbruch nach dem Anlegen wird die Nachricht in Odoo wiedergefunden
        if mail.message_id:
            loc = self.odoo.message_location(mail.message_id)
            if loc:
                if loc[0] == "crm.lead":
                    mail.odoo_lead_id = loc[1]
                mail.status, mail.reason = Status.DONE, f"war bereits in Odoo ({loc[0]} #{loc[1]})"
                return

        opts = mail.options or {}
        c = contact_for(mail)

        if mail.action == Action.NEW_LEAD:
            partner_id = self.ensure_partner(c, opts) if opts.get("create_contact", True) else None
            lead_id = self.odoo.create("crm.lead", self.lead_vals(mail, c, partner_id))
            author = partner_id if mail.direction == "in" else self.odoo.partner_by_email(mail.from_email)
            mail.odoo_message_id = self.odoo.post_mail("crm.lead", lead_id, parsed, author)
            mail.odoo_partner_id, mail.odoo_lead_id = partner_id, lead_id

        elif mail.action == Action.ATTACH:
            lead_id = mail.target_lead_id
            if not lead_id and mail.thread_parent_id:
                parent = session.get(Mail, mail.thread_parent_id)
                if parent.status == Status.DONE and parent.odoo_lead_id:
                    lead_id = parent.odoo_lead_id
                elif parent.status in (Status.NEW, Status.PENDING, Status.APPROVED):
                    raise Deferred(f"wartet auf Bezugs-Mail #{parent.id} ({Status.LABELS[parent.status]})")
                else:
                    raise NeedsReview(f"Bezugs-Mail #{parent.id} wurde nicht als Lead importiert "
                                      f"({Status.LABELS.get(parent.status, parent.status)})")
            if not lead_id:
                raise NeedsReview("kein Ziel-Lead angegeben")
            if not self.odoo.lead_info(lead_id):
                raise NeedsReview(f"Lead #{lead_id} existiert in Odoo nicht")
            partner_id = (self.ensure_partner(c, opts) if opts.get("create_contact", True)
                          else self.odoo.partner_by_email(c.email) if c.email else None)
            author = partner_id if mail.direction == "in" else self.odoo.partner_by_email(mail.from_email)
            mail.odoo_message_id = self.odoo.post_mail("crm.lead", lead_id, parsed, author)
            mail.odoo_partner_id, mail.odoo_lead_id = partner_id, lead_id

        elif mail.action == Action.CONTACT:
            partner_id = self.ensure_partner(c, opts)
            if not partner_id:
                raise NeedsReview("keine E-Mail-Adresse für den Kontakt")
            mail.odoo_partner_id = partner_id
            if opts.get("post_on_partner", True):
                author = partner_id if mail.direction == "in" else self.odoo.partner_by_email(mail.from_email)
                mail.odoo_message_id = self.odoo.post_mail("res.partner", partner_id, parsed, author)

        mail.status = Status.DONE


@dataclass
class ApplyStats:
    done: int = 0
    deferred: int = 0
    review: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return f"{self.done} übertragen, {self.deferred} wartend, {self.review} zurück zur Prüfung, {self.errors} Fehler"


NETWORK_ERRORS = (OSError, xmlrpc.client.ProtocolError)


def apply_approved(session_factory, settings: Settings, store: RawStore, odoo: OdooClient | None = None,
                   limit: int | None = None, ids: list[int] | None = None) -> ApplyStats:
    odoo = odoo or OdooClient(settings)
    ex = Executor(odoo, store, settings)
    stats = ApplyStats()
    with session_factory() as s:
        q = select(Mail.id).where(Mail.status == Status.APPROVED)
        if ids:
            q = q.where(Mail.id.in_(ids))
        todo = list(s.scalars(q.order_by(Mail.date.asc().nulls_first(), Mail.id)).all())
    if limit:
        todo = todo[:limit]
    # mehrere Durchgänge, damit Antworten nach ihrem (später sortierten) Ursprung landen
    for _pass in range(3):
        waiting = []
        progressed = False
        for mail_id in todo:
            with session_factory() as s:
                mail = s.get(Mail, mail_id)
                if mail.status != Status.APPROVED:
                    continue
                try:
                    ex.apply(s, mail)
                    mail.error = None
                    stats.done += mail.status == Status.DONE
                    progressed = True
                except Deferred as exc:
                    mail.error = str(exc)
                    waiting.append(mail_id)
                except NeedsReview as exc:
                    mail.status, mail.error = Status.PENDING, str(exc)
                    stats.review += 1
                except NETWORK_ERRORS:
                    s.rollback()
                    raise  # Odoo nicht erreichbar -> Lauf abbrechen, nichts als Fehler markieren
                except Exception as exc:  # noqa: BLE001
                    log.exception("Mail #%s konnte nicht übertragen werden", mail_id)
                    mail.status, mail.error = Status.ERROR, str(exc)[:2000]
                    stats.errors += 1
                s.commit()
        todo = waiting
        if not waiting or not progressed:
            break
    stats.deferred = len(todo)
    return stats
