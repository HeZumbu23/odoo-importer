"""Configuration: environment variables + importer.yaml (accounts) + rules.yaml."""
from __future__ import annotations

import os
import re
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

import yaml

_ENV_RE = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")


def expand_env(value: Any) -> Any:
    """Replace ${VAR} / ${VAR:-default} in all strings of a loaded YAML structure."""
    if isinstance(value, str):
        return _ENV_RE.sub(lambda m: os.environ.get(m.group(1), m.group(2) or ""), value)
    if isinstance(value, list):
        return [expand_env(v) for v in value]
    if isinstance(value, dict):
        return {k: expand_env(v) for k, v in value.items()}
    return value


def load_yaml(path: str | Path) -> dict:
    with open(path, encoding="utf-8") as fh:
        return expand_env(yaml.safe_load(fh) or {})


def _bool(value: str | None, default: bool) -> bool:
    if value is None or value == "":
        return default
    return value.strip().lower() in ("1", "true", "yes", "ja", "on")


@dataclass
class Account:
    name: str
    host: str
    username: str
    password: str
    port: int | None = None
    ssl: bool = True
    starttls: bool = False
    folders: list[str] = field(default_factory=lambda: ["INBOX"])
    exclude_folders: list[str] = field(default_factory=list)
    since: date | None = None
    enabled: bool = True

    @classmethod
    def from_dict(cls, d: dict) -> "Account":
        since = d.get("since")
        if isinstance(since, str) and since:
            since = date.fromisoformat(since)
        return cls(
            name=d["name"],
            host=d["host"],
            username=str(d["username"]),
            password=str(d["password"]),
            port=int(d["port"]) if d.get("port") else None,
            ssl=bool(d.get("ssl", True)),
            starttls=bool(d.get("starttls", False)),
            folders=list(d.get("folders") or ["INBOX"]),
            exclude_folders=list(d.get("exclude_folders") or []),
            since=since or None,
            enabled=bool(d.get("enabled", True)),
        )


@dataclass
class Settings:
    database_url: str
    data_dir: Path
    config_file: Path
    rules_file: Path
    odoo_url: str
    odoo_db: str
    odoo_user: str
    odoo_password: str
    odoo_api: str  # auto | xmlrpc | json2
    odoo_public_url: str  # für Links in der Oberfläche
    ui_user: str
    ui_password: str
    worker_interval: int
    auto_approve: bool
    own_domains: list[str] = field(default_factory=list)
    accounts: list[Account] = field(default_factory=list)
    batch_size: int = 100
    max_size_mb: int = 40
    apply_batch: int = 200

    @classmethod
    def load(cls) -> "Settings":
        data_dir = Path(os.environ.get("DATA_DIR", "./data"))
        s = cls(
            database_url=os.environ.get("DATABASE_URL", f"sqlite:///{data_dir / 'importer.db'}"),
            data_dir=data_dir,
            config_file=Path(os.environ.get("CONFIG_FILE", "config/importer.yaml")),
            rules_file=Path(os.environ.get("RULES_FILE", "config/rules.yaml")),
            odoo_url=os.environ.get("ODOO_URL", "http://localhost:8069").rstrip("/"),
            odoo_db=os.environ.get("ODOO_DB", "odoo"),
            odoo_user=os.environ.get("ODOO_USER", "admin"),
            odoo_password=os.environ.get("ODOO_PASSWORD", "admin"),
            odoo_api=os.environ.get("ODOO_API", "auto").lower(),
            odoo_public_url=os.environ.get("ODOO_PUBLIC_URL", "http://localhost:8069").rstrip("/"),
            ui_user=os.environ.get("UI_USER", "admin"),
            ui_password=os.environ.get("UI_PASSWORD", "admin"),
            worker_interval=int(os.environ.get("WORKER_INTERVAL", "0") or 0),
            auto_approve=_bool(os.environ.get("AUTO_APPROVE"), True),
        )
        if s.config_file.exists():
            cfg = load_yaml(s.config_file)
            s.own_domains = [d.lower() for d in cfg.get("own_domains") or []]
            s.accounts = [Account.from_dict(a) for a in cfg.get("accounts") or []]
            fetch = cfg.get("fetch") or {}
            s.batch_size = int(fetch.get("batch_size", s.batch_size))
            s.max_size_mb = int(fetch.get("max_size_mb", s.max_size_mb))
            s.apply_batch = int((cfg.get("apply") or {}).get("batch_size", s.apply_batch))
        return s

    def account(self, name: str) -> Account:
        for acc in self.accounts:
            if acc.name == name:
                return acc
        raise KeyError(f"Unbekanntes Konto: {name}")
