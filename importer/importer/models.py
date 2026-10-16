"""Staging database: every fetched mail, its proposed/approved action and the Odoo result."""
from __future__ import annotations

import logging
import os
import socket
import time
from datetime import datetime, timezone
from pathlib import Path

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    String,
    Text,
    UniqueConstraint,
    create_engine,
    text,
)
from sqlalchemy.engine import make_url
from sqlalchemy.exc import OperationalError
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, sessionmaker


log = logging.getLogger(__name__)


def utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class Base(DeclarativeBase):
    pass


class Status:
    NEW = "new"            # abgerufen, noch nicht klassifiziert
    PENDING = "pending"    # Vorschlag liegt vor, wartet auf Prüfung
    APPROVED = "approved"  # freigegeben, wird beim nächsten "Ausführen" nach Odoo übertragen
    DONE = "done"          # in Odoo angelegt
    IGNORED = "ignored"    # bewusst nicht importiert
    ERROR = "error"        # Fehler beim Abruf oder bei der Übertragung

    ALL = [NEW, PENDING, APPROVED, DONE, IGNORED, ERROR]
    LABELS = {
        NEW: "neu",
        PENDING: "zu prüfen",
        APPROVED: "freigegeben",
        DONE: "erledigt",
        IGNORED: "ignoriert",
        ERROR: "Fehler",
    }


class Action:
    NEW_LEAD = "new_lead"
    ATTACH = "attach_to_lead"
    CONTACT = "create_contact"
    IGNORE = "ignore"
    REVIEW = "review"

    ALL = [NEW_LEAD, ATTACH, CONTACT, IGNORE, REVIEW]
    LABELS = {
        NEW_LEAD: "Neuer Lead",
        ATTACH: "An Lead anhängen",
        CONTACT: "Nur Kontakt",
        IGNORE: "Ignorieren",
        REVIEW: "Manuell entscheiden",
    }


class Mail(Base):
    __tablename__ = "mail"
    __table_args__ = (UniqueConstraint("account", "folder", "uidvalidity", "uid", name="uq_mail_imap"),)

    id: Mapped[int] = mapped_column(primary_key=True)
    account: Mapped[str] = mapped_column(String(100), index=True)
    folder: Mapped[str] = mapped_column(String(500))
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    uid: Mapped[int] = mapped_column(BigInteger)
    sha256: Mapped[str] = mapped_column(String(64), index=True)
    raw_path: Mapped[str | None] = mapped_column(String(500))
    size: Mapped[int] = mapped_column(Integer, default=0)

    message_id: Mapped[str | None] = mapped_column(String(998), index=True)
    in_reply_to: Mapped[str | None] = mapped_column(Text)
    references: Mapped[list] = mapped_column(JSON, default=list)
    subject: Mapped[str] = mapped_column(Text, default="")
    from_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    from_name: Mapped[str] = mapped_column(String(500), default="")
    to_addrs: Mapped[list] = mapped_column(JSON, default=list)   # [[name, email], ...]
    cc_addrs: Mapped[list] = mapped_column(JSON, default=list)
    date: Mapped[datetime | None] = mapped_column(DateTime, index=True)
    direction: Mapped[str] = mapped_column(String(3), default="in")  # in | out
    counterpart_email: Mapped[str] = mapped_column(String(320), default="", index=True)
    counterpart_name: Mapped[str] = mapped_column(String(500), default="")
    body_text: Mapped[str] = mapped_column(Text, default="")
    has_html: Mapped[bool] = mapped_column(Boolean, default=False)
    attachments: Mapped[list] = mapped_column(JSON, default=list)  # [{filename, content_type, size}]
    headers: Mapped[dict] = mapped_column(JSON, default=dict)

    status: Mapped[str] = mapped_column(String(20), default=Status.NEW, index=True)
    action: Mapped[str | None] = mapped_column(String(20), index=True)
    rule: Mapped[str | None] = mapped_column(String(200), index=True)
    reason: Mapped[str | None] = mapped_column(Text)
    decided_by: Mapped[str | None] = mapped_column(String(20))  # rule | user
    target_lead_id: Mapped[int | None] = mapped_column(Integer)
    thread_parent_id: Mapped[int | None] = mapped_column(ForeignKey("mail.id"), index=True)
    options: Mapped[dict] = mapped_column(JSON, default=dict)
    duplicate_of: Mapped[int | None] = mapped_column(Integer)

    odoo_partner_id: Mapped[int | None] = mapped_column(Integer)
    odoo_lead_id: Mapped[int | None] = mapped_column(Integer, index=True)
    odoo_message_id: Mapped[int | None] = mapped_column(Integer)
    error: Mapped[str | None] = mapped_column(Text)

    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)

    @property
    def counterpart_domain(self) -> str:
        return self.counterpart_email.rpartition("@")[2].lower()


class FetchState(Base):
    __tablename__ = "fetch_state"

    account: Mapped[str] = mapped_column(String(100), primary_key=True)
    folder: Mapped[str] = mapped_column(String(500), primary_key=True)
    uidvalidity: Mapped[int] = mapped_column(BigInteger)
    last_uid: Mapped[int] = mapped_column(BigInteger, default=0)
    updated_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow, onupdate=utcnow)


class LearnedRule(Base):
    """Rules created from the review UI ("immer so behandeln")."""

    __tablename__ = "learned_rule"

    id: Mapped[int] = mapped_column(primary_key=True)
    kind: Mapped[str] = mapped_column(String(20))  # email | domain
    pattern: Mapped[str] = mapped_column(String(320), index=True)
    action: Mapped[str] = mapped_column(String(20))
    auto: Mapped[bool] = mapped_column(Boolean, default=False)
    note: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)


class JobLog(Base):
    __tablename__ = "job_log"

    id: Mapped[int] = mapped_column(primary_key=True)
    job: Mapped[str] = mapped_column(String(50))
    started_at: Mapped[datetime] = mapped_column(DateTime, default=utcnow)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime)
    ok: Mapped[bool | None] = mapped_column(Boolean)
    summary: Mapped[str] = mapped_column(Text, default="")


def _ensure_postgres_database(url: str) -> None:
    """Create the importer database on first start (the Postgres volume may already exist)."""
    u = make_url(url)
    if not u.drivername.startswith("postgresql") or not u.database:
        return
    admin = create_engine(u.set(database="postgres"), isolation_level="AUTOCOMMIT")
    try:
        with admin.connect() as conn:
            exists = conn.execute(text("SELECT 1 FROM pg_database WHERE datname = :d"), {"d": u.database}).scalar()
            if not exists:
                conn.execute(text(f'CREATE DATABASE "{u.database}"'))
    finally:
        admin.dispose()


def _describe_host(url: str) -> str:
    u = make_url(url)
    try:
        found = sorted({a[4][0] for a in socket.getaddrinfo(u.host, u.port or 5432, proto=socket.IPPROTO_TCP)})
        return f"{u.host}:{u.port or 5432} -> {', '.join(found)}"
    except OSError as exc:
        return f"{u.host}:{u.port or 5432} -> Namensauflösung fehlgeschlagen ({exc})"


def _with_retry(fn, url: str):
    """Postgres may still be starting (or Docker DNS not ready yet): retry instead of crashing."""
    attempts = int(os.environ.get("DB_CONNECT_RETRIES", "30"))
    delay = float(os.environ.get("DB_CONNECT_DELAY", "2"))
    for attempt in range(1, attempts + 1):
        try:
            return fn()
        except OperationalError as exc:
            if attempt == attempts:
                raise
            log.warning("Datenbank nicht erreichbar (Versuch %d/%d): %s | %s", attempt, attempts,
                        str(exc.orig or exc).strip(), _describe_host(url))
            time.sleep(delay)


def make_session_factory(url: str) -> sessionmaker:
    if url.startswith("sqlite:///"):
        Path(url.removeprefix("sqlite:///")).parent.mkdir(parents=True, exist_ok=True)
        engine = create_engine(url, connect_args={"check_same_thread": False})
        Base.metadata.create_all(engine)
    else:
        _with_retry(lambda: _ensure_postgres_database(url), url)
        engine = create_engine(url, pool_pre_ping=True)
        _with_retry(lambda: Base.metadata.create_all(engine), url)
    return sessionmaker(engine, expire_on_commit=False)
