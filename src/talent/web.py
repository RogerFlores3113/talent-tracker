"""The app window: a local web UI on 127.0.0.1 opened in the user's browser."""
from __future__ import annotations

import secrets
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any
from urllib.parse import urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import __version__, actions, importers, integrations, recipes
from .paths import data_dir, db_path, resource_dir
from .store import (CANDIDATE_FIELDS, DEFAULT_SETTINGS, JOB_STATUSES, STAGE_OUTCOMES, TIMELINE_KINDS, NotFound,
                    Store, snippet_html, split_tags, to_dt)
from .typesense_index import SearchService, TypesenseIndex

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}
MAX_UPLOAD = 10 * 1024 * 1024
TABLE_SORTS = {
    "name": "lower(c.name)", "title": "lower(coalesce(c.current_title, ''))",
    "company": "lower(coalesce(c.current_company, ''))", "location": "lower(coalesce(c.location, ''))",
    "contacted": "coalesce(c.last_contacted_at, '')", "updated": "c.updated_at",
}


class CandidateIn(BaseModel):
    name: str | None = None
    email: str | None = None
    phone: str | None = None
    linkedin_url: str | None = None
    website: str | None = None
    current_title: str | None = None
    current_company: str | None = None
    location: str | None = None
    about: str | None = None
    tags: str | list[str] | None = None
    source: str | None = None
    last_contacted_at: str | None = None


def _ago(iso: str | None) -> str:
    dt = to_dt(iso)
    if not dt:
        return "never"
    days = (datetime.now(timezone.utc) - dt).days
    if days < 0:
        return "upcoming"
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


def _local(iso: str | None, fmt: str = "%a %b %-d, %-I:%M %p") -> str:
    dt = to_dt(iso)
    if not dt:
        return ""
    local = dt.astimezone()
    try:
        return local.strftime(fmt)
    except ValueError:  # Windows strftime has no %-d
        return local.strftime(fmt.replace("%-", "%#"))


def _history_from_text(text: str) -> list[dict[str, Any]]:
    """One role per line: Title | Company | Start | End."""
    out = []
    for line in (text or "").splitlines():
        if not line.strip():
            continue
        parts = [p.strip() for p in line.split("|")] + ["", "", "", ""]
        out.append({"title": parts[0], "company": parts[1], "start": parts[2], "end": parts[3]})
    return out


def _history_to_text(entries: list[dict[str, Any]]) -> str:
    return "\n".join(" | ".join(e.get(k) or "" for k in ("title", "company", "start", "end")).rstrip(" |")
                     for e in entries)


def create_app(store: Store, search: SearchService | None = None) -> FastAPI:
    search = search or SearchService(store, TypesenseIndex.from_env())
    app = FastAPI(title="Talent Search", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    res = resource_dir()
    templates = Jinja2Templates(directory=str(res / "templates"))
    env = templates.env
    env.filters["snippet"] = snippet_html
    env.filters["date"] = lambda iso: (iso or "")[:10]
    env.filters["ago"] = _ago
    env.filters["local"] = _local
    env.filters["claude_link"] = recipes.claude_link
    env.globals["version"] = __version__
    app.mount("/static", StaticFiles(directory=str(res / "static")), name="static")
    imports_dir = Path(tempfile.gettempdir()) / "talent-search-imports"

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The server only listens on 127.0.0.1, but a web page in the same browser
        # could still try to reach it. Block DNS-rebinding (foreign Host header)
        # and cross-site form posts (foreign Origin) before any handler runs.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if host not in LOCAL_HOSTS:
            return PlainTextResponse("Forbidden", status_code=403)
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin and origin != "null" and urlsplit(origin).hostname not in LOCAL_HOSTS:
                return PlainTextResponse("Forbidden", status_code=403)
            if request.headers.get("sec-fetch-site") == "cross-site":
                return PlainTextResponse("Forbidden", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        return response

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"engine": search.engine, **ctx}, status_code=status_code)

    def is_htmx(request: Request) -> bool:
        return bool(request.headers.get("HX-Request"))

    def back(request: Request, default: str = "/") -> RedirectResponse:
        ref = request.headers.get("referer") or ""
        target = urlsplit(ref)
        dest = (target.path + ("?" + target.query if target.query else "")) if target.hostname in LOCAL_HOSTS else default
        return RedirectResponse(dest or default, status_code=303)

    def cand_or_404(candidate_id: int) -> dict[str, Any]:
        try:
            return store.get_candidate(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")

    def job_or_404(job_id: int) -> dict[str, Any]:
        try:
            return store.get_job(job_id)
        except NotFound:
            raise HTTPException(404, "Job not found")

    # ------------------------------------------------------------ overview
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "", location: str = "", title: str = "", tag: str = ""):
        searching = bool(q or location or title or tag)
        result = search.search(q, location=location or None, title=title or None, tag=tag or None, limit=50)
        ctx = dict(result=result, q=q, location=location, title=title, tag=tag, total_all=store.count_candidates(),
                   searching=searching)
        if is_htmx(request) and request.headers.get("HX-Target") == "results":
            if not searching:
                return page(request, "_overview.html", **ctx, **overview_ctx())
            return page(request, "_results.html", **ctx)
        return page(request, "index.html", **ctx, **overview_ctx())

    def overview_ctx() -> dict[str, Any]:
        return {"today": actions.action_items(store), "jobs": store.list_jobs("open"),
                "recent": store.recent_candidates(6), "recipes_plan": recipes.plan_my_day()}

    @app.get("/search", response_class=HTMLResponse)
    def search_fragment(request: Request, q: str = "", location: str = "", title: str = "", tag: str = ""):
        result = search.search(q, location=location or None, title=title or None, tag=tag or None, limit=50)
        return page(request, "_results.html", result=result, q=q, total_all=store.count_candidates(), searching=True)

    # ------------------------------------------------------------ table
    @app.get("/candidates", response_class=HTMLResponse)
    def table(request: Request, q: str = "", tag: str = "", sort: str = "updated", dir: str = "desc",
              page_no: int = 1):
        per = 100
        sql_sort = TABLE_SORTS.get(sort, TABLE_SORTS["updated"])
        direction = "ASC" if dir == "asc" else "DESC"
        if q.strip():
            result = search.search(q, tag=tag or None, limit=per, offset=(page_no - 1) * per)
            rows, total = [h.candidate for h in result.hits], result.total
        else:
            where, params = "", []
            if tag:
                where, params = "WHERE c.id IN (SELECT candidate_id FROM candidate_tags WHERE tag = ?)", [tag]
            total = store._row(f"SELECT count(*) AS n FROM candidates c {where}", params)["n"]
            rows = [store._hydrate(r) for r in store._rows(
                f"SELECT c.* FROM candidates c {where} ORDER BY {sql_sort} {direction}, c.id DESC LIMIT ? OFFSET ?",
                (*params, per, (page_no - 1) * per))]
        return page(request, "table.html", rows=rows, total=total, q=q, tag=tag, sort=sort, dir=dir,
                    page_no=page_no, pages=max(1, (total + per - 1) // per), tags=store.list_tags(40))

    # -------------------------------------------------------- candidates
    @app.get("/candidates/new", response_class=HTMLResponse)
    def new_candidate(request: Request, job_id: int | None = None):
        return page(request, "candidate_form.html", c={}, history_text="", error=None, job_id=job_id)

    @app.post("/candidates", response_class=HTMLResponse)
    async def create_candidate_form(request: Request):
        form = await request.form()
        data = _form_fields(form)
        history = _history_from_text(str(form.get("work_history", "")))
        try:
            cand, created = store.upsert_candidate(work_history=history or None, **data)
            job_id = str(form.get("job_id") or "")
            if job_id.isdigit():
                store.add_to_pipeline(cand["id"], int(job_id))
                return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        except ValueError as exc:
            return page(request, "candidate_form.html", c=data, history_text=str(form.get("work_history", "")),
                        error=str(exc), job_id=form.get("job_id"))
        dest = f"/candidates/{cand['id']}" + ("" if created else "?merged=1")
        return RedirectResponse(dest, status_code=303)

    @app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
    def candidate_detail(request: Request, candidate_id: int, edit: bool = False, merged: bool = False):
        cand_or_404(candidate_id)
        c = store.candidate_detail(candidate_id)
        if edit:
            return page(request, "candidate_form.html", c=c, history_text=_history_to_text(c["work_history"]),
                        error=None, job_id=None)
        in_jobs = {p["job_id"] for p in c["pipelines"]}
        other_jobs = [j for j in store.list_jobs("open") if j["id"] not in in_jobs]
        stages = {p["job_id"]: store.job_stages(p["job_id"]) for p in c["pipelines"]}
        return page(request, "candidate.html", c=c, other_jobs=other_jobs, stages=stages, merged=merged,
                    kinds=[k for k in TIMELINE_KINDS if k != "stage"],
                    refresh_prompt=recipes.save_from_linkedin(c.get("linkedin_url") or c["name"]))

    @app.post("/candidates/{candidate_id}", response_class=HTMLResponse)
    async def update_candidate_form(request: Request, candidate_id: int):
        cand_or_404(candidate_id)
        form = await request.form()
        data = _form_fields(form, keep_blank=True)
        history = _history_from_text(str(form.get("work_history", ""))) if "work_history" in form else None
        try:
            store.update_candidate(candidate_id, work_history=history, **data)
        except ValueError as exc:
            return page(request, "candidate_form.html", c={"id": candidate_id, **data},
                        history_text=str(form.get("work_history", "")), error=str(exc), job_id=None)
        return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)

    @app.post("/candidates/{candidate_id}/delete")
    def delete_candidate_form(candidate_id: int):
        try:
            store.delete_candidate(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")
        return RedirectResponse("/?deleted=1", status_code=303)

    @app.post("/candidates/{candidate_id}/log", response_class=HTMLResponse)
    async def log_entry(request: Request, candidate_id: int):
        cand_or_404(candidate_id)
        form = await request.form()
        job = str(form.get("job_id") or "")
        try:
            store.log(candidate_id, str(form.get("kind") or "note"), str(form.get("body") or ""),
                      at=str(form.get("at") or "") or None, job_id=int(job) if job.isdigit() else None,
                      source="App")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        return RedirectResponse(f"/candidates/{candidate_id}#timeline", status_code=303)

    @app.post("/candidates/{candidate_id}/contacted")
    def mark_contacted(candidate_id: int):
        cand_or_404(candidate_id)
        store.log(candidate_id, "message", "Marked as contacted", source="App")
        return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)

    @app.post("/candidates/{candidate_id}/jobs")
    async def add_candidate_to_job(request: Request, candidate_id: int):
        cand_or_404(candidate_id)
        form = await request.form()
        job = str(form.get("job_id") or "")
        if job.isdigit():
            store.add_to_pipeline(candidate_id, int(job))
        return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)

    # ----------------------------------------------------------- paste
    @app.get("/paste", response_class=HTMLResponse)
    def paste_page(request: Request):
        return page(request, "paste.html", text="", prompt=recipes.save_from_text(""))

    @app.post("/paste", response_class=HTMLResponse)
    async def paste_preview(request: Request):
        form = await request.form()
        text = str(form.get("text") or "")
        if not text.strip():
            return page(request, "paste.html", text="", prompt=recipes.save_from_text(""),
                        error="Paste some profile or resume text first.")
        guess = importers.quick_parse(text)
        dupe = store.find_duplicate(guess.get("email"), guess.get("linkedin_url"))
        return page(request, "candidate_form.html", c=guess, history_text="", error=None, job_id=None,
                    from_paste=True, dupe=dupe, prompt=recipes.save_from_text(text))

    # ----------------------------------------------------------- csv
    @app.get("/import", response_class=HTMLResponse)
    def import_page(request: Request):
        return page(request, "import.html", step="upload")

    @app.post("/import", response_class=HTMLResponse)
    async def import_preview(request: Request):
        form = await request.form()
        upload = form.get("file")
        if upload is None or not getattr(upload, "filename", ""):
            return page(request, "import.html", step="upload", error="Choose a CSV file first.")
        data = await upload.read(MAX_UPLOAD + 1)
        if len(data) > MAX_UPLOAD:
            return page(request, "import.html", step="upload", error="That file is over 10 MB. Split it and try again.")
        try:
            header, rows = importers.read_csv(importers.decode(data))
        except ValueError as exc:
            return page(request, "import.html", step="upload", error=str(exc))
        imports_dir.mkdir(parents=True, exist_ok=True)
        token = secrets.token_hex(8)
        (imports_dir / f"{token}.csv").write_bytes(data)
        mapping = importers.guess_mapping(header)
        return page(request, "import.html", step="map", token=token, header=header, rows=rows[:5], total=len(rows),
                    mapping=mapping, targets=importers.IMPORT_TARGETS, filename=upload.filename,
                    has_name=any(m in ("name", "first_name", "last_name") for m in mapping))

    @app.post("/import/run", response_class=HTMLResponse)
    async def import_run(request: Request):
        form = await request.form()
        token = str(form.get("token") or "")
        path = imports_dir / f"{token}.csv"
        if not token.isalnum() or not path.exists():
            return page(request, "import.html", step="upload", error="That upload expired. Choose the file again.")
        header, rows = importers.read_csv(importers.decode(path.read_bytes()))
        mapping = [str(form.get(f"col{i}") or "") for i in range(len(header))]
        mapping = [m if m in importers.IMPORT_TARGETS else "" for m in mapping]
        if not any(m in ("name", "first_name", "last_name") for m in mapping):
            return page(request, "import.html", step="map", token=token, header=header, rows=rows[:5],
                        total=len(rows), mapping=mapping, targets=importers.IMPORT_TARGETS,
                        error="Pick which column holds the name.", has_name=False)
        result = importers.import_rows(store, header, rows, mapping, source=str(form.get("source") or "CSV import"))
        path.unlink(missing_ok=True)  # our own temporary copy of the upload
        return page(request, "import.html", step="done", result=result)

    @app.get("/export.csv")
    def export_csv():
        stamp = datetime.now().strftime("%Y-%m-%d")
        return Response(importers.export_csv(store), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="talent-search-{stamp}.csv"'})

    @app.get("/backup")
    def backup():
        stamp = datetime.now().strftime("%Y-%m-%d")
        tmp = Path(tempfile.mkstemp(suffix=".db")[1])
        store.backup_to(tmp)
        return FileResponse(tmp, filename=f"talent-search-backup-{stamp}.db", media_type="application/octet-stream",
                            background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))

    @app.post("/sample-data")
    def sample_data():
        from .sample_data import load_sample_data

        load_sample_data(store)
        return RedirectResponse("/?sample=1", status_code=303)

    # ------------------------------------------------------------ jobs
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, status: str = ""):
        jobs = store.list_jobs(status or None)
        return page(request, "jobs.html", jobs=jobs, status=status)

    @app.get("/jobs/new", response_class=HTMLResponse)
    def new_job(request: Request):
        return page(request, "job_form.html", j={}, error=None)

    @app.post("/jobs", response_class=HTMLResponse)
    async def create_job(request: Request):
        form = await request.form()
        data = {k: str(form.get(k) or "") for k in ("title", "company", "location", "description")}
        stages = [s for s in str(form.get("stages") or "").splitlines() if s.strip()]
        try:
            job = store.add_job(**data, stages=stages or None)
        except ValueError as exc:
            return page(request, "job_form.html", j=data, error=str(exc))
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int, view: str = "", q: str = ""):
        job = job_or_404(job_id)
        if view in ("board", "table"):
            store.set_settings(pipeline_view=view)
        view = view if view in ("board", "table") else store.setting("pipeline_view")
        add_results = []
        if q.strip():
            in_pipe = {a["candidate_id"] for a in store.pipeline(job_id)}
            add_results = [h.candidate for h in search.search(q, limit=8).hits if h.candidate["id"] not in in_pipe]
        ctx = dict(job=job, groups=store.pipeline_by_stage(job_id), stages=store.job_stages(job_id), view=view,
                   q=q, add_results=add_results, prompt=recipes.source_for_job(job))
        if is_htmx(request) and request.headers.get("HX-Target") == "add-results":
            return page(request, "_add_results.html", **ctx)
        return page(request, "job.html", **ctx)

    @app.post("/jobs/{job_id}", response_class=HTMLResponse)
    async def update_job(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        data = {k: str(form.get(k) or "") for k in ("title", "company", "location", "description", "status")
                if k in form}
        try:
            store.update_job(job_id, **data)
        except ValueError as exc:
            return page(request, "job_form.html", j={**store.get_job(job_id), **data}, error=str(exc))
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.get("/jobs/{job_id}/edit", response_class=HTMLResponse)
    def edit_job(request: Request, job_id: int):
        return page(request, "job_form.html", j=job_or_404(job_id), error=None, stages=store.job_stages(job_id),
                    outcomes=STAGE_OUTCOMES, statuses=JOB_STATUSES)

    @app.post("/jobs/{job_id}/stages", response_class=HTMLResponse)
    async def edit_stages(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        op = str(form.get("op") or "")
        name = str(form.get("stage") or "")
        try:
            if op == "add":
                store.add_stage(job_id, str(form.get("new_name") or ""), after=str(form.get("after") or "") or None)
            elif op == "rename":
                store.rename_stage(job_id, name, str(form.get("new_name") or ""))
            elif op in ("up", "down"):
                names = [s["name"] for s in store.job_stages(job_id)]
                i = [n.lower() for n in names].index(name.lower())
                j = i - 1 if op == "up" else i + 1
                if 0 <= j < len(names):
                    names[i], names[j] = names[j], names[i]
                    store.reorder_stages(job_id, names)
            elif op == "outcome":
                store.set_stage_outcome(job_id, name, str(form.get("outcome") or "open"))
            elif op == "remove":
                store.remove_empty_stage(job_id, name)
        except ValueError as exc:
            return page(request, "job_form.html", j=store.get_job(job_id), error=str(exc),
                        stages=store.job_stages(job_id), outcomes=STAGE_OUTCOMES, statuses=JOB_STATUSES)
        return RedirectResponse(f"/jobs/{job_id}/edit#stages", status_code=303)

    @app.post("/jobs/{job_id}/move", response_class=HTMLResponse)
    async def move(request: Request, job_id: int):
        job = job_or_404(job_id)
        form = await request.form()
        cid = str(form.get("candidate_id") or "")
        error = None
        try:
            store.move_stage(int(cid), job_id, str(form.get("stage") or ""))
        except (ValueError, NotFound) as exc:
            error = str(exc)
        if is_htmx(request):
            view = str(form.get("view") or store.setting("pipeline_view"))
            return page(request, "_pipeline.html", job=job, groups=store.pipeline_by_stage(job_id),
                        stages=store.job_stages(job_id), view=view, error=error)
        return back(request, f"/jobs/{job_id}")

    @app.post("/jobs/{job_id}/add")
    async def add_existing(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        cid = str(form.get("candidate_id") or "")
        if cid.isdigit():
            store.add_to_pipeline(int(cid), job_id)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    # ------------------------------------------------------- dashboard acts
    @app.post("/actions/snooze")
    async def snooze(request: Request):
        form = await request.form()
        key = str(form.get("key") or "")
        if key:
            store.snooze(key, int(str(form.get("days") or "7")))
        return back(request)

    @app.post("/actions/suggestions/{suggestion_id}/done")
    def suggestion_done(request: Request, suggestion_id: int):
        try:
            store.complete_suggestion(suggestion_id)
        except NotFound:
            raise HTTPException(404)
        return back(request)

    @app.post("/actions/advance")
    async def advance(request: Request):
        form = await request.form()
        try:
            store.move_stage(int(str(form.get("candidate_id"))), int(str(form.get("job_id"))), str(form.get("stage")))
        except (ValueError, NotFound, TypeError):
            pass
        return back(request)

    # ---------------------------------------------------------- settings etc
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: bool = False):
        return page(request, "settings.html", s=store.settings(), flags=actions.FLAGS, saved=saved,
                    data_folder=str(data_dir().resolve()), db_file=str(db_path().resolve()))

    @app.post("/settings")
    async def save_settings(request: Request):
        form = await request.form()
        values: dict[str, str] = {}
        for key, _, _ in actions.FLAGS:
            values[key] = "1" if form.get(key) else "0"
        for key in ("stuck_days", "thin_min", "stale_days"):
            raw = str(form.get(key) or "").strip()
            if raw.isdigit() and 0 < int(raw) < 10000:
                values[key] = raw
        if form.get("pipeline_view") in ("board", "table"):
            values["pipeline_view"] = str(form.get("pipeline_view"))
        store.set_settings(**values)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes_page(request: Request):
        items = [(r, recipes.render(r.key)) for r in recipes.RECIPES]
        return page(request, "recipes.html", items=items, jobs=store.list_jobs("open"),
                    job_prompts={j["id"]: recipes.source_for_job(j) for j in store.list_jobs("open")})

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return page(request, "privacy.html", data_folder=str(data_dir().resolve()))

    @app.get("/report", response_class=HTMLResponse)
    def report_page(request: Request, where: str = ""):
        return page(request, "report.html", link=None, where=where, env_line=recipes.environment_line())

    @app.post("/report", response_class=HTMLResponse)
    async def report_build(request: Request):
        form = await request.form()
        summary = str(form.get("summary") or "").strip()
        if not summary:
            return page(request, "report.html", link=None, error="Say in a few words what went wrong.",
                        env_line=recipes.environment_line(), where=str(form.get("where") or ""))
        link = recipes.issue_url(summary, str(form.get("details") or ""), where=str(form.get("where") or "app"))
        return page(request, "report.html", link=link, summary=summary, env_line=recipes.environment_line(),
                    where=str(form.get("where") or ""))

    # ----------------------------------------------------- AI app setup
    @app.get("/connect", response_class=HTMLResponse)
    def connect_page(request: Request):
        return page(
            request,
            "connect.html",
            claude_on=integrations.claude_connected(),
            codex_on=integrations.codex_connected(),
            lmstudio_on=integrations.lmstudio_connected(),
            json_snippet=integrations.generic_json_snippet(),
            codex_snippet=integrations.codex_snippet(),
        )

    @app.post("/connect/{target}", response_class=HTMLResponse)
    def connect(request: Request, target: str):
        fn = {"claude": integrations.connect_claude, "codex": integrations.connect_codex,
              "lmstudio": integrations.connect_lmstudio}.get(target)
        if fn is None:
            raise HTTPException(404)
        try:
            result = fn()
            return page(request, "_connect_result.html", ok=True, target=target, result=result)
        except (OSError, ValueError) as exc:
            return page(request, "_connect_result.html", ok=False, target=target, error=str(exc))

    # ------------------------------------------------------------ JSON
    @app.get("/api/health")
    def health():
        return {"ok": True, "version": __version__, "search": search.engine, "candidates": store.count_candidates()}

    @app.get("/api/candidates")
    def api_search(q: str = "", location: str | None = None, title: str | None = None, tag: str | None = None,
                   limit: int = 25, offset: int = 0):
        r = search.search(q, location=location, title=title, tag=tag, limit=max(1, min(limit, 200)),
                          offset=max(0, offset))
        return {"total": r.total, "engine": r.engine,
                "results": [{**h.candidate, "snippet": h.snippet} for h in r.hits]}

    @app.post("/api/candidates", status_code=201)
    def api_create(body: CandidateIn):
        try:
            cand, created = store.upsert_candidate(**body.model_dump(exclude_none=True))
        except ValueError as exc:
            raise HTTPException(422, str(exc))
        return JSONResponse({"created": created, "candidate": cand}, status_code=201 if created else 200)

    @app.get("/api/candidates/{candidate_id}")
    def api_get(candidate_id: int):
        try:
            return store.candidate_detail(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")

    @app.patch("/api/candidates/{candidate_id}")
    def api_update(candidate_id: int, body: CandidateIn):
        try:
            return store.update_candidate(candidate_id, **body.model_dump(exclude_unset=True))
        except NotFound:
            raise HTTPException(404, "Candidate not found")
        except ValueError as exc:
            raise HTTPException(422, str(exc))

    @app.delete("/api/candidates/{candidate_id}", status_code=204)
    def api_delete(candidate_id: int):
        try:
            store.delete_candidate(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")

    @app.get("/api/today")
    def api_today():
        return actions.action_items(store)

    @app.exception_handler(404)
    async def not_found(request: Request, exc):
        if request.url.path.startswith("/api/"):
            return JSONResponse({"detail": getattr(exc, "detail", "Not found")}, status_code=404)
        detail = getattr(exc, "detail", None)
        return page(request, "message.html", status_code=404, title="Not found",
                    message=detail if detail and detail != "Not Found" else "That page doesn't exist.")

    return app


def _form_fields(form: Any, keep_blank: bool = False) -> dict[str, Any]:
    out = {}
    for f in CANDIDATE_FIELDS:
        if f in form:
            v = str(form[f])
            if v.strip() or keep_blank:
                out[f] = v
    return out


__all__ = ["create_app", "DEFAULT_SETTINGS", "split_tags"]
