"""Ops review: one table across every open req for the Monday sheet or the Friday report.

Per job: in process, new this week, moved this week, stuck over N days, interviews booked, offers out, and days
since the client's last update. Every count is a link to the Candidates list with the same filter (job plus a
`pipe` filter), so the number and the list behind it come from the same SQL and always agree.
Read only. Nothing here writes, and nothing needs a migration.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import urlencode

from fastapi import Request
from fastapi.responses import HTMLResponse

from . import actions, reports
from .store import Store, to_dt

WEEK_DAYS = 7
STUCK_DAYS = 14
INTERVIEW_DAYS = 30  # booked interviews counted from two hours ago to this many days ahead
UPDATE_WARN_DAYS = 7  # a client update older than this reads amber, older than UPDATE_LATE_DAYS red
UPDATE_LATE_DAYS = 14
PIPES = ("process", "new", "moved", "stuck", "interviews", "offers")
LABELS = {"process": "In process", "new": "New this week", "moved": "Moved this week", "stuck": "Stuck",
          "interviews": "Interviews booked", "offers": "Offers out"}
_OPEN = ("NOT EXISTS (SELECT 1 FROM job_stages ps WHERE ps.job_id = a.job_id AND lower(ps.name) = lower(a.stage)"
         " AND ps.outcome <> 'open')")


def clamp_days(value: Any, default: int) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        return default
    return n if 1 <= n <= 365 else default


def pipe_sql(pipe: str, days: int | None = None, now: datetime | None = None) -> tuple[str, list[Any]]:
    """A SQL condition on pipeline row `a` (applications) for one column of the review, and its parameters.
    Empty when `pipe` is not one of PIPES."""
    now = now or datetime.now(timezone.utc)

    def ago(n: int) -> str:
        return (now - timedelta(days=n)).replace(microsecond=0).isoformat()

    if pipe == "process":
        return _OPEN, []
    if pipe == "new":
        return "a.created_at >= ?", [ago(clamp_days(days, WEEK_DAYS))]
    if pipe == "moved":
        cut = ago(clamp_days(days, WEEK_DAYS))
        return "a.stage_changed_at >= ? AND a.created_at < ?", [cut, cut]
    if pipe == "stuck":
        return f"{_OPEN} AND a.stage_changed_at < ?", [ago(clamp_days(days, STUCK_DAYS))]
    if pipe == "interviews":
        from .store import LIVE_INTERVIEW
        start = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
        end = (now + timedelta(days=INTERVIEW_DAYS)).replace(microsecond=0).isoformat()
        return (f"EXISTS (SELECT 1 FROM timeline t WHERE t.kind = 'interview' AND t.candidate_id = a.candidate_id"
                f" AND t.job_id = a.job_id AND t.at >= ? AND t.at <= ? AND {LIVE_INTERVIEW})", [start, end])
    if pipe == "offers":
        # Recorded offers are the main source. Someone sitting in an Offer stage with no offer recorded at all (an
        # import put them there) counts too, so the column isn't 0 beside 93 people in Offer Extended.
        return ("(EXISTS (SELECT 1 FROM offers o WHERE o.candidate_id = a.candidate_id AND o.job_id = a.job_id"
                " AND o.status = 'out') OR (" + _OPEN + " AND ' ' || replace(replace(lower(a.stage), '-', ' '), '_', ' ')"
                " || ' ' LIKE '% offer %' AND NOT EXISTS (SELECT 1 FROM offers o WHERE o.candidate_id = a.candidate_id"
                " AND o.job_id = a.job_id)))", [])
    return "", []


def pipe_label(pipe: str, days: int | None = None) -> str:
    """'Stuck over 14 days' for the Candidates page's filter chip."""
    if pipe == "stuck":
        return f"Stuck over {clamp_days(days, STUCK_DAYS)} days"
    if pipe in ("new", "moved"):
        return f"{LABELS[pipe]} (last {clamp_days(days, WEEK_DAYS)} days)"
    return LABELS.get(pipe, "")


def link(job_id: int | None, pipe: str, stuck_days: int, client: str | None = None) -> str:
    """The Candidates list behind one cell."""
    q: dict[str, Any] = {}
    if job_id:
        q["job"] = job_id
    elif client:
        q["client"] = client
    q["pipe"] = pipe
    if pipe == "stuck":
        q["days"] = stuck_days
    return "/candidates?" + urlencode(q)


def _counts(store: Store, pipe: str, days: int, now: datetime) -> dict[int, int]:
    cond, params = pipe_sql(pipe, days, now)
    rows = store._rows(
        f"SELECT a.job_id, count(DISTINCT a.candidate_id) AS n FROM applications a JOIN jobs j ON j.id = a.job_id"
        f" WHERE j.status = 'open' AND {cond} GROUP BY a.job_id", params)
    return {r["job_id"]: r["n"] for r in rows}


def review(store: Store, stuck_days: int = STUCK_DAYS, client: str | None = None,
           now: datetime | None = None) -> dict[str, Any]:
    """{"rows": [...], "totals": {...}, "stuck_days", "week_days"}: one row per open job, grouped by client.
    Each row: job_id, title, client, hiring_manager, counts {pipe: n}, links {pipe: url}, last_update_sent_at,
    days_since_update (None when never sent, or the job names no client)."""
    now = now or datetime.now(timezone.utc)
    stuck_days = clamp_days(stuck_days, STUCK_DAYS)
    want = actions.company_key(client) if client and client.strip() else None
    counts = {p: _counts(store, p, stuck_days if p == "stuck" else WEEK_DAYS, now) for p in PIPES}
    sent: dict[str, str | None] = {}
    rows = []
    jobs = [j for j in store._rows("SELECT * FROM jobs WHERE status = 'open'")
            if want is None or actions.company_key(j.get("company")) == want]
    for j in sorted(jobs, key=lambda j: ((j.get("company") or "~").lower(), j["title"].lower(), j["id"])):
        key = actions.company_key(j.get("company"))
        if key and key not in sent:
            try:
                sent[key] = reports.last_sent(store, reports.client(store, key))
            except Exception:  # a client row that can't be read leaves the cell blank, not the page broken
                sent[key] = None
        last = sent.get(key) if key else None
        dt = to_dt(last)
        nums = actions.job_numbers(store, j["id"])
        rows.append({
            "hired": nums["hired_all"], "openings": nums["openings"],
            "over_openings": nums["hired_all"] > nums["openings"],
            "job_id": j["id"], "title": j["title"], "client": (j.get("company") or "").strip(),
            "client_slug": reports.slug(key) if key else None, "hiring_manager": j.get("hiring_manager"),
            "counts": {p: counts[p].get(j["id"], 0) for p in PIPES},
            "links": {p: link(j["id"], p, stuck_days) for p in PIPES},
            "last_update_sent_at": last,
            "days_since_update": max(0, actions.local_days(dt, now)) if dt else None})
        d = rows[-1]["days_since_update"]
        rows[-1]["update_tone"] = "" if d is None or d <= UPDATE_WARN_DAYS else "overdue" if d > UPDATE_LATE_DAYS else "warn"
    totals = {p: sum(r["counts"][p] for r in rows) for p in PIPES}
    return {"rows": rows, "totals": totals, "stuck_days": stuck_days, "week_days": WEEK_DAYS,
            "interview_days": INTERVIEW_DAYS, "client": client or None}


def text(data: dict[str, Any]) -> str:
    """The review as plain lines for the AI: a header, then one line per job."""
    rows = data["rows"]
    if not rows:
        return "No open jobs to review."
    t = data["totals"]
    head = (f"{len(rows)} open {'job' if len(rows) == 1 else 'jobs'}: {t['process']} in process, {t['new']} new this "
            f"week, {t['moved']} moved this week, {t['stuck']} stuck over {data['stuck_days']} days, "
            f"{t['interviews']} interviews booked, {t['offers']} offers out.")
    lines = [head]
    for r in rows:
        c = r["counts"]
        upd = ("update sent " + (f"{r['days_since_update']} days ago" if r["days_since_update"] else "today")
               if r["days_since_update"] is not None else ("no update sent yet" if r["client"] else "no client"))
        over = (f" ({actions.against_text(r['hired'], r['openings'])}: check openings)" if r.get("over_openings")
                else "")
        lines.append(f"- {r['title']}" + (f" ({r['client']})" if r["client"] else "") + f" [job_id {r['job_id']}]{over}: "
                     f"{c['process']} in process, {c['new']} new, {c['moved']} moved, {c['stuck']} stuck, "
                     f"{c['interviews']} interviews, {c['offers']} offers out, {upd}")
    return "\n".join(lines)


def register_routes(app: Any, store: Store, page: Any) -> None:
    """GET /clients/review. Registered before /clients/{slug} so the word 'review' is not read as a client."""
    @app.get("/clients/review", response_class=HTMLResponse)
    def ops_review_page(request: Request, stuck: str = "", client: str = ""):
        data = review(store, clamp_days(stuck, STUCK_DAYS), client or None)
        names = {actions.company_key(r["company"]): r["company"].strip() for r in store._rows(
            "SELECT DISTINCT company FROM jobs WHERE status = 'open' AND trim(coalesce(company, '')) <> ''")}
        clients = sorted(names.values(), key=str.lower)
        return page(request, "ops_review.html", data=data, labels=LABELS, pipes=PIPES, client=client,
                    client_choices=clients, slug=reports.slug)
