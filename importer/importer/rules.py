"""Rule engine: proposes an action for every fetched mail.

Rules live in rules.yaml (see the commented example there) and are evaluated top-down,
the first matching rule wins. Rules learned in the web UI are inserted at `- use: learned`.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

from sqlalchemy import select
from sqlalchemy.orm import Session

from .models import Action, LearnedRule, Mail, Status
from .settings import load_yaml

TEXT_CONDITIONS = {"from", "to", "subject", "body", "folder", "account", "counterpart"}
BOOL_CONDITIONS = {"has_attachment", "thread", "sender_has_open_lead", "known_partner"}
OTHER_CONDITIONS = {"header", "direction", "subject_ref", "any", "all", "not",
                    "counterpart_email_is", "counterpart_domain_is"}
OPTION_KEYS = {"create_contact", "company_from_domain", "post_on_partner", "lead", "extract", "target"}
TARGET_STRATEGIES = ("subject_ref", "thread", "sender")


@dataclass
class Rule:
    name: str
    action: str
    conditions: dict = field(default_factory=dict)
    auto: bool = False
    options: dict = field(default_factory=dict)

    @classmethod
    def from_dict(cls, d: dict, index: int) -> "Rule":
        d = dict(d)
        name = d.pop("name", f"Regel {index}")
        action = d.pop("action", None)
        if action not in Action.ALL:
            raise ValueError(f"{name}: unbekannte action '{action}' (erlaubt: {', '.join(Action.ALL)})")
        auto = bool(d.pop("auto", False))
        options = {k: d.pop(k) for k in list(d) if k in OPTION_KEYS}
        _validate_conditions(name, d)
        return cls(name=name, action=action, conditions=d, auto=auto, options=options)


def _validate_conditions(name: str, cond: dict) -> None:
    for key, value in cond.items():
        if key in TEXT_CONDITIONS | BOOL_CONDITIONS | OTHER_CONDITIONS:
            for v in (value if isinstance(value, list) else [value]):
                if key in TEXT_CONDITIONS and isinstance(v, str):
                    re.compile(v)
            if key in ("any", "all"):
                for sub in value:
                    _validate_conditions(name, sub)
            if key == "not":
                _validate_conditions(name, value)
            continue
        raise ValueError(f"{name}: unbekannte Bedingung '{key}'")


LEARNED_MARKER = object()


class RuleSet:
    def __init__(self, defaults: dict, entries: list):
        self.defaults = defaults
        self.entries = entries  # Rule | LEARNED_MARKER

    @classmethod
    def load(cls, path: str | Path) -> "RuleSet":
        data = load_yaml(path)
        entries: list = []
        for i, raw in enumerate(data.get("rules") or [], start=1):
            if raw.get("use") == "learned":
                entries.append(LEARNED_MARKER)
            else:
                entries.append(Rule.from_dict(raw, i))
        if LEARNED_MARKER not in entries:
            entries.insert(0, LEARNED_MARKER)
        return cls(data.get("defaults") or {}, entries)

    def iter_rules(self, learned: list[Rule]):
        for entry in self.entries:
            if entry is LEARNED_MARKER:
                yield from learned
            else:
                yield entry

    @property
    def rules(self) -> list[Rule]:
        return [e for e in self.entries if e is not LEARNED_MARKER]


def learned_to_rules(rows: list[LearnedRule]) -> list[Rule]:
    rules = []
    for r in rows:
        cond = {"counterpart_email_is": r.pattern} if r.kind == "email" else {"counterpart_domain_is": r.pattern}
        rules.append(Rule(name=f"Gelernt: {r.pattern}", action=r.action, conditions=cond, auto=r.auto))
    return rules


# ------------------------------------------------------------------------------------------
class Lookups:
    """Odoo lookups with per-run cache. `odoo` may be None (offline classification)."""

    def __init__(self, odoo):
        self.odoo = odoo
        self._c: dict = {}

    def _cached(self, key, fn: Callable):
        if key not in self._c:
            self._c[key] = fn() if self.odoo is not None else None
        return self._c[key]

    def lead_by_message_ids(self, ids):
        return self._cached(("thread", tuple(ids)), lambda: self.odoo.lead_by_message_ids(list(ids)))

    def open_lead_for_email(self, email):
        return self._cached(("open", email), lambda: self.odoo.open_lead_for_email(email)) if email else None

    def partner_by_email(self, email):
        return self._cached(("partner", email), lambda: self.odoo.partner_by_email(email)) if email else None

    def lead_exists(self, lead_id):
        return bool(self._cached(("lead", lead_id), lambda: self.odoo.lead_info(lead_id)))

    def message_location(self, message_id):
        return self._cached(("msg", message_id), lambda: self.odoo.message_location(message_id))


class Ctx:
    def __init__(self, session: Session, mail: Mail, lookups: Lookups):
        self.session, self.mail, self.lookups = session, mail, lookups
        self._thread = None
        self.subject_ref_lead: int | None = None

    # -- thread: closest ancestor that is (or will become) a lead ---------------------------
    def thread(self) -> tuple[Mail | None, int | None]:
        if self._thread is None:
            self._thread = self._find_thread()
        return self._thread

    def _find_thread(self):
        m = self.mail
        refs = []
        if m.in_reply_to:
            refs.append(m.in_reply_to)
        refs += [r for r in reversed(m.references or []) if r not in refs]
        if not refs:
            return (None, None)
        cands = self.session.scalars(select(Mail).where(Mail.message_id.in_(refs), Mail.id != m.id)).all()
        by_mid: dict[str, Mail] = {}
        for c in cands:
            if c.duplicate_of:
                c = self.session.get(Mail, c.duplicate_of) or c
            if c.action in (Action.NEW_LEAD, Action.ATTACH) and c.status in (
                    Status.PENDING, Status.APPROVED, Status.DONE):
                by_mid.setdefault(c.message_id, c)
        for ref in refs:
            parent = by_mid.get(ref)
            if parent is not None:
                return (parent, parent.odoo_lead_id if parent.status == Status.DONE else None)
        lead = self.lookups.lead_by_message_ids(refs)
        return (None, lead) if lead else (None, None)

    def has_thread(self) -> bool:
        parent, lead = self.thread()
        return bool(parent or lead)

    def sender_open_lead(self) -> int | None:
        return self.lookups.open_lead_for_email(self.mail.counterpart_email)

    def known_partner(self) -> bool:
        return bool(self.lookups.partner_by_email(self.mail.counterpart_email))


def _rx(pattern: str, value: str) -> bool:
    return re.search(pattern, value or "", re.I | re.M) is not None


def _match_text(spec: Any, value: str) -> bool:
    specs = spec if isinstance(spec, list) else [spec]
    return any(_rx(str(s), value) for s in specs)


def _addr_list(addrs) -> str:
    # jede Adresse einmal pur und einmal mit Namen, damit ^...$ (zeilenweise) funktioniert
    lines = []
    for n, a in addrs or []:
        lines.append(a)
        if n:
            lines.append(f"{n} <{a}>")
    return "\n".join(lines)


def matches(cond: dict, ctx: Ctx) -> bool:
    m = ctx.mail
    for key, spec in cond.items():
        if key == "from":
            ok = _match_text(spec, _addr_list([(m.from_name, m.from_email)]))
        elif key == "to":
            ok = _match_text(spec, _addr_list(m.to_addrs) + "\n" + _addr_list(m.cc_addrs))
        elif key == "subject":
            ok = _match_text(spec, m.subject)
        elif key == "body":
            ok = _match_text(spec, m.body_text)
        elif key == "folder":
            ok = _match_text(spec, m.folder)
        elif key == "account":
            ok = _match_text(spec, m.account)
        elif key == "counterpart":
            ok = _match_text(spec, _addr_list([(m.counterpart_name, m.counterpart_email)]))
        elif key == "counterpart_email_is":
            ok = m.counterpart_email.lower() == str(spec).lower()
        elif key == "counterpart_domain_is":
            ok = m.counterpart_domain == str(spec).lower()
        elif key == "header":
            ok = all(_match_text(v, (m.headers or {}).get(_header_key(m.headers, h), "")) for h, v in spec.items())
        elif key == "direction":
            ok = m.direction == spec
        elif key == "has_attachment":
            ok = bool(m.attachments) == bool(spec)
        elif key == "thread":
            ok = ctx.has_thread() == bool(spec)
        elif key == "sender_has_open_lead":
            ok = bool(ctx.sender_open_lead()) == bool(spec)
        elif key == "known_partner":
            ok = ctx.known_partner() == bool(spec)
        elif key == "subject_ref":
            ok = False
            found = re.search(spec, m.subject or "", re.I)
            if found and found.groups():
                lead_id = int(found.group(1))
                if ctx.lookups.odoo is None or ctx.lookups.lead_exists(lead_id):
                    ctx.subject_ref_lead, ok = lead_id, True
        elif key == "any":
            ok = any(matches(sub, ctx) for sub in spec)
        elif key == "all":
            ok = all(matches(sub, ctx) for sub in spec)
        elif key == "not":
            ok = not matches(spec, ctx)
        else:
            ok = False
        if not ok:
            return False
    return True


def _header_key(headers: dict, name: str) -> str:
    for k in headers or {}:
        if k.lower() == name.lower():
            return k
    return name


def _resolve_target(rule: Rule, ctx: Ctx) -> tuple[int | None, Mail | None, str]:
    strategies = rule.options.get("target") or TARGET_STRATEGIES
    if isinstance(strategies, str):
        strategies = [strategies]
    for strat in strategies:
        if strat == "subject_ref" and ctx.subject_ref_lead:
            return ctx.subject_ref_lead, None, f"Lead #{ctx.subject_ref_lead} aus Betreff"
        if strat == "thread":
            parent, lead = ctx.thread()
            if parent is not None:
                return lead, parent, f"Antwort auf Mail #{parent.id}"
            if lead:
                return lead, None, f"Thread gehört zu Odoo-Lead #{lead}"
        if strat == "sender":
            lead = ctx.sender_open_lead()
            if lead:
                return lead, None, f"offener Lead #{lead} von {ctx.mail.counterpart_email}"
    return None, None, ""


def _extract(spec: dict, body: str) -> dict:
    out = {}
    for fld, pattern in (spec or {}).items():
        found = re.search(pattern, body or "", re.I | re.M)
        if found:
            out[fld] = (found.group(1) if found.groups() else found.group(0)).strip()
    return out


def build_options(ruleset: RuleSet, rule: Rule | None, mail: Mail) -> dict:
    d = ruleset.defaults
    opts = {
        "create_contact": d.get("create_contact", True),
        "company_from_domain": d.get("company_from_domain", False),
        "post_on_partner": d.get("post_on_partner", True),
        "lead": dict(d.get("lead") or {}),
    }
    if rule is not None:
        for k in ("create_contact", "company_from_domain", "post_on_partner"):
            if k in rule.options:
                opts[k] = rule.options[k]
        opts["lead"].update(rule.options.get("lead") or {})
        extracted = _extract(rule.options.get("extract") or {}, mail.body_text)
        if extracted:
            opts["extract"] = extracted
    return opts


def classify(session: Session, mail: Mail, ruleset: RuleSet, lookups: Lookups, learned: list[Rule],
             auto_approve: bool) -> None:
    mail.error = None
    mail.target_lead_id = None
    mail.thread_parent_id = None
    if mail.message_id and lookups.odoo is not None:
        loc = lookups.message_location(mail.message_id)
        if loc:
            mail.status, mail.action, mail.rule, mail.decided_by = Status.IGNORED, Action.IGNORE, "bereits in Odoo", "rule"
            mail.reason = f"Message-ID existiert bereits in Odoo ({loc[0]} #{loc[1]})"
            return

    ctx = Ctx(session, mail, lookups)
    for rule in ruleset.iter_rules(learned):
        ctx.subject_ref_lead = None
        if not matches(rule.conditions, ctx):
            continue
        reason = f"Regel „{rule.name}“"
        if rule.action == Action.ATTACH:
            lead_id, parent, why = _resolve_target(rule, ctx)
            if lead_id is None and parent is None:
                continue  # Anhängen ohne Ziel ist kein Treffer -> nächste Regel
            mail.target_lead_id = lead_id
            mail.thread_parent_id = parent.id if parent is not None else None
            reason += f": {why}"
        _set(mail, rule.action, rule.name, reason, build_options(ruleset, rule, mail), rule.auto, auto_approve)
        return

    action = ruleset.defaults.get("action", Action.REVIEW)
    _set(mail, action, "Standard", "keine Regel hat gegriffen", build_options(ruleset, None, mail), False, auto_approve)


def _set(mail: Mail, action: str, rule: str, reason: str, options: dict, auto: bool, auto_approve: bool) -> None:
    mail.action, mail.rule, mail.reason, mail.options, mail.decided_by = action, rule, reason, options, "rule"
    if action == Action.REVIEW or not (auto and auto_approve):
        mail.status = Status.PENDING
    elif action == Action.IGNORE:
        mail.status = Status.IGNORED
    else:
        mail.status = Status.APPROVED
