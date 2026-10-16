"""Offline tests: parser, IMAP fetch (fake server), rule engine, web UI. No Odoo needed."""
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

import fake_imap
from importer import seed
from importer.jobs import App
from importer.mailparse import parse
from importer.models import Mail, Status
from importer.settings import Account, Settings
from importer.web import create_app

ROOT = Path(__file__).resolve().parents[1]


@pytest.fixture()
def env(tmp_path):
    server, store = fake_imap.start({"archiv": "pw", "vertrieb": "pw"})
    port = server.server_address[1]
    accounts = [Account(name=n, host="127.0.0.1", port=port, username=n, password="pw", ssl=False,
                        folders=["*"], exclude_folders=["Trash"]) for n in ("archiv", "vertrieb")]
    s = Settings(database_url=f"sqlite:///{tmp_path}/t.db", data_dir=tmp_path, config_file=tmp_path / "none.yaml",
                 rules_file=ROOT / "config" / "rules.yaml", odoo_url="http://invalid.invalid", odoo_db="x",
                 odoo_user="x", odoo_password="x", odoo_api="xmlrpc", odoo_public_url="http://odoo.test",
                 ui_user="u", ui_password="p", worker_interval=0, auto_approve=True,
                 own_domains=["meinefirma.de"], accounts=accounts)
    yield App(s), s
    server.shutdown()


def test_parse_umlauts_html_attachment():
    m = seed.build_scenarios(None)
    html = next(x for x in m if "Ausschreibung" in x.msg["Subject"]).msg
    p = parse(html.as_bytes())
    assert "Größere Ausschreibung" in p.subject and p.from_email == "juergen.gross@gross-consulting.at"
    assert "Hätten Sie Interesse" in p.body_html and "bereiten eine Ausschreibung" in p.body_text
    assert p.body_as_html() == p.body_html
    att = next(x for x in m if x.msg["Subject"].startswith("AW: AW: Anfrage")).msg
    assert [a.filename for a in parse(att.as_bytes()).attachments] == ["Dachplan_Halle3.pdf"]


def test_fetch_is_incremental_and_dedupes(env):
    app, s = env
    from importer.fetcher import fetch_all
    mails = seed.build_scenarios(None)
    seed.append_mails(s, mails)
    first = fetch_all(app.sessions, app.store, s)
    assert sum(st.fetched for st in first.values()) == 14 and first["archiv"].duplicates == 1
    again = fetch_all(app.sessions, app.store, s)
    assert sum(st.fetched + st.duplicates for st in again.values()) == 0
    # idempotent seeding
    assert seed.append_mails(s, mails) == {"archiv": 0, "vertrieb": 0}


def test_bulk_generates_threads():
    mails = seed.build_bulk(200)
    assert len(mails) == 200
    assert sum(1 for m in mails if m.msg["In-Reply-To"]) > 30


def classified(env):
    app, s = env
    from importer.fetcher import fetch_all
    seed.append_mails(s, seed.build_scenarios(None))
    fetch_all(app.sessions, app.store, s)
    app.classify(offline=True)
    with app.sessions() as sess:
        return {m.subject: m for m in sess.query(Mail).all() if not m.duplicate_of}


def test_rules_offline(env):
    by = classified(env)
    get = lambda part: next(m for k, m in by.items() if part in k)  # noqa: E731
    for part in ("Newsletter", "Abwesenheit", "Undelivered", "Wochenbericht"):
        assert get(part).status == Status.IGNORED, part
    web = get("Kontaktanfrage")
    assert (web.action, web.status) == ("new_lead", Status.APPROVED)
    assert web.options["extract"]["email"] == "m.wagner@wagner-elektro.de"
    reply = get("AW: AW: Anfrage")
    assert reply.action == "attach_to_lead" and reply.thread_parent_id and reply.status == Status.APPROVED
    first = get("Anfrage: Photovoltaik")
    assert first.action == "new_lead" and first.status == Status.PENDING  # needs human review


def test_web_review_flow(env):
    app, _ = env
    by = classified(env)
    c = TestClient(create_app(app), follow_redirects=False)
    assert c.get("/mails").status_code == 401
    c.headers["Authorization"] = "Basic dTpw"  # u:p
    assert c.get("/mails?status=pending").status_code == 200
    first = next(m for k, m in by.items() if k.startswith("Anfrage: Photovoltaik"))
    r = c.post(f"/mails/{first.id}", data={"submit": "approve", "action": "new_lead", "create_contact": "1",
                                           "lead_name": "PV Halle Ulm", "learn": "domain"})
    assert r.status_code == 303
    with app.sessions() as s:
        m = s.get(Mail, first.id)
        assert m.status == Status.APPROVED and m.decided_by == "user" and m.options["lead_name"] == "PV Halle Ulm"
    assert "PV Halle Ulm" in c.get(f"/mails/{first.id}").text
    assert "hoffmann-solar.de" in c.get("/rules").text  # gelernte Regel sichtbar
    # Sammelaktion: alle übrigen offenen ignorieren
    r = c.post("/mails/bulk", data={"op": "ignore", "scope": "filtered", "f_status": "pending"})
    assert r.status_code == 303
    with app.sessions() as s:
        assert s.query(Mail).filter(Mail.status == Status.PENDING).count() == 0


def test_db_connect_retries_then_fails(monkeypatch, caplog):
    from importer.models import make_session_factory
    monkeypatch.setenv("DB_CONNECT_RETRIES", "3")
    monkeypatch.setenv("DB_CONNECT_DELAY", "0")
    with caplog.at_level("WARNING"), pytest.raises(Exception):
        make_session_factory("postgresql+psycopg://u:p@does-not-exist.invalid:5432/x")
    msgs = [r.message for r in caplog.records if "nicht erreichbar" in r.message]
    assert len(msgs) == 2 and "does-not-exist.invalid:5432" in msgs[0]
