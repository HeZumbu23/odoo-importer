"""Browser test with Playwright (skipped if playwright/chromium are not installed). Uses only data-testid hooks."""
import os
import threading

import pytest

pytest.importorskip("playwright.sync_api")
import uvicorn  # noqa: E402
from playwright.sync_api import sync_playwright  # noqa: E402

from importer.fetcher import fetch_all  # noqa: E402
from importer.web import create_app  # noqa: E402
from test_core import classified, env  # noqa: E402,F401


def test_review_in_browser(env):  # noqa: F811
    app, s = env
    classified(env)
    server = uvicorn.Server(uvicorn.Config(create_app(app), port=0, log_level="warning"))
    t = threading.Thread(target=server.run, daemon=True)
    t.start()
    while not server.started:
        pass
    port = server.servers[0].sockets[0].getsockname()[1]
    exe = os.environ.get("CHROMIUM", "/opt/pw-browsers/chromium")
    with sync_playwright() as p:
        b = p.chromium.launch(executable_path=exe if os.path.exists(exe) else None, args=["--no-sandbox"])
        pg = b.new_context(http_credentials={"username": "u", "password": "p"}).new_page()
        pg.goto(f"http://127.0.0.1:{port}/mails?status=pending")
        rows = pg.get_by_test_id("mail-row")
        assert rows.count() >= 1
        rows.first.get_by_test_id("mail-link").click()
        pg.get_by_test_id("action-ignore").check()
        pg.get_by_test_id("submit-ignore").click()
        pg.wait_for_load_state()
        assert pg.get_by_test_id("job-fetch").is_visible()
        pg.goto(f"http://127.0.0.1:{port}/mails?status=ignored")
        assert pg.get_by_test_id("mail-row").count() >= 5
        b.close()
    server.should_exit = True
