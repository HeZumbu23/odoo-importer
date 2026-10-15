"""Job orchestration: fetch -> classify -> apply. Runs from the CLI, the web UI or the worker thread."""
from __future__ import annotations

import logging
import threading
import time
import traceback
from dataclasses import dataclass

from sqlalchemy import select

from .executor import apply_approved
from .fetcher import fetch_all
from .mailparse import RawStore
from .models import JobLog, LearnedRule, Mail, Status, make_session_factory, utcnow
from .odoo import OdooClient
from .rules import Lookups, RuleSet, classify, learned_to_rules
from .settings import Settings

log = logging.getLogger(__name__)


@dataclass
class ClassifyStats:
    pending: int = 0
    approved: int = 0
    ignored: int = 0
    errors: int = 0

    def __str__(self) -> str:
        return (f"{self.approved} automatisch freigegeben, {self.pending} zur Prüfung, "
                f"{self.ignored} ignoriert, {self.errors} Fehler")


class App:
    def __init__(self, settings: Settings | None = None):
        self.settings = settings or Settings.load()
        self.sessions = make_session_factory(self.settings.database_url)
        self.store = RawStore(self.settings.data_dir)
        self.lock = threading.Lock()
        self.running: str | None = None

    def ruleset(self) -> RuleSet:
        return RuleSet.load(self.settings.rules_file)

    def odoo(self) -> OdooClient:
        return OdooClient(self.settings)

    # -- jobs -----------------------------------------------------------------------
    def fetch(self, account: str | None = None, limit: int | None = None) -> str:
        res = fetch_all(self.sessions, self.store, self.settings, only=account, limit=limit)
        return "; ".join(f"{name}: {st}" for name, st in res.items()) or "keine aktiven Konten"

    def classify(self, ids: list[int] | None = None, statuses=(Status.NEW,), include_user_decisions=False,
                 offline: bool = False) -> ClassifyStats:
        ruleset = self.ruleset()
        lookups = Lookups(None if offline else self.odoo())
        stats = ClassifyStats()
        with self.sessions() as s:
            learned = learned_to_rules(list(s.scalars(select(LearnedRule).order_by(LearnedRule.id))))
            q = select(Mail.id).where(Mail.status.in_(statuses))
            if ids:
                q = q.where(Mail.id.in_(ids))
            if not include_user_decisions:
                q = q.where((Mail.decided_by.is_(None)) | (Mail.decided_by != "user"))
            todo = list(s.scalars(q.order_by(Mail.date.asc().nulls_first(), Mail.id)))
        for mail_id in todo:  # chronologisch, damit Antworten ihren Ursprung schon kennen
            with self.sessions() as s:
                mail = s.get(Mail, mail_id)
                try:
                    classify(s, mail, ruleset, lookups, learned, self.settings.auto_approve)
                except OSError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    log.exception("Klassifizierung von #%s fehlgeschlagen", mail_id)
                    mail.status, mail.error = Status.PENDING, f"Klassifizierung fehlgeschlagen: {exc}"
                    stats.errors += 1
                s.commit()
                if mail.status == Status.APPROVED:
                    stats.approved += 1
                elif mail.status == Status.IGNORED:
                    stats.ignored += 1
                else:
                    stats.pending += 1
        return stats

    def apply(self, ids: list[int] | None = None, limit: int | None = None) -> str:
        return str(apply_approved(self.sessions, self.settings, self.store, limit=limit, ids=ids))

    def cycle(self) -> str:
        parts = [f"Abruf: {self.fetch()}", f"Regeln: {self.classify()}", f"Odoo: {self.apply()}"]
        return " | ".join(parts)

    # -- execution with lock + log ------------------------------------------------------
    def run_job(self, name: str, fn, *args, **kwargs) -> str | None:
        if not self.lock.acquire(blocking=False):
            log.info("Job %s übersprungen - %s läuft noch", name, self.running)
            return None
        self.running = name
        with self.sessions() as s:
            entry = JobLog(job=name)
            s.add(entry)
            s.commit()
            entry_id = entry.id
        ok, summary = True, ""
        try:
            summary = str(fn(*args, **kwargs))
            log.info("Job %s: %s", name, summary)
            return summary
        except Exception as exc:  # noqa: BLE001
            ok, summary = False, f"{type(exc).__name__}: {exc}"
            log.error("Job %s fehlgeschlagen: %s\n%s", name, exc, traceback.format_exc())
            return summary
        finally:
            with self.sessions() as s:
                entry = s.get(JobLog, entry_id)
                entry.finished_at, entry.ok, entry.summary = utcnow(), ok, summary[:4000]
                s.commit()
            self.running = None
            self.lock.release()

    def run_in_background(self, name: str, fn, *args, **kwargs) -> bool:
        if self.lock.locked():
            return False
        threading.Thread(target=self.run_job, args=(name, fn, *args), kwargs=kwargs, daemon=True).start()
        return True

    def start_worker(self) -> None:
        interval = self.settings.worker_interval
        if interval <= 0:
            return

        def loop():
            time.sleep(5)
            while True:
                self.run_job("auto", self.cycle)
                time.sleep(interval)

        threading.Thread(target=loop, daemon=True, name="worker").start()
        log.info("Worker aktiv: alle %s s Abruf -> Regeln -> Odoo", interval)
