"""Command line: python -m importer <command>"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path


def main(argv: list[str] | None = None) -> None:
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    logging.getLogger("imapclient").setLevel(logging.WARNING)

    p = argparse.ArgumentParser(prog="python -m importer", description="IMAP -> Odoo CRM Importer")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("serve", help="Weboberfläche (+ Worker, falls WORKER_INTERVAL > 0)")
    s.add_argument("--host", default="0.0.0.0")
    s.add_argument("--port", type=int, default=8000)

    sub.add_parser("check", help="Verbindung zu Odoo und allen IMAP-Konten testen")

    f = sub.add_parser("fetch", help="Neue Mails per IMAP abrufen")
    f.add_argument("--account")
    f.add_argument("--limit", type=int, help="höchstens N neue Mails (Probelauf)")

    c = sub.add_parser("classify", help="Regeln auf neue Mails anwenden")
    c.add_argument("--pending", action="store_true", help="auch offene Vorschläge neu berechnen")
    c.add_argument("--offline", action="store_true", help="ohne Odoo-Abfragen (nur Text-Regeln)")

    a = sub.add_parser("apply", help="Freigegebene Mails nach Odoo übertragen")
    a.add_argument("--limit", type=int)

    sub.add_parser("cycle", help="fetch + classify + apply")

    sd = sub.add_parser("seed", help="Testdaten in Odoo und IMAP anlegen")
    sd.add_argument("--odoo-only", action="store_true")
    sd.add_argument("--imap-only", action="store_true")
    sd.add_argument("--bulk", type=int, default=0, help="zusätzlich N generierte Mails (Lasttest)")
    sd.add_argument("--force", action="store_true", help="IMAP-Testmails erneut einspielen")

    u = sub.add_parser("upload", help=".eml-Dateien/Ordner oder .mbox in ein IMAP-Postfach laden")
    u.add_argument("paths", nargs="+", type=Path)
    u.add_argument("--account", required=True)
    u.add_argument("--folder", default="INBOX")

    args = p.parse_args(argv)

    if args.cmd == "serve":
        import uvicorn

        from .web import create_app

        uvicorn.run(create_app(), host=args.host, port=args.port, proxy_headers=True)
        return

    from .jobs import App
    from .models import Status

    app = App()
    if args.cmd == "check":
        ok = _check(app)
        sys.exit(0 if ok else 1)
    elif args.cmd == "fetch":
        print(app.run_job("fetch", app.fetch, account=args.account, limit=args.limit))
    elif args.cmd == "classify":
        statuses = (Status.NEW, Status.PENDING, Status.ERROR) if args.pending else (Status.NEW,)
        print(app.run_job("classify", app.classify, statuses=statuses, offline=args.offline))
    elif args.cmd == "apply":
        print(app.run_job("apply", app.apply, limit=args.limit))
    elif args.cmd == "cycle":
        print(app.run_job("cycle", app.cycle))
    elif args.cmd == "seed":
        _seed(app, args)
    elif args.cmd == "upload":
        from .seed import upload_files

        n = upload_files(app.settings.account(args.account), args.folder, args.paths)
        print(f"{n} Nachrichten nach {args.account}/{args.folder} hochgeladen")


def _check(app) -> bool:
    from .fetcher import connect, select_folders

    ok = True
    try:
        odoo = app.odoo()
        print(f"Odoo  OK   {app.settings.odoo_url} db={app.settings.odoo_db} uid={odoo.rpc.uid} "
              f"api={odoo.rpc.name} base={odoo.version()}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"Odoo  FAIL {app.settings.odoo_url}: {exc}")
    for acc in app.settings.accounts:
        if not acc.enabled:
            print(f"IMAP  --   {acc.name} deaktiviert (enabled: false)")
            continue
        try:
            client = connect(acc)
            folders = select_folders(client, acc)
            client.logout()
            print(f"IMAP  OK   {acc.name} ({acc.username}@{acc.host}) Ordner: {', '.join(folders) or '-'}"
                  f"{'' if acc.enabled else '  [deaktiviert]'}")
        except Exception as exc:  # noqa: BLE001
            ok = False
            print(f"IMAP  FAIL {acc.name} ({acc.host}): {exc}")
    try:
        rs = app.ruleset()
        print(f"Regeln OK  {len(rs.rules)} Regeln aus {app.settings.rules_file}")
    except Exception as exc:  # noqa: BLE001
        ok = False
        print(f"Regeln FAIL {exc}")
    return ok


def _seed(app, args) -> None:
    from .seed import append_mails, build_bulk, build_scenarios, seed_odoo

    ids = None
    if not args.imap_only:
        ids = seed_odoo(app.odoo())
        print(f"Odoo: Testdaten angelegt/vorhanden - Leads {ids['lead_cnc']}, {ids['lead_buero']}, "
              f"{ids['lead_won']} (gewonnen), {ids['lead_archived']} (archiviert)")
    if not args.odoo_only:
        if ids is None:
            try:
                ids = seed_odoo(app.odoo())
            except Exception as exc:  # noqa: BLE001
                print(f"Odoo nicht erreichbar ({exc}) - Mail mit Lead-Referenz wird ausgelassen")
        mails = build_scenarios(ids)
        if args.bulk:
            mails += build_bulk(args.bulk)
        res = append_mails(app.settings, mails, force=args.force)
        print("IMAP: " + ", ".join(f"{k}: {v} Mails" for k, v in res.items()))
