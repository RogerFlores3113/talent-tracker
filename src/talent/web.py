"""The app window: a local web UI on 127.0.0.1 opened in the user's browser."""
from __future__ import annotations

from typing import Any
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

from . import __version__, integrations
from .paths import resource_dir
from .store import CANDIDATE_FIELDS, NotFound, Store, snippet_html
from .typesense_index import SearchService, TypesenseIndex

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1", "testserver"}


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


def create_app(store: Store, search: SearchService | None = None) -> FastAPI:
    search = search or SearchService(store, TypesenseIndex.from_env())
    app = FastAPI(title="Talent Search", version=__version__, docs_url=None, redoc_url=None)
    res = resource_dir()
    templates = Jinja2Templates(directory=str(res / "templates"))
    templates.env.filters["snippet"] = snippet_html
    templates.env.filters["date"] = lambda iso: (iso or "")[:10]
    app.mount("/static", StaticFiles(directory=str(res / "static")), name="static")

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
        return await call_next(request)

    def page(request: Request, name: str, **ctx: Any) -> HTMLResponse:
        return templates.TemplateResponse(request, name, {"engine": search.engine, **ctx})

    # ------------------------------------------------------------ pages
    @app.get("/", response_class=HTMLResponse)
    def home(request: Request, q: str = "", location: str = "", title: str = ""):
        result = search.search(q, location=location or None, title=title or None, limit=50)
        if request.headers.get("HX-Request") and request.headers.get("HX-Target") == "results":
            return page(request, "_results.html", result=result, q=q, total_all=store.count_candidates())
        return page(request, "index.html", result=result, q=q, location=location, title=title,
                    total_all=store.count_candidates())

    @app.get("/search", response_class=HTMLResponse)
    def search_fragment(request: Request, q: str = "", location: str = "", title: str = ""):
        result = search.search(q, location=location or None, title=title or None, limit=50)
        return page(request, "_results.html", result=result, q=q, total_all=store.count_candidates())

    @app.get("/candidates/new", response_class=HTMLResponse)
    def new_candidate(request: Request):
        return page(request, "candidate_form.html", c={}, error=None)

    @app.post("/candidates", response_class=HTMLResponse)
    async def create_candidate_form(request: Request):
        data = _form_fields(await request.form())
        try:
            cand, _ = store.upsert_candidate(**data)
        except ValueError as exc:
            return page(request, "candidate_form.html", c=data, error=str(exc))
        return RedirectResponse(f"/candidates/{cand['id']}", status_code=303)

    @app.get("/candidates/{candidate_id}", response_class=HTMLResponse)
    def candidate_detail(request: Request, candidate_id: int, edit: bool = False):
        try:
            c = store.get_candidate(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")
        if edit:
            return page(request, "candidate_form.html", c=c, error=None)
        return page(request, "candidate.html", c=c, pipelines=store.candidate_pipelines(candidate_id))

    @app.post("/candidates/{candidate_id}", response_class=HTMLResponse)
    async def update_candidate_form(request: Request, candidate_id: int):
        data = _form_fields(await request.form(), keep_blank=True)
        try:
            store.update_candidate(candidate_id, **data)
        except NotFound:
            raise HTTPException(404, "Candidate not found")
        except ValueError as exc:
            return page(request, "candidate_form.html", c={"id": candidate_id, **data}, error=str(exc))
        return RedirectResponse(f"/candidates/{candidate_id}", status_code=303)

    @app.post("/candidates/{candidate_id}/delete")
    def delete_candidate_form(candidate_id: int):
        try:
            store.delete_candidate(candidate_id)
        except NotFound:
            raise HTTPException(404, "Candidate not found")
        return RedirectResponse("/", status_code=303)

    @app.post("/sample-data")
    def sample_data():
        from .sample_data import load_sample_data

        load_sample_data(store)
        return RedirectResponse("/", status_code=303)

    # ----------------------------------------------------- AI app setup
    @app.get("/connect", response_class=HTMLResponse)
    def connect_page(request: Request):
        return page(
            request,
            "connect.html",
            claude_on=integrations.claude_connected(),
            codex_on=integrations.codex_connected(),
            json_snippet=integrations.generic_json_snippet(),
            codex_snippet=integrations.codex_snippet(),
        )

    @app.post("/connect/{target}", response_class=HTMLResponse)
    def connect(request: Request, target: str):
        fn = {"claude": integrations.connect_claude, "codex": integrations.connect_codex}.get(target)
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
    def api_search(q: str = "", location: str | None = None, title: str | None = None,
                   limit: int = 25, offset: int = 0):
        r = search.search(q, location=location, title=title, limit=min(limit, 200), offset=offset)
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
            return store.get_candidate(candidate_id)
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

    return app


def _form_fields(form: Any, keep_blank: bool = False) -> dict[str, Any]:
    out = {}
    for f in CANDIDATE_FIELDS:
        if f in form:
            v = str(form[f])
            if v.strip() or keep_blank:
                out[f] = v
    return out
