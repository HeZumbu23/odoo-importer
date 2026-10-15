"""IMAP fetch: incremental per folder (UIDVALIDITY + last UID), never marks mails as read."""
from __future__ import annotations

import fnmatch
import logging
from dataclasses import dataclass

from imapclient import IMAPClient
from sqlalchemy import select
from sqlalchemy.orm import Session

from .mailparse import RawStore, parse, sha256
from .models import FetchState, Mail, Status
from .settings import Account, Settings

log = logging.getLogger(__name__)


def connect(acc: Account) -> IMAPClient:
    client = IMAPClient(acc.host, port=acc.port, ssl=acc.ssl, timeout=120)
    if acc.starttls and not acc.ssl:
        client.starttls()
    client.login(acc.username, acc.password)
    return client


def select_folders(client: IMAPClient, acc: Account) -> list[str]:
    result = []
    for flags, _delim, name in client.list_folders():
        if any(f.lower() in (b"\\noselect", b"\\nonexistent") for f in flags):
            continue
        if not any(p == "*" or fnmatch.fnmatchcase(name, p) for p in acc.folders):
            continue
        if any(fnmatch.fnmatchcase(name, p) for p in acc.exclude_folders):
            continue
        result.append(name)
    return result


def direction_and_counterpart(from_email, from_name, to, cc, own_domains):
    def own(addr: str) -> bool:
        return addr.rpartition("@")[2] in own_domains

    if own_domains and from_email and own(from_email):
        for name, addr in list(to) + list(cc):
            if not own(addr):
                return "out", addr, name
        first = (list(to) + list(cc) or [("", "")])[0]
        return "out", first[1], first[0]
    return "in", from_email, from_name


def store_message(session: Session, store: RawStore, settings: Settings, account: str, folder: str,
                  uidvalidity: int, uid: int, raw: bytes) -> Mail:
    digest = sha256(raw)
    p = parse(raw)
    dup = None
    if p.message_id:
        dup = session.scalar(select(Mail.id).where(Mail.message_id == p.message_id).order_by(Mail.id).limit(1))
    if dup is None:
        dup = session.scalar(select(Mail.id).where(Mail.sha256 == digest).order_by(Mail.id).limit(1))
    direction, cp_email, cp_name = direction_and_counterpart(
        p.from_email, p.from_name, p.to, p.cc, settings.own_domains)
    mail = Mail(
        account=account, folder=folder, uidvalidity=uidvalidity, uid=uid, sha256=digest,
        raw_path=None if dup else store.save(digest, raw), size=len(raw),
        message_id=p.message_id, in_reply_to=p.in_reply_to, references=p.references,
        subject=p.subject[:2000], from_email=p.from_email, from_name=p.from_name[:500],
        to_addrs=[list(a) for a in p.to], cc_addrs=[list(a) for a in p.cc], date=p.date,
        direction=direction, counterpart_email=cp_email or "", counterpart_name=(cp_name or "")[:500],
        body_text=p.body_text[:200_000], has_html=p.body_html is not None,
        attachments=[{"filename": a.filename, "content_type": a.content_type, "size": a.size} for a in p.attachments],
        headers=p.headers, options={},
    )
    if dup:
        mail.status = Status.IGNORED
        mail.action = "ignore"
        mail.duplicate_of = dup
        mail.reason = f"Duplikat von #{dup} (gleiche Message-ID/gleicher Inhalt)"
    session.add(mail)
    return mail


@dataclass
class FetchStats:
    folders: int = 0
    fetched: int = 0
    duplicates: int = 0
    too_large: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (f"{self.fetched} neu, {self.duplicates} Duplikate, {self.too_large} zu groß, "
                f"{self.errors} Fehler ({self.folders} Ordner)")


def fetch_account(session_factory, store: RawStore, settings: Settings, acc: Account,
                  limit: int | None = None) -> FetchStats:
    stats = FetchStats()
    client = connect(acc)
    try:
        for folder in select_folders(client, acc):
            stats.folders += 1
            info = client.select_folder(folder, readonly=True)
            uidvalidity = int(info[b"UIDVALIDITY"])
            with session_factory() as session:
                state = session.get(FetchState, (acc.name, folder))
                if state is None:
                    state = FetchState(account=acc.name, folder=folder, uidvalidity=uidvalidity, last_uid=0)
                    session.add(state)
                elif state.uidvalidity != uidvalidity:
                    log.warning("UIDVALIDITY von %s/%s geändert - Ordner wird neu gelesen", acc.name, folder)
                    state.uidvalidity, state.last_uid = uidvalidity, 0
                session.commit()
                last_uid = state.last_uid
            criteria = ["UID", f"{last_uid + 1}:*"]
            if acc.since:
                criteria += ["SINCE", acc.since]
            uids = sorted(u for u in client.search(criteria) if u > last_uid)
            if limit is not None:
                uids = uids[: max(0, limit - stats.fetched)]
            log.info("%s/%s: %d neue Nachrichten", acc.name, folder, len(uids))
            for i in range(0, len(uids), settings.batch_size):
                batch = uids[i: i + settings.batch_size]
                sizes = client.fetch(batch, ["RFC822.SIZE"])
                max_bytes = settings.max_size_mb * 1024 * 1024
                ok = [u for u in batch if sizes.get(u, {}).get(b"RFC822.SIZE", 0) <= max_bytes]
                data = client.fetch(ok, ["BODY.PEEK[]"]) if ok else {}
                with session_factory() as session:
                    for uid in batch:
                        try:
                            if uid not in ok:
                                stats.too_large += 1
                                session.add(Mail(
                                    account=acc.name, folder=folder, uidvalidity=uidvalidity, uid=uid,
                                    sha256="", size=sizes.get(uid, {}).get(b"RFC822.SIZE", 0),
                                    status=Status.ERROR, references=[], to_addrs=[], cc_addrs=[],
                                    attachments=[], headers={}, options={},
                                    error=f"Nachricht größer als {settings.max_size_mb} MB - nicht abgerufen"))
                                session.commit()
                                continue
                            raw = data[uid][b"BODY[]"]
                            mail = store_message(session, store, settings, acc.name, folder, uidvalidity, uid, raw)
                            session.commit()
                            if mail.duplicate_of:
                                stats.duplicates += 1
                            else:
                                stats.fetched += 1
                        except Exception:  # einzelne kaputte Mail darf den Lauf nicht stoppen
                            log.exception("Fehler bei %s/%s UID %s", acc.name, folder, uid)
                            session.rollback()
                            stats.errors += 1
                    state = session.get(FetchState, (acc.name, folder))
                    state.last_uid = max(batch)
                    session.commit()
            if limit is not None and stats.fetched >= limit:
                break
    finally:
        try:
            client.logout()
        except Exception:
            pass
    return stats


def fetch_all(session_factory, store: RawStore, settings: Settings, only: str | None = None,
              limit: int | None = None) -> dict[str, FetchStats]:
    out = {}
    for acc in settings.accounts:
        if not acc.enabled or (only and acc.name != only):
            continue
        out[acc.name] = fetch_account(session_factory, store, settings, acc, limit=limit)
    return out
