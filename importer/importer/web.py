"""Review UI (FastAPI + Jinja2, no JavaScript framework)."""
from __future__ import annotations

import logging
import secrets
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlencode

from fastapi import APIRouter, Depends, FastAPI, Form, HTTPException, Request
from fastapi.responses import JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.security import HTTPBasic, HTTPBasicCredentials
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from sqlalchemy import func, or_, select

from .jobs import App
from .models import Action, JobLog, LearnedRule, Mail, Status

log = logging.getLogger(__name__)
HERE = Path(__file__).parent
PAGE_SIZE = 50


def create_app(app_state: App | None = None) -> FastAPI:
    state = app_state or App()
    security = HTTPBasic()

    def auth(creds: HTTPBasicCredentials = Depends(security)):
        ok = secrets.compare_digest(creds.username.encode(), state.settings.ui_user.encode()) and \
            secrets.compare_digest(creds.password.encode(), state.settings.ui_password.encode())
        if not ok:
            raise HTTPException(401, "Anmeldung erforderlich", headers={"WWW-Authenticate": "Basic"})

    @asynccontextmanager
    async def lifespan(_app):
        state.start_worker()
        yield

    api = FastAPI(title="Odoo Mail Importer", docs_url=None, redoc_url=None, lifespan=lifespan)
    api.mount("/static", StaticFiles(directory=HERE / "static"), name="static")
    templates = Jinja2Templates(directory=HERE / "templates")
    templates.env.globals.update(Status=Status, Action=Action, odoo_url=state.settings.odoo_public_url)
    ui = APIRouter(dependencies=[Depends(auth)])

    @api.get("/healthz", include_in_schema=False)
    def healthz():
        return {"ok": True, "job": state.running}

    def render(request: Request, name: str, **ctx):
        with state.sessions() as s:
            jobs = list(s.scalars(select(JobLog).order_by(JobLog.id.desc()).limit(5)))
        return templates.TemplateResponse(request, name, {"running": state.running, "jobs": jobs, **ctx})

    def filtered_query(f: dict):
        q = select(Mail)
        if f.get("status"):
            q = q.where(Mail.status == f["status"])
        if f.get("action"):
            q = q.where(Mail.action == f["action"])
        if f.get("rule"):
            q = q.where(Mail.rule == f["rule"])
        if f.get("account"):
            q = q.where(Mail.account == f["account"])
        if f.get("q"):
            like = f"%{f['q']}%"
            q = q.where(or_(Mail.subject.ilike(like), Mail.from_email.ilike(like),
                            Mail.counterpart_email.ilike(like), Mail.from_name.ilike(like)))
        return q

    FILTER_KEYS = ("status", "action", "rule", "account", "q")
    SORT_COLUMNS = {
        "id": Mail.id, "date": Mail.date, "contact": Mail.counterpart_email, "subject": Mail.subject,
        "action": Mail.action, "rule": Mail.rule, "status": Mail.status,
    }

    @ui.get("/")
    def index():
        return RedirectResponse("/mails?status=pending", 303)

    @ui.get("/mails")
    def mails(request: Request, page: int = 1, sort: str = "date", dir: str = "asc"):
        f = {k: request.query_params.get(k, "") for k in FILTER_KEYS}
        sort = sort if sort in SORT_COLUMNS else "date"
        dir = "desc" if dir == "desc" else "asc"
        col = SORT_COLUMNS[sort]
        order = [col.desc().nulls_last() if dir == "desc" else col.asc().nulls_first(), Mail.id.asc()]
        with state.sessions() as s:
            q = filtered_query(f)
            total = s.scalar(select(func.count()).select_from(q.subquery()))
            rows = list(s.scalars(q.order_by(*order).offset((page - 1) * PAGE_SIZE).limit(PAGE_SIZE)))
            counts = dict(s.execute(select(Mail.status, func.count()).group_by(Mail.status)).all())
            aq = select(Mail.action, func.count()).group_by(Mail.action)
            if f.get("status"):
                aq = aq.where(Mail.status == f["status"])
            action_counts = dict(s.execute(aq).all())
            rq = select(Mail.rule, func.count()).where(Mail.rule.is_not(None)).group_by(Mail.rule).order_by(Mail.rule)
            if f.get("status"):
                rq = rq.where(Mail.status == f["status"])
            if f.get("action"):
                rq = rq.where(Mail.action == f["action"])
            rule_counts = dict(s.execute(rq).all())
            rules = [r for (r,) in s.execute(select(Mail.rule).where(Mail.rule.is_not(None)).distinct().order_by(Mail.rule))]
            accounts = [a for (a,) in s.execute(select(Mail.account).distinct().order_by(Mail.account))]
        pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
        qs = urlencode({k: v for k, v in f.items() if v})
        qs_sort = urlencode({"sort": sort, "dir": dir})
        return render(request, "mails.html", sort=sort, dir=dir, rule_counts=rule_counts,
                      qs_sort=qs_sort, mails=rows, f=f, total=total, page=page, pages=pages, qs=qs,
                      counts=counts, action_counts=action_counts, rules=rules, accounts=accounts)

    @ui.post("/mails/bulk")
    async def bulk(request: Request):
        form = await request.form()
        op = form.get("op")
        f = {k: form.get(f"f_{k}", "") for k in FILTER_KEYS}
        with state.sessions() as s:
            if form.get("scope") == "filtered":
                targets = list(s.scalars(filtered_query(f)))
            else:
                ids = [int(i) for i in form.getlist("ids")]
                targets = list(s.scalars(select(Mail).where(Mail.id.in_(ids)))) if ids else []
            targets = [m for m in targets if m.status != Status.DONE]
            reclassify = []
            for m in targets:
                if op == "approve":
                    if m.action and m.action != Action.REVIEW:
                        m.status = Status.IGNORED if m.action == Action.IGNORE else Status.APPROVED
                        m.decided_by = "user"
                elif op == "ignore":
                    m.action, m.status, m.decided_by = Action.IGNORE, Status.IGNORED, "user"
                elif op == "to_review":
                    m.status = Status.PENDING
                elif op == "set_action" and form.get("action") in Action.ALL:
                    m.action, m.status, m.decided_by = form["action"], Status.PENDING, "user"
                    if m.action == Action.ATTACH and form.get("target_lead_id"):
                        m.target_lead_id = int(form["target_lead_id"])
                        m.thread_parent_id = None
                elif op == "reclassify":
                    m.status, m.decided_by = Status.NEW, None
                    reclassify.append(m.id)
            s.commit()
        if reclassify:
            state.run_in_background("classify", state.classify, ids=reclassify)
        back = urlencode({k: v for k, v in f.items() if v})
        return RedirectResponse(f"/mails?{back}", 303)

    @ui.get("/mails/{mail_id}")
    def mail_detail(request: Request, mail_id: int):
        with state.sessions() as s:
            mail = s.get(Mail, mail_id)
            if mail is None:
                raise HTTPException(404)
            parent = s.get(Mail, mail.thread_parent_id) if mail.thread_parent_id else None
            children = list(s.scalars(select(Mail).where(Mail.thread_parent_id == mail.id)))
            same_sender = list(s.scalars(select(Mail).where(
                Mail.counterpart_email == mail.counterpart_email, Mail.id != mail.id)
                .order_by(Mail.date.desc()).limit(10))) if mail.counterpart_email else []
        suggestions, odoo_error = [], None
        if mail.counterpart_email and mail.status != Status.DONE:
            try:
                suggestions = state.odoo().leads_for_email(mail.counterpart_email)
            except Exception as exc:  # noqa: BLE001
                odoo_error = str(exc)
        with state.sessions() as s:
            nxt = next_pending(s, mail)
            pending_total = s.scalar(select(func.count()).select_from(Mail).where(Mail.status == Status.PENDING))
        return render(request, "mail.html", mail=mail, parent=parent, next_id=nxt, pending_total=pending_total, children=children,
                      same_sender=same_sender, suggestions=suggestions, odoo_error=odoo_error,
                      back=request.headers.get("referer") or "/mails")

    @ui.get("/api/leads")
    def api_leads(q: str = ""):
        try:
            rows = state.odoo().search_leads(q)
        except Exception as exc:  # noqa: BLE001
            return JSONResponse({"error": str(exc), "leads": []}, status_code=502)
        leads = [{
            "id": r["id"], "name": r["name"], "stage": r["stage_id"][1] if r.get("stage_id") else "",
            "partner": r["partner_id"][1] if r.get("partner_id") else "", "email": r.get("email_from") or "",
            "active": r.get("active", True), "won": (r.get("probability") or 0) >= 100,
        } for r in rows]
        return {"leads": leads}

    def next_pending(s, mail: Mail) -> int | None:
        """Next mail to review in list order (oldest first), after the given one."""
        q = select(Mail.id).where(Mail.status == Status.PENDING, Mail.id != mail.id)
        later = q.where((Mail.date > mail.date) | ((Mail.date == mail.date) & (Mail.id > mail.id))) \
            if mail.date else q.where(Mail.id > mail.id)
        return s.scalar(later.order_by(Mail.date.asc(), Mail.id).limit(1)) or \
            s.scalar(q.order_by(Mail.date.asc().nulls_first(), Mail.id).limit(1))

    @ui.get("/mails/{mail_id}/raw")
    def mail_raw(mail_id: int):
        with state.sessions() as s:
            mail = s.get(Mail, mail_id)
            path = mail.raw_path if mail else None
            if mail and not path and mail.duplicate_of:
                path = s.get(Mail, mail.duplicate_of).raw_path
        if not path:
            raise HTTPException(404)
        return Response(state.store.load(path), media_type="message/rfc822",
                        headers={"Content-Disposition": f'attachment; filename="mail-{mail_id}.eml"'})

    @ui.post("/mails/{mail_id}")
    def mail_update(
        mail_id: int,
        submit: str = Form("save"),
        action: str = Form(...),
        target_lead_id: str = Form(""),
        lead_name: str = Form(""),
        create_contact: str = Form(""),
        contact_email: str = Form(""),
        contact_name: str = Form(""),
        partner_name: str = Form(""),
        learn: str = Form(""),
        learn_auto: str = Form(""),
    ):
        with state.sessions() as s:
            mail = s.get(Mail, mail_id)
            if mail is None:
                raise HTTPException(404)
            if mail.status == Status.DONE:
                return RedirectResponse(f"/mails/{mail_id}", 303)
            if action not in Action.ALL:
                raise HTTPException(400, "unbekannte Aktion")
            opts = dict(mail.options or {})
            opts["create_contact"] = bool(create_contact)
            opts["lead_name"] = lead_name.strip() or None
            ex = dict(opts.get("extract") or {})
            for key, val in (("email", contact_email), ("contact_name", contact_name), ("partner_name", partner_name)):
                if val.strip():
                    ex[key] = val.strip()
                else:
                    ex.pop(key, None)
            opts["extract"] = ex
            mail.options = opts
            if action != mail.action or target_lead_id.strip():
                mail.target_lead_id = int(target_lead_id) if target_lead_id.strip().isdigit() else None
                if mail.target_lead_id:
                    mail.thread_parent_id = None
            mail.action, mail.decided_by, mail.error = action, "user", None
            if submit in ("approve", "apply_now") and action != Action.REVIEW:
                mail.status = Status.IGNORED if action == Action.IGNORE else Status.APPROVED
            elif submit == "ignore":
                mail.action, mail.status = Action.IGNORE, Status.IGNORED
            else:
                mail.status = Status.PENDING
            if learn in ("email", "domain") and mail.counterpart_email:
                pattern = mail.counterpart_email.lower() if learn == "email" else mail.counterpart_domain
                s.add(LearnedRule(kind=learn, pattern=pattern, action=mail.action, auto=bool(learn_auto),
                                  note=f"aus Mail #{mail.id}"))
            s.commit()
            nxt = next_pending(s, mail) if submit != "save" else None
        if submit == "apply_now":
            state.run_job("apply", state.apply, ids=[mail_id])
        return RedirectResponse(f"/mails/{nxt}" if nxt else f"/mails/{mail_id}" if submit == "save" else "/mails?status=pending", 303)

    @ui.post("/jobs/{name}")
    def start_job(request: Request, name: str):
        jobs = {
            "fetch": state.fetch,
            "classify": state.classify,
            "apply": state.apply,
            "cycle": state.cycle,
            "reclassify": lambda: state.classify(statuses=(Status.PENDING, Status.ERROR)),
        }
        if name not in jobs:
            raise HTTPException(404)
        state.run_in_background(name, jobs[name])
        return RedirectResponse(request.headers.get("referer") or "/mails", 303)

    @ui.get("/rules")
    def rules_page(request: Request):
        error, ruleset = None, None
        try:
            ruleset = state.ruleset()
        except Exception as exc:  # noqa: BLE001
            error = str(exc)
        with state.sessions() as s:
            learned = list(s.scalars(select(LearnedRule).order_by(LearnedRule.id)))
            stats = dict(s.execute(select(Mail.rule, func.count()).group_by(Mail.rule)).all())
        try:
            source = state.settings.rules_file.read_text(encoding="utf-8")
        except OSError:
            source = ""
        return render(request, "rules.html", ruleset=ruleset, learned=learned, error=error, source=source,
                      stats=stats, settings=state.settings)

    @ui.post("/rules/learned/{rule_id}/delete")
    def delete_learned(rule_id: int):
        with state.sessions() as s:
            rule = s.get(LearnedRule, rule_id)
            if rule:
                s.delete(rule)
                s.commit()
        return RedirectResponse("/rules", 303)

    @ui.get("/jobs")
    def jobs_page(request: Request):
        with state.sessions() as s:
            logs = list(s.scalars(select(JobLog).order_by(JobLog.id.desc()).limit(100)))
        return render(request, "jobs.html", logs=logs)

    @api.get("/favicon.ico", include_in_schema=False)
    def favicon():
        return Response(status_code=204)

    @ui.get("/robots.txt", include_in_schema=False)
    def robots():
        return PlainTextResponse("User-agent: *\nDisallow: /\n")

    api.include_router(ui)
    return api
