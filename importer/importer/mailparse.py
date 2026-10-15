"""Robust e-mail parsing (compat32 policy: never raises on broken headers)."""
from __future__ import annotations

import email
import email.utils
import hashlib
import html
import re
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.header import decode_header, make_header
from email.message import Message
from pathlib import Path

MSGID_RE = re.compile(r"<[^<>\s]+>")


def decode_hdr(value) -> str:
    if value is None:
        return ""
    value = str(value)
    try:
        return str(make_header(decode_header(value))).strip()
    except Exception:
        return value.strip()


def parse_addresses(raw_values: list[str]) -> list[tuple[str, str]]:
    out = []
    for name, addr in email.utils.getaddresses([str(v) for v in raw_values if v]):
        addr = (addr or "").strip().lower()
        if "@" not in addr:
            continue
        out.append((decode_hdr(name).strip().strip('"'), addr))
    return out


def _decode_payload(part: Message) -> str:
    payload = part.get_payload(decode=True)
    if payload is None:
        return ""
    charset = part.get_content_charset() or "utf-8"
    try:
        return payload.decode(charset, errors="replace")
    except LookupError:
        return payload.decode("latin-1", errors="replace")


_BLOCK_TAGS = re.compile(r"<\s*(br|/p|/div|/tr|/h\d|li|/li|/table)\b[^>]*>", re.I)


def html_to_text(markup: str) -> str:
    markup = re.sub(r"(?is)<(script|style|head)\b.*?</\1>", " ", markup)
    markup = _BLOCK_TAGS.sub("\n", markup)
    markup = re.sub(r"(?s)<[^>]+>", " ", markup)
    text = html.unescape(markup)
    text = re.sub(r"[ \t\xa0]+", " ", text)
    text = re.sub(r"\n\s*\n\s*\n+", "\n\n", text)
    return "\n".join(line.strip() for line in text.splitlines()).strip()


def text_to_html(text: str) -> str:
    return "<p>" + html.escape(text).replace("\n", "<br>") + "</p>"


@dataclass
class Attachment:
    filename: str
    content_type: str
    payload: bytes

    @property
    def size(self) -> int:
        return len(self.payload)


@dataclass
class ParsedMail:
    message_id: str | None = None
    in_reply_to: str | None = None
    references: list[str] = field(default_factory=list)
    subject: str = ""
    from_name: str = ""
    from_email: str = ""
    to: list[tuple[str, str]] = field(default_factory=list)
    cc: list[tuple[str, str]] = field(default_factory=list)
    date: datetime | None = None
    body_text: str = ""
    body_html: str | None = None
    attachments: list[Attachment] = field(default_factory=list)
    headers: dict[str, str] = field(default_factory=dict)

    @property
    def thread_refs(self) -> list[str]:
        """Ancestor message-ids, closest first."""
        refs = []
        if self.in_reply_to:
            refs.append(self.in_reply_to)
        for r in reversed(self.references):
            if r not in refs:
                refs.append(r)
        return refs

    def body_as_html(self) -> str:
        return self.body_html if self.body_html else text_to_html(self.body_text)


def _walk(part: Message, out: ParsedMail, counter: list[int]) -> None:
    ctype = part.get_content_type()
    if part.is_multipart() and ctype != "message/rfc822":
        for sub in part.get_payload():
            _walk(sub, out, counter)
        return
    disposition = (part.get("Content-Disposition") or "").split(";")[0].strip().lower()
    filename = part.get_filename()
    if ctype == "message/rfc822":
        inner = part.get_payload()
        payload = inner[0].as_bytes() if isinstance(inner, list) and inner else (part.get_payload(decode=True) or b"")
        sub = decode_hdr(inner[0].get("Subject")) if isinstance(inner, list) and inner else ""
        name = decode_hdr(filename) if filename else f"{(sub or 'weitergeleitet')[:60]}.eml"
        out.attachments.append(Attachment(name, ctype, payload))
        return
    is_attachment = disposition == "attachment" or bool(filename)
    if not is_attachment and ctype == "text/plain" and not out.body_text:
        out.body_text = _decode_payload(part)
        return
    if not is_attachment and ctype == "text/html" and out.body_html is None:
        out.body_html = _decode_payload(part)
        return
    if not is_attachment and ctype.startswith("text/"):
        return  # weitere Textalternativen ignorieren
    payload = part.get_payload(decode=True) or b""
    counter[0] += 1
    name = decode_hdr(filename) if filename else f"anhang-{counter[0]}"
    out.attachments.append(Attachment(name, ctype, payload))


def parse(raw: bytes) -> ParsedMail:
    msg = email.message_from_bytes(raw)
    p = ParsedMail()
    mid = MSGID_RE.findall(str(msg.get("Message-ID") or ""))
    p.message_id = mid[0] if mid else None
    irt = MSGID_RE.findall(str(msg.get("In-Reply-To") or ""))
    p.in_reply_to = irt[0] if irt else None
    p.references = MSGID_RE.findall(str(msg.get("References") or ""))
    p.subject = decode_hdr(msg.get("Subject"))
    frm = parse_addresses(msg.get_all("From") or [])
    if frm:
        p.from_name, p.from_email = frm[0]
    p.to = parse_addresses(msg.get_all("To") or [])
    p.cc = parse_addresses(msg.get_all("Cc") or [])
    try:
        d = email.utils.parsedate_to_datetime(str(msg.get("Date")))
        if d.tzinfo is not None:
            d = d.astimezone(timezone.utc).replace(tzinfo=None)
        p.date = d
    except Exception:
        p.date = None
    headers: dict[str, str] = {}
    for k, v in msg.items():
        val = decode_hdr(v)[:2000]
        headers[k] = f"{headers[k]}\n{val}" if k in headers else val
    p.headers = headers
    _walk(msg, p, [0])
    if not p.body_text and p.body_html:
        p.body_text = html_to_text(p.body_html)
    return p


def sha256(raw: bytes) -> str:
    return hashlib.sha256(raw).hexdigest()


class RawStore:
    """Keeps the original .eml files on disk (content addressed)."""

    def __init__(self, root: Path):
        self.root = Path(root) / "raw"

    def save(self, digest: str, raw: bytes) -> str:
        rel = Path(digest[:2]) / digest[2:4] / f"{digest}.eml"
        path = self.root / rel
        if not path.exists():
            path.parent.mkdir(parents=True, exist_ok=True)
            tmp = path.with_suffix(".tmp")
            tmp.write_bytes(raw)
            tmp.replace(path)
        return str(rel)

    def load(self, rel: str) -> bytes:
        return (self.root / rel).read_bytes()
