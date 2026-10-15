"""Thin Odoo client with two transports:

* XML-RPC  (/xmlrpc/2)    - Odoo 8 .. 19 (ab 19 als veraltet markiert)
* JSON-2   (/json/2/...)  - ab Odoo 19, benötigt einen API-Schlüssel

`ODOO_API=auto` (Standard) nimmt JSON-2, wenn der Server es anbietet, sonst XML-RPC.
"""
from __future__ import annotations

import base64
import json
import logging
import urllib.error
import urllib.request
import xmlrpc.client
from datetime import datetime

from .mailparse import ParsedMail
from .settings import Settings

log = logging.getLogger(__name__)

# Beim Import keine Follower-Mails, kein Tracking-Rauschen, keine Auto-Abos
IMPORT_CONTEXT = {
    "tracking_disable": True,
    "mail_create_nolog": True,
    "mail_create_nosubscribe": True,
    "mail_notrack": True,
    "mail_auto_subscribe_no_notify": True,
}


class OdooError(Exception):
    pass


def _esc_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


class _XmlRpc:
    name = "xmlrpc"

    def __init__(self, url: str, db: str, user: str, password: str, timeout: int = 120):
        self.db, self.password = db, password
        base = xmlrpc.client.SafeTransport if url.startswith("https") else xmlrpc.client.Transport

        class _Transport(base):  # xmlrpc.client kennt von sich aus kein Timeout
            def make_connection(self, host):
                conn = super().make_connection(host)
                conn.timeout = timeout
                return conn

        common = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/common", allow_none=True, transport=_Transport())
        self.uid = common.authenticate(db, user, password, {})
        if not self.uid:
            raise OdooError("Odoo-Anmeldung fehlgeschlagen (Benutzer/API-Schlüssel/Datenbank prüfen)")
        self.models = xmlrpc.client.ServerProxy(f"{url}/xmlrpc/2/object", allow_none=True, transport=_Transport())

    def call(self, model: str, method: str, ids=None, **kwargs):
        if method == "create":
            args, kwargs = [kwargs.pop("vals_list")], kwargs
        else:
            args = [ids] if ids is not None else []
        try:
            return self.models.execute_kw(self.db, self.uid, self.password, model, method, args, kwargs)
        except xmlrpc.client.Fault as exc:
            raise OdooError(_short(exc.faultString)) from None


class _Json2:
    name = "json2"

    def __init__(self, url: str, db: str, user: str, api_key: str, timeout: int = 120):
        self.url, self.db, self.key, self.timeout = url, db, api_key, timeout
        info = self.call("res.users", "context_get")
        self.uid = info.get("uid") if isinstance(info, dict) else None

    def call(self, model: str, method: str, ids=None, **kwargs):
        body = dict(kwargs)
        if ids is not None:
            body["ids"] = ids
        req = urllib.request.Request(
            f"{self.url}/json/2/{model}/{method}",
            data=json.dumps(body, default=str).encode(),
            headers={
                "Content-Type": "application/json",
                "Authorization": f"bearer {self.key}",
                "X-Odoo-Database": self.db,
            },
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                return json.loads(resp.read() or b"null")
        except urllib.error.HTTPError as exc:
            try:
                err = json.loads(exc.read())
                msg = err.get("message") or err.get("name") or str(err)
            except Exception:
                msg = f"HTTP {exc.code}"
            raise OdooError(_short(msg)) from None


def _short(text: str) -> str:
    lines = [ln.strip() for ln in str(text).strip().splitlines() if ln.strip()]
    return (lines[-1] if lines else str(text))[:500]


def json2_available(url: str, db: str, api_key: str) -> bool:
    try:
        _Json2(url, db, "", api_key, timeout=15)
        return True
    except Exception:
        return False


class OdooClient:
    def __init__(self, settings: Settings):
        mode = settings.odoo_api
        url, db = settings.odoo_url, settings.odoo_db
        if mode == "json2" or (mode == "auto" and json2_available(url, db, settings.odoo_password)):
            self.rpc = _Json2(url, db, settings.odoo_user, settings.odoo_password)
        else:
            self.rpc = _XmlRpc(url, db, settings.odoo_user, settings.odoo_password)
        self.url = url
        self._cache: dict = {}

    # ---- generic -------------------------------------------------------------------
    def call(self, model, method, ids=None, **kw):
        return self.rpc.call(model, method, ids, **kw)

    def search_read(self, model, domain, fields, limit=None, order=None, context=None):
        kw = {"domain": domain, "fields": fields}
        if limit:
            kw["limit"] = limit
        if order:
            kw["order"] = order
        if context:
            kw["context"] = context
        return self.call(model, "search_read", **kw)

    def search_ids(self, model, domain, limit=None, order=None, context=None) -> list[int]:
        return [r["id"] for r in self.search_read(model, domain, ["id"], limit=limit, order=order, context=context)]

    def create(self, model, vals: dict) -> int:
        res = self.call(model, "create", vals_list=[vals], context=IMPORT_CONTEXT)
        return res[0] if isinstance(res, list) else res

    def version(self) -> str:
        res = self.search_read("ir.module.module", [("name", "=", "base")], ["latest_version"], limit=1)
        return res[0]["latest_version"] if res else "?"

    # ---- lookups -------------------------------------------------------------------
    def lead_by_message_ids(self, message_ids: list[str]) -> int | None:
        if not message_ids:
            return None
        res = self.search_read("mail.message", [("message_id", "in", message_ids), ("model", "=", "crm.lead")],
                               ["res_id"], limit=1, order="id desc")
        return res[0]["res_id"] if res else None

    def message_location(self, message_id: str) -> tuple[str, int] | None:
        res = self.search_read("mail.message", [("message_id", "=", message_id)], ["model", "res_id"], limit=1)
        return (res[0]["model"], res[0]["res_id"]) if res else None

    def open_lead_for_email(self, email: str) -> int | None:
        email = email.lower()
        domain = ["|", ("email_normalized", "=", email), ("partner_id.email_normalized", "=", email),
                  "|", ("probability", "=", False), ("probability", "<", 100)]
        ids = self.search_ids("crm.lead", domain, limit=1, order="write_date desc")
        return ids[0] if ids else None

    def leads_for_email(self, email: str, limit: int = 10) -> list[dict]:
        email = email.lower()
        domain = ["|", ("email_normalized", "=", email), ("partner_id.email_normalized", "=", email)]
        return self.search_read("crm.lead", domain, ["name", "stage_id", "probability", "active", "type"],
                                limit=limit, order="write_date desc", context={"active_test": False})

    def search_leads(self, q: str, limit: int = 15) -> list[dict]:
        """Free-text lead search for the picker: ID, title, contact, company, e-mail."""
        q = q.strip()
        if not q:
            return []
        domain = ["|", "|", "|", ("name", "ilike", q), ("email_from", "ilike", q),
                  ("partner_id.name", "ilike", q), ("partner_name", "ilike", q)]
        if q.lstrip("#").isdigit():
            domain = ["|", ("id", "=", int(q.lstrip("#")))] + domain
        rows = self.search_read("crm.lead", domain,
                                ["name", "stage_id", "partner_id", "email_from", "active", "probability", "type"],
                                limit=limit, order="write_date desc", context={"active_test": False})
        return rows

    def lead_info(self, lead_id: int) -> dict | None:
        res = self.search_read("crm.lead", [("id", "=", lead_id)], ["name", "stage_id", "active", "type"],
                               limit=1, context={"active_test": False})
        return res[0] if res else None

    def partner_by_email(self, email: str) -> int | None:
        ids = self.search_ids("res.partner", [("email_normalized", "=", email.lower())], limit=1,
                              order="is_company asc, id asc")
        return ids[0] if ids else None

    # ---- helpers with cache ----------------------------------------------------------
    def xmlid(self, module: str, name: str) -> int:
        key = ("xmlid", module, name)
        if key not in self._cache:
            res = self.search_read("ir.model.data", [("module", "=", module), ("name", "=", name)], ["res_id"], limit=1)
            if not res:
                raise OdooError(f"XML-ID {module}.{name} nicht gefunden")
            self._cache[key] = res[0]["res_id"]
        return self._cache[key]

    def tag_ids(self, names: list[str]) -> list[int]:
        out = []
        for name in names:
            key = ("tag", name.lower())
            if key not in self._cache:
                ids = self.search_ids("crm.tag", [("name", "=ilike", _esc_like(name))], limit=1)
                self._cache[key] = ids[0] if ids else self.create("crm.tag", {"name": name})
            out.append(self._cache[key])
        return out

    def team_id(self, name: str) -> int | None:
        key = ("team", name.lower())
        if key not in self._cache:
            ids = self.search_ids("crm.team", [("name", "=ilike", _esc_like(name))], limit=1)
            self._cache[key] = ids[0] if ids else None
        return self._cache[key]

    def user_id(self, login: str) -> int | None:
        key = ("user", login.lower())
        if key not in self._cache:
            ids = self.search_ids("res.users", [("login", "=ilike", _esc_like(login))], limit=1)
            self._cache[key] = ids[0] if ids else None
        return self._cache[key]

    def company_partner(self, name: str, website: str | None = None) -> int:
        key = ("company", name.lower())
        if key not in self._cache:
            ids = self.search_ids("res.partner", [("is_company", "=", True), ("name", "=ilike", _esc_like(name))], limit=1)
            if not ids and website:
                ids = self.search_ids("res.partner", [("is_company", "=", True), ("website", "ilike", website)], limit=1)
            vals = {"name": name, "is_company": True}
            if website:
                vals["website"] = website
            self._cache[key] = ids[0] if ids else self.create("res.partner", vals)
        return self._cache[key]

    # ---- message posting -------------------------------------------------------------
    def post_mail(self, model: str, res_id: int, mail: ParsedMail, author_id: int | None) -> int:
        """Store the original e-mail in the record's chatter without sending notifications.

        mail.message is created directly (instead of message_post) so that the original
        date and Message-ID are kept - Odoo then threads later replies correctly.
        """
        att_ids = []
        for att in mail.attachments:
            att_ids.append(self.create("ir.attachment", {
                "name": att.filename[:250] or "anhang",
                "datas": base64.b64encode(att.payload).decode(),
                "mimetype": att.content_type,
                "res_model": model,
                "res_id": res_id,
            }))
        from_header = f'"{mail.from_name}" <{mail.from_email}>' if mail.from_name else mail.from_email
        vals = {
            "model": model,
            "res_id": res_id,
            "message_type": "email",
            "subtype_id": self.xmlid("mail", "mt_comment"),
            "subject": mail.subject or False,
            "body": mail.body_as_html(),
            "email_from": from_header or False,
            "author_id": author_id or False,
            "date": (mail.date or datetime.utcnow()).strftime("%Y-%m-%d %H:%M:%S"),
            "attachment_ids": [(6, 0, att_ids)],
        }
        if mail.message_id:
            vals["message_id"] = mail.message_id
        return self.create("mail.message", vals)
