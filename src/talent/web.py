"""The app window: a local web UI on 127.0.0.1 opened in the user's browser."""
from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import secrets
import tempfile
import time
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import __version__, actions, importers, integrations, paths, recipes
from .sample_data import load_sample_data, remove_sample_data, sample_ids
from .paths import backups_dir, data_dir, db_path, imports_dir, resource_dir
from .store import (CANDIDATE_FIELDS, CHANGE_KIND, CHANGE_SOURCES, DEFAULT_SETTINGS, FIELD_LABELS,
                    INTERVIEW_OUTCOME_LABELS, JOB_EXTRA_FIELDS, JOB_STATUSES, STAGE_OUTCOMES, TIMELINE_KINDS, NotFound,
                    Store, _labels, dropped_text, snippet_html, split_tags, to_dt)
from .typesense_index import SearchService, TypesenseIndex, env_status

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MAX_UPLOAD = 10 * 1024 * 1024
ONE_SHOT_PARAMS = {"notice", "flash_error", "error", "saved", "sample", "deleted", "merged"}  # messages, not state
UPLOAD_TTL = 3600  # seconds an upload waits between column matching and the import
PREVIEW_MAX_ROWS = 20000  # bigger files import without the "what will change" preview
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DOWNLOADS = {"/backup", "/export.csv"}  # whole-database downloads: never for another site's page
OPEN_PATHS = {"/launch", "/api/health"}  # everything else needs the app window's key
KEY_HEADER = "x-talent-key"
KEY_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetimes at 400 days
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
LOCKED = "Open Talent Search from its app icon (or run talent open) to unlock this window."
CHANGE_WHO = {"ai": "Your AI app", "import": "CSV import", "app": "You, in the app"}
CHANGE_SINCE = {"1": "Last 24 hours", "7": "Last 7 days", "30": "Last 30 days"}
CHANGES_SHOWN = 300
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


def _shown(field: str, value: Any) -> str:
    if value in (None, "", []):
        return ""
    if field == "work_history":
        n = len(value) if isinstance(value, list) else 0
        return f"{n} role{'' if n == 1 else 's'}"
    if field == "last_contacted_at":
        return str(value)[:10]
    text = re.sub(r"\s+", " ", str(value)).strip()
    return text if len(text) <= 60 else text[:57].rstrip() + "..."


def change_lines(data: Any) -> list[str]:
    """A change entry's old and new values: ['Company: Acme to Initech', 'Cleared email (was ana@x.com)']."""
    if not isinstance(data, dict) or not isinstance(data.get("new"), dict):
        return []
    old = data.get("old") if isinstance(data.get("old"), dict) else {}
    out = []
    for k, v in data["new"].items():
        label = FIELD_LABELS.get(k, k.replace("_", " "))
        cap = label[:1].upper() + label[1:]
        was, now = _shown(k, old.get(k)), _shown(k, v)
        if not now:
            out.append(f"Cleared {label}" + (f" (was {was})" if was else ""))
        elif not was:
            out.append(f"{cap}: {now} (was empty)")
        else:
            out.append(f"{cap}: {was} to {now}")
    return out


def profile_label(url: str | None) -> str:
    """'LinkedIn' only for a linkedin.com address (or a subdomain of it), else 'Profile'."""
    host = (urlsplit(url or "").hostname or "").lower()
    return "LinkedIn" if host == "linkedin.com" or host.endswith(".linkedin.com") else "Profile"


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


def key_cookie_name(key: str) -> str:
    """Cookies are shared by every port on 127.0.0.1, so each install's cookie gets its own name."""
    return "talent_key_" + hashlib.sha256(key.encode()).hexdigest()[:10]


def safe_local_path(path: str) -> bool:
    """A path on this server: not '//host', '/\\host' or anything a browser could read as another site."""
    return (path.startswith("/") and not path.startswith(("//", "/\\")) and "\\" not in path
            and not any(ch.isspace() or ord(ch) < 32 for ch in path))


def create_app(store: Store, search: SearchService | None = None, key: str | None = None) -> FastAPI:
    search = search or SearchService(store, TypesenseIndex.from_env())
    key = key or paths.app_key()
    cookie_name = key_cookie_name(key)
    app = FastAPI(title="Talent Search", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    res = resource_dir()
    templates = Jinja2Templates(directory=str(res / "templates"))
    env = templates.env
    env.filters["snippet"] = snippet_html
    env.filters["date"] = lambda iso: (iso or "")[:10]
    env.filters["ago"] = _ago
    env.filters["local"] = _local
    env.filters["claude_link"] = recipes.claude_link
    env.filters["change_lines"] = change_lines
    env.filters["profile_label"] = profile_label
    env.globals["version"] = __version__
    env.globals["sample_ids"] = lambda: sample_ids(store)  # templates badge sample people and jobs
    env.globals["ai_status_note"] = lambda job_id: ai_status_note(store, job_id)
    env.globals["job_label"] = actions.job_label
    env.globals["interview_outcomes"] = INTERVIEW_OUTCOME_LABELS
    app.mount("/static", StaticFiles(directory=str(res / "static")), name="static")

    @app.middleware("http")
    async def local_only(request: Request, call_next):
        # The server only listens on 127.0.0.1, but a web page in the same browser
        # could still try to reach it. Block DNS-rebinding (foreign Host header)
        # and cross-site requests (CSRF) before any handler runs.
        host_header = (request.headers.get("host") or "").strip().lower()
        if _hostname(host_header) not in LOCAL_HOSTS:
            return PlainTextResponse("Forbidden", status_code=403)
        request.state.unlocked = has_key(request)
        path = request.url.path
        if not request.state.unlocked and path not in OPEN_PATHS and not path.startswith("/static/"):
            return locked(request)
        fetch_site = (request.headers.get("sec-fetch-site") or "").lower()
        if request.method in UNSAFE_METHODS:
            origin = request.headers.get("origin")
            if origin is not None:
                # Sandboxed frames, file:// pages and some redirects send "null": never ours.
                # Otherwise the origin must be exactly this server, port included.
                if origin.strip().lower() == "null" or urlsplit(origin.strip().lower()).netloc != host_header:
                    return PlainTextResponse("Forbidden", status_code=403)
            elif fetch_site and fetch_site not in ("same-origin", "none"):
                return PlainTextResponse("Forbidden", status_code=403)
            # With neither header (curl, scripts, very old browsers) we allow it: browsers
            # always send at least one of them on a cross-site request, so this can't be CSRF.
        elif request.url.path in DOWNLOADS and fetch_site in ("cross-site", "same-site"):
            return PlainTextResponse("Forbidden", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response

    def has_key(request: Request) -> bool:
        given = request.cookies.get(cookie_name) or request.headers.get(KEY_HEADER) or ""
        return hmac.compare_digest(given.encode(), key.encode())

    def locked(request: Request) -> Response:
        if request.url.path.startswith("/api/"):
            resp: Response = JSONResponse({"detail": LOCKED}, status_code=401)
        else:
            resp = page(request, "message.html", status_code=401, title="This window is locked", message=LOCKED)
        for k, v in (("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff"),
                     ("Referrer-Policy", "same-origin"), ("Content-Security-Policy", CSP)):
            resp.headers[k] = v
        return resp

    @app.get("/launch")
    def launch(request: Request, code: str = ""):
        """The app icon and `talent open` come here with a one-time code; it leaves a cookie that unlocks the window."""
        if not (request.state.unlocked or paths.use_launch_code(code)):
            return locked(request)
        resp = RedirectResponse("/", status_code=303)
        resp.set_cookie(cookie_name, key, max_age=KEY_MAX_AGE, httponly=True, samesite="strict", path="/")
        return resp

    def page(request: Request, name: str, status_code: int = 200, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"engine": search.engine, **ctx}, status_code=status_code)

    def is_htmx(request: Request) -> bool:
        return bool(request.headers.get("HX-Request"))

    def back(request: Request, default: str = "/", notice: str = "", error: str = "") -> RedirectResponse:
        """Redirect to the page the form was on, with an optional one-line message (shown by base.html)."""
        ref = request.headers.get("referer") or ""
        host = (request.headers.get("host") or "").lower()
        try:  # only a page of this very server, never '//other.site' or another port
            target = urlsplit(ref)
            ours = target.scheme in ("http", "https") and target.netloc.lower() == host and safe_local_path(target.path)
        except ValueError:
            ours = False
        path, query = (target.path, target.query) if ours else (urlsplit(default).path, urlsplit(default).query)
        params = [(k, v) for k, v in parse_qsl(query) if k not in ONE_SHOT_PARAMS]
        params += [(k, v) for k, v in (("notice", notice), ("flash_error", error)) if v]
        return RedirectResponse(path + ("?" + urlencode(params) if params else ""), status_code=303)

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

    def _todos(job_id: int) -> list[dict[str, Any]]:
        today = datetime.now().astimezone().date().isoformat()
        return [{**s, "due_text": actions.due_text(s.get("due_on"), today),
                 "overdue": bool(s.get("due_on") and s["due_on"] < today)} for s in store.open_suggestions(job_id)]

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
        people, sample_jobs = sample_ids(store)
        return {"today": actions.action_items(store), "jobs": store.list_jobs("open"),
                "recent": store.recent_candidates(6), "recipes_plan": recipes.plan_my_day(),
                "real_count": store.count_candidates() - len(people), "has_sample": bool(people or sample_jobs)}

    @app.get("/search", response_class=HTMLResponse)
    def search_fragment(request: Request, q: str = "", location: str = "", title: str = "", tag: str = "",
                        offset: int = 0):
        offset = max(0, offset)
        result = search.search(q, location=location or None, title=title or None, tag=tag or None, limit=50,
                               offset=offset)
        ctx = dict(result=result, q=q, location=location, title=title, tag=tag, offset=offset,
                   total_all=store.count_candidates(), searching=True)
        # "Show more" asks for the next cards only, which replace the button.
        return page(request, "_result_cards.html" if offset else "_results.html", **ctx)

    # ------------------------------------------------------------ table
    @app.get("/candidates", response_class=HTMLResponse)
    def table(request: Request, q: str = "", tag: str = "", location: str = "", source: str = "",
              sort: str = "updated", dir: str = "desc", page_no: int = 1, more: int = 0):
        per = 100
        page_no = max(1, page_no)
        sql_sort = TABLE_SORTS.get(sort, TABLE_SORTS["updated"])
        direction = "ASC" if dir == "asc" else "DESC"
        suggestion = None
        if q.strip():
            result = search.search(q, tag=tag or None, location=location or None, source=source or None,
                                   limit=per, offset=(page_no - 1) * per)
            rows, total = [h.candidate for h in result.hits], result.total
            suggestion = result.suggestion
        else:
            conds, params, _ = store.candidate_filters(tag=tag or None, location=location or None,
                                                       source=source or None)
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            total = store._row(f"SELECT count(*) AS n FROM candidates c {where}", params)["n"]
            rows = [store._hydrate(r) for r in store._rows(
                f"SELECT c.* FROM candidates c {where} ORDER BY {sql_sort} {direction}, c.id DESC LIMIT ? OFFSET ?",
                (*params, per, (page_no - 1) * per))]
        ctx = dict(rows=rows, total=total, q=q, tag=tag, location=location, source=source, sort=sort, dir=dir,
                   page_no=page_no, pages=max(1, (total + per - 1) // per), shown=min(total, page_no * per),
                   suggestion=suggestion, corrected=bool(suggestion and result.corrected))
        if more and is_htmx(request):  # "Show more": just the next rows, which replace the button's row
            return page(request, "_table_rows.html", **ctx)
        return page(request, "table.html", **ctx, tags=store.list_tags(40), sources=store.list_sources())

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
        open_jobs = store.list_jobs("open")
        hints = actions.twin_hints(open_jobs)
        other_jobs = [{**j, "hint": hints.get(j["id"])} for j in open_jobs if j["id"] not in in_jobs]
        stages = {p["job_id"]: store.job_stages(p["job_id"]) for p in c["pipelines"]}
        return page(request, "candidate.html", c=c, other_jobs=other_jobs, stages=stages, merged=merged,
                    dupes=store.possible_duplicates(candidate_id),
                    kinds=[k for k in TIMELINE_KINDS if k != "stage"],
                    refresh_prompt=recipes.refresh_candidate(c))

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
        c = cand_or_404(candidate_id)
        try:
            importers.backup_before_import(store, reason="delete")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'flash_error': str(exc)})}",
                                    status_code=303)
        try:
            store.delete_candidate(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")
        return RedirectResponse("/?" + urlencode({"notice": f"Deleted {c['name']}. This can't be undone."}),
                                status_code=303)

    @app.get("/candidates/{candidate_id}/merge/{other_id}", response_class=HTMLResponse)
    def merge_confirm(request: Request, candidate_id: int, other_id: int):
        """App window only: look at two records side by side before merging them."""
        cand_or_404(candidate_id)
        cand_or_404(other_id)
        if candidate_id == other_id:
            raise HTTPException(404, "Pick two different people")
        return page(request, "merge.html", keep=store.candidate_detail(candidate_id, timeline_limit=5),
                    other=store.candidate_detail(other_id, timeline_limit=5))

    @app.post("/candidates/{candidate_id}/merge/{other_id}")
    def merge_form(candidate_id: int, other_id: int):
        """App window only. The AI has no merge tool: merging deletes the other record."""
        cand_or_404(candidate_id)
        other = cand_or_404(other_id)
        try:
            importers.backup_before_import(store, reason="merge")
            cand = store.merge_candidates(candidate_id, other_id, by="app")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'flash_error': str(exc)})}",
                                    status_code=303)
        m = cand["_merged"]
        bits = [f"filled {_labels(m['filled'])}"] if m["filled"] else []
        if m["timeline"]:
            bits.append(f"moved {m['timeline']} timeline entr{'y' if m['timeline'] == 1 else 'ies'}")
        if m["jobs"]:
            bits.append("moved " + ", ".join(m["jobs"]))
        notice = (f"Merged {other['name']} (id {other_id}) into {cand['name']} (id {candidate_id})"
                  + (": " + "; ".join(bits) if bits else "") + ".")
        if m.get("dropped"):
            notice += (" Kept this profile's values where both had one. The other profile had "
                       + dropped_text(m["dropped"], 120) + ". This is noted on the timeline.")
        notice += " A backup was saved first."
        return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'notice': notice})}", status_code=303)

    @app.post("/candidates/{candidate_id}/log", response_class=HTMLResponse)
    async def log_entry(request: Request, candidate_id: int):
        cand_or_404(candidate_id)
        form = await request.form()
        job = str(form.get("job_id") or "")
        job_id = int(job) if job.isdigit() else None
        kind, body = str(form.get("kind") or "note"), str(form.get("body") or "")
        follow_up = str(form.get("follow_up") or "").strip()
        notice = ""
        day, day_error = None, ""
        if follow_up:
            try:
                day = actions.parse_day(follow_up)
            except ValueError as exc:
                day_error = str(exc)
        logged = False
        try:
            if body.strip() or kind != "note" or not (day or day_error):  # a follow-up date alone needs no note
                store.log(candidate_id, kind, body, at=str(form.get("at") or "") or None, job_id=job_id, source="App")
                logged = True
            if day:
                actions.schedule_followup(store, candidate_id, day, job_id=job_id, note=body.strip() or None)
                notice = f"Follow-up added. It shows on Today from {actions.local_time(day.isoformat() + 'T12:00:00', '%a %b %-d')}."
        except (ValueError, NotFound) as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        if day_error:  # keep what they wrote, and say what was wrong with the date
            msg = ("Saved, but the follow-up wasn't added. " if logged else "The follow-up wasn't added. ") + day_error
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': msg})}#timeline", status_code=303)
        return RedirectResponse(f"/candidates/{candidate_id}{'?' + urlencode({'notice': notice}) if notice else ''}#timeline",
                                status_code=303)

    @app.post("/candidates/{candidate_id}/interviews/{entry_id}")
    async def interview_form(request: Request, candidate_id: int, entry_id: int):
        """Reschedule, cancel, restore or record the outcome of an interview. Nothing is deleted."""
        cand_or_404(candidate_id)
        form = await request.form()
        op = str(form.get("op") or "")
        try:
            if op == "reschedule":
                at = str(form.get("at") or "").strip()
                if not at:
                    raise ValueError("Pick the new date and time.")
                reason = str(form.get("reason") or "").strip()
                iv = store.update_interview(entry_id, at=at, reason=reason or None, candidate_id=candidate_id)
                notice = f"Moved the interview to {_local(iv['at'])}."
            elif op == "cancel":
                store.update_interview(entry_id, cancel=True, candidate_id=candidate_id)
                notice = "Interview cancelled. It stays on the timeline, marked cancelled."
            elif op == "restore":
                store.update_interview(entry_id, restore=True, candidate_id=candidate_id)
                notice = "Interview back on the schedule."
            elif op == "outcome":
                outcome = str(form.get("outcome") or "")
                if outcome not in INTERVIEW_OUTCOME_LABELS:
                    raise ValueError("Pick how the interview went.")
                store.update_interview(entry_id, outcome=outcome, note=str(form.get("note") or ""),
                                       candidate_id=candidate_id)
                notice = f"Saved: {INTERVIEW_OUTCOME_LABELS[outcome]}."
            else:
                raise ValueError("Couldn't change that interview. Reload the page and try again.")
        except NotFound:
            raise HTTPException(404, "That interview isn't on this profile.")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'notice': notice})}#timeline",
                                status_code=303)

    @app.post("/candidates/{candidate_id}/undo/{timeline_id}")
    def undo_change_form(candidate_id: int, timeline_id: int):
        cand_or_404(candidate_id)
        try:
            cand = store.undo_change(timeline_id, candidate_id=candidate_id, by="app")
        except NotFound:
            raise HTTPException(404, "That change isn't on this profile.")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        notice = f"Undid the change to {_labels(cand['_restored'])}."
        if cand["_skipped"]:
            notice += " Couldn't put back the " + " or the ".join(cand["_skipped"]) + "."
        return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'notice': notice})}#timeline",
                                status_code=303)

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
        history_text = _history_to_text(guess.pop("work_history", None) or [])
        dupe = store.find_duplicate(guess.get("email"), guess.get("linkedin_url"))
        return page(request, "candidate_form.html", c=guess, history_text=history_text, error=None, job_id=None,
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
        _sweep_uploads()
        token = secrets.token_hex(8)
        fd = os.open(imports_dir() / f"{token}.csv", os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        mapping = importers.guess_mapping(header, rows)
        return map_step(request, token, header, rows, mapping, importers.default_source(header), upload.filename)

    def map_step(request: Request, token: str, header: list[str], rows: list[list[str]], mapping: list[str],
                 source: str, filename: str | None = None, error: str | None = None) -> HTMLResponse:
        has_name = any(m in ("name", "first_name", "last_name") for m in mapping)
        preview = None
        if has_name and len(rows) <= PREVIEW_MAX_ROWS:
            try:
                preview = importers.preview_import(store, header, rows, mapping, source=source)
            except Exception:  # a preview that can't be worked out never blocks the import
                preview = None
        return page(request, "import.html", step="map", token=token, header=header, rows=rows[:5], total=len(rows),
                    mapping=mapping, targets=importers.IMPORT_TARGETS, labels=importers.TARGET_LABELS,
                    filename=filename, source=source, error=error, has_name=has_name, preview=preview,
                    preview_lines=importers.preview_lines(preview) if preview else [],
                    button_label=importers.button_label(preview, len(rows)))

    def _upload(form: Any) -> tuple[str, Path] | None:
        token = str(form.get("token") or "")
        _sweep_uploads()
        path = imports_dir() / f"{token}.csv"
        return (token, path) if token.isalnum() and path.exists() else None

    def _form_mapping(form: Any, header: list[str]) -> list[str]:
        mapping = [str(form.get(f"col{i}") or "") for i in range(len(header))]
        return [m if m in importers.IMPORT_TARGETS else "" for m in mapping]

    @app.post("/import/check", response_class=HTMLResponse)
    async def import_check(request: Request):
        """The map step again, with the dry run worked out for the columns as they're matched now."""
        form = await request.form()
        up = _upload(form)
        if up is None:
            return page(request, "import.html", step="upload", error="That upload expired. Choose the file again.")
        header, rows = importers.read_csv(importers.decode(up[1].read_bytes()))
        return map_step(request, up[0], header, rows, _form_mapping(form, header),
                        str(form.get("source") or "CSV import"))

    @app.post("/import/run", response_class=HTMLResponse)
    async def import_run(request: Request):
        form = await request.form()
        up = _upload(form)
        if up is None:
            return page(request, "import.html", step="upload", error="That upload expired. Choose the file again.")
        token, path = up
        header, rows = importers.read_csv(importers.decode(path.read_bytes()))
        mapping = _form_mapping(form, header)
        source = str(form.get("source") or "CSV import").strip()[:80] or "CSV import"
        if not any(m in ("name", "first_name", "last_name") for m in mapping):
            return map_step(request, token, header, rows, mapping, source, error="Pick which column holds the name.")
        try:
            result = importers.import_rows(store, header, rows, mapping, source=source)
        except ValueError as exc:  # the backup before importing failed, so nothing changed
            return page(request, "import.html", step="upload", error=str(exc))
        finally:
            path.unlink(missing_ok=True)  # our own private copy of the upload
        return page(request, "import.html", step="done", result=result)

    def _sweep_uploads() -> None:
        """Uploads nobody finished importing are deleted after an hour."""
        cutoff = time.time() - UPLOAD_TTL
        for f in imports_dir().glob("*.csv"):
            try:
                if f.stat().st_mtime < cutoff:
                    f.unlink()
            except OSError:
                pass

    @app.get("/export.csv")
    def export_csv():
        stamp = datetime.now().strftime("%Y-%m-%d")
        return Response(importers.export_csv(store), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="talent-search-{stamp}.csv"'})

    @app.get("/backup")
    def backup():
        stamp = datetime.now().strftime("%Y-%m-%d")
        fd, name = tempfile.mkstemp(prefix="download-", suffix=".db", dir=backups_dir())  # 0600, private folder
        os.close(fd)
        tmp = Path(name)
        store.backup_to(tmp)
        return FileResponse(tmp, filename=f"talent-search-backup-{stamp}.db", media_type="application/octet-stream",
                            background=BackgroundTask(lambda: tmp.unlink(missing_ok=True)))

    @app.post("/sample-data")
    def sample_data():
        load_sample_data(store)
        return RedirectResponse("/?sample=1", status_code=303)

    @app.post("/sample-data/remove")
    def sample_data_remove(request: Request):
        try:
            r = remove_sample_data(store)
        except ValueError as exc:  # the backup failed, so nothing was removed
            return back(request, error=str(exc))
        people, jobs, kp, kj = r["people"], r["jobs"], r["kept_people"], r["kept_jobs"]
        if not (people or jobs or kp or kj):
            return back(request, notice="There was no sample data to remove.")
        msg = (f"Removed the sample data: {people} candidate{'' if people == 1 else 's'} and "
               f"{jobs} job{'' if jobs == 1 else 's'}.")
        kept = ([f"{len(kp)} sample {'person' if len(kp) == 1 else 'people'} you worked with ({', '.join(kp)})"]
                if kp else []) + ([f"the job{'' if len(kj) == 1 else 's'} {', '.join(kj)}"] if kj else [])
        if kept:
            msg += " Kept " + " and ".join(kept) + ". They are your own data now."
        return back(request, notice=msg + " A backup was saved first.")

    # ------------------------------------------------------------ jobs
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, status: str = ""):
        jobs = store.list_jobs(status if status in JOB_STATUSES else None)
        hints = actions.twin_hints(jobs)
        rollup = actions.company_rollup(store)
        companies = [{**r, "text": actions.status_digest(store, r["company"])} for r in rollup if r["company"]]
        return page(request, "jobs.html", jobs=[{**j, "hint": hints.get(j["id"])} for j in jobs], status=status,
                    rollup=rollup, companies=companies,
                    digest_text=actions.status_digest(store) if rollup else "")

    @app.get("/jobs/new", response_class=HTMLResponse)
    def new_job(request: Request):
        return page(request, "job_form.html", j={}, error=None)

    @app.post("/jobs", response_class=HTMLResponse)
    async def create_job(request: Request):
        form = await request.form()
        data = {k: str(form.get(k) or "") for k in ("title", "company", "location", "description", *JOB_EXTRA_FIELDS)}
        stages = [s for s in str(form.get("stages") or "").splitlines() if s.strip()]
        twins = store.similar_open_jobs(data["title"], data["company"])
        try:
            job = store.add_job(**data, stages=stages or None)
        except ValueError as exc:
            return page(request, "job_form.html", j=data, error=str(exc))
        if twins:
            ids = ", ".join(f"#{j['id']}" for j in twins)
            notice = (f"Created. Heads up: open job {ids} has the same title and company. "
                      "If it's a duplicate, close one of them.")
            return RedirectResponse(f"/jobs/{job['id']}?{urlencode({'notice': notice})}", status_code=303)
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int, view: str = "", q: str = ""):
        job = job_or_404(job_id)
        # ?omit= previews a choice without saving it; otherwise the job's remembered Leave out choice applies.
        omit = [o for o in request.query_params.getlist("omit") if o.strip()] or actions.saved_omit(store, job_id)
        if view in ("board", "table"):
            store.set_settings(pipeline_view=view)
        view = view if view in ("board", "table") else store.setting("pipeline_view")
        add_results = []
        if q.strip():
            in_pipe = {a["candidate_id"] for a in store.pipeline(job_id)}
            add_results = [h.candidate for h in search.search(q, limit=8).hits if h.candidate["id"] not in in_pipe]
        ctx = dict(job=job, groups=store.pipeline_by_stage(job_id), stages=store.job_stages(job_id), view=view,
                   q=q, add_results=add_results, prompt=recipes.source_for_job(job),
                   next_iv=actions.next_interviews(store, job_id), omit=omit,
                   status_text=actions.job_status_text(store, job_id, omit_stages=omit),
                   nums=actions.job_numbers(store, job_id), todos=_todos(job_id))
        if is_htmx(request) and request.headers.get("HX-Target") == "add-results":
            return page(request, "_add_results.html", **ctx)
        return page(request, "job.html", **ctx)

    @app.post("/jobs/{job_id}", response_class=HTMLResponse)
    async def update_job(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        data = {k: str(form.get(k) or "") for k in ("title", "company", "location", "description", "status",
                                                    *JOB_EXTRA_FIELDS) if k in form}
        try:
            store.update_job(job_id, **data)
        except ValueError as exc:
            return page(request, "job_form.html", j={**store.get_job(job_id), **data}, error=str(exc),
                        stages=store.job_stages(job_id), outcomes=STAGE_OUTCOMES, statuses=JOB_STATUSES)
        if set(data) == {"status"} and data["status"] == "closed":
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': 'Job closed. Its pipeline is kept.'})}",
                                    status_code=303)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/omit")
    async def save_omit(request: Request, job_id: int):
        """Remember which stages to leave out of this job's status update, here, on /jobs and for the AI."""
        job_or_404(job_id)
        form = await request.form()
        names = [str(v) for v in form.getlist("omit")]
        stages = {s["name"].lower(): s["name"] for s in store.job_stages(job_id)}
        kept = actions.save_omit(store, job_id, [stages[n.lower()] for n in names if n.lower() in stages])
        notice = ("Status updates for this job now leave out " + ", ".join(kept) + "." if kept
                  else "Status updates for this job now include every stage.")
        return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice, 'status': 'open'})}#status",
                                status_code=303)

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
                store.rename_stage(job_id, name, str(form.get("new_name") or ""),
                                   outcome=str(form.get("outcome") or "") or None)
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
            store.move_stage(int(cid), job_id, str(form.get("stage") or ""), notes=str(form.get("notes") or "") or None,
                             by="app")
        except (ValueError, NotFound) as exc:
            error = str(exc)
        if is_htmx(request):
            view = str(form.get("view") or store.setting("pipeline_view"))
            return page(request, "_pipeline.html", job=job, groups=store.pipeline_by_stage(job_id),
                        stages=store.job_stages(job_id), view=view, error=error,
                        next_iv=actions.next_interviews(store, job_id))
        return back(request, f"/jobs/{job_id}", error=error or "")

    @app.post("/jobs/{job_id}/delete")
    def delete_job_form(job_id: int):
        """App window only. The AI has no tool to delete a job."""
        job = job_or_404(job_id)
        try:
            importers.backup_before_import(store, reason="delete")
        except ValueError as exc:
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'flash_error': str(exc)})}", status_code=303)
        store.delete_job(job_id)
        return RedirectResponse("/jobs?" + urlencode({"notice": f"Deleted {actions.job_label(job['title'], job.get('company'))}. "
                                                      "The candidates are still saved."}), status_code=303)

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
        days = str(form.get("days") or "7")
        if not key or not days.isdigit() or not 0 < int(days) < 3650:
            return back(request, error="Couldn't snooze that. Reload the page and try again.")
        store.snooze(key, int(days))
        return back(request, notice=f"Snoozed for {days} day{'' if days == '1' else 's'}.")

    @app.post("/actions/todo")
    async def add_todo(request: Request):
        """Add a to-do to Today from Today or a job page, optionally about a job and from a day on."""
        form = await request.form()
        job = str(form.get("job_id") or "")
        due = str(form.get("due") or "").strip()
        try:
            job_id = int(job) if job.isdigit() else None
            if job_id is not None:
                store.get_job(job_id)
            s = actions.add_followup(store, str(form.get("text") or ""), due or None, job_id=job_id, source="App")
        except NotFound:
            return back(request, error="That job is gone. Reload the page and try again.")
        except ValueError as exc:
            msg = str(exc).replace("A suggestion needs some text.", "Write what the to-do is.")
            return back(request, error=msg)
        when = f" It shows on Today from {actions.local_time(s['due_on'] + 'T12:00:00', '%a %b %-d')}." \
            if s.get("due_on") and s["due_on"] > datetime.now().astimezone().date().isoformat() else ""
        return back(request, notice="To-do added." + when)

    @app.post("/actions/suggestions/{suggestion_id}/done")
    def suggestion_done(request: Request, suggestion_id: int):
        try:
            actions.complete_followup(store, suggestion_id)
        except NotFound:
            return back(request, error="That follow-up is already gone.")
        return back(request, notice="Marked done.")

    @app.post("/actions/advance")
    async def advance(request: Request):
        form = await request.form()
        cid, jid, stage = (str(form.get(k) or "") for k in ("candidate_id", "job_id", "stage"))
        if not (cid.isdigit() and jid.isdigit() and stage):
            return back(request, error="Couldn't move that candidate. Reload the page and try again.")
        try:
            app_row = store.move_stage(int(cid), int(jid), stage, by="app")
        except (ValueError, NotFound) as exc:
            return back(request, error=str(exc))
        return back(request, notice=f"Moved {app_row['candidate_name']} to {app_row['stage']}.")

    # ---------------------------------------------------------- AI changes
    def change_rows(who: str = "", since: str | None = None, limit: int = CHANGES_SHOWN) -> list[dict[str, Any]]:
        """Everything on Changes, newest first: profile edits by you or an import (kept on the timeline),
        and every write your AI app made (kept in its own log, with what each replaced)."""
        rows: list[dict[str, Any]] = []
        if who != "ai":
            where, params = ["t.kind = ?", "coalesce(t.source, '') <> ?"], [CHANGE_KIND, CHANGE_SOURCES["ai"]]
            if who in CHANGE_SOURCES:
                where.append("t.source = ?")
                params.append(CHANGE_SOURCES[who])
            if since:
                where.append("t.at >= ?")
                params.append(since)
            for r in store._rows(
                    f"""SELECT t.id, t.candidate_id, t.body, t.at, t.data, c.name AS candidate_name
                        FROM timeline t JOIN candidates c ON c.id = t.candidate_id WHERE {' AND '.join(where)}
                        ORDER BY t.at DESC, t.id DESC LIMIT ?""", (*params, limit)):
                d = _json_or_none(r["data"]) or {}
                lines = change_lines(d)
                rows.append({"at": r["at"], "who": d.get("by") if d.get("by") in CHANGE_WHO else "app",
                             "name": r["candidate_name"], "href": f"/candidates/{r['candidate_id']}#timeline",
                             "text": ("Undo: " if (r["body"] or "").startswith("Undid") else "")
                             + ("; ".join(lines) if lines else r["body"] or ""),
                             "undoable": bool(d.get("old")) and not d.get("undone_at") and not d.get("merged"),
                             "undone": bool(d.get("undone_at")), "undo_url": f"/changes/{r['id']}/undo"})
        if who in ("", "ai"):
            for a in store.ai_actions(since, limit):
                if a["candidate_id"]:
                    name, href = a["candidate_name"], f"/candidates/{a['candidate_id']}#timeline"
                elif a["job_id"]:
                    name, href = a["job_title"], f"/jobs/{a['job_id']}"
                else:
                    name, href = "To-do", "/"
                lines = change_lines(a["change_data"]) if a["kind"] == "profile" else []
                rows.append({"at": a["at"], "who": "ai", "name": name, "href": href,
                             "text": "; ".join(lines) if lines else a["summary"], "undoable": a["undoable"],
                             "undone": bool(a["undone_at"]), "note": a.get("undo_note"),
                             "undo_url": f"/changes/ai/{a['id']}/undo"})
        rows.sort(key=lambda r: r["at"], reverse=True)  # stable, so each list keeps its own newest-first order
        return rows[:limit]

    def since_iso(since: str) -> str | None:
        """A start time from the Changes form: 1, 7 or 30 (days back), epoch seconds, or ISO with a zone
        (a '+' that a hand-built form turned into a space still works)."""
        since = (since or "").strip()
        if since in CHANGE_SINCE:
            return (datetime.now(timezone.utc) - timedelta(days=int(since))).replace(microsecond=0).isoformat()
        try:
            if since.isdigit():
                dt = datetime.fromtimestamp(int(since), timezone.utc)
            else:
                dt = to_dt(re.sub(r" (\d{2}:?\d{2})$", r"+\1", since)) if since else None
        except (ValueError, OverflowError, OSError):
            return None
        return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat() if dt else None

    @app.get("/changes", response_class=HTMLResponse)
    def changes_page(request: Request, who: str = "", since: str = ""):
        who = who if who in CHANGE_WHO else ""
        since = since if since in CHANGE_SINCE else ""
        start = since_iso(since)
        rows = change_rows(who, start)
        ai_since = start or since_iso("1")
        ai_pending = sum(1 for a in store.ai_actions(ai_since, limit=1_000_000) if a["undoable"])
        return page(request, "changes.html", rows=rows, who=who, since=since, who_options=CHANGE_WHO,
                    since_options=CHANGE_SINCE, ai_since=ai_since, ai_pending=ai_pending, limit=CHANGES_SHOWN,
                    ai_since_epoch=int(to_dt(ai_since).timestamp()))

    @app.post("/changes/{timeline_id}/undo")
    def undo_from_changes(request: Request, timeline_id: int):
        try:
            cand = store.undo_change(timeline_id, by="app")
        except NotFound:
            return back(request, "/changes", error="That change is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=str(exc))
        notice = f"Undid the change to {cand['name']}'s {_labels(cand['_restored'])}."
        if cand["_skipped"]:
            notice += " Couldn't put back the " + " or the ".join(cand["_skipped"]) + "."
        return back(request, "/changes", notice=notice)

    @app.post("/changes/ai/{action_id}/undo")
    def undo_ai_action(request: Request, action_id: int):
        try:
            what = store.undo_ai_action(action_id)
        except NotFound:
            return back(request, "/changes", error="That change is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=f"Couldn't undo that. {exc}")
        return back(request, "/changes", notice=f"Undid: {what}.")

    @app.post("/changes/undo-ai")
    async def undo_ai_since(request: Request):
        form = await request.form()
        start = since_iso(str(form.get("since") or ""))
        if not start:
            return back(request, "/changes", error="Couldn't tell which changes to undo. Reload the page and try again.")
        undone, skipped = [], []
        for a in store.ai_actions(start, limit=1_000_000):  # newest first, so older values come back last
            if a["undone_at"]:
                continue
            try:
                undone.append(store.undo_ai_action(a["id"], by="app"))
            except NotFound:
                continue  # went with a person or job taken out just before
            except ValueError as exc:
                skipped.append(f"{a['summary']} ({str(exc).rstrip('.')})")
        if not undone and not skipped:
            return back(request, "/changes", notice="There were no AI changes to undo in that time.")
        notice = f"Undid {len(undone)} AI change{'' if len(undone) == 1 else 's'}" + (
            ": " + _listed(undone) if undone else "") + "."
        if skipped:
            notice += f" Left {len(skipped)} as {'it is' if len(skipped) == 1 else 'they are'}: {_listed(skipped)}."
        return back(request, "/changes", notice=notice)

    # ---------------------------------------------------------- settings etc
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: bool = False):
        return render_settings(request, saved=saved)

    def render_settings(request: Request, saved: bool = False, s: dict[str, str] | None = None,
                        errors: dict[str, str] | None = None, status_code: int = 200) -> HTMLResponse:
        people, jobs = sample_ids(store)
        return page(request, "settings.html", status_code=status_code, s=s or store.settings(), flags=actions.FLAGS,
                    thresholds=actions.THRESHOLDS, saved=saved, errors=errors or {}, sample_people=len(people),
                    sample_jobs=len(jobs), data_folder=str(data_dir().resolve()), db_file=str(db_path().resolve()),
                    backup_count=len(_backup_files()), backup_folder=str(backups_dir().resolve()),
                    typesense=env_status())

    def _backup_files() -> list[Path]:
        return [f for f in backups_dir().iterdir() if f.is_file() and f.name.endswith(".db")]

    @app.post("/settings/backups/delete")
    def delete_backups(request: Request):
        """App window only: remove the automatic backups (copies taken before imports, deletes and merges)."""
        removed = 0
        for f in _backup_files():
            try:
                f.unlink()
                removed += 1
            except OSError:
                pass
        store.checkpoint()
        left = len(_backup_files())
        msg = (f"Deleted {removed} backup{'' if removed == 1 else 's'}." if removed else "There were no backups to delete.")
        if left:
            msg += f" {left} couldn't be deleted. Close other apps using them and try again."
        return RedirectResponse("/settings?" + urlencode({"notice": msg}), status_code=303)

    @app.post("/settings", response_class=HTMLResponse)
    async def save_settings(request: Request):
        form = await request.form()
        values: dict[str, str] = {}
        errors: dict[str, str] = {}
        for key, _, _ in actions.FLAGS:
            values[key] = "1" if form.get(key) else "0"
        for key, _ in actions.THRESHOLDS:
            raw = str(form.get(key) or "").strip()
            if not raw:
                continue  # left blank: keep what was there
            if raw.isdigit() and 0 < int(raw) < 10000:
                values[key] = str(int(raw))
            else:
                values[key] = raw
                errors[key] = "Use a whole number from 1 to 9999."
        if form.get("pipeline_view") in ("board", "table"):
            values["pipeline_view"] = str(form.get("pipeline_view"))
        if errors:
            return render_settings(request, s={**store.settings(), **values}, errors=errors, status_code=400)
        store.set_settings(**values)
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes_page(request: Request):
        items = [(r, recipes.render(r.key)) for r in recipes.RECIPES]
        return page(request, "recipes.html", items=items, jobs=store.list_jobs("open"),
                    job_prompts={j["id"]: recipes.source_for_job(j) for j in store.list_jobs("open")})

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        return page(request, "privacy.html", data_folder=str(data_dir().resolve()), typesense=env_status())

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
        details, where = str(form.get("details") or ""), str(form.get("where") or "app")
        link = recipes.issue_url(summary, details, where=where)
        title, body = recipes.issue_text(summary, details, where=where)
        return page(request, "report.html", link=link, summary=summary, env_line=recipes.environment_line(),
                    where=str(form.get("where") or ""), preview_title=title, preview_body=body)

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
    def health(request: Request):
        """Open without the key so a second launch can find the running app. Says nothing about the data."""
        if not request.state.unlocked:
            return {"ok": True, "version": __version__}
        return {"ok": True, "version": __version__, "search": search.engine, "candidates": store.count_candidates()}

    @app.get("/api/candidates")
    def api_search(q: str = "", location: str | None = None, title: str | None = None, tag: str | None = None,
                   limit: int = 25, offset: int = 0):
        r = search.search(q, location=location, title=title, tag=tag, limit=max(1, min(limit, 200)),
                          offset=max(0, offset))
        return {"total": r.total, "engine": r.engine, "exact_total": r.exact_total, "suggestion": r.suggestion,
                "corrected": r.corrected,
                "results": [{**h.candidate, "snippet": h.snippet, "closest": h.closest, "reason": h.reason}
                            for h in r.hits]}

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

    for route in app.routes:  # HEAD works wherever GET does (link checkers, curl -I)
        if isinstance(route, APIRoute) and "GET" in route.methods and route.path != "/launch":  # HEAD never spends a code
            route.methods.add("HEAD")
    return app


def _hostname(host_header: str) -> str:
    """'127.0.0.1:8765' -> '127.0.0.1', '[::1]:8765' -> '[::1]', 'localhost' -> 'localhost'."""
    if host_header.startswith("["):
        return host_header.split("]", 1)[0] + "]"
    return host_header.rsplit(":", 1)[0] if host_header.count(":") == 1 else host_header


def ai_status_note(store: Store, job_id: int) -> str:
    """'Closed by your AI app on Nov 5' while the job still has the status the AI app gave it, else ''."""
    change = store.ai_status_change(job_id)
    if not change:
        return ""
    verb = {"closed": "Closed", "on_hold": "Put on hold", "open": "Reopened"}.get(change["status"], "Changed")
    return f"{verb} by your AI app on {_local(change['at'], '%b %-d')}"


def _listed(items: list[str], shown: int = 6) -> str:
    """'A; B; C and 4 more' for a notice that must stay one readable line."""
    head = "; ".join(items[:shown])
    return head + (f"; and {len(items) - shown} more" if len(items) > shown else "")


def _json_or_none(text: str | None) -> Any:
    try:
        return json.loads(text) if text else None
    except ValueError:
        return None


def _form_fields(form: Any, keep_blank: bool = False) -> dict[str, Any]:
    out = {}
    for f in CANDIDATE_FIELDS:
        if f in form:
            v = str(form[f])
            if v.strip() or keep_blank:
                out[f] = v
    return out


__all__ = ["create_app", "DEFAULT_SETTINGS", "split_tags"]
