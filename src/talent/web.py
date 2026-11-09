"""The app window: a local web UI on 127.0.0.1 opened in the user's browser."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
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
from fastapi.exception_handlers import request_validation_exception_handler
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, HTMLResponse, JSONResponse, PlainTextResponse, RedirectResponse, Response
from fastapi.routing import APIRoute
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from starlette.background import BackgroundTask

from . import __version__, actions, backups, importers, integrations, paths, recipes, review, shortlist, reports
from . import sessions, skills
from . import ai_caps, trust
from . import injection, outreach
from .sample_data import load_sample_data, remove_sample_data, sample_ids
from . import decide, interviews, loops, offers, plan
from . import chase, desk, duplicates, opsreview, tour
from . import searchquery
from .paths import backups_dir, data_dir, db_path, imports_dir, resource_dir
from .store import (CANDIDATE_FIELDS, CHANGE_KIND, CHANGE_SOURCES, DEFAULT_SETTINGS, FIELD_LABELS, DuplicateValue,
                    INTERVIEW_FORM_CHOICES, INTERVIEW_OUTCOME_LABELS, INTERVIEW_RESULT_CHOICES,
                    JOB_EXTRA_FIELDS, JOB_STATUSES, STAGE_OUTCOMES, TIMELINE_KINDS, NotFound,
                    HL_END, HL_START, Store, _labels, dropped_text, fold_name, interview_label, interview_note, snippet_html,
                    split_tags, to_dt)
from .store import as_local, parse_when
from .store import BATCH_KINDS, DEFAULT_THEME, THEMES
from .store import COLORS, clean_color, next_color
from .typesense_index import SearchService, TypesenseIndex, env_status

LOCAL_HOSTS = {"127.0.0.1", "localhost", "[::1]", "::1"}
MAX_UPLOAD = 10 * 1024 * 1024
ONE_SHOT_PARAMS = {"notice", "flash_error", "error", "saved", "sample", "deleted", "merged"}  # messages, not state
UPLOAD_TTL = 3600  # seconds an upload waits between column matching and the import
PREVIEW_MAX_ROWS = 20000  # bigger files import without the "what will change" preview
BIG_IMPORT_ROWS = 1000  # from here on the import page says it takes a minute, and shows it's working
UNSAFE_METHODS = {"POST", "PUT", "PATCH", "DELETE"}
DOWNLOADS = {"/backup", "/export.csv"}  # whole-database downloads: never for another site's page
OPEN_PATHS = {"/launch", "/api/health"}  # everything else needs the app window's key
KEY_HEADER = "x-talent-key"
KEY_MAX_AGE = 400 * 24 * 3600  # browsers cap cookie lifetimes at 400 days
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
       "object-src 'none'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'")
LOCKED = "Open Talent Search from its app icon (or run talent open) to unlock this window."
LOCKED_LOG = "A browser window asked for a page while locked. To unlock it, open Talent Search from its app icon or run: talent open"
# One-line messages after a form (notice, flash_error, error) travel in a short-lived cookie, not the
# address, so names never land in browser history or logs. The cookie is signed with the app key.
FLASH_KEYS = ("notice", "flash_error", "error")
FLASH_MAX_AGE = 120  # seconds
FLASH_CHARS = 700  # longest message kept
FLASH_COOKIE_CHARS = 3000  # a browser drops a cookie over about 4 KB, and with it the whole message
UNDO_AI_LAST = "undo_ai_last"  # settings key: what the last Undo all AI did, shown on Changes for a day
REDIRECTS = {301, 302, 303, 307, 308}
log = logging.getLogger(__name__)
CHANGE_WHO = {"ai": "Your AI app", "import": "CSV import", "app": "You, in the app"}
CHANGE_SINCE = {"1": "Last 24 hours", "7": "Last 7 days", "30": "Last 30 days"}
# "Undo all AI changes from the last ...": hours back.
UNDO_WINDOWS = {"1": "hour", "24": "24 hours", "168": "7 days", "720": "30 days"}
CHANGES_SHOWN = 300
# The colour bubble's words: one letter in the bubble too, so colour is never the only clue.
COLOR_LETTERS = {"red": "R", "orange": "O", "yellow": "Y", "green": "G", "blue": "B", "purple": "P"}
# Rainbow order as SQL: red first, then orange... purple, then people with no colour.
COLOR_RANK_SQL = ("CASE c.color " + " ".join(f"WHEN '{c}' THEN {i}" for i, c in enumerate(COLORS))
                  + f" ELSE {len(COLORS)} END")
COLORED_FIRST_POOL = 500  # a search with Colored first sorts this many best matches by colour


def color_info(color: str | None, who: str = "") -> dict[str, Any]:
    """What a colour bubble says and does: its name, letter, the colours a click and a right-click go to, and
    the label for screen readers. With the person's name it reads "Daniel Example: color red. Click for orange.
    Shift+Enter goes back one color." so a list of bubbles has a different name for each."""
    color = color if color in COLORS else None
    nxt, prev = next_color(color, 1), next_color(color, -1)
    name = color or "none"
    go = f"Click for {nxt}" if nxt else "Click to clear it"
    back = "Shift+Enter, Shift-click or right-click goes back one color."
    lead = f"{who}: color" if who else "Color:"
    return {"color": color or "", "name": name, "letter": COLOR_LETTERS.get(color or "", ""),
            "next": nxt or "none", "prev": prev or "none", "aria": f"{lead} {name}. {go}. {back}",
            "title": f"{lead} {name}. {go}. {back}"}


TABLE_SORTS = {
    "color": COLOR_RANK_SQL,
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


def _ago(iso: str | None, now: datetime | None = None) -> str:
    """'today', 'yesterday', '3 days ago'... counted in calendar days on this computer's clock, so something
    from 11 PM last night is 'yesterday' at 1 AM, not 'today'."""
    dt = to_dt(iso)
    if not dt:
        return "never"
    now = now or datetime.now(timezone.utc)
    if dt > now + timedelta(minutes=1):
        return "upcoming"
    days = max(0, actions.local_days(dt, now))
    if days == 0:
        return "today"
    if days == 1:
        return "yesterday"
    if days < 60:
        return f"{days} days ago"
    if days < 730:
        return f"{days // 30} months ago"
    return f"{days // 365} years ago"


IV_TIME = actions.IV_TIME  # interview times say their zone


def _local(iso: str | None, fmt: str = "%a %b %-d, %-I:%M %p") -> str:
    dt = to_dt(iso)
    if not dt:
        return ""
    local = as_local(dt)
    try:
        return local.strftime(fmt)
    except ValueError:  # Windows strftime has no %-d
        return local.strftime(fmt.replace("%-", "%#"))


def _local_date(iso: str | None) -> str:
    """'2026-11-04': a timestamp's day on this computer's clock (a plain day stays as it is)."""
    v = iso or ""
    if "T" not in v:
        return v[:10]
    dt = to_dt(v)
    return as_local(dt).date().isoformat() if dt else v[:10]


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


def _role(r: dict[str, Any]) -> str:
    return " at ".join(x for x in (r.get("title"), r.get("company")) if x) or "a role"


def _span(r: dict[str, Any]) -> str:
    return f"{r.get('start') or '?'} to {r.get('end') or 'now'}"


def history_change(old: Any, new: Any) -> str:
    """Which work history roles changed: 'Senior Engineer at Acme to Staff Engineer at Acme; added PM at Ramp'."""
    old = [r for r in old if isinstance(r, dict)] if isinstance(old, list) else []
    new = [r for r in new if isinstance(r, dict)] if isinstance(new, list) else []
    key = lambda r: (str(r.get("title") or "").casefold().strip(), str(r.get("company") or "").casefold().strip())
    left, right, parts = list(old), list(new), []
    for r in new:  # the same role on both sides: only its dates can have changed
        hit = next((o for o in left if key(o) == key(r)), None)
        if hit is not None:
            left.remove(hit)
            right.remove(r)
            if (hit.get("start"), hit.get("end")) != (r.get("start"), r.get("end")):
                parts.append(f"{_role(r)} now {_span(r)} (was {_span(hit)})")
    for o, r in zip(left, right):
        parts.append(f"{_role(o)} to {_role(r)}")
    parts += [f"added {_role(r)}" for r in right[len(left):]]
    parts += [f"removed {_role(o)}" for o in left[len(right):]]
    return "; ".join(parts)


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
        if k == "work_history" and was and now and (what := history_change(old.get(k), v)):
            out.append(f"{cap}: {what}")
        elif not now:
            out.append(f"Cleared {label}" + (f" (was {was})" if was else ""))
        elif not was:
            out.append(f"Added {label}: {now}")
        else:
            out.append(f"{cap}: {was} to {now}")
    return out


def ai_change_text(a: dict[str, Any]) -> str:
    """An AI action's line on Changes; interview moves and cancels say when, in local time."""
    old, new = a.get("old_data") or {}, a.get("new_data") or {}
    if a["kind"] == "interview_move" and old.get("at") and new.get("at"):
        return f"{a['summary']} (was {_local(old['at'])}, now {_local(new['at'])})"
    if a["kind"] == "interview_cancel" and old.get("at"):
        return f"{a['summary']} (it was set for {_local(old['at'])})"
    return a["summary"]


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


def current_theme(store: Store) -> str:
    """The saved color theme, or the default when nothing valid is saved."""
    value = store.setting("theme")
    return value if value in {t[0] for t in THEMES} else DEFAULT_THEME


def key_cookie_name(key: str) -> str:
    """Cookies are shared by every port on 127.0.0.1, so each install's cookie gets its own name."""
    return "talent_key_" + hashlib.sha256(key.encode()).hexdigest()[:10]


def flash_cookie_name(key: str) -> str:
    return "talent_flash_" + hashlib.sha256(key.encode()).hexdigest()[:10]


def _flash_sig(key: str, raw: str) -> str:
    return hmac.new(paths.subkey(key, "flash"), raw.encode(), hashlib.sha256).hexdigest()[:32]


def encode_flash(key: str, messages: dict[str, str]) -> str:
    limit = FLASH_CHARS
    while True:  # names in other scripts take several bytes each: shorten until the cookie surely fits
        kept = {k: (v if len(v) <= limit else v[:limit - 3].rstrip() + "...") for k, v in messages.items()}
        raw = base64.urlsafe_b64encode(json.dumps(kept).encode()).decode().rstrip("=")
        if len(raw) <= FLASH_COOKIE_CHARS or limit <= 80:
            return raw + "." + _flash_sig(key, raw)
        limit = limit * 2 // 3


def decode_flash(value: str, key: str | None = None) -> dict[str, str]:
    """The messages in a flash cookie. With key, only when the signature is ours; {} otherwise."""
    raw, _, sig = (value or "").strip('"').partition(".")
    if key is not None and not (sig and hmac.compare_digest(sig.encode(), _flash_sig(key, raw).encode())):
        return {}
    try:
        data = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
    except (ValueError, TypeError):
        return {}
    return {k: str(v) for k, v in data.items() if k in FLASH_KEYS and v} if isinstance(data, dict) else {}


def plain_snippet(snippet: str | None) -> str | None:
    """A search snippet without the highlight markers, for JSON."""
    return snippet.replace(HL_START, "").replace(HL_END, "") if snippet else snippet


def safe_local_path(path: str) -> bool:
    """A path on this server: not '//host', '/\\host' or anything a browser could read as another site."""
    return (path.startswith("/") and not path.startswith(("//", "/\\")) and "\\" not in path
            and not any(ch.isspace() or ord(ch) < 32 for ch in path))


def tuck_import_rows(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Changes shows one row per import: the per-person edits it made go under it (its "children"), not
    beside it. An edit is the import's when it was written while that import ran."""
    imports = [r for r in rows if r.get("span")]
    out: list[dict[str, Any]] = []
    for r in rows:
        if r.get("by_import"):
            home = next((i for i in imports if i["span"][0] <= r["at"] <= i["span"][1]), None)
            if home is not None:
                home.setdefault("children", []).append(r)
                continue
        out.append(r)
    return out


def create_app(store: Store, search: SearchService | None = None, key: str | None = None) -> FastAPI:
    search = search or SearchService(store, TypesenseIndex.from_env())
    key = key or paths.app_key()
    cookie_name = key_cookie_name(key)
    reports.apply_zone(store)  # times show in the recruiter's own zone
    flash_name = flash_cookie_name(key)
    locked_logged = [float("-inf")]  # monotonic time can be under 60s on a just-booted computer
    app = FastAPI(title="Talent Search", version=__version__, docs_url=None, redoc_url=None, openapi_url=None)
    res = resource_dir()
    templates = Jinja2Templates(directory=str(res / "templates"))
    env = templates.env
    env.filters["snippet"] = snippet_html
    env.filters["date"] = _local_date
    env.filters["ago"] = _ago
    env.filters["local"] = _local
    env.filters["day"] = actions.day_text  # a date, Windows-safe
    env.globals["IV_TIME"] = IV_TIME
    env.globals["color_info"] = color_info
    env.globals["COLORS"] = COLORS
    env.globals["local_zone"] = lambda: as_local(datetime.now(timezone.utc)).tzname() or ""
    env.globals["computer_zone"] = lambda: datetime.now().astimezone().tzname() or ""
    env.globals["user_zone"] = lambda: reports.user_zone(store)
    env.globals["shown_zone_name"] = lambda: reports.user_zone(store) or reports.fallback_zone(store)
    env.globals["common_zones"] = reports.COMMON_ZONES
    env.filters["claude_link"] = recipes.claude_link
    env.filters["change_lines"] = change_lines
    env.filters["profile_label"] = profile_label
    env.globals["version"] = __version__
    env.globals["theme"] = lambda: current_theme(store)  # base.html puts it on <html>, so there is no flash of another theme
    env.globals["themes"] = THEMES
    env.globals["sample_ids"] = lambda: sample_ids(store)  # templates badge sample people and jobs
    env.globals["tour_payload"] = lambda request: tour_for_page(request)  # base.html draws the tour from this
    env.globals["ai_status_note"] = lambda job_id: ai_status_note(store, job_id)
    env.globals["ai_contacts_change"] = lambda key: review.contacts_change(store, key)  # "Contacts changed by your AI app"
    env.globals["ai_email_changes"] = lambda job_ids=None: review.email_changes(store, job_ids)  # job emails it set
    env.globals["job_label"] = actions.job_label
    env.globals["followups"] = lambda job_id: followups_for(job_id)
    env.globals["elsewhere_badges"] = lambda job_id: outreach.board_badges(store, job_id)  # "In play at 1 other client"
    env.globals["interview_outcomes"] = INTERVIEW_OUTCOME_LABELS
    env.globals["interview_choices"] = INTERVIEW_RESULT_CHOICES  # Today's buttons: the interviewer's word
    env.globals["interview_form_choices"] = INTERVIEW_FORM_CHOICES  # the profile's How it went menu
    env.globals["verdict_choices"] = interviews.VERDICT_CHOICES  # one interviewer's own verdict (Decide)
    env.globals["interview_label"] = interview_label  # 'Strong yes', 'Mixed', 'Passed'
    env.globals["interview_note"] = interview_note
    env.globals["inhouse"] = lambda: actions.inhouse(store)  # "Hiring team" wording instead of "Client"
    env.globals["hiring_for_answered"] = lambda: actions.hiring_for_answered(store)
    env.globals["move_reasons"] = lambda: actions.reasons(store)
    env.globals["shortlist_waiting"] = lambda job_id: shortlist.waiting(store, job_id)
    env.globals["offers_on"] = lambda job_id: offers.on_job(store, job_id)  # board cards: "Offer out 3 days"
    env.globals["offers_of"] = lambda candidate_id: offers.of_candidate(store, candidate_id)
    env.globals["stage_groups_of"] = lambda job_id: {  # each stage's funnel step (in-house has no Submitted)
        k: ("screening" if g == "submitted" and actions.inhouse(store) else g)
        for k, g in reports.stage_groups(store, job_id).items()}
    env.globals["funnel_choices"] = lambda: reports.funnel_choices(actions.inhouse(store))
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
        request.state.flash = decode_flash(request.cookies.get(flash_name, ""), key)
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
        flash_to_cookie(request, response)
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("Referrer-Policy", "same-origin")
        response.headers.setdefault("Content-Security-Policy", CSP)
        if not path.startswith("/static/"):  # pages, the API, the export and backups: never kept in the browser cache
            response.headers["Cache-Control"] = "no-store"
        return response

    def flash_to_cookie(request: Request, response: Response) -> None:
        """A redirect's ?notice= (or error) moves into the flash cookie; a page that showed it clears it."""
        if response.status_code in REDIRECTS and "location" in response.headers:
            target = urlsplit(response.headers["location"])
            pairs = parse_qsl(target.query, keep_blank_values=True)
            messages = {k: v for k, v in pairs if k in FLASH_KEYS and v}
            if messages or any(k in FLASH_KEYS for k, _ in pairs):
                rest = urlencode([(k, v) for k, v in pairs if k not in FLASH_KEYS])
                response.headers["location"] = target._replace(query=rest).geturl()
            if messages:
                response.set_cookie(flash_name, encode_flash(key, messages), max_age=FLASH_MAX_AGE, httponly=True,
                                    samesite="strict", path="/")
                return
        shown = (request.method == "GET" and not is_htmx(request) and response.status_code not in REDIRECTS
                 and response.headers.get("content-type", "").startswith("text/html"))
        if request.cookies.get(flash_name) and shown:
            response.delete_cookie(flash_name, path="/", httponly=True, samesite="strict")

    def has_key(request: Request) -> bool:
        given = request.cookies.get(cookie_name) or request.headers.get(KEY_HEADER) or ""
        return hmac.compare_digest(given.encode(), key.encode())

    def locked(request: Request) -> Response:
        if request.url.path.startswith("/api/"):
            resp: Response = JSONResponse({"detail": LOCKED}, status_code=401)
        else:  # only the way to unlock: every other link would land on this page again
            resp = templates.TemplateResponse(request, "locked.html", {"message": LOCKED}, status_code=401)
            if time.monotonic() - locked_logged[0] > 60:  # the same hint in the serve log, once a minute at most
                locked_logged[0] = time.monotonic()
                log.info(LOCKED_LOG)
        for k, v in (("X-Frame-Options", "DENY"), ("X-Content-Type-Options", "nosniff"),
                     ("Referrer-Policy", "same-origin"), ("Content-Security-Policy", CSP), ("Cache-Control", "no-store")):
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
        return {"saved_searches": store.list_saved_searches(counts=True, limit=6),
                "today": actions.action_items(store), "jobs": store.list_jobs("open"),
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
              sort: str = "color", dir: str = "", page_no: int = 1, more: int = 0, job: str = "",
              client: str = "", stage: str = "", pipe: str = "", days: str = "", color: str = "",
              color_pick: str = "", cf: str = "",
              title: str = "", company: str = "", skills: str = "", skill_mode: str = "", remote: str = "",
              exp_min: str = "", exp_max: str = "", contacted: str = "", contacted_days: str = "",
              added_days: str = "", has_email: str = "", has_linkedin: str = ""):
        per = 100
        # The chips above the table are submit buttons: pressing the colour already chosen clears it.
        color = (color_pick if color_pick != color else "") if color_pick else color
        color = color.strip().lower()
        color = color if color in (*COLORS, "none") else ""
        colored_first = cf == "1"
        if sort not in TABLE_SORTS:
            sort = "color"
        dir = dir if dir in ("asc", "desc") else ("asc" if sort == "color" else "desc")
        job_id = int(job) if job.strip().isdigit() else None
        pipe = pipe if pipe in opsreview.PIPES else ""
        day_n = int(days) if days.strip().isdigit() else None
        # The "More filters" (searchquery.py): cleaned once, used for the search and kept in every link.
        more_params = searchquery.clean_params(dict(
            title=title, company=company, skills=skills, skill_mode=skill_mode, remote=remote, exp_min=exp_min,
            exp_max=exp_max, contacted=contacted, contacted_days=contacted_days, added_days=added_days,
            has_email=has_email, has_linkedin=has_linkedin))
        member = dict(job_id=job_id, client=client or None, stage=stage or None, pipe=pipe or None, days=day_n,
                      **searchquery.search_kwargs(more_params))
        page_no = max(1, page_no)
        sql_sort = TABLE_SORTS[sort]
        direction = "ASC" if dir == "asc" else "DESC"
        suggestion = None
        if q.strip():
            member["color"] = color or None
            if colored_first:  # best matches, grouped by colour (red first), best match first inside each colour
                result = search.search(q, tag=tag or None, location=location or None, source=source or None,
                                       limit=COLORED_FIRST_POOL, offset=0, **member)
                ranked = sorted(result.hits, key=lambda h: (COLORS.index(h.candidate["color"])
                                                            if h.candidate.get("color") in COLORS else len(COLORS)))
                start = (page_no - 1) * per
                rows = [h.candidate for h in ranked[start:start + per]]
            else:
                result = search.search(q, tag=tag or None, location=location or None, source=source or None,
                                       limit=per, offset=(page_no - 1) * per, **member)
                rows = [h.candidate for h in result.hits]
            total = result.total
            suggestion = result.suggestion
        else:
            conds, params, _ = store.candidate_filters(tag=tag or None, location=location or None,
                                                       source=source or None, color=color or None, **member)
            where = ("WHERE " + " AND ".join(conds)) if conds else ""
            total = store._row(f"SELECT count(*) AS n FROM candidates c {where}", params)["n"]
            if sort == "color":  # rainbow order, newest first inside each colour (desc: untagged first, purple back to red)
                order = f"{sql_sort} {direction}, c.updated_at DESC, c.id DESC"
            else:
                order = f"{sql_sort} {direction}, c.id DESC"
            rows = [store._hydrate(r) for r in store._rows(
                f"SELECT c.* FROM candidates c {where} ORDER BY {order} LIMIT ? OFFSET ?",
                (*params, per, (page_no - 1) * per))]
        keep = {k: v for k, v in dict(tag=tag, location=location, source=source, job="" if job_id is None else job_id,
                                      client=client, stage=stage, pipe=pipe, color=color,
                                      cf="1" if colored_first and q.strip() else "",
                                      days="" if not pipe or day_n is None else day_n,
                                      **more_params).items() if v != ""}
        ctx = dict(rows=rows, total=total, q=q, tag=tag, location=location, source=source, sort=sort, dir=dir,
                   job=job_id or "", client=client, stage=stage, keep=keep, mf=more_params,
                   more_open=any(k in more_params for k in searchquery.MORE_KEYS),
                   active_chips=filter_chips(q, keep, sort, dir, job_id, more_params),
                   saved=store.list_saved_searches(counts=True), save_params=searchquery.clean_params({**keep, "q": q}),
                   min_years_note="exp_min" in more_params,
                   pipe_label=opsreview.pipe_label(pipe, day_n) if pipe else "", page_no=page_no, pages=max(1, (total + per - 1) // per), shown=min(total, page_no * per),
                   suggestion=suggestion, corrected=bool(suggestion and result.corrected), color=color,
                   colored_first=colored_first and bool(q.strip()), color_counts=store.color_counts())
        if more and is_htmx(request):  # "Show more": just the next rows, which replace the button's row
            return page(request, "_table_rows.html", **ctx)
        return page(request, "table.html", **ctx, tags=store.list_tags(40), sources=store.list_sources(),
                    dup_count=duplicates.count(store),
                    **store.membership_choices(job_id))

    def filter_chips(q: str, keep: dict[str, Any], sort: str, dir: str, job_id: int | None,
                     more: dict[str, str]) -> list[dict[str, str]]:
        """One chip per filter that is on, each linking to the same table with just that filter off."""
        names = {"tag": "Skill", "location": "Location", "source": "Found via", "client": "Client", "stage": "Stage",
                 "title": "Title", "company": "Company"}
        out: list[dict[str, str]] = []

        def chip(label: str, *drop: str) -> None:
            rest = {k: v for k, v in keep.items() if k not in drop}
            if "q" not in drop and q:
                rest["q"] = q
            rest.update(sort=sort, dir=dir)
            out.append({"label": label, "href": "/candidates?" + urlencode(rest)})

        for k, label in names.items():
            if keep.get(k):
                chip(f"{label}: {keep[k]}", k)
        if job_id is not None:
            chip("In one job", "job", "stage")
        if "skills" in more:
            chip(("All of: " if more.get("skill_mode") == "all" else "Any of: ") + more["skills"], "skills", "skill_mode")
        if "remote" in more:
            chip("Remote", "remote")
        if "exp_min" in more or "exp_max" in more:
            lo, hi = more.get("exp_min"), more.get("exp_max")
            chip(f"{lo} to {hi} years" if lo and hi else f"{lo}+ years" if lo else f"Up to {hi} years", "exp_min", "exp_max")
        c = more.get("contacted")
        if c:
            n = more.get("contacted_days", "")
            chip("Never contacted" if c == "never" else f"Last contacted over {n} days ago" if c == "older"
                 else f"Contacted in the last {n} days", "contacted", "contacted_days")
        if "added_days" in more:
            chip(f"Added in the last {more['added_days']} days", "added_days")
        if "has_email" in more:
            chip("Has email", "has_email")
        if "has_linkedin" in more:
            chip("Has LinkedIn", "has_linkedin")
        return out

    # ------------------------------------------------------ saved searches
    @app.post("/saved-searches")
    async def saved_search_create(request: Request):
        form = await request.form()
        params = searchquery.clean_params({k: str(v) for k, v in form.items()})
        try:
            row = store.save_search(str(form.get("search_name") or ""), params, by="app")
        except ValueError as exc:
            return back(request, "/candidates", error=str(exc))
        return RedirectResponse("/candidates?" + urlencode([*params.items(), ("notice", f"Saved search “{row['name']}”.")]),
                                status_code=303)

    @app.get("/saved-searches/{search_id}")
    def saved_search_open(search_id: int):
        """Open a saved search: the Candidates table with its words and filters, and note that it was looked at
        (so "new" counts only people added after now)."""
        try:
            row = store.get_saved_search(search_id)
        except NotFound:
            raise HTTPException(404, "That saved search isn't there any more.")
        store.mark_search_viewed(search_id)
        return RedirectResponse("/candidates?" + urlencode(row["params"]), status_code=303)

    @app.post("/saved-searches/{search_id}/rename")
    async def saved_search_rename(request: Request, search_id: int):
        form = await request.form()
        try:
            row = store.rename_saved_search(search_id, str(form.get("search_name") or ""))
        except NotFound:
            raise HTTPException(404, "That saved search isn't there any more.")
        except ValueError as exc:
            return back(request, "/candidates", error=str(exc))
        return back(request, "/candidates", notice=f"Renamed to “{row['name']}”.")

    @app.post("/saved-searches/{search_id}/delete")
    def saved_search_delete(request: Request, search_id: int):
        """The recruiter's own action: the AI has no tool that deletes a saved search."""
        try:
            row = store.delete_saved_search(search_id)
        except NotFound:
            raise HTTPException(404, "That saved search isn't there any more.")
        return back(request, "/candidates", notice=f"Deleted the saved search “{row['name']}”.")
    # ------------------------------------------------------- duplicates
    @app.get("/candidates/duplicates", response_class=HTMLResponse)
    def duplicates_page(request: Request, shown: int = 50):
        shown = max(50, min(shown, 2000))
        pairs = duplicates.find_pairs(store)
        total = len(pairs)
        rows = []
        for p in pairs[:shown]:
            rows.append({**p, "a": store.get_candidate(p["keep"]), "b": store.get_candidate(p["other"])})
        return page(request, "duplicates.html", rows=rows, total=total, shown=shown,
                    dismissed=len(duplicates.dismissed(store)))

    @app.post("/candidates/duplicates/dismiss")
    async def duplicates_dismiss(request: Request):
        """App window only: these two are different people. The pair never comes back."""
        form = await request.form()
        a, b = str(form.get("a") or ""), str(form.get("b") or "")
        if not (a.isdigit() and b.isdigit()):
            raise HTTPException(400, "Pick two people")
        try:
            duplicates.dismiss(store, int(a), int(b))
        except (NotFound, ValueError):
            raise HTTPException(404, "Candidate not found")
        return RedirectResponse("/candidates/duplicates?" + urlencode({"notice": "Got it. That pair won't show up again."}),
                                status_code=303)

    @app.post("/candidates/duplicates/reset")
    def duplicates_reset():
        n = duplicates.forget_dismissed(store)
        return RedirectResponse("/candidates/duplicates?" + urlencode(
            {"notice": f"{n} pair{'' if n == 1 else 's'} can show up again."}), status_code=303)

    # -------------------------------------------------------- candidates
    @app.get("/candidates/new", response_class=HTMLResponse)
    def new_candidate(request: Request, job_id: int | None = None):
        return page(request, "candidate_form.html", c={}, history_text="", error=None, job_id=job_id)

    @app.post("/candidates", response_class=HTMLResponse)
    async def create_candidate_form(request: Request):
        form = await request.form()
        data = _form_fields(form)
        history = _history_from_text(str(form.get("work_history", "")))
        job_id = str(form.get("job_id") or "")
        if (form.get("create_anyway") != "1" and data.get("name")
                and store.find_duplicate(data.get("email"), data.get("linkedin_url")) is None):
            same_name = store.find_by_name(str(data["name"]))  # the same check the AI app's save gets
            if same_name:
                in_job = ({p["candidate_id"] for p in store.pipeline(int(job_id))}
                          if job_id.isdigit() and store._row("SELECT id FROM jobs WHERE id = ?", (int(job_id),)) else set())
                same_exact = all(fold_name(d["name"]) == fold_name(str(data["name"])) for d in same_name)
                return page(request, "candidate_form.html", c=data, history_text=str(form.get("work_history", "")),
                            error=None, job_id=job_id if job_id.isdigit() else None, same_name=same_name, in_job=in_job,
                            same_exact=same_exact, fill_fields=FILL_FIELDS)
        try:
            cand, created = store.upsert_candidate(work_history=history or None, **data)
            if job_id.isdigit():
                warning = add_with_warning(cand["id"], int(job_id))
                return RedirectResponse(f"/jobs/{job_id}" + (f"?{urlencode({'flash_error': warning})}" if warning else ""),
                                        status_code=303)
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
                    inject_warnings=injection.candidate_warnings(c),
                    dupes=store.possible_duplicates(candidate_id),
                    kinds=[*(k for k in TIMELINE_KINDS if k != "stage"), outreach.REPLY_KIND],
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

    @app.post("/candidates/{candidate_id}/color")
    async def set_candidate_color(request: Request, candidate_id: int):
        """The colour bubble: save one colour (or none) at once. A fetch from the page gets JSON back so the bubble
        updates in place; a plain form post (no script) goes back to the page it came from."""
        cand_or_404(candidate_id)
        form = await request.form()
        wants_json = "application/json" in request.headers.get("accept", "")
        try:
            saved = store.set_color(candidate_id, clean_color(str(form.get("color", ""))))
        except ValueError as exc:
            if wants_json:
                return JSONResponse({"error": str(exc)}, status_code=400)
            return back(request, default=f"/candidates/{candidate_id}", error=str(exc))
        if wants_json:
            return JSONResponse({"id": candidate_id, **color_info(saved.get("color"), saved.get("name") or ""),
                                 "counts": store.color_counts()})
        return back(request, default=f"/candidates/{candidate_id}")

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
                reply = kind == outreach.REPLY_KIND
                if job_id is None and kind in (*outreach.OUTREACH_KINDS, outreach.REPLY_KIND):
                    job_id = outreach.job_for(store, candidate_id)
                entry = store.log(candidate_id, "message" if reply else kind, body or ("Replied" if reply else None),
                                  at=str(form.get("at") or "") or None, job_id=job_id, source="App")
                logged = True
                if reply:
                    outreach.mark_reply(store, entry["id"])
                    move = outreach.reply_stage(store, candidate_id, job_id) if job_id is not None else None
                    if move:
                        notice = f"Logged their reply. Move to {move['to']} is on Today when you're ready."
                elif job_id is not None:
                    moved = outreach.advance(store, candidate_id, job_id, entry["kind"], entry["at"])
                    if moved:
                        notice = f"Moved them from {moved['from']} to {moved['to']}."
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
                notice = f"Moved the interview to {_local(iv['at'], IV_TIME)}."
            elif op == "cancel":
                store.update_interview(entry_id, cancel=True, candidate_id=candidate_id)
                notice = "Interview cancelled. It stays on the timeline, marked cancelled."
            elif op == "restore":
                store.update_interview(entry_id, restore=True, candidate_id=candidate_id)
                notice = "Interview back on the schedule."
            elif op == "outcome":
                outcome = str(form.get("outcome") or "")
                if outcome not in INTERVIEW_OUTCOME_LABELS and outcome not in dict(INTERVIEW_FORM_CHOICES):
                    raise ValueError("Pick how the interview went.")
                iv = store.update_interview(entry_id, outcome=outcome, note=str(form.get("note") or ""),
                                            candidate_id=candidate_id)
                notice = f"Saved: {interview_label(iv['data']) or 'how it went'}."
            else:
                raise ValueError("Couldn't change that interview. Reload the page and try again.")
        except NotFound:
            raise HTTPException(404, "That interview isn't on this profile.")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        if str(form.get("next") or "") == "/":  # recorded from Today: back there
            return RedirectResponse("/?" + urlencode({"notice": f"{notice[:-1]} for {cand_or_404(candidate_id)['name']}."}),
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

    @app.post("/candidates/{candidate_id}/timeline/{timeline_id}/strike")
    def strike_entry(candidate_id: int, timeline_id: int):
        """Strike out a note, call, email, message, meeting or interview the AI app logged. It stays, marked struck."""
        cand_or_404(candidate_id)
        try:
            store.strike_entry(timeline_id, candidate_id=candidate_id, by="app")
        except NotFound:
            raise HTTPException(404, "That entry isn't on this profile.")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        notice = "Struck it out. It stays on the timeline, but reports, search and your AI app leave it out."
        return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'notice': notice})}#timeline",
                                status_code=303)

    @app.post("/candidates/{candidate_id}/timeline/{timeline_id}/unstrike")
    def unstrike_entry(candidate_id: int, timeline_id: int):
        cand_or_404(candidate_id)
        try:
            store.unstrike_entry(timeline_id, candidate_id=candidate_id)
        except NotFound:
            raise HTTPException(404, "That entry isn't on this profile.")
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#timeline",
                                    status_code=303)
        return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'notice': 'Put it back.'})}#timeline",
                                status_code=303)

    @app.post("/candidates/{candidate_id}/contacted")
    def mark_contacted(candidate_id: int):
        cand_or_404(candidate_id)
        job_id = outreach.job_for(store, candidate_id)
        notice = ""
        if outreach.logged_today(store, candidate_id):  # an InMail logged today already says so
            notice = "Outreach is already logged for today."
        else:
            entry = store.log(candidate_id, "message", "Marked as contacted", job_id=job_id, source="App")
            moved = outreach.advance(store, candidate_id, job_id, "message", entry["at"]) if job_id else None
            if moved:
                notice = f"Moved them from {moved['from']} to {moved['to']}."
        return RedirectResponse(f"/candidates/{candidate_id}" + (f"?{urlencode({'notice': notice})}" if notice else ""),
                                status_code=303)

    @app.post("/candidates/{candidate_id}/jobs")
    async def add_candidate_to_job(request: Request, candidate_id: int):
        cand_or_404(candidate_id)
        form = await request.form()
        job = str(form.get("job_id") or "")
        warning = add_with_warning(candidate_id, int(job)) if job.isdigit() else ""
        return RedirectResponse(f"/candidates/{candidate_id}" + (f"?{urlencode({'flash_error': warning})}" if warning else ""),
                                status_code=303)

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
        found, missing = duplicates.found_fields(guess)
        history_text = _history_to_text(guess.pop("work_history", None) or [])
        dupe = duplicates.already_here(store, guess)
        if dupe and len(text.split()) == 1 and guess.get("linkedin_url"):  # just their link: open who has it
            return RedirectResponse(f"/candidates/{dupe['id']}?{urlencode({'notice': f'Already saved: {dupe['name']}.'})}",
                                    status_code=303)
        return page(request, "candidate_form.html", c=guess, history_text=history_text, error=None, job_id=None,
                    from_paste=True, dupe=dupe, prompt=recipes.save_from_text(text), found=found, missing=missing,
                    dupe_same_id=store.find_duplicate(guess.get("email"), guess.get("linkedin_url")) is not None)

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
        mapping, prof = importers.mapping_for(store, header, rows)
        return map_step(request, token, header, rows, mapping,
                        (prof or {}).get("source") or importers.default_source(header), _file_name(upload.filename),
                        add_stages=bool((prof or {}).get("add_stages", True)),
                        move_stages=True, client=(prof or {}).get("client"),
                        replace_stages=bool((prof or {}).get("replace_stages", True)))

    def _file_name(name: Any) -> str:
        """The uploaded file's own name, without any folder, for Changes and the backup list."""
        return re.split(r"[\\/]", str(name or ""))[-1].strip()[:120]

    def _save_lists(token: str, which: str, result: dict[str, Any]) -> None:
        """Keep an import's (or a dry run's) detail lists for an hour, for "Download the full list"."""
        lists = {k: result.get(k) or [] for k, _ in importers.PANELS}
        path = imports_dir() / f"{token}.{which}.json"
        try:
            fd = os.open(path, os.O_CREAT | os.O_TRUNC | os.O_WRONLY, 0o600)
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(lists, f)
        except OSError:
            pass  # the panels still show their first lines

    def map_step(request: Request, token: str, header: list[str], rows: list[list[str]], mapping: list[str],
                 source: str, filename: str | None = None, error: str | None = None,
                 add_stages: bool = True, move_stages: bool = True, client: str | None = None,
                 reopen_jobs: bool = False, replace_stages: bool = True) -> HTMLResponse:
        has_name = any(m in ("name", "first_name", "last_name") for m in mapping)
        prof = importers.sync_profile(store, header)
        client = (client or "").strip()[:120]
        preview = None
        if has_name and len(rows) <= PREVIEW_MAX_ROWS:
            try:
                preview = importers.preview_import(store, header, rows, mapping, source=source, add_stages=add_stages,
                                                   move_stages=move_stages, profile={**(prof or {}), "client": client},
                                                   reopen_jobs=reopen_jobs, replace_stages=replace_stages)
            except Exception:  # a preview that can't be worked out never blocks the import
                preview = None
        if preview:
            _save_lists(token, "preview", preview)
        return page(request, "import.html", step="map", token=token, header=header, rows=rows[:5], total=len(rows),
                    mapping=mapping, targets=importers.IMPORT_TARGETS, labels=importers.TARGET_LABELS,
                    filename=filename, source=source, error=error, has_name=has_name, preview=preview,
                    export_phrase=importers.export_phrase(importers.detect_export(header)),
                    preview_lines=importers.preview_lines(preview) if preview else [],
                    panels=importers.result_panels(preview) if preview else [], big=len(rows) > BIG_IMPORT_ROWS,
                    button_label=importers.button_label(preview, len(rows)), add_stages=add_stages,
                    move_stages=move_stages, profile=prof, client=client, reopen_jobs=reopen_jobs,
                    replace_stages=replace_stages, no_client="job_title" in mapping and "job_company" not in mapping)

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
                        str(form.get("source") or "CSV import"), _file_name(form.get("filename")),
                        add_stages=form.get("add_stages") == "1", move_stages=form.get("move_stages") == "1",
                        client=str(form.get("client") or ""), reopen_jobs=form.get("reopen_jobs") == "1",
                        replace_stages=form.get("replace_stages") == "1")

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
        add_stages = form.get("add_stages") == "1"
        move_stages = form.get("move_stages") == "1"
        reopen_jobs, replace_stages = form.get("reopen_jobs") == "1", form.get("replace_stages") == "1"
        client = str(form.get("client") or "").strip()[:120]
        filename = _file_name(form.get("filename"))
        if not any(m in ("name", "first_name", "last_name") for m in mapping):
            return map_step(request, token, header, rows, mapping, source, filename,
                            error="Pick which column holds the name.", add_stages=add_stages,
                            move_stages=move_stages, client=client, reopen_jobs=reopen_jobs,
                            replace_stages=replace_stages)
        prof = importers.sync_profile(store, header) or {}
        try:
            result = importers.import_rows(store, header, rows, mapping, source=source, add_stages=add_stages,
                                           file_name=filename or None, move_stages=move_stages,
                                           profile={**prof, "client": client}, reopen_jobs=reopen_jobs,
                                           replace_stages=replace_stages)
        except ValueError as exc:  # the backup before importing failed, so nothing changed
            return page(request, "import.html", step="upload", error=str(exc))
        finally:
            path.unlink(missing_ok=True)  # our own private copy of the upload
        importers.save_sync_profile(store, header, mapping, result, file_name=filename, source=source,
                                    add_stages=add_stages, move_stages=move_stages, client=client,
                                    replace_stages=replace_stages)
        backups.record_import(store, result, filename, len(rows))
        if result.get("backup"):
            result["backup_name"] = Path(result["backup"]).name
        _save_lists(token, "result", result)
        ask = set(importers.openings_to_ask(store))
        new_jobs = [{**store.get_job(i), "hired": actions.job_numbers(store, i)["hired_all"]}
                    for i in result.get("new_job_ids") or [] if i in ask]
        return page(request, "import.html", step="done", result=result, token=token,
                    panels=importers.result_panels(result), new_jobs=new_jobs[:30],
                    closed_lines=importers.closed_job_lines(result))

    @app.get("/import/openings", response_class=HTMLResponse)
    def import_openings_page(request: Request):
        """Jobs an import made, asked once how many openings each has."""
        jobs = []
        for i in importers.openings_to_ask(store):
            try:
                jobs.append({**store.get_job(i), "hired": actions.job_numbers(store, i)["hired_all"]})
            except LookupError:
                continue
        return page(request, "import.html", step="openings", new_jobs=jobs[:50])

    @app.post("/import/openings")
    async def import_openings(request: Request):
        form = await request.form()
        answers: dict[int, int | None] = {}
        for key, value in form.items():
            if key.startswith("openings_") and key[9:].isdigit():
                v = str(value or "").strip()
                answers[int(key[9:])] = int(v) if v.isdigit() and 0 < int(v) < 1000 else None
        name = str(form.get("backup") or "")
        keep_undo = bool(name) and backups.import_undo_state(store, name) == "ok"
        n = importers.answer_openings(store, answers)
        if keep_undo:  # answering the import's own question doesn't take its Undo away
            backups.remember_import(store, name)
        what = f"Saved openings for {n} job{'' if n == 1 else 's'}." if n else "Okay, we won't ask about those again."
        return RedirectResponse("/jobs?" + urlencode({"notice": what}), status_code=303)

    @app.get("/import/{token}/{which}.csv")
    def import_list_csv(token: str, which: str, panel: str = ""):
        """One result panel in full, as a CSV. Kept for an hour after the import or dry run."""
        titles = dict(importers.PANELS)
        path = imports_dir() / f"{token}.{which}.json"
        if not token.isalnum() or which not in ("result", "preview") or panel not in titles or not path.is_file():
            raise HTTPException(404, "That list is gone. Lists are kept for an hour after an import.")
        try:
            lines = [str(x) for x in json.loads(path.read_text(encoding="utf-8")).get(panel) or []]
        except (OSError, ValueError, AttributeError):
            raise HTTPException(404, "That list is gone. Lists are kept for an hour after an import.") from None
        name = f"import-{titles[panel].lower().replace(' ', '-').replace(',', '')}.csv"
        return Response(importers.panel_csv(titles[panel], lines), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.post("/import/undo")
    async def import_undo(request: Request):
        """Put back the backup taken before an import, only while nothing else has changed since."""
        form = await request.form()
        return undo_import(str(form.get("backup") or ""))

    @app.post("/changes/import/{name}/undo")
    def undo_import_from_changes(name: str):
        return undo_import(name)

    def undo_import(name: str) -> RedirectResponse:
        state = backups.import_undo_state(store, name)
        if state == "changed":
            return RedirectResponse("/settings?" + urlencode({"flash_error": (
                "Something changed after that import (an edit, a note, a stage move or your AI app), so undoing it "
                "here would lose that too. To go back anyway, restore the backup from before the import below.")})
                + "#backups", status_code=303)
        if state != "ok":
            return RedirectResponse("/settings?" + urlencode({"flash_error": (
                "That import can't be undone here any more. Its backup may still be in the list below.")})
                + "#backups", status_code=303)
        try:
            _, saved = restore_backup(name)
        except ValueError as exc:
            return RedirectResponse("/settings?" + urlencode({"flash_error": str(exc)}) + "#backups", status_code=303)
        return RedirectResponse("/candidates?" + urlencode({"notice": (
            "Undid the import. Your data is back to how it was just before it. A copy with the import in it was "
            f"saved first as {saved.name}, in Settings under Automatic backups.")}), status_code=303)

    def _sweep_uploads() -> None:
        """Uploads nobody finished importing are deleted after an hour."""
        cutoff = time.time() - UPLOAD_TTL
        for f in [*imports_dir().glob("*.csv"), *imports_dir().glob("*.json")]:
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

    def removal_notice(r: dict[str, Any], what: str = "the sample data") -> str:
        """One line on what removing the sample data did, and what it kept because the recruiter worked with it."""
        people, jobs, kp, kj = r["people"], r["jobs"], r["kept_people"], r["kept_jobs"]
        dp, ds = r.get("deleted_people") or [], r.get("deleted_searches") or []
        kp = [n for n in kp if n not in dp]
        msg = (f"Removed {what}: {people} candidate{'' if people == 1 else 's'} and "
               f"{jobs} job{'' if jobs == 1 else 's'}.")
        kept = ([f"{len(kp)} sample {'person' if len(kp) == 1 else 'people'} you worked with ({', '.join(kp)})"]
                if kp else []) + ([f"the job{'' if len(kj) == 1 else 's'} {', '.join(kj)}"] if kj else [])
        if kept:
            msg += " Kept " + " and ".join(kept) + ". They are your own data now."
        if dp:
            msg += f" Also deleted {', '.join(dp)}."
        if ds:
            msg += f" Also removed the saved search{'' if len(ds) == 1 else 'es'} {', '.join(ds)}."
        empty = [x["name"] for x in store.list_saved_searches(counts=True) if x["count"] == 0]
        if empty:
            msg += (f" {len(empty)} saved search{'' if len(empty) == 1 else 'es'} now match nobody ({', '.join(empty[:3])}"
                    f"{', and more' if len(empty) > 3 else ''}). Delete what you don't need under Manage saved searches.")
        return msg + " A backup was saved first."

    @app.post("/sample-data/remove")
    def sample_data_remove(request: Request):
        try:
            r = remove_sample_data(store)
        except ValueError as exc:  # the backup failed, so nothing was removed
            return back(request, error=str(exc))
        if not (r["people"] or r["jobs"] or r["kept_people"] or r["kept_jobs"]):
            return back(request, notice="There was no sample data to remove.")
        return back(request, notice=removal_notice(r))

    # ------------------------------------------------------------ first-run tour
    def tour_for_page(request: Request) -> dict[str, Any] | None:
        """The tour to draw on this page, or None. A first run starts it; ?tour=short or ?tour=full starts a replay."""
        if request.method != "GET" or request.url.path.startswith(("/static/", "/api/")):
            return None
        try:
            if not tour.state(store)["active"]:
                if request.query_params.get("tour") in tour.MODES:
                    tour.start(store, request.query_params["tour"])
                elif tour.should_autostart(store):
                    tour.start(store, tour.FULL)
            return tour.payload(store)
        except Exception:  # the tour must never break a page
            log.exception("tour state could not be read")
            return None

    def tour_start_path() -> str:
        p = tour.payload(store)
        return (p["steps"][p["step"]]["path"] if p else None) or "/"

    @app.post("/tour/start")
    async def tour_start(request: Request):
        """Start the tour (mode full or short) and go to its first page."""
        mode = str((await request.form()).get("mode") or tour.SHORT)
        if mode not in tour.MODES:
            raise HTTPException(422, "mode must be full or short")
        tour.start(store, mode)
        return RedirectResponse(tour_start_path(), status_code=303)

    @app.post("/tour/step")
    async def tour_step(request: Request):
        """Remember which step the user is on, so the tour carries across pages."""
        raw = str((await request.form()).get("step") or "")
        if not raw.lstrip("-").isdigit():
            raise HTTPException(422, "step must be a number")
        n = tour.set_step(store, int(raw))
        if n is None:
            return JSONResponse({"ok": False, "detail": "No tour is running."}, status_code=409)
        return {"ok": True, "step": n}

    @app.post("/tour/load")
    def tour_load(request: Request):
        """The tour's own button: add the demo people (full tour only), then show the next step."""
        added = tour.load_demo(store)
        if added is None:
            return back(request, error="The demo people are only added by the full tour.")
        return RedirectResponse(tour_start_path(), status_code=303)

    def _id_set(raw: Any) -> set[int]:
        return {int(x) for x in str(raw or "").split(",") if x.strip().isdigit()}

    async def tour_end(request: Request):
        mode = tour.state(store)["mode"]
        form = await request.form()
        try:
            removed = tour.end(store, _id_set(form.get("delete_people")), _id_set(form.get("delete_searches")))
        except ValueError as exc:  # the backup failed, so nothing was removed and the tour stays open
            return back(request, error=str(exc))
        if removed and (removed["people"] or removed["jobs"] or removed["kept_people"] or removed["kept_jobs"]):
            return RedirectResponse("/?" + urlencode({"notice": removal_notice(removed, "the demo data")}), status_code=303)
        if mode == tour.SHORT:
            return back(request)
        return RedirectResponse("/", status_code=303)

    @app.post("/tour/skip")
    async def tour_skip(request: Request):
        """End the tour now. Demo people the tour added are cleared; nothing else is touched."""
        return await tour_end(request)

    @app.post("/tour/finish")
    async def tour_finish(request: Request):
        """The last step: clear the demo people the tour added and close the tour for good."""
        return await tour_end(request)

    # ------------------------------------------------------------ jobs
    @app.get("/jobs", response_class=HTMLResponse)
    def jobs_page(request: Request, status: str = ""):
        jobs = store.list_jobs(status if status in JOB_STATUSES else None)
        hints = actions.twin_hints(jobs)
        by_manager = actions.by_manager(store)  # in-house: one row and one update per hiring manager
        if by_manager:
            rollup = actions.manager_rollup(store)
            companies = [{**r, "text": actions.status_digest(store, hiring_manager=r["manager"])}
                         for r in rollup if r["manager"]]
        else:
            rollup = [{**r, "label": r["company"]} for r in actions.company_rollup(store)]
            companies = [{**r, "text": actions.status_digest(store, r["company"])} for r in rollup if r["company"]]
        held = actions.held_jobs(store)
        return page(request, "jobs.html", jobs=[{**j, "hint": hints.get(j["id"])} for j in jobs], status=status,
                    rollup=rollup, companies=companies, held=held, by_manager=by_manager,
                    digest_text=actions.status_digest(store) if rollup or held else "",
                    hidden_notes=actions.digest_hidden_notes(store) if rollup else [])

    @app.post("/jobs/wording")
    async def save_wording(request: Request):
        """'I hire for my own company': in-house wording (Hiring team passed, Where each team stands)."""
        form = await request.form()
        on = str(form.get("inhouse") or "") == "1"
        actions.set_inhouse(store, on)
        notice = ("Using in-house wording, since you hire for your own company." if on
                  else "Using agency wording, since you hire for clients.")
        go = str(form.get("next") or "")
        go = go if go in ("/", "/settings", "/jobs") else "/jobs"
        return RedirectResponse(go + "?" + urlencode({"notice": notice}), status_code=303)

    @app.get("/jobs/new", response_class=HTMLResponse)
    def new_job(request: Request, company: str = ""):
        # From a client's page: the company is filled in, so the new job lands on that client.
        return page(request, "job_form.html", j={"stage_set": actions.default_stage_set(store),
                                                 "company": company.strip()[:200] or None}, error=None,
                    stage_sets=actions.STAGE_SETS, copy_jobs=store.list_jobs(None))

    @app.post("/jobs", response_class=HTMLResponse)
    async def create_job(request: Request):
        form = await request.form()
        data = {k: str(form.get(k) or "") for k in ("title", "company", "location", "description", *JOB_EXTRA_FIELDS)}
        stages = [s for s in str(form.get("stages") or "").splitlines() if s.strip()]
        picked = str(form.get("stage_set") or "default")
        copy_from = str(form.get("copy_from") or "").strip()
        twins = store.similar_open_jobs(data["title"], data["company"])
        try:
            if stages:
                chosen = stages
            elif copy_from.isdigit():
                chosen = actions.new_job_stages(store, from_job_id=int(copy_from))
            else:
                chosen = actions.stage_set(picked)
            from_set = not stages and not copy_from.isdigit()
            stages, out_note = actions.with_out_stages(chosen)
            job = store.add_job(**data, stages=stages or None)
            if from_set:
                actions.apply_set_groups(store, job["id"], picked)
        except ValueError as exc:
            return page(request, "job_form.html", j={**data, "stages": str(form.get("stages") or ""),
                                                     "stage_set": picked, "copy_from": copy_from}, error=str(exc),
                        stage_sets=actions.STAGE_SETS, copy_jobs=store.list_jobs(None))
        notes = ["Created."] if twins or out_note else []
        if twins:
            ids = ", ".join(f"#{j['id']}" for j in twins)
            notes.append(f"Heads up: open job {ids} has the same title and company. If it's a duplicate, close one "
                         "of them.")
        if out_note:
            notes.append(out_note)
        if notes:
            return RedirectResponse(f"/jobs/{job['id']}?{urlencode({'notice': ' '.join(notes)})}", status_code=303)
        return RedirectResponse(f"/jobs/{job['id']}", status_code=303)

    @app.get("/jobs/{job_id}", response_class=HTMLResponse)
    def job_detail(request: Request, job_id: int, view: str = "", q: str = "", show: str = "", ask_cancel: str = ""):
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
                   inject_warnings=injection.job_warnings(job),
                   q=q, add_results=add_results, prompt=recipes.source_for_job(job),
                   next_iv=actions.next_interviews(store, job_id), omit=omit,
                   status_text=actions.job_status_text(store, job_id, omit_stages=omit),
                   hidden_note=actions.hidden_note(store, job_id, omit),
                   nums=actions.job_numbers(store, job_id), todos=_todos(job_id), show=show,
                   omit_default=not actions.has_saved_omit(store, job_id) and not request.query_params.getlist("omit"),
                   close=actions.close_out_plan(store, job_id),
                   shown_active=actions.job_numbers(store, job_id, actions._omitted(store, job_id, omit))["active"],
                   ask=actions.exit_interviews(store, int(ask_cancel), job_id) if ask_cancel.isdigit() else None)
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
        plan = actions.close_out_plan(store, job_id) if data.get("status") == "closed" else None
        if plan:
            n = plan["count"]
            notice = (f"Job closed. Its pipeline is kept. {n} {'person is' if n == 1 else 'people are'} still in "
                      "play; you can move them out below.")
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice})}#close-out", status_code=303)
        if set(data) == {"status"} and data["status"] == "closed":
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': 'Job closed. Its pipeline is kept.'})}",
                                    status_code=303)
        return RedirectResponse(f"/jobs/{job_id}", status_code=303)

    @app.post("/jobs/{job_id}/close-out")
    async def close_out_form(request: Request, job_id: int):
        """Move everyone still in play to Rejected (Position filled), cancel their upcoming interviews and,
        when "Also close the job" is ticked, close it. Changes lists it as one change with its own undo."""
        job_or_404(job_id)
        form = await request.form()
        try:
            done = actions.close_out(store, job_id, by="app", close_job=str(form.get("close_job") or "") == "1")
        except ValueError as exc:
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'flash_error': str(exc)})}", status_code=303)
        n, m = done["moved"], done["cancelled"]
        notice = (f"Moved {n} {'person' if n == 1 else 'people'} to {done['stage']} (Position filled)"
                  + (f" and cancelled {m} upcoming interview{'' if m == 1 else 's'}." if m else ".")
                  + (" The job is closed." if done["closed_job"] else "")
                  + " You can undo this close-out from Changes.")
        return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice})}", status_code=303)

    @app.post("/jobs/{job_id}/cancel-interviews")
    async def cancel_interviews_form(request: Request, job_id: int):
        """The "Cancel it?" question after moving someone out or to hired."""
        job_or_404(job_id)
        form = await request.form()
        cid = str(form.get("candidate_id") or "")
        if not cid.isdigit():
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        keep = {i["id"] for i in actions.upcoming_for(store, int(cid), job_id)}
        ids = [int(v) for v in form.getlist("interview_id") if str(v).isdigit() and int(v) in keep]
        for iv in ids:
            store.update_interview(iv, cancel=True, candidate_id=int(cid), by="app")
        notice = (f"Cancelled {'the interview' if len(ids) == 1 else f'{len(ids)} interviews'}. It stays on the "
                  "timeline, marked cancelled." if ids else "Nothing to cancel. It may already be cancelled.")
        return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice})}", status_code=303)

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
        notice = ""
        try:
            if op != "add" and not name.strip():  # a blank name would otherwise mean the first stage
                raise ValueError("Pick which stage to change first. Nothing was changed.")
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
                picked = str(form.get("outcome") or "open")
                step = picked.split(":", 1)[1] if picked.startswith("open:") else None
                res = actions.change_stage_outcome(store, job_id, name, "open" if step else picked)
                notice = res["notice"]
                if step:  # In play, counted as this funnel step in client reports
                    got = reports.set_stage_group(store, job_id, name, step)
                    word = (reports.INHOUSE_LABELS if actions.inhouse(store) else reports.GROUP_LABELS)[step]
                    notice = " ".join(x for x in (notice, f"Reports now count {got['stage']} as {word}.") if x)
                if res["asks"]:  # people already in the stage who still have interviews: offer to cancel
                    return RedirectResponse(f"/jobs/{job_id}?{urlencode({'ask_cancel': res['asks'][0]['candidate_id'], 'notice': notice})}#pipeline",
                                            status_code=303)
            elif op == "remove":
                store.remove_empty_stage(job_id, name)
        except ValueError as exc:
            return page(request, "job_form.html", j=store.get_job(job_id), error=str(exc),
                        stages=store.job_stages(job_id), outcomes=STAGE_OUTCOMES, statuses=JOB_STATUSES)
        return RedirectResponse(f"/jobs/{job_id}/edit" + (f"?{urlencode({'notice': notice})}" if notice else "")
                                + "#stages", status_code=303)

    def resubmit_warning(candidate_id: int, job_id: int, before: str | None) -> str:
        """After an add or a move into a Submitted or Presented stage: the warning when the person was already
        submitted to this client in the last 12 months, else ''."""
        sub = reports.submittal_note(store, candidate_id, job_id)
        return (sub["warning"] or "") if sub and sub["stage"] != before else ""

    def add_with_warning(candidate_id: int, job_id: int) -> str:
        """add_to_pipeline (first stage), and the resubmit warning when that stage is a submittal."""
        before = next((p["stage"] for p in store.candidate_pipelines(candidate_id) if p["job_id"] == job_id), None)
        store.add_to_pipeline(candidate_id, job_id)
        elsewhere = None if before else outreach.other_clients_text(store, candidate_id, job_id)
        return " ".join(w for w in (resubmit_warning(candidate_id, job_id, before), elsewhere) if w)

    @app.post("/jobs/{job_id}/move", response_class=HTMLResponse)
    async def move(request: Request, job_id: int):
        job = job_or_404(job_id)
        form = await request.form()
        cid = str(form.get("candidate_id") or "")
        error, ask, resubmit = None, None, ""
        try:
            before = next((p["stage"] for p in store.candidate_pipelines(int(cid)) if p["job_id"] == job_id), None)
            store.move_stage(int(cid), job_id, str(form.get("stage") or ""), notes=str(form.get("notes") or "") or None,
                             by="app")
            ask = actions.exit_interviews(store, int(cid), job_id) or None
            resubmit = resubmit_warning(int(cid), job_id, before)
        except (ValueError, NotFound) as exc:
            error = str(exc)
        if is_htmx(request):
            view = str(form.get("view") or store.setting("pipeline_view"))
            return page(request, "_pipeline.html", job=job, groups=store.pipeline_by_stage(job_id),
                        stages=store.job_stages(job_id), view=view, error=error, ask=ask, resubmit=resubmit,
                        next_iv=actions.next_interviews(store, job_id))
        if ask:
            q = {"ask_cancel": cid, **({"flash_error": resubmit} if resubmit else {})}
            return RedirectResponse(f"/jobs/{job_id}?{urlencode(q)}#pipeline", status_code=303)
        return back(request, f"/jobs/{job_id}", error=error or resubmit)

    @app.get("/jobs/{job_id}/submit/{candidate_id}", response_class=HTMLResponse)
    def submit_page(request: Request, job_id: int, candidate_id: int):
        """Submit to client: what happens, any heads up, and the write-up draft to send from their own mail."""
        job = job_or_404(job_id)
        cand = cand_or_404(candidate_id)
        app_row = next((p for p in store.candidate_pipelines(candidate_id) if p["job_id"] == job_id), None)
        if app_row is None:
            raise HTTPException(404, "That person isn't on this job.")
        target = chase.submit_stage(store, job_id)
        done = bool(target) and app_row["stage"].lower() == target["name"].lower()
        return page(request, "submit.html", job=job, cand=cand, stage=app_row["stage"], target=target, done=done,
                    since=app_row["stage_changed_at"], draft=chase.writeup(store, candidate_id, job_id),
                    warning=None if done else chase.earlier_to_client(store, candidate_id, job_id),
                    elsewhere=chase.other_clients(store, candidate_id, job_id))

    @app.post("/jobs/{job_id}/submit/{candidate_id}")
    def submit_form(job_id: int, candidate_id: int):
        job_or_404(job_id)
        cand_or_404(candidate_id)
        go = f"/jobs/{job_id}/submit/{candidate_id}"
        try:
            got = chase.submit(store, candidate_id, job_id, by="app")
        except (ValueError, NotFound) as exc:
            return RedirectResponse(f"{go}?{urlencode({'flash_error': str(exc)})}", status_code=303)
        notice = (f"Moved to {got['stage']}. Now email the write-up from your own mail." if got["moved"]
                  else f"Already in {got['stage']}, so nothing moved.")
        return RedirectResponse(f"{go}?{urlencode({'notice': notice})}#draft", status_code=303)

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
        if not cid.isdigit():
            return RedirectResponse(f"/jobs/{job_id}", status_code=303)
        cand = cand_or_404(int(cid))
        warning = add_with_warning(cand["id"], job_id)
        # From the same-name box on Add: fill the details they have empty with what was typed. Never replace one.
        fill = {f: str(form.get("fill_" + f)).strip() for f in FILL_FIELDS
                if str(form.get("fill_" + f) or "").strip() and not (cand.get(f) or "").strip()}
        skipped = []
        while fill:
            try:
                store.update_candidate(cand["id"], by="app", **fill)
                break
            except DuplicateValue as exc:
                skipped.append(exc.label)
                fill.pop(exc.field, None)
            except ValueError:
                fill = {}
        notice = f"Added {cand['name']} to this job"
        if fill:
            notice += " and filled in their " + _and([FIELD_LABELS.get(f, f).lower() for f in fill])
        notice += "."
        if skipped:
            notice += f" Didn't fill the {_and(skipped)}: another saved candidate has it."
        q = {"notice": notice, **({"flash_error": warning} if warning else {})}
        return RedirectResponse(f"/jobs/{job_id}?{urlencode(q)}", status_code=303)

    # ------------------------------------------------------------ shortlist
    def to_shortlist(job_id: int, notice: str = "", error: str = "") -> RedirectResponse:
        q = urlencode([(k, v) for k, v in (("notice", notice), ("flash_error", error)) if v])
        return RedirectResponse(f"/jobs/{job_id}/shortlist" + (f"?{q}" if q else ""), status_code=303)

    @app.get("/jobs/{job_id}/shortlist", response_class=HTMLResponse)
    def shortlist_page(request: Request, job_id: int, choose: str = ""):
        """People the AI app proposed for this job. Keep saves (or links) them and adds them to the first stage;
        Pass sets them aside with an optional reason, so they aren't proposed for this job again."""
        job = job_or_404(job_id)
        rows = shortlist.listed(store, job_id, "proposed")
        for r in rows:
            r["check_lines"] = shortlist.check_lines(r.get("checks") or {})
        decided = shortlist.listed(store, job_id, "all", with_checks=False)
        return page(request, "shortlist.html", job=job, rows=rows, counts=shortlist.counts(store, job_id),
                    kept=[r for r in decided if r["status"] == "kept"],
                    passed=[r for r in decided if r["status"] == "passed"],
                    first_stage=(store.job_stages(job_id) or [{"name": "the first stage"}])[0]["name"],
                    keep_stages=shortlist.keep_stages(store, job_id),
                    choose=int(choose) if choose.isdigit() else None)

    @app.post("/jobs/{job_id}/shortlist/keep-all")
    async def shortlist_keep_all(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        try:
            done = shortlist.keep_all(store, job_id, stage=str(form.get("stage") or "") or None)
        except (ValueError, NotFound) as exc:
            return to_shortlist(job_id, error=str(exc))
        n, left, held = len(done["kept"]), done["left"], done.get("held") or []
        stage = done["kept"][0]["stage"] if done["kept"] else ""
        notice = (f"Kept {n} {'person' if n == 1 else 'people'}" + (f" and added them to {stage}" if n else "") + "."
                  + (" You can undo this on Changes." if n else "")
                  + (f" {len(left)} {'needs' if len(left) == 1 else 'need'} your choice first: {_and(left)}."
                     if left else "")
                  + (f" Left {len(held)} whose name reads like a sentence; keep them one at a time if they are "
                     "real people." if held else ""))
        warn = " ".join(k["elsewhere"] for k in done["kept"] if k.get("elsewhere"))
        return to_shortlist(job_id, notice=notice, error=warn)

    @app.post("/jobs/{job_id}/shortlist/pass-all")
    async def shortlist_pass_all(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        n = shortlist.pass_all(store, job_id, str(form.get("reason") or ""))
        return to_shortlist(job_id, notice=f"Passed on {n} {'person' if n == 1 else 'people'}. They won't be "
                                           "proposed for this job again." if n else "Nobody was waiting.")

    @app.post("/jobs/{job_id}/shortlist/{prospect_id}/keep")
    async def shortlist_keep(request: Request, job_id: int, prospect_id: int):
        job_or_404(job_id)
        form = await request.form()
        link = str(form.get("link_id") or "")
        try:
            done = shortlist.keep(store, job_id, prospect_id, link_id=int(link) if link.isdigit() else None,
                                  as_new=str(form.get("as_new") or "") == "1", stage=str(form.get("stage") or "") or None)
        except shortlist.NeedsChoice as exc:
            return RedirectResponse(f"/jobs/{job_id}/shortlist?{urlencode({'choose': prospect_id, 'flash_error': str(exc)})}"
                                    f"#p{prospect_id}", status_code=303)
        except (ValueError, NotFound) as exc:
            return to_shortlist(job_id, error=str(exc))
        c = done["candidate"]
        if done["already"]:
            notice = f"Kept {c['name']}. They were already in this job, in {done['stage']}."
        else:
            notice = (f"{'Saved' if done['created'] else 'Kept'} {c['name']} and added them to {done['stage']}"
                      + ("." if done["created"] else ", linked to the person you already had."))
        return to_shortlist(job_id, notice=notice, error=done.get("elsewhere") or "")

    @app.post("/jobs/{job_id}/shortlist/{prospect_id}/pass")
    async def shortlist_pass(request: Request, job_id: int, prospect_id: int):
        job_or_404(job_id)
        form = await request.form()
        try:
            p = shortlist.pass_on(store, job_id, prospect_id, str(form.get("reason") or ""))
        except (ValueError, NotFound) as exc:
            return to_shortlist(job_id, error=str(exc))
        return to_shortlist(job_id, notice=f"Passed on {p['name']}. They won't be proposed for this job again.")

    @app.post("/jobs/{job_id}/shortlist/{prospect_id}/put-back")
    def shortlist_put_back(job_id: int, prospect_id: int):
        job_or_404(job_id)
        try:
            p = shortlist.put_back(store, job_id, prospect_id)
        except (ValueError, NotFound) as exc:
            return to_shortlist(job_id, error=str(exc))
        return to_shortlist(job_id, notice=f"Put {p['name']} back on the Shortlist.")

    # ------------------------------------------------------------ follow-ups
    def followups_for(job_id: int) -> dict[str, Any]:
        """A job's follow-up sequence and where each person is in it, for the job page."""
        seq = outreach.sequence(store, job_id) or {}
        people = outreach.progress(store, job_id)
        for p in people:
            if p["status"] in ("due", "waiting"):
                p["due_text"] = outreach.due_text(p)
            elif p["status"] == "replied":
                move = outreach.reply_stage(store, p["candidate_id"], job_id)
                p["next_stage"] = move["to"] if move else None
        contact = outreach.contact_stage(store, job_id)
        return {"steps": seq.get("steps") or [], "since": seq.get("since"), "people": people,
                "defaults": list(outreach.DEFAULT_STEPS), "contact": contact["name"] if contact else None,
                "copy_from": outreach.sequence_jobs(store, exclude=job_id),
                "not_following": outreach.not_following(store, job_id) if seq else [],
                "preview": outreach.preview(seq.get("steps") or list(outreach.DEFAULT_STEPS)),
                "enrolled": len(seq.get("enrolled") or [])}

    @app.post("/jobs/{job_id}/sequence/enroll")
    async def enroll_existing(request: Request, job_id: int):
        """Enroll the people already messaged on this job into its follow-up steps (from today, or from their
        first message)."""
        job_or_404(job_id)
        form = await request.form()
        start = str(form.get("start") or "today")
        try:
            got = outreach.enroll_existing(store, job_id, None, start)
        except ValueError as exc:
            return back(request, f"/jobs/{job_id}#followups", error=str(exc))
        if not got:
            return back(request, f"/jobs/{job_id}#followups", notice="Nobody needed enrolling.")
        names = ", ".join(p["name"] for p in got[:3]) + (f" and {len(got) - 3} more" if len(got) > 3 else "")
        when = ("Their next follow-up counts from today." if start == "today" else
                "Their next follow-up comes due from their first message, so some may be due now.")
        return back(request, f"/jobs/{job_id}#followups", notice=(
            f"Enrolled {len(got)} {'person' if len(got) == 1 else 'people'} ({names}). {when}"))

    @app.post("/jobs/{job_id}/followups/batch")
    async def followups_batch(request: Request, job_id: int):
        """I sent them all: log the follow-up each ticked person is due, once each."""
        job_or_404(job_id)
        form = await request.form()
        ids = [int(v) for v in form.getlist("candidate_ids") if str(v).isdigit()]
        if not ids:
            return back(request, f"/jobs/{job_id}#followups", error="Nobody was picked.")
        try:
            got = outreach.log_sent_batch(store, job_id, ids)
        except NotFound as exc:
            return back(request, f"/jobs/{job_id}#followups", error=str(exc))
        n = len(got["logged"])
        notice = f"Logged the follow-up to {n} {'person' if n == 1 else 'people'}." if n else "Nothing new to log."
        if got["already"]:
            notice += f" Already logged today for {', '.join(got['already'][:3])}, so not again."
        return back(request, f"/jobs/{job_id}#followups", notice=notice)

    @app.get("/jobs/{job_id}/outreach", response_class=HTMLResponse)
    def outreach_page(request: Request, job_id: int):
        """Everyone messaged on a job: first message, step, next due, replied, source, and reply rates."""
        job = job_or_404(job_id)
        only = request.query_params.get("status") or None
        return page(request, "outreach.html", job=job, t=outreach.table(store, job_id, only=only),
                    not_following=len(outreach.not_following(store, job_id)))

    @app.post("/jobs/{job_id}/outreach/bulk")
    async def outreach_bulk(request: Request, job_id: int):
        """A bulk action on the people ticked on the Outreach tab: sent, replied, pause, resume or enroll."""
        job_or_404(job_id)
        form = await request.form()
        ids = [int(v) for v in form.getlist("candidate_ids") if str(v).isdigit()]
        try:
            notice = outreach.bulk_apply(store, job_id, str(form.get("action") or ""), ids,
                                         str(form.get("start") or "today"))
        except (ValueError, NotFound) as exc:
            return back(request, f"/jobs/{job_id}/outreach", error=str(exc))
        return back(request, f"/jobs/{job_id}/outreach", notice=notice)

    @app.post("/jobs/{job_id}/sequence/copy")
    async def copy_sequence(request: Request, job_id: int):
        """Copy steps from another job: the same cadence on a new req, with this job's title and client filled in."""
        job_or_404(job_id)
        form = await request.form()
        src = str(form.get("from_job_id") or "")
        try:
            if not src.isdigit():
                raise ValueError("Pick a job to copy the steps from.")
            steps = outreach.copy_sequence(store, job_id, int(src))
            name = store.get_job(int(src))["title"]
        except (ValueError, NotFound) as exc:
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'flash_error': str(exc), 'sequence': '1'})}#followups",
                                    status_code=303)
        notice = (f"Copied {len(steps)} steps from {name}. Follow-ups show on Today when they come due. "
                  "Check the text fits this job.")
        return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice})}#followups", status_code=303)

    @app.post("/jobs/{job_id}/sequence")
    async def save_sequence(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        try:
            steps = outreach.save_sequence(store, job_id, [] if form.get("remove") else outreach.steps_from_form(form))
        except ValueError as exc:
            return RedirectResponse(f"/jobs/{job_id}?{urlencode({'flash_error': str(exc), 'sequence': '1'})}#followups",
                                    status_code=303)
        notice = (f"Saved {len(steps)} steps. Follow-ups show on Today when they come due." if steps
                  else "Follow-ups are off for this job.")
        return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice})}#followups", status_code=303)

    @app.post("/jobs/{job_id}/followups/{candidate_id}/{op}")
    def followup_done(request: Request, job_id: int, candidate_id: int, op: str):
        """I sent it (logs the follow-up as a message on this job) or They replied (ends their follow-ups)."""
        job_or_404(job_id)
        cand = cand_or_404(candidate_id)
        if op not in ("sent", "replied"):
            raise HTTPException(404, "Not found")
        step = next((p for p in outreach.progress(store, job_id) if p["candidate_id"] == candidate_id), None)
        if op == "replied":
            entry = store.log(candidate_id, "message", "Replied", job_id=job_id, source="App")
            outreach.mark_reply(store, entry["id"])
            notice = f"Logged a reply from {cand['name']}. Their follow-ups for this job stop."
            move = outreach.reply_stage(store, candidate_id, job_id)
            if move:
                notice += f" They're still in {move['from']}: Move to {move['to']} is on Today when you're ready."
        else:
            label = step["label"] if step and step.get("label") else "Follow-up"
            store.log(candidate_id, "message", f"{label} sent", job_id=job_id, source="App")
            notice = f"Logged the {label.lower()} to {cand['name']}."
        return back(request, f"/jobs/{job_id}#followups", notice=notice)

    # ------------------------------------------------------------ clients
    @app.get("/clients", response_class=HTMLResponse)
    def clients_page(request: Request):
        return page(request, "clients.html", rows=reports.overview(store), no_company=reports.no_company_jobs(store))

    @app.get("/clients/due", response_class=HTMLResponse)
    def clients_due_page(request: Request):
        """The Friday send-out: every update due this week, ready to copy, email and mark as sent."""
        due = reports.due_updates(store)
        return page(request, "due.html", due=due, num=reports._num,
                    split={p["key"]: reports.split_note(store, p["key"]) for p in due["parts"]})

    @app.post("/clients/new")
    async def new_client(request: Request):
        """A client before its first job: name, and if known its contacts, update day and time zone."""
        form = await request.form()
        fields = {k: str(form.get(k) or "") for k in ("contacts", "update_day", "time_zone")}
        try:
            got = reports.create_client(store, str(form.get("name") or ""), **fields)
        except ValueError as exc:
            return RedirectResponse("/clients?" + urlencode({"flash_error": str(exc)}), status_code=303)
        c = got["client"]
        notice = (f"Added {c['name']}. Add its first job when the req comes in." if got["created"]
                  else f"{c['name']} is already here, so nothing changed.")
        return RedirectResponse(f"/clients/{c['slug']}?{urlencode({'notice': notice})}", status_code=303)

    @app.get("/clients/chase", response_class=HTMLResponse)
    def chase_page(request: Request):
        """Everyone waiting on a client's feedback, per client and hiring manager, with a nudge for each."""
        days = chase.chase_days(store)
        left: dict[str, Any] = {}
        groups = chase.chase_list(store, days=days, left_out=left)
        for g in groups:  # a snoozed nudge says when it was chased and when it's next due, and isn't drafted again
            if g.get("next_on"):
                g["chased_line"] = chase.chased_line(g.get("chased_at"), g["next_on"])
        return page(request, "chase.html", groups=groups, days=days, slug=reports.slug, left_out=left,
                    shown=chase.PAGE_SHOWN, show_all=str(request.query_params.get("all") or "")[:200],
                    max_days=chase.chase_max_days(store), stage_choices=chase.stage_choices(store))

    @app.post("/clients/chase/days")
    async def chase_days_form(request: Request):
        form = await request.form()
        try:
            n = chase.set_chase_days(store, form.get("days"))
            top = chase.set_chase_max_days(store, form.get("max_days")) if "max_days" in form else \
                chase.chase_max_days(store)
        except ValueError as exc:
            return RedirectResponse("/clients/chase?" + urlencode({"flash_error": str(exc)}), status_code=303)
        span = f"{n} to {top} days" if top else f"{n} days or more"
        return RedirectResponse("/clients/chase?" + urlencode({"notice": f"Now listing people waiting {span}."}),
                                status_code=303)

    @app.post("/clients/chase/stages")
    async def chase_stages_form(request: Request):
        """Which client-facing stages the chase counts (Onsite and reference checks are left out by default)."""
        form = await request.form()
        chase.set_stage_picks(store, [str(x) for x in form.getlist("shown")][:200],
                              [str(x) for x in form.getlist("count")][:200])
        return RedirectResponse("/clients/chase?" + urlencode({"notice": "Saved which stages the chase counts."}),
                                status_code=303)

    opsreview.register_routes(app, store, page)  # /clients/review, before /clients/{slug}

    def client_or_404(slug: str) -> dict[str, Any]:
        try:
            return reports.client(store, reports.unslug(slug))
        except NotFound:
            raise HTTPException(404, "Client not found")

    def client_report_or_error(c: dict[str, Any], since: str, until: str) -> tuple[dict[str, Any], str]:
        try:
            return reports.report(store, c["key"], since or None, until or None), ""
        except ValueError as exc:
            return reports.report(store, c["key"]), str(exc)

    @app.get("/clients/{slug}/desk", response_class=HTMLResponse)
    def client_desk_page(request: Request, slug: str):
        """One client's run sheet: the update, who to chase, feedback owed, offers and follow-ups, in work order."""
        c = client_or_404(slug)
        return page(request, "desk.html", c=c, d=desk.desk(store, c["key"]), max_items=desk.MAX_ITEMS)

    @app.get("/clients/{slug}", response_class=HTMLResponse)
    def client_page(request: Request, slug: str, since: str = "", until: str = ""):
        c = client_or_404(slug)
        rep, error = client_report_or_error(c, since, until)
        # In-house with hiring managers named, one update per manager; else the one client update.
        span = (None, None) if error else (since or None, until or None)
        parts = reports.reports_for(store, c["key"], *span) if reports.managers(store, c["key"]) else [rep]
        sections = [{"rep": r, "text": (t := reports.report_text(r)), "email": (e := reports.email_link(r, t)),
                     "mailto": e["href"] if e else None, "notes": reports.recruiter_notes(r),
                     "cc_email": chase.cc_main_link(r, t)} for r in parts]
        early = c.get("update_day") if c.get("update_day") and reports._day_start(
            c.get("update_day"), datetime.now(timezone.utc)) is None else None
        return page(request, "client.html", c=c, rep=rep, sections=sections, error=error, since=since, until=until,
                    early=early, split_note=reports.split_note(store, c["key"]),
                    submittals=reports.submittals(store, c["key"]),
                    updates=reports.updates(store, c["key"]), groups=reports.GROUPS, days=reports.UPDATE_DAYS,
                    labels=rep.get("labels") or (reports.INHOUSE_LABELS if rep["inhouse"] else reports.GROUP_LABELS),
                    zones=reports.COMMON_ZONES, in_zone=reports.in_zone, due=bool(reports.due_parts(store, c["key"])),
                    num=reports._num, ai_moves=[m for x in sections for m in x["rep"]["ai_moves"]],
                    since_day=_local(rep["since"], "%Y-%m-%d"),
                    until_day=_local(rep["until"], "%Y-%m-%d"))

    @app.post("/clients/{slug}", response_class=HTMLResponse)
    async def save_client(request: Request, slug: str):
        c = client_or_404(slug)
        form = await request.form()
        fields = {k: str(form.get(k) or "") for k in ("name", "contacts", "notes", "update_day", "time_zone") if k in form}
        try:
            reports.save_client(store, c["key"], **fields)
        except ValueError as exc:
            return RedirectResponse(f"/clients/{c['slug']}?{urlencode({'flash_error': str(exc)})}#details",
                                    status_code=303)
        return RedirectResponse(f"/clients/{c['slug']}?{urlencode({'notice': 'Saved.'})}#details", status_code=303)

    @app.post("/clients/{slug}/sent")
    async def client_sent(request: Request, slug: str):
        """Mark this update as sent: keep a copy, and start the next one from now."""
        c = client_or_404(slug)
        form = await request.form()
        back = "/clients/due" if form.get("back") == "due" else f"/clients/{c['slug']}"
        manager = str(form["manager"]) if "manager" in form else None  # one hiring manager's update, in-house
        since, until = str(form.get("since") or "") or None, str(form.get("until") or "") or None
        try:
            # The confirm named the AI moves the page showed; one made since then stops it, so it's seen first.
            seen = {int(v) for v in str(form.get("ai_seen") or "").split(",") if v.strip().isdigit()}
            new = [m for m in reports.unreviewed_in(store, c["key"], manager, since, until)
                   if m["action_id"] not in seen]
            if new:
                it = "it" if len(new) == 1 else "them"
                raise ValueError(f"Not marked as sent: the update has {len(new)} move{'' if len(new) == 1 else 's'} "
                                 f"your AI app made that you haven't seen here yet: {reports.ai_moves_list(new)}. "
                                 f"Check {it} on this page, then mark the update as sent.")
            done = reports.mark_sent(store, c["key"], since, until, manager=manager)
        except ValueError as exc:
            return RedirectResponse(f"{back}?{urlencode({'flash_error': str(exc)})}", status_code=303)
        who = f"{done['manager']}'s update" if done.get("manager") else (
            "The update for jobs with no hiring manager" if manager is not None else "")
        if done.get("already"):
            notice = (f"Already marked as sent at {_local(done['sent_at'], '%-I:%M %p')}. Nothing new since, so "
                      "nothing changed.")
            return RedirectResponse(f"{back}?{urlencode({'notice': notice})}", status_code=303)
        if back == "/clients/due":
            notice = f"Marked {who or 'the update to ' + c['name']} as sent. A copy is kept on its page."
        else:
            notice = (f"Marked {who} as sent. A copy is kept below, and their next update starts from now." if who
                      else "Marked as sent. A copy is kept below, and the next update starts from now.")
        return RedirectResponse(f"{back}?{urlencode({'notice': notice})}", status_code=303)

    @app.post("/clients/{slug}/chased")
    async def client_chased(request: Request, slug: str):
        """The recruiter sent a hiring manager's nudge: Today stops asking until it's due again."""
        c = client_or_404(slug)
        form = await request.form()
        mk = reports.manager_key(str(form.get("manager") or ""))
        chase.mark_chased(store, c["key"], mk)
        who = str(form.get("manager_name") or "").strip()[:200] or c["name"]
        if form.get("back") == "desk":
            return RedirectResponse(f"/clients/{c['slug']}/desk?" + urlencode({"notice": f"Marked {who} as chased today."}),
                                    status_code=303)
        return RedirectResponse("/clients/chase?" + urlencode({"notice": f"Marked {who} as chased today."}),
                                status_code=303)

    @app.post("/clients/{slug}/chased-all")
    async def client_chased_all(request: Request, slug: str):
        """Mark the whole client chased, every hiring manager at once, and keep Today quiet for N days."""
        c = client_or_404(slug)
        form = await request.form()
        try:
            n = chase.snooze_days_from(form.get("snooze"))
        except ValueError as exc:
            return RedirectResponse("/clients/chase?" + urlencode({"flash_error": str(exc)}), status_code=303)
        chase.mark_client_chased(store, c["key"], n)
        span = f" Today stays quiet about them for {n} days." if n else ""
        return RedirectResponse("/clients/chase?" + urlencode({"notice": f"Marked {c['name']} as chased.{span}"}),
                                status_code=303)

    @app.post("/clients/{slug}/note")
    async def client_note_form(request: Request, slug: str):
        """The note that opens this client's next update (one per hiring manager when updates are split)."""
        c = client_or_404(slug)
        form = await request.form()
        manager = str(form["manager"]) if "manager" in form else None
        back = "/clients/due" if form.get("back") == "due" else f"/clients/{c['slug']}"
        try:
            text = reports.save_client_note(store, c["key"], str(form.get("note") or ""), manager)
        except ValueError as exc:
            return RedirectResponse(f"{back}?{urlencode({'flash_error': str(exc)})}", status_code=303)
        notice = ("Saved. The note opens the update, and Mark as sent clears it." if text
                  else "Took the note out of the update.")
        return RedirectResponse(f"{back}?{urlencode({'notice': notice})}#report", status_code=303)

    @app.post("/clients/{slug}/ai-reviewed")
    async def client_ai_reviewed(request: Request, slug: str):
        """The recruiter has looked at the AI's moves in this update: stop flagging them."""
        c = client_or_404(slug)
        form = await request.form()
        back = "/clients/due" if form.get("back") == "due" else f"/clients/{c['slug']}"
        n = reports.mark_ai_reviewed(store, [str(v) for v in form.getlist("action_id")])
        notice = (f"Marked {n} AI move{'' if n == 1 else 's'} as reviewed." if n
                  else "Those AI moves were already marked as reviewed.")
        return RedirectResponse(f"{back}?{urlencode({'notice': notice})}", status_code=303)

    @app.post("/clients/{slug}/updates/{update_id}/undo")
    def client_sent_undo(slug: str, update_id: int):
        c = client_or_404(slug)
        try:
            reports.undo_sent(store, c["key"], update_id)
        except ValueError as exc:
            return RedirectResponse(f"/clients/{c['slug']}?{urlencode({'flash_error': str(exc)})}", status_code=303)
        notice = "Took back that Mark as sent. The update starts where it did before."
        return RedirectResponse(f"/clients/{c['slug']}?{urlencode({'notice': notice})}", status_code=303)

    @app.get("/clients/{slug}/pipeline.csv")
    def client_csv(slug: str, since: str = "", until: str = "", out: str = ""):
        c = client_or_404(slug)
        rep, _ = client_report_or_error(c, since, until)
        stamp = datetime.now().strftime("%Y-%m-%d")
        name = re.sub(r"[^A-Za-z0-9-]+", "-", c["slug"]).strip("-") or "client"
        return Response(reports.pipeline_csv(rep, with_out=out == "1"), media_type="text/csv; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}-pipeline-{stamp}.csv"'})

    # ------------------------------------------------------- dashboard acts
    @app.post("/actions/interviews/out")
    async def exit_interviews_form(request: Request):
        """Today's "Dana is out of Backend but still has an interview" row: Cancel interview, or Keep it."""
        form = await request.form()
        cid, jid, op = (str(form.get(k) or "") for k in ("candidate_id", "job_id", "op"))
        group = next((g for g in actions.exit_prompts(store) if str(g["candidate_id"]) == cid
                      and str(g["job_id"]) == jid), None) if cid.isdigit() and jid.isdigit() else None
        if group is None or op not in ("cancel", "keep"):
            return back(request, notice="Nothing to change. That interview may already be cancelled or kept.")
        ids = [i["id"] for i in group["interviews"]]
        for iv in ids:
            if op == "cancel":
                store.update_interview(iv, cancel=True, candidate_id=int(cid), by="app")
            else:
                store.update_interview(iv, keep=True, candidate_id=int(cid), by="app")
        n = "the interview" if len(ids) == 1 else f"{len(ids)} interviews"
        notice = (f"Cancelled {n}. It stays on the timeline, marked cancelled." if op == "cancel"
                  else f"Kept {n}. It shows on Today as usual.")
        return back(request, notice=notice)

    @app.post("/actions/snooze")
    async def snooze(request: Request):
        form = await request.form()
        key = str(form.get("key") or "")
        days = str(form.get("days") or "7")
        if not key or not days.isdigit() or not 0 < int(days) < 3650:
            return back(request, error="Couldn't snooze that. Reload the page and try again.")
        store.snooze(key, int(days))
        return back(request, notice=f"Snoozed for {days} day{'' if days == '1' else 's'}.")

    @app.post("/actions/snooze-import")
    async def snooze_import(request: Request):
        """Snooze every stuck reminder about people one import or sync brought in."""
        form = await request.form()
        run = str(form.get("run") or "")[:40]
        n = actions.snooze_import(store, run) if run else 0
        if not n:
            return back(request, error="Nothing to snooze. Those reminders may be gone already.")
        return back(request, notice=f"Snoozed {n} stuck reminder{'' if n == 1 else 's'} from that import for "
                                    f"{actions.STUCK_SNOOZE_DAYS} days.")

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

    # ------------------------------------------------- deciding and interviews
    def _ids(raw: str) -> list[int]:
        return [int(x) for x in re.split(r"[,\s]+", raw or "") if x.isdigit()][:6]

    def _decide_back(job_id: int, error: str) -> RedirectResponse:
        """Always back to a fresh Decide form (never the page the stale form was sent from)."""
        return RedirectResponse(f"/jobs/{job_id}/decide?{urlencode({'flash_error': error})}", status_code=303)

    def _render_decide(request: Request, job_id: int, ids: str = "", warn: dict[str, Any] | None = None):
        job = job_or_404(job_id)
        try:
            d = decide.finalists(store, job_id, _ids(ids) or None)
        except ValueError as exc:
            return back(request, f"/jobs/{job_id}/decide", error=str(exc))
        return page(request, "decide.html", job=job, people=d["people"], more=d["more"], offer_stage=d["offer_stage"],
                    out_stage=d["out_stage"], ids=",".join(map(str, _ids(ids))), warn=warn,
                    pass_label="Hiring team passed" if actions.inhouse(store) else "Client passed")

    @app.get("/jobs/{job_id}/decide", response_class=HTMLResponse)
    def decide_page(request: Request, job_id: int, ids: str = ""):
        """The finalists side by side: every interview, who ran it, how it went and what each interviewer said."""
        return _render_decide(request, job_id, ids)

    @app.post("/jobs/{job_id}/decide")
    async def decide_form(request: Request, job_id: int):
        """Record a decision: offer to one, pass on the others. One change, undone as one under Changes. When
        feedback or an outcome is still missing for anyone in it, the page asks first (and can add nudge to-dos)."""
        job_or_404(job_id)
        form = await request.form()
        offer = str(form.get("offer") or "")
        if not offer.isdigit() and offer != "none":
            return _decide_back(job_id, "Pick who gets the offer, or No offer yet.")
        passed = [int(v) for v in form.getlist("pass") if str(v).isdigit()]
        offer_id = int(offer) if offer.isdigit() else None
        reason, note = str(form.get("reason") or "") or None, str(form.get("note") or "") or None
        if not form.get("confirmed"):
            missing = decide.missing_for(store, job_id, ([] if offer_id is None else [offer_id]) + passed)
            if missing:
                return _render_decide(request, job_id, str(form.get("ids") or ""), warn={
                    "lines": decide.warning_lines(missing), "offer": offer, "pass": passed, "reason": reason or "",
                    "note": note or "", "ids": str(form.get("ids") or ""),
                    "owed": bool(any(m["owed"] for m in missing))})
        try:
            res = decide.record_decision(store, job_id, offer_id, passed, reason=reason, note=note, by="app",
                                         nudge=bool(form.get("nudge")))
        except (ValueError, NotFound) as exc:
            if "in play" in str(exc):  # someone moved since the form was opened: a fresh form, not a bare error
                return RedirectResponse(f"/jobs/{job_id}/decide?" + urlencode(
                    {"notice": "Things changed since you opened this. Here's the latest."}), status_code=303)
            return _decide_back(job_id, str(exc))
        lets = res.get("todos", 0) - res.get("nudged", 0)
        notice = f"Recorded: {res['text']}." + (
            f" Cancelled {res['cancelled']} interview{'' if res['cancelled'] == 1 else 's'} still ahead." if res["cancelled"]
            else "") + (f" Added {lets} to-do{'' if lets == 1 else 's'} on Today to let them know." if lets > 0 else ""
                        ) + (f" Added {res['nudged']} to-do{'' if res['nudged'] == 1 else 's'} to ask for the missing "
                             "feedback." if res.get("nudged") else "") + " You can undo it under Changes."
        return RedirectResponse(f"/jobs/{job_id}?{urlencode({'notice': notice})}#pipeline", status_code=303)

    # ------------------------------------------------- the loop planner
    def _loop_page(request: Request, job_id: int, candidate: str = "", start: str = "", out: str = ""):
        job = job_or_404(job_id)
        tpl = loops.get_template(store, job_id)
        people = actions.in_play(store, job_id)
        plan, error = None, ""
        if candidate.isdigit() and tpl:
            try:
                plan = loops.plan(store, job_id, int(candidate), start or None, interviews.interviewers(out))
            except (ValueError, NotFound) as exc:
                error = str(exc)
        return page(request, "loop.html", job=job, tpl=tpl, tpl_text=loops.template_text(tpl["sessions"]) if tpl else "",
                    hours=f"{tpl['hours'][0]}-{tpl['hours'][1]}" if tpl else "9-17", people=people,
                    cand_id=int(candidate) if candidate.isdigit() else None, start=start, out=out, plan=plan,
                    plan_error=error)

    @app.get("/jobs/{job_id}/loop", response_class=HTMLResponse)
    def loop_page(request: Request, job_id: int, candidate: str = "", start: str = "", out: str = ""):
        """Plan a candidate's onsite loop from the job's template: slots, warnings and invite text to paste."""
        return _loop_page(request, job_id, candidate, start, out)

    @app.post("/jobs/{job_id}/loop/template")
    async def loop_template_form(request: Request, job_id: int):
        job_or_404(job_id)
        form = await request.form()
        try:
            saved = loops.save_template(store, job_id, str(form.get("sessions") or ""), str(form.get("hours") or ""))
        except ValueError as exc:
            return RedirectResponse(f"/jobs/{job_id}/loop?{urlencode({'flash_error': str(exc)})}", status_code=303)
        note = (f"Saved the loop template: {len(saved['sessions'])} session{'' if len(saved['sessions']) == 1 else 's'}."
                if saved["sessions"] else "Removed the loop template.")
        return RedirectResponse(f"/jobs/{job_id}/loop?{urlencode({'notice': note})}", status_code=303)

    @app.post("/jobs/{job_id}/loop/book")
    async def loop_book_form(request: Request, job_id: int):
        """Log the planned sessions as interviews (the same as logging each by hand). No calendar is written."""
        job_or_404(job_id)
        form = await request.form()
        cid = str(form.get("candidate_id") or "")
        names, ats = form.getlist("s_name"), form.getlist("s_at")
        mins, whos = form.getlist("s_min"), form.getlist("s_who")
        try:
            if not cid.isdigit() or not names or not len(names) == len(ats) == len(mins) == len(whos):
                raise ValueError("There is nothing to book. Plan the loop again.")
            tpl = {x["name"].casefold(): x["name"] for x in (loops.get_template(store, job_id) or {"sessions": []})["sessions"]}
            slots = [{"session": tpl[n.casefold()], "at": parse_when(a), "minutes": max(10, min(240, int(m))),
                      "interviewer": w or None}
                     for n, a, m, w in zip(names, ats, mins, whos) if n.casefold() in tpl and m.isdigit()][:loops.MAX_SESSIONS]
            if not slots:
                raise ValueError("There is nothing to book. Plan the loop again.")
            loops.book(store, int(cid), job_id, slots, by="app")
        except (ValueError, NotFound) as exc:
            return RedirectResponse(f"/jobs/{job_id}/loop?{urlencode({'flash_error': str(exc), 'candidate': cid})}",
                                    status_code=303)
        note = f"Logged {len(slots)} interview{'' if len(slots) == 1 else 's'}. You can see them under Interviews."
        return RedirectResponse(f"/jobs/{job_id}/loop?{urlencode({'notice': note})}", status_code=303)

    @app.post("/candidates/{candidate_id}/interviews/{entry_id}/feedback")
    async def feedback_form(request: Request, candidate_id: int, entry_id: int):
        """Add one interviewer's feedback to an interview, from the Decide view or the profile."""
        cand_or_404(candidate_id)
        form = await request.form()
        nxt = str(form.get("next") or "")
        nxt = nxt if safe_local_path(nxt.split("?", 1)[0]) else f"/candidates/{candidate_id}"
        verdict = str(form.get("verdict") or "").strip()
        who = str(form.get("interviewer") or "") or None
        try:
            if verdict:  # their own verdict too: the session's outcome rolls up from everyone's
                interviews.give_verdict(store, entry_id, verdict, who, str(form.get("text") or ""),
                                        candidate_id=candidate_id, by="app")
            else:
                store.add_interview_feedback(entry_id, str(form.get("text") or ""), who, candidate_id=candidate_id,
                                             by="app")
        except NotFound:
            raise HTTPException(404, "That interview isn't on this profile.")
        except ValueError as exc:
            return back(request, nxt, error=str(exc))
        sep = "&" if "?" in nxt else "?"
        left = interviews.owed_after(store.get_interview(entry_id))
        return RedirectResponse(f"{nxt}{sep}{urlencode({'notice': 'Feedback saved.' + left})}#iv-{entry_id}",
                                status_code=303)

    @app.post("/candidates/{candidate_id}/interviews/{entry_id}/verdict")
    async def verdict_form(request: Request, candidate_id: int, entry_id: int):
        """Record or change one interviewer's verdict on a session, from the Decide view."""
        cand_or_404(candidate_id)
        form = await request.form()
        nxt = str(form.get("next") or "")
        nxt = nxt if safe_local_path(nxt.split("?", 1)[0]) else f"/candidates/{candidate_id}"
        try:
            iv = interviews.give_verdict(store, entry_id, str(form.get("verdict") or ""),
                                         str(form.get("interviewer") or "") or None, candidate_id=candidate_id, by="app")
        except NotFound:
            raise HTTPException(404, "That interview isn't on this profile.")
        except ValueError as exc:
            return back(request, nxt, error=str(exc))
        notice = f"Saved. The session now reads {interview_label(iv['data']) or 'as recorded'}." + \
            interviews.owed_after(iv)
        sep = "&" if "?" in nxt else "?"
        return RedirectResponse(f"{nxt}{sep}{urlencode({'notice': notice})}#iv-{entry_id}", status_code=303)

    @app.get("/interviews/{interview_id}/job", response_class=HTMLResponse)
    def interview_job_page(request: Request, interview_id: int):
        """An interview logged with no job: pick which job it's for, so it lands in the right client's update."""
        try:
            got = chase.interview_jobs(store, interview_id)
        except NotFound:
            raise HTTPException(404, "Interview not found")
        return page(request, "interview_job.html", iv=got["interview"], jobs=got["jobs"])

    @app.post("/interviews/{interview_id}/job")
    async def interview_job_form(request: Request, interview_id: int):
        form = await request.form()
        jid = str(form.get("job_id") or "")
        try:
            iv = chase.set_interview_job(store, interview_id, int(jid) if jid.isdigit() else -1)
        except NotFound:
            raise HTTPException(404, "Interview not found")
        except ValueError as exc:
            return RedirectResponse(f"/interviews/{interview_id}/job?{urlencode({'flash_error': str(exc)})}",
                                    status_code=303)
        job = store.get_job(iv["job_id"])
        notice = f"That interview is now for {actions.job_label(job['title'], job.get('company'))}."
        return RedirectResponse(f"/candidates/{iv['candidate_id']}?{urlencode({'notice': notice})}#timeline",
                                status_code=303)

    @app.get("/interviews", response_class=HTMLResponse)
    def interviews_page(request: Request, week: str = "", view: str = "", month: str = ""):
        """The month calendar (the default), or with ?view=week or ?week=<day> the week: every session booked
        across all jobs, grouped into loops, with clashes and what's owed flagged."""
        calendar_prompt = recipes.log_calendar_interviews()
        if week or view == "week":
            try:
                day = interviews.week_day(week)
            except ValueError:
                day = None
            return page(request, "interviews.html", w=interviews.week(store, day), calendar_prompt=calendar_prompt)
        try:
            first = interviews.month_first(month)
        except ValueError:
            first = interviews.month_first("")
        return page(request, "interviews_month.html", m=interviews.month(store, first), calendar_prompt=calendar_prompt)

    @app.get("/interviews.ics")
    def interviews_ics(month: str = ""):
        """The month's interviews (or every upcoming one) as a calendar file for the recruiter's own calendar app."""
        try:
            first = interviews.month_first(month) if month.strip() else None
        except ValueError:
            raise HTTPException(400, "month is like 2026-11.")
        name = f"interviews-{first.strftime('%Y-%m')}.ics" if first else "interviews-upcoming.ics"
        return Response(interviews.ics_for_month(store, first), media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="{name}"'})

    @app.get("/candidates/{candidate_id}/interviews/{entry_id:int}.ics")
    def interview_ics(candidate_id: int, entry_id: int):
        """One interview as a calendar file (Add to calendar on the profile)."""
        try:
            body = interviews.ics_for_interview(store, candidate_id, entry_id)
        except LookupError:
            raise HTTPException(404, "Interview not found")
        return Response(body, media_type="text/calendar; charset=utf-8",
                        headers={"Content-Disposition": f'attachment; filename="interview-{entry_id}.ics"'})

    @app.post("/candidates/{candidate_id}/interviews/{entry_id}/session")
    async def add_session_form(request: Request, candidate_id: int, entry_id: int):
        """Add another session on the same day and job as this interview, so an onsite loop can be built here."""
        cand_or_404(candidate_id)
        form = await request.form()
        try:
            iv = store.get_interview(entry_id, candidate_id)
        except NotFound:
            raise HTTPException(404, "That interview isn't on this profile.")
        at = str(form.get("time") or "").strip()
        try:
            if not re.fullmatch(r"\d{1,2}:\d{2}", at):
                raise ValueError("Pick a start time for the new session.")
            day = _local(iv["at"], "%Y-%m-%d")
            new = store.log(candidate_id, "interview", str(form.get("body") or ""), at=f"{day}T{at}",
                            job_id=iv.get("job_id"), source="App",
                            interviewer=str(form.get("interviewer") or "") or None)
        except ValueError as exc:
            return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'error': str(exc)})}#iv-{entry_id}",
                                    status_code=303)
        notice = f"Added a session at {_local(new['at'], IV_TIME)}. It shows with the others as one loop."
        return RedirectResponse(f"/candidates/{candidate_id}?{urlencode({'notice': notice})}#iv-{new['id']}",
                                status_code=303)

    offers.register_routes(app, store, page, back)  # the offer page, its answer and its one-click move
    plan.register_routes(app, store, page, back)  # the Hiring plan page

    @app.post("/changes/decision/{batch_id}/undo")
    def undo_decision_form(request: Request, batch_id: int):
        try:
            notice = decide.undo_decision(store, batch_id, by="app")
        except NotFound:
            return back(request, "/changes", error="That decision is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=str(exc))
        return back(request, "/changes", notice=notice)

    @app.post("/actions/advance")
    async def advance(request: Request):
        form = await request.form()
        cid, jid, stage = (str(form.get(k) or "") for k in ("candidate_id", "job_id", "stage"))
        if not (cid.isdigit() and jid.isdigit() and stage):
            return back(request, error="Couldn't move that candidate. Reload the page and try again.")
        try:
            app_row = store.move_stage(int(cid), int(jid), stage, notes=str(form.get("notes") or "").strip() or None,
                                       by="app")
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
                             "who_label": "You, on the Shortlist card in your AI app" if d.get("via") == "claude" else None,
                             "name": r["candidate_name"], "href": f"/candidates/{r['candidate_id']}#timeline",
                             "text": ("Undo: " if (r["body"] or "").startswith("Undid") else "")
                             + ("; ".join(lines) if lines else r["body"] or ""),
                             "undoable": bool(d.get("old")) and not d.get("undone_at") and not d.get("merged"),
                             "undone": bool(d.get("undone_at")), "undo_url": f"/changes/{r['id']}/undo",
                             "by_import": d.get("by") == "import"})
        if who in ("", "ai"):
            ai = store.ai_actions(since, limit)
            for a in ai:
                if a.get("batch_id"):  # one save_candidates call is one row: the batch's own, below
                    continue
                if a["candidate_id"]:
                    name, href = a["candidate_name"], f"/candidates/{a['candidate_id']}#timeline"
                elif a["job_id"]:
                    name, href = a["job_title"], f"/jobs/{a['job_id']}"
                elif a["kind"].startswith("client_"):
                    d = a.get("new_data") or {}
                    name, href = d.get("_client") or "Client", f"/clients/{reports.slug(d.get('_key') or '')}"
                else:
                    name, href = "To-do", "/"
                lines = change_lines(a["change_data"]) if a["kind"] == "profile" else []
                rows.append({"at": a["at"], "who": "ai", "name": name, "href": href,
                             "text": "; ".join(lines) if lines else ai_change_text(a), "undoable": a["undoable"],
                             "undone": bool(a["undone_at"]), "note": a.get("undo_note"),
                             "blocked": a.get("blocked"), "record": a.get("record"),
                             "undo_url": f"/changes/ai/{a['id']}/undo",
                             # newest first, so every newer AI change is already in this list
                             "from_count": sum(1 for b in ai if b["id"] >= a["id"] and b["undo_pending"])
                             if a["undo_pending"] else 0,
                             "undo_from_url": f"/changes/ai/{a['id']}/undo-from"})
                if a["kind"] in BATCH_KINDS and a.get("children"):  # "this and everything after" starts at its first save
                    first = min(a["children"])
                    rows[-1].update(undo_label="Undo this batch", undo_from_url=f"/changes/ai/{first}/undo-from",
                                    from_count=sum(1 for b in ai if b["id"] >= first and b["undo_pending"]))
        if who in ("", "app"):  # a Keep or Pass on Review (a kept change is also an AI row, with its Undo)
            for r in review.decided(store, since, limit):
                name, href = (r["candidate_name"], f"/candidates/{r['candidate_id']}#timeline") if r["candidate_id"] \
                    else (r["job_title"] or "AI change", f"/jobs/{r['job_id']}" if r["job_id"] else "/review")
                result = r.get("result") or ""
                verb = "Passed" if r["status"] == "passed" and not result.startswith("Couldn't") else "Kept"
                text = f"{verb} on Review: {r['summary']}" + (f". {result}" if result.startswith("Couldn't") else "")
                rows.append({"at": r["decided_at"], "who": "app", "name": name, "href": href, "text": text,
                             "undoable": False, "undone": False,
                             "hint": "Its change is listed as your AI app's, with Undo." if verb == "Kept" and
                             not result.startswith("Couldn't") else None})
        if who in ("", "import"):  # each import is one row, with Undo while its backup can still put it back
            rows.extend(import_change_rows(since)[:limit])
        if who in ("", "app"):  # a web close-out: one row for the whole batch, with its own undo
            for b in actions.close_out_batches(store, since)[:limit]:
                rows.append({"at": b["at"], "who": "app", "name": b["job_title"], "href": f"/jobs/{b['job_id']}",
                             "text": b["text"], "undoable": b["undoable"], "undone": not b["undoable"],
                             "undo_url": f"/changes/close-out/{b['id']}/undo", "undo_label": "Undo this close-out"})
        if who in ("", "app"):  # a Keep or Keep all on a Shortlist: one row each, with its own undo
            by_card = {r["target_id"] for r in store._rows("SELECT target_id FROM ai_actions WHERE kind = 'card_keep'")}
            for b in shortlist.keep_batches(store, since)[:limit]:
                if b["id"] in by_card:  # pressed on the card in Claude: listed as your AI app's, with Undo
                    continue
                rows.append({"at": b["at"], "who": "app", "name": b["job_title"], "href": f"/jobs/{b['job_id']}/shortlist",
                             "text": b["text"], "undoable": b["undoable"], "undone": not b["undoable"],
                             "note": b.get("note"), "undo_url": f"/changes/shortlist-keep/{b['id']}/undo",
                             "undo_label": "Undo this keep",
                             "confirm": "Undo this keep? They go back on the Shortlist and out of the job. People it "
                                        "saved are taken out again if nobody worked on them since; a backup is "
                                        "saved first."})
        if who in ("", "app"):  # a decision recorded in the Decide view: one row, with its own undo
            for b in decide.decision_batches(store, since)[:limit]:
                rows.append({"at": b["at"], "who": "app", "name": b["job_title"], "href": f"/jobs/{b['job_id']}/decide",
                             "text": b["text"], "undoable": b["undoable"], "undone": not b["undoable"],
                             "undo_url": f"/changes/decision/{b['id']}/undo", "undo_label": "Undo this decision",
                             "confirm": "Undo this decision? Everyone goes back to the stage they were in, and the "
                                        "interviews it cancelled are put back."})
        if who in ("", "app"):  # what a stage counts as, changed in the app
            for c in actions.stage_outcome_changes(store, since)[:limit]:
                rows.append({"at": c["at"], "who": "app", "name": c["job_title"], "href": f"/jobs/{c['job_id']}/edit#stages",
                             "text": c["text"], "undoable": c["undoable"], "undone": not c["undoable"],
                             "undo_url": f"/changes/stage-outcome/{c['id']}/undo"})
        rows = tuck_import_rows(rows)
        rows.sort(key=lambda r: r["at"], reverse=True)  # stable, so each list keeps its own newest-first order
        return rows[:limit]

    def import_change_rows(since: str | None) -> list[dict[str, Any]]:
        out = []
        for e in backups.import_log(store):
            if since and e["at"] < since:
                continue
            name = e.get("backup") or ""
            state = backups.import_undo_state(store, name) if name else "none"
            hint = None
            if state == "changed":
                hint = ("Something changed since, so it can't be undone here. Its backup is under Automatic backups "
                        "in Settings.")
            elif state == "gone" and backups.backup_file(name):
                hint = "Its backup is under Automatic backups in Settings."
            out.append({"at": e["at"], "who": "import", "name": "CSV import", "href": "/settings#backups",
                        "text": backups.import_text(e), "undoable": state == "ok", "undone": False, "hint": hint,
                        "undo_url": f"/changes/import/{name}/undo", "undo_label": "Undo this import",
                        "span": (e.get("started") or e["at"], e["at"]),
                        "confirm": "Undo this import? Your data goes back to how it was just before it. A copy with "
                                   "the import in it is backed up first."})
        return out

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
        ai_pending = sum(1 for a in store.ai_actions(ai_since, limit=1_000_000)
                         if a["undo_pending"] and not a.get("confirmed_at"))
        ai_waiting = store._row("SELECT count(*) AS n FROM ai_review WHERE status = 'pending' AND created_at >= ?",
                                (ai_since,))["n"]
        return page(request, "changes.html", rows=rows, who=who, since=since, who_options=CHANGE_WHO,
                    ai_waiting=ai_waiting, ai_sessions=sessions.listing(store) if who in ("", "ai") else [],
                    since_options=CHANGE_SINCE, ai_since=ai_since, ai_pending=ai_pending, limit=CHANGES_SHOWN,
                    undo_last=last_undo_ai(), ai_allowance=ai_caps.allowance(store),
                    ai_since_epoch=int(to_dt(ai_since).timestamp()), undo_windows=UNDO_WINDOWS,
                    undo_window={"1": "24", "7": "168", "30": "720"}.get(since, "24"))

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

    def ai_backup():
        """Undoing a person or job the AI added deletes it, so back up first, once per request, as Delete does."""
        done: list[Path] = []

        def run() -> None:
            if not done:
                done.append(importers.backup_before_import(store, reason="delete"))
        return run

    def undo_ai_batch(request: Request, todo: list[dict[str, Any]], none_left: str, waiting_since: str | None = None,
                      include_kept: bool = False):
        """Undo these AI changes, newest first, skipping ones already undone, and say what happened. With
        waiting_since, what the AI app left waiting on Review since then is passed too. Changes the recruiter
        kept (on Review, or with Yes on Today) stay unless include_kept."""
        undone, skipped = [], []
        kept = [a for a in todo if a.get("confirmed_at") and a["undo_pending"]]
        if not include_kept:
            todo = [a for a in todo if not a.get("confirmed_at")]
        queued = (store._row("SELECT count(*) AS n FROM ai_review WHERE status = 'pending' AND created_at >= ?",
                             (waiting_since,))["n"] if waiting_since else 0)
        saved = None
        if any(a["undo_pending"] for a in todo) or queued:  # always keep a copy from just before
            try:
                saved = importers.backup_before_import(store, reason="undo-ai")
            except ValueError as exc:
                return back(request, "/changes", error=str(exc))
        backup = lambda: None  # noqa: E731 (that copy also covers people and jobs it takes out)
        passed = review.pass_since(store, waiting_since) if queued else 0
        for a in sorted(todo, key=lambda a: a["id"], reverse=True):  # newest first, so older values come back last
            if a["undone_at"] or a.get("record") or a["kind"] in BATCH_KINDS:  # a batch's saves are undone one by one
                continue
            try:
                undone.append(store.undo_ai_action(a["id"], by="app", before_delete=backup))
            except NotFound:
                continue  # went with a person or job taken out just before
            except ValueError as exc:
                skipped.append(f"{a['summary']} ({str(exc).rstrip('.')})")
        if not undone and not skipped and not passed:
            return back(request, "/changes", notice=none_left)
        notice = f"Undid {len(undone)} AI change{'' if len(undone) == 1 else 's'}" + (
            ": " + _listed(undone) if undone else "") + "."
        if passed:
            notice += f" Passed {passed} change{'' if passed == 1 else 's'} that waited on Review."
        if skipped:
            notice += f" Left {len(skipped)} as {'it is' if len(skipped) == 1 else 'they are'}: {_listed(skipped)}."
        if kept and not include_kept:
            notice += (f" Left {len(kept)} you kept on Review or on Today as {'it is' if len(kept) == 1 else 'they are'}.")
        if saved is not None:
            notice += " A backup from just before is under Automatic backups in Settings."
        remember_undo_ai(notice)
        return back(request, "/changes", notice=notice)

    def remember_undo_ai(notice: str) -> None:
        """Keep what an Undo all AI did, so Changes still says so if the one-time message didn't arrive."""
        at = datetime.now(timezone.utc).replace(microsecond=0).isoformat()
        with store._write():
            store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE"
                                " SET value = excluded.value", (UNDO_AI_LAST, json.dumps({"at": at, "notice": notice})))

    def last_undo_ai() -> dict[str, str] | None:
        """The last Undo all AI, from the last 24 hours ({"at", "notice"}), or None."""
        try:
            last = json.loads(store.setting(UNDO_AI_LAST) or "null")
            fresh = isinstance(last, dict) and to_dt(last.get("at")) > datetime.now(timezone.utc) - timedelta(days=1)
        except (TypeError, ValueError):
            return None
        return {"at": str(last["at"]), "notice": str(last.get("notice") or "")} if fresh else None

    @app.post("/changes/stage-outcome/{change_id}/undo")
    def undo_stage_outcome(request: Request, change_id: int):
        try:
            notice = actions.undo_stage_outcome(store, change_id)
        except NotFound:
            return back(request, "/changes", error="That change is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=str(exc))
        return back(request, "/changes", notice=notice)

    @app.post("/changes/close-out/{batch_id}/undo")
    def undo_close_out(request: Request, batch_id: int):
        try:
            notice = actions.undo_close_out(store, batch_id, by="app")
        except NotFound:
            return back(request, "/changes", error="That close-out is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=str(exc))
        return back(request, "/changes", notice=notice)

    @app.post("/changes/shortlist-keep/{batch_id}/undo")
    def undo_shortlist_keep(request: Request, batch_id: int):
        try:
            notice = shortlist.undo_keep(store, batch_id, before_delete=ai_backup())
        except NotFound:
            return back(request, "/changes", error="That keep is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=f"Couldn't undo that. {exc}")
        return back(request, "/changes", notice=notice)

    @app.post("/changes/ai/{action_id}/undo")
    def undo_ai_action(request: Request, action_id: int):
        try:
            what = store.undo_ai_action(action_id, before_delete=ai_backup())
        except NotFound:
            return back(request, "/changes", error="That change is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/changes", error=f"Couldn't undo that. {exc}")
        return back(request, "/changes", notice=f"Undid: {what}.")

    @app.post("/changes/ai/{action_id}/undo-from")
    def undo_ai_from(request: Request, action_id: int):
        """Undo this AI change and every AI change made after it."""
        try:
            store.ai_action(action_id)
        except NotFound:
            return back(request, "/changes", error="That change is gone. Reload the page.")
        todo = [a for a in store.ai_actions(limit=1_000_000) if a["id"] >= action_id]
        return undo_ai_batch(request, todo, "Those AI changes were already undone.",
                             waiting_since=store.ai_action(action_id)["at"])

    @app.post("/changes/undo-ai")
    async def undo_ai_since(request: Request):
        form = await request.form()
        window = str(form.get("window") or "")
        if window in UNDO_WINDOWS:  # the window picked next to the button
            start = (datetime.now(timezone.utc) - timedelta(hours=int(window))).replace(microsecond=0).isoformat()
        else:
            start = since_iso(str(form.get("since") or ""))
        if not start:
            return back(request, "/changes", error="Couldn't tell which changes to undo. Reload the page and try again.")
        return undo_ai_batch(request, store.ai_actions(start, limit=1_000_000),
                             "There were no AI changes to undo in that time.", waiting_since=start,
                             include_kept=form.get("include_kept") == "1")

    @app.post("/changes/session/roll-back")
    async def roll_back_session(request: Request):
        """App window only (the AI app has no tool for it): undo one AI session's writes and pass what it left
        waiting on Review."""
        form = await request.form()
        sid = str(form.get("session") or "")
        try:
            res = sessions.roll_back(store, sid)
        except NotFound as exc:
            return back(request, "/changes", error=str(exc))
        except ValueError as exc:  # the backup failed, so nothing changed
            return back(request, "/changes", error=str(exc))
        notice = sessions.notice(res, _listed)
        remember_undo_ai(notice)
        return back(request, "/changes", notice=notice)

    @app.post("/jobs/{job_id}/ai-email/{action_id}/keep")
    def keep_ai_email(request: Request, job_id: int, action_id: int):
        """Keep on "Set by your AI app" next to a job's hiring manager email: it stays, and nothing asks again."""
        try:
            review.keep_email(store, action_id)
        except NotFound as exc:
            return back(request, f"/jobs/{job_id}", error=str(exc))
        except ValueError as exc:
            return back(request, f"/jobs/{job_id}", error=str(exc))
        return back(request, f"/jobs/{job_id}", notice="Kept the hiring manager email.")

    @app.post("/changes/ai/{action_id}/confirm")
    def confirm_ai_action(request: Request, action_id: int):
        """Yes on Today's "Your AI app marked X hired. Right?" (or a job it closed): it stays, and Today stops asking."""
        try:
            a = store.confirm_ai_action(action_id)
        except NotFound:
            return back(request, "/", error="That change is gone. Reload the page.")
        except ValueError as exc:
            return back(request, "/", error=str(exc))
        return back(request, "/", notice=f"Kept: {a['summary']}.")

    # ---------------------------------------------------------- AI review queue
    @app.get("/review", response_class=HTMLResponse)
    def review_page(request: Request):
        return page(request, "review.html", groups=review.sessions(store), cautious=review.cautious(store),
                    kinds=review.KINDS)

    def _review_done(request: Request, res: dict[str, list[str]], kept: bool, none: str):
        done, left = res["done"], res["left"]
        if not done and not left:
            return back(request, "/review", notice=none)
        verb = "Kept" if kept else "Passed"
        notice = f"{verb} {len(done)} change{'' if len(done) == 1 else 's'}" + (": " + _listed(done) if done else "") + "."
        if left:
            notice += f" Left {len(left)}: {_listed(left)}."
        return back(request, "/review", notice=notice)

    @app.post("/review/{review_id}/keep")
    def review_keep(request: Request, review_id: int):
        try:
            what = review.keep(store, review_id)
        except NotFound as exc:
            return back(request, "/review", error=str(exc))
        except ValueError as exc:
            return back(request, "/review", error=str(exc))
        return back(request, "/review", notice=f"Kept: {what}. You can still undo it on Changes.")

    @app.post("/review/{review_id}/pass")
    def review_pass(request: Request, review_id: int):
        try:
            what = review.pass_(store, review_id)
        except NotFound as exc:
            return back(request, "/review", error=str(exc))
        except ValueError as exc:
            return back(request, "/review", error=str(exc))
        return back(request, "/review", notice=f"Passed: {what}. Nothing was changed.")

    @app.post("/review/{op}-session")
    async def review_session(request: Request, op: str):
        """Keep all or Pass all of one AI session's waiting changes."""
        if op not in ("keep", "pass"):
            raise HTTPException(404)
        form = await request.form()
        session = str(form.get("session") or "")
        ids = [r["id"] for r in review.pending(store, session=session)] if session else \
            [r["id"] for r in review.pending(store) if not r.get("session")]
        if op == "keep" and ids:  # many changes at once: keep a copy from just before
            try:
                importers.backup_before_import(store, reason="keep-all")
            except ValueError as exc:
                return back(request, "/review", error=str(exc))
        return _review_done(request, review.decide_many(store, ids, op == "keep"), op == "keep",
                            "Nothing from that session waits for review.")

    @app.post("/settings/cautious")
    async def set_cautious(request: Request):
        form = await request.form()
        on = form.get("ai_cautious") == "1"
        review.set_cautious(store, on)
        review.set_options(store, **{k: form.get(k) == "1" for k in review.OPTIONS})
        msg = ("Cautious mode is on. Your AI app's overwrites, moves into Hired or out, job, client and interview "
               "changes, and notes now wait on Review." if on else "Cautious mode is off. Your AI app's changes apply straight away again.")
        if on:
            msg += review.extra_holds_text(store)
        return RedirectResponse("/settings?" + urlencode({"notice": msg}) + "#cautious", status_code=303)

    @app.post("/settings/ai-apps")
    async def set_ai_apps(request: Request):
        """How far each AI app that has connected is trusted, and what a new one starts as."""
        form = await request.form()
        trust.set_new_apps(store, "suggest" if form.get("new_apps") == "suggest" else "trusted")
        changed = 0
        for a in trust.apps(store):
            level = str(form.get(f"trust_{a['id']}") or "")
            if level in trust.LEVELS and level != a["trust"]:
                trust.set_trust(store, a["id"], level)
                changed += 1
        msg = ("Saved. " + (f"{changed} AI app{'' if changed == 1 else 's'} changed. " if changed else "")
               + ("New AI apps start at Suggest only." if trust.new_apps_level(store) == "suggest"
                  else "New AI apps start as Trusted."))
        return RedirectResponse("/settings?" + urlencode({"notice": msg}) + "#ai-apps", status_code=303)

    @app.post("/settings/time-zone")
    async def set_time_zone(request: Request):
        """The recruiter's own time zone, for when it isn't this computer's (blank: this computer's)."""
        form = await request.form()
        home = str(form.get("next") or "") == "/"  # the banner on Today: back there
        try:
            name = reports.save_user_zone(store, str(form.get("time_zone") or ""))
        except ValueError as exc:
            if home:
                return back(request, "/", error=str(exc))
            return RedirectResponse("/settings?" + urlencode({"flash_error": str(exc)}) + "#time-zone", status_code=303)
        msg = f"Times now show in {name}." if name else "Times now show in this computer's time zone."
        if home:
            return RedirectResponse("/?" + urlencode({"notice": msg}), status_code=303)
        return RedirectResponse("/settings?" + urlencode({"notice": msg}) + "#time-zone", status_code=303)

    # ---------------------------------------------------------- settings etc
    @app.get("/settings", response_class=HTMLResponse)
    def settings_page(request: Request, saved: bool = False):
        return render_settings(request, saved=saved)

    def render_settings(request: Request, saved: bool = False, s: dict[str, str] | None = None,
                        errors: dict[str, str] | None = None, status_code: int = 200) -> HTMLResponse:
        people, jobs = sample_ids(store)
        return page(request, "settings.html", status_code=status_code, s=s or store.settings(), flags=actions.FLAGS,
                    thresholds=actions.THRESHOLDS, saved=saved, errors=errors or {},
                    chase_import=chase.from_import_day(store), sample_people=len(people),
                    sample_jobs=len(jobs), data_folder=str(data_dir().resolve()), db_file=str(db_path().resolve()),
                    backup_count=len(_backup_files()), backup_folder=str(backups_dir().resolve()),
                    backup_list=backups.list_backups(), typesense=env_status(), ai_apps=trust.apps(store),
                    new_apps_level=trust.new_apps_level(store), trust_levels=trust.LABELS, trust_explain=trust.EXPLAIN)

    def _backup_files() -> list[Path]:
        return [f for f in backups_dir().iterdir() if f.is_file() and f.name.endswith(".db")]

    def restore_backup(name: str) -> tuple[dict[str, Any], Path]:
        """Restore a backup (a fresh one is taken first), then let Typesense catch up if it's in use."""
        r = backups.restore(store, name)
        if search.index is not None:
            try:
                search.index.rebuild(store)
            except Exception as exc:  # built-in search already has the restored data
                log.warning("Typesense rebuild after a restore failed: %s", exc)
        return r["restored"], r["saved"]

    @app.post("/settings/backups/restore")
    async def restore_from_settings(request: Request):
        """App window only (the AI has no restore tool). Saves the data as it is now, then swaps the backup in."""
        form = await request.form()
        try:
            info, saved = restore_backup(str(form.get("name") or ""))
        except ValueError as exc:
            return RedirectResponse("/settings?" + urlencode({"flash_error": str(exc)}) + "#backups", status_code=303)
        people, jobs = info["people"], info["jobs"]
        msg = (f"Restored the backup from {_local(info['when'])} ({info['why'].lower()}): {people} "
               f"{'person' if people == 1 else 'people'} and {jobs} job{'' if jobs == 1 else 's'}. Your data from "
               f"just before was saved first as {saved.name}, at the top of the list below, so you can go back to it. "
               "Your Cautious mode and AI app settings stayed as they are now.")
        return RedirectResponse("/settings?" + urlencode({"notice": msg}) + "#backups", status_code=303)

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
        whole = bool(form.get("flags_shown"))  # the Settings page sends its Today boxes, ticked or not
        for key, _, _ in actions.FLAGS:  # anyone else changes only the boxes they send
            if whole or key in form:
                values[key] = "1" if str(form.get(key) or "") not in ("", "0", "false", "off") else "0"
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
        if str(form.get("theme") or "") in {t[0] for t in THEMES}:  # anything else is ignored
            values["theme"] = str(form.get("theme"))
        if errors:
            return render_settings(request, s={**store.settings(), **values}, errors=errors, status_code=400)
        if form.get("hiring_for") in ("own", "clients"):
            actions.set_inhouse(store, form.get("hiring_for") == "own")
        store.set_settings(**values)
        if whole:  # the Settings page sends this box ticked or not
            chase.set_from_import_day(store, str(form.get("chase_from_import") or "") not in ("", "0", "false", "off"))
        return RedirectResponse("/settings?saved=1", status_code=303)

    @app.get("/recipes", response_class=HTMLResponse)
    def recipes_page(request: Request):
        items = [(r, recipes.render(r.key)) for r in recipes.RECIPES]
        return page(request, "recipes.html", items=items, jobs=store.list_jobs("open"),
                    job_prompts={j["id"]: recipes.source_for_job(j) for j in store.list_jobs("open")})

    @app.get("/privacy", response_class=HTMLResponse)
    def privacy(request: Request):
        ts = env_status()
        remote = None
        if ts and not ts["refused"] and ts["has_key"]:  # candidates leave this computer when the index is elsewhere
            from .typesense_index import _loopback
            host = (urlsplit(os.environ.get("TYPESENSE_URL", "").strip()).hostname or "").lower()
            remote = host if host and not _loopback(host) else None
        return page(request, "privacy.html", data_folder=str(data_dir().resolve()), typesense=ts,
                    folder_private=paths.data_dir_private(), search_remote=remote)

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
        private = recipes.report_private(store)
        link = recipes.issue_url(summary, details, where=where, private=private)
        title, body = recipes.issue_text(summary, details, where=where, private=private)
        return page(request, "report.html", link=link, summary=summary, env_line=recipes.environment_line(),
                    where=str(form.get("where") or ""), preview_title=title, preview_body=body)

    # ----------------------------------------------------- AI app setup
    @app.get("/connect", response_class=HTMLResponse)
    def connect_page(request: Request):
        status = integrations.claude_status()
        return page(
            request,
            "connect.html",
            claude=status,
            claude_on=status["connected"],
            claude_entry_json=json.dumps(status["entry"], indent=2) if status["entry"] else "",
            claude_quit=integrations.quit_instruction(),
            codex_on=integrations.codex_connected(),
            lmstudio_on=integrations.lmstudio_connected(),
            json_snippet=integrations.generic_json_snippet(),
            codex_snippet=integrations.codex_snippet(),
            skill_status=[skills.status(t) for t in skills.TARGETS],
            skill_names=skills.skill_names(),
            desktop_steps=skills.DESKTOP_STEPS[0],
            skills_source=str(skills.source_dir()),
            skill_zips=[{"name": n, "label": n.removeprefix(skills.PREFIX).replace("-", " ").capitalize()}
                        for n in skills.skill_names()],
        )

    @app.get("/connect/skills/{name}.zip")
    def skill_zip(name: str):
        """One skill as its own zip, for Claude Desktop's Customize > Skills."""
        if name not in skills.skill_names():
            raise HTTPException(404)
        return Response(skills.build_skill_zip(name), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{name}.zip"'})

    @app.get("/connect/skills.zip")
    def skills_zip():
        """The skills as a zip for Claude Desktop (Customize > Skills). Same files the app installs elsewhere."""
        return Response(skills.build_zip(), media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="{skills.ZIP_NAME}"'})

    @app.post("/connect/skills/{target}/{action}", response_class=HTMLResponse)
    def connect_skills(request: Request, target: str, action: str):
        """Add or remove the Talent Search skills. 'preview-*' only lists the files; nothing is written until
        the person presses the confirm button, which posts 'install' or 'remove'."""
        if target not in skills.TARGETS or action not in ("preview-install", "install", "preview-remove", "remove"):
            raise HTTPException(404)
        try:
            if action == "preview-install":
                result = skills.plan_install(target)
            elif action == "install":
                result = skills.install(target)
            elif action == "preview-remove":
                result = skills.plan_remove(target)
            else:
                result = skills.remove(target)
        except Exception as exc:  # never a 500 or a traceback in the window
            integrations.log_failure(exc)
            return page(request, "_skills_result.html", action="error", target=target,
                        error=integrations.friendly_error(exc))
        return page(request, "_skills_result.html", action=action, target=target, result=result)

    @app.post("/connect/claude/report", response_class=HTMLResponse)
    def connect_report(request: Request):
        """Everything we know about the Claude Desktop hookup, with a fresh test, as text to paste into a bug report."""
        try:
            text = integrations.claude_report()
        except Exception as exc:
            integrations.log_failure(exc)
            text = "We couldn't put the report together: " + integrations.friendly_error(exc)
        return page(request, "_connect_report.html", report=text)

    @app.post("/connect/{target}", response_class=HTMLResponse)
    def connect(request: Request, target: str):
        fn = getattr(integrations, f"connect_{target}", None) if target in ("claude", "codex", "lmstudio") else None
        if fn is None:
            raise HTTPException(404)
        try:
            result = fn()
            check = None
            if result.get("ok") and target in ("claude", "lmstudio") and result.get("entry"):
                check = integrations.test_launch(result["entry"], timeout=45)
                check["line"] = integrations.result_line(check)
        except Exception as exc:  # never a 500 or a traceback in the window
            integrations.log_failure(exc)
            return page(request, "_connect_result.html", ok=False, target=target, error=integrations.friendly_error(exc))
        return page(request, "_connect_result.html", ok=True, target=target, result=result, check=check,
                    quit_text=integrations.quit_instruction(), look_text=integrations.claude_where_to_look(),
                    healthy_line=integrations.HEALTHY_LINE, reopen_line=integrations.REOPEN_LINE)

    @app.post("/connect/{target}/test", response_class=HTMLResponse)
    def connect_test(request: Request, target: str):
        if target not in ("claude", "lmstudio", "codex"):
            raise HTTPException(404)
        try:
            entry = integrations.entry_for(target)
            check = None
            if entry is not None:
                check = integrations.test_launch(entry)
                check["line"] = integrations.result_line(check)
            problems = integrations.check_entry(entry) if entry else []
        except Exception as exc:
            integrations.log_failure(exc)
            return page(request, "_connect_result.html", ok=False, target=target, error=integrations.friendly_error(exc))
        return page(request, "_connect_test.html", target=target, entry=entry, check=check, problems=problems,
                    quit_text=integrations.quit_instruction(), look_text=integrations.claude_where_to_look(),
                    healthy_line=integrations.HEALTHY_LINE, reopen_line=integrations.REOPEN_LINE)

    # ------------------------------------------------------------ JSON
    @app.get("/api/health")
    def health(request: Request, nonce: str = ""):
        """Open without the key so a second launch can find the running app. Says nothing about the data.
        With a nonce it also answers `proof` (paths.health_proof), so the launcher can tell this app from
        another program on the port before it sends a launch code. The proof is bound to the port this
        request really arrived on (the socket's, not a header), so a relay on another port can't use it."""
        out: dict[str, Any] = {"ok": True, "version": __version__}
        server = request.scope.get("server") or ()
        if nonce and len(nonce) <= 128 and len(server) == 2 and isinstance(server[1], int):
            out["proof"] = paths.health_proof(key, nonce, server[1])
        if request.state.unlocked:
            out.update(search=search.engine, candidates=store.count_candidates())
        return out

    @app.get("/api/candidates")
    def api_search(q: str = "", location: str | None = None, title: str | None = None, tag: str | None = None,
                   limit: int = 25, offset: int = 0):
        r = search.search(q, location=location, title=title, tag=tag, limit=max(1, min(limit, 200)),
                          offset=max(0, offset))
        return {"total": r.total, "engine": r.engine, "exact_total": r.exact_total, "suggestion": r.suggestion,
                "corrected": r.corrected,
                "results": [{**h.candidate, "snippet": plain_snippet(h.snippet), "closest": h.closest, "reason": h.reason}
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

    @app.exception_handler(RequestValidationError)
    async def bad_request(request: Request, exc: RequestValidationError):
        """The API keeps FastAPI's 422 details. In the app window a bad id in the address is Not found."""
        if request.url.path.startswith("/api/"):
            return await request_validation_exception_handler(request, exc)
        if any((e.get("loc") or ("",))[0] == "path" for e in exc.errors()):
            return page(request, "message.html", status_code=404, title="Not found", message="That page doesn't exist.")
        return page(request, "message.html", status_code=400, title="That link isn't right",
                    message="Something in that address isn't right. Go back and try again.")

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


FILL_FIELDS = ("email", "phone", "current_company", "current_title", "location")  # same-name "Add them" fills these


def _and(items: list[str]) -> str:
    return items[0] if len(items) == 1 else ", ".join(items[:-1]) + " and " + items[-1]


def _form_fields(form: Any, keep_blank: bool = False) -> dict[str, Any]:
    out = {}
    for f in CANDIDATE_FIELDS:
        if f in form:
            v = str(form[f])
            if v.strip() or keep_blank:
                out[f] = v
    return out


__all__ = ["create_app", "DEFAULT_SETTINGS", "split_tags"]
