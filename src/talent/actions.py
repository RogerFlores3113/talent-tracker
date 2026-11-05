"""Today's action items, computed on read from the database.

Nothing here is stored except snoozes, AI suggestions and follow-up dates. Each
kind of item can be switched off in Settings, and its threshold changed.
"""
from __future__ import annotations

from datetime import date, datetime, timedelta, timezone
from typing import Any

from .store import NotFound, Store, parse_when, to_dt

FLAGS: tuple[tuple[str, str, str], ...] = (
    # (setting key, label, help)
    ("flag_interviews", "Upcoming interviews", "Interviews in the next 7 days."),
    ("flag_suggestions", "Follow-ups", "Follow-ups you scheduled and ones your AI app wrote for you."),
    ("flag_stuck", "Stuck in a stage", "Candidates who haven't moved stage in a while."),
    ("flag_thin", "Jobs that need more candidates", "Open jobs with too few people still in the running."),
    ("flag_stale", "Profiles that may be out of date", "Candidates whose details haven't been updated in months."),
)

THRESHOLDS: tuple[tuple[str, str], ...] = (
    ("stuck_days", "Stuck after this many days in a stage"),
    ("thin_min", "A job needs more candidates below this many"),
    ("stale_days", "A profile may be out of date after this many days"),
)

FOLLOWUP_PREFIX = "followup:"  # settings key followup:<suggestion id> = YYYY-MM-DD, shown from that day on
STALE_NAMES = 3


def _days_ago(iso: str | None, now: datetime) -> int:
    dt = to_dt(iso)
    return (now - dt).days if dt else 0


def _int(cfg: dict[str, str], key: str, default: int) -> int:
    raw = str(cfg.get(key) or "").strip()
    return int(raw) if raw.isdigit() and int(raw) > 0 else default


def job_label(title: str | None, company: str | None) -> str:
    """'Senior Backend Engineer, Acme Payments' (or just the title)."""
    return ", ".join(x for x in (title, company) if x)


def twin_hints(jobs: list[dict[str, Any]]) -> dict[int, str]:
    """{job id: location, or '#id'} for open jobs that share a title and company with another open job,
    so two "Backend Engineer, Acme" openings can be told apart in lists."""
    groups: dict[tuple[str, str], list[dict[str, Any]]] = {}
    for j in jobs:
        if j.get("status", "open") == "open":
            groups.setdefault(((j.get("title") or "").lower(), (j.get("company") or "").lower()), []).append(j)
    return {j["id"]: (j.get("location") or f"#{j['id']}") for g in groups.values() if len(g) > 1 for j in g}


def local_time(iso: str | None, fmt: str = "%a %b %-d, %-I:%M %p") -> str:
    dt = to_dt(iso)
    if not dt:
        return ""
    local = dt.astimezone()
    try:
        return local.strftime(fmt)
    except ValueError:  # Windows strftime has no %-d
        return local.strftime(fmt.replace("%-", "%#"))


def _in_stage(days: int) -> str:
    return "today" if days <= 0 else "1 day" if days == 1 else f"{days} days"


# ---------------------------------------------------------------- settings
def _put_setting(store: Store, key: str, value: str) -> None:
    # Store.set_settings only takes the known settings, so these extra keys are written directly.
    with store._lock, store._conn:
        store._conn.execute(
            "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value))


def _drop_setting(store: Store, key: str) -> None:
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM settings WHERE key = ?", (key,))


# --------------------------------------------------------------- follow-ups
def parse_day(value: str) -> date:
    """'2026-12-01', or any date store.parse_when reads ('12/1/2026', 'Dec 1, 2026', 'today'), as a local day."""
    v = (value or "").strip()
    try:
        return date.fromisoformat(v[:10])
    except ValueError:
        pass
    try:
        return to_dt(parse_when(v, default_now=False)).astimezone().date()
    except (ValueError, AttributeError):
        raise ValueError("Pick a follow-up date like 2026-12-01.") from None


def add_followup(store: Store, text: str, on: date | str | None = None, *, candidate_id: int | None = None,
                 job_id: int | None = None, source: str = "App") -> dict[str, Any]:
    """Add a to-do to Today. With `on`, it shows from that day on (the log form's "Follow up on")."""
    day = parse_day(on) if isinstance(on, str) else on
    s = store.add_suggestion(text, candidate_id=candidate_id, job_id=job_id, source=source)
    if day is None:
        return s
    _put_setting(store, f"{FOLLOWUP_PREFIX}{s['id']}", day.isoformat())
    return {**s, "due_on": day.isoformat()}


def schedule_followup(store: Store, candidate_id: int, on: date | str, job_id: int | None = None) -> dict[str, Any]:
    """Add "Follow up with <name>" to Today, starting on the given day."""
    cand = store.get_candidate(candidate_id)
    return add_followup(store, f"Follow up with {cand['name']}", on, candidate_id=candidate_id, job_id=job_id)


def complete_followup(store: Store, suggestion_id: int) -> None:
    store.complete_suggestion(suggestion_id)
    _drop_setting(store, f"{FOLLOWUP_PREFIX}{suggestion_id}")


def _followup_days(cfg: dict[str, str]) -> dict[int, str]:
    out = {}
    for k, v in cfg.items():
        if k.startswith(FOLLOWUP_PREFIX) and k[len(FOLLOWUP_PREFIX):].isdigit():
            out[int(k[len(FOLLOWUP_PREFIX):])] = v
    return out


# ------------------------------------------------------------- interviews
def _closing_pairs(store: Store, candidate_ids: set[int]) -> set[tuple[int, int]]:
    """(candidate, job) pairs whose stage is a closing one (hired or no longer in the running)."""
    if not candidate_ids:
        return set()
    marks = ",".join("?" * len(candidate_ids))
    rows = store._rows(
        f"""SELECT a.candidate_id, a.job_id FROM applications a
            JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
            WHERE s.outcome != 'open' AND a.candidate_id IN ({marks})""", sorted(candidate_ids))
    return {(r["candidate_id"], r["job_id"]) for r in rows}


def _live_interviews(store: Store, rows: list[dict[str, Any]], jobs: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """Drop interviews for closed jobs and for candidates whose application there has closed."""
    closing = _closing_pairs(store, {r["candidate_id"] for r in rows if r.get("job_id")})
    out = []
    for r in rows:
        jid = r.get("job_id")
        if jid is not None:
            job = jobs.get(jid)
            if job and job["status"] == "closed":
                continue
            if (r["candidate_id"], jid) in closing:
                continue
        out.append(r)
    return out


def next_interviews(store: Store, job_id: int, now: datetime | None = None) -> dict[int, str]:
    """{candidate_id: ISO time} of each pipeline candidate's next interview for this job (or with no job)."""
    now = now or datetime.now(timezone.utc)
    start = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
    rows = store._rows(
        """SELECT t.candidate_id, min(t.at) AS at FROM timeline t
           JOIN applications a ON a.candidate_id = t.candidate_id AND a.job_id = ?
           WHERE t.kind = 'interview' AND t.at >= ? AND (t.job_id = ? OR t.job_id IS NULL)
           GROUP BY t.candidate_id""", (job_id, start, job_id))
    return {r["candidate_id"]: r["at"] for r in rows}


def ai_identity_changes(store: Store, now: datetime | None = None, days: int = 7) -> list[dict[str, Any]]:
    """One Today item per candidate whose name, email, phone or LinkedIn the AI app changed lately
    (and nobody undid), so a prompt-injected profile can't quietly redirect outreach."""
    import json

    now = now or datetime.now(timezone.utc)
    since = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
    watched = {"name": "name", "email": "email", "phone": "phone", "linkedin_url": "LinkedIn"}
    rows = store._rows(
        """SELECT t.id, t.candidate_id, t.at, t.data, c.name AS candidate_name FROM timeline t
           JOIN candidates c ON c.id = t.candidate_id
           WHERE t.kind = 'change' AND t.source = 'AI app' AND t.at >= ? ORDER BY t.at DESC, t.id DESC""", (since,))
    found: dict[int, dict[str, Any]] = {}
    for r in rows:
        try:
            data = json.loads(r["data"] or "{}")
        except ValueError:
            continue
        fields = [f for f in watched if f in (data.get("new") or {})]
        if not fields or data.get("undone_at"):
            continue
        item = found.setdefault(r["candidate_id"], {
            "key": f"aichange:{r['id']}", "kind": "ai_change", "rank": 1, "when": r["at"], "fields": [],
            "candidate_id": r["candidate_id"], "name": r["candidate_name"], "href": "/changes?who=ai&since=7"})
        item["fields"] += [f for f in fields if f not in item["fields"]]
    out = []
    for item in found.values():
        fields = item.pop("fields")
        labels = [watched[f] for f in watched if f in fields]
        what = labels[0] if len(labels) == 1 else ", ".join(labels[:-1]) + " and " + labels[-1]
        name = item.pop("name")
        out.append({**item, "title": f"Your AI app changed {name}'s {what}. Review or undo.",
                    "detail": "Check it's right before you reach out."})
    return out


# ------------------------------------------------------------------ today
def action_items(store: Store, now: datetime | None = None) -> dict[str, Any]:
    """{'items': [...], 'counts': {...}, 'hidden': n, 'scheduled': n, 'stale_total': n}.

    Items are ordered most urgent first: interviews, then follow-ups, then the rest.
    Jobs that need more candidates fold into one row, and so do out-of-date profiles.
    """
    now = now or datetime.now(timezone.utc)
    cfg = store.settings()
    on = lambda k: cfg.get(k, "1") == "1"  # noqa: E731
    snoozed = store.snoozed_keys()
    jobs = {j["id"]: j for j in store.list_jobs()}
    items: list[dict[str, Any]] = []
    hidden = scheduled = 0

    def label(job_id: int | None, fallback: str | None = None) -> str:
        j = jobs.get(job_id) if job_id else None
        return job_label(j["title"], j.get("company")) if j else (fallback or "")

    if on("flag_interviews"):
        for t in _live_interviews(store, store.upcoming_interviews(days=7, now=now), jobs):
            items.append({
                "key": f"interview:{t['id']}", "kind": "interview", "rank": 0, "when": t["at"],
                "title": f"Interview with {t['candidate_name']}",
                "detail": " · ".join(x for x in (label(t.get("job_id"), t.get("job_title")), t.get("body")) if x),
                "candidate_id": t["candidate_id"], "job_id": t.get("job_id"),
                "href": f"/candidates/{t['candidate_id']}",
            })

    if on("flag_suggestions"):
        due = _followup_days(cfg)
        today = now.astimezone().date().isoformat()
        for s in store.open_suggestions():
            if due.get(s["id"], "") > today:
                scheduled += 1
                continue
            mine = s["id"] in due and (s.get("source") or "") != "AI app"
            detail = [label(s.get("job_id"), s.get("job_title"))]
            if not mine:
                detail.append("Suggested by your AI app")
            items.append({
                "key": f"suggestion:{s['id']}", "kind": "suggestion", "rank": 1,
                "when": due.get(s["id"]) or s["created_at"],
                "title": s["text"], "detail": " · ".join(x for x in detail if x),
                "candidate_id": s.get("candidate_id"), "job_id": s.get("job_id"), "suggestion_id": s["id"],
                "href": f"/candidates/{s['candidate_id']}" if s.get("candidate_id")
                else f"/jobs/{s['job_id']}" if s.get("job_id") else "/",
            })

    items += ai_identity_changes(store, now)
    if on("flag_stuck"):
        limit = _int(cfg, "stuck_days", 7)
        for a in store.open_applications():
            days = _days_ago(a["stage_changed_at"], now)
            if days >= limit:
                nxt = store.next_stage(a["job_id"], a["stage"])
                items.append({
                    "key": f"stuck:{a['id']}", "kind": "stuck", "rank": 2, "when": a["stage_changed_at"],
                    "title": f"{a['candidate_name']} has been in {a['stage']} for {days} days",
                    "detail": label(a["job_id"], a["job_title"]), "candidate_id": a["candidate_id"],
                    "job_id": a["job_id"], "stage": a["stage"], "next_stage": nxt, "days": days,
                    "href": f"/candidates/{a['candidate_id']}",
                })

    if on("flag_thin"):
        minimum = _int(cfg, "thin_min", 5)
        thin = []
        for j in jobs.values():
            if j["status"] == "open" and j["active_count"] < minimum:
                if f"thin:{j['id']}" in snoozed:
                    hidden += 1
                else:
                    thin.append(j)
        aim = f"Aim for at least {minimum} active candidate{'' if minimum == 1 else 's'}."
        if len(thin) == 1:
            j = thin[0]
            n = j["active_count"]
            items.append({
                "key": f"thin:{j['id']}", "kind": "thin", "rank": 3, "when": j["updated_at"],
                "title": f"{j['title']} has {'no' if n == 0 else 'only ' + str(n)} active candidate{'' if n == 1 else 's'}",
                "detail": " ".join(x for x in (f"{j['company']}." if j.get("company") else "", aim) if x),
                "job_id": j["id"], "jobs": [j], "href": f"/jobs/{j['id']}",
            })
        elif thin:
            items.append({
                "key": "thin:all", "kind": "thin", "rank": 3, "when": max(j["updated_at"] for j in thin),
                "title": f"{len(thin)} jobs have thin pipelines",
                "detail": "; ".join(job_label(j["title"], j.get("company")) for j in thin) + ". " + aim,
                "jobs": thin, "aim": aim, "href": "/jobs?status=open",
            })

    stale_total = 0
    if on("flag_stale"):
        days = _int(cfg, "stale_days", 180)
        cutoff = (now - timedelta(days=days)).replace(microsecond=0).isoformat()
        skip = sorted(int(k[6:]) for k in snoozed if k.startswith("stale:") and k[6:].isdigit())
        where = "updated_at < ?" + (f" AND id NOT IN ({','.join('?' * len(skip))})" if skip else "")
        stale_total = store._row(f"SELECT count(*) AS n FROM candidates WHERE {where}", (cutoff, *skip))["n"]
        if stale_total:
            oldest = [store._hydrate(r) for r in store._rows(
                f"SELECT * FROM candidates WHERE {where} ORDER BY updated_at, id LIMIT ?",
                (cutoff, *skip, STALE_NAMES))]
            if stale_total == 1:
                c = oldest[0]
                items.append({
                    "key": f"stale:{c['id']}", "kind": "stale", "rank": 4, "when": c["updated_at"],
                    "title": f"Refresh {c['name']}'s profile",
                    "detail": f"Last updated {_days_ago(c['updated_at'], now)} days ago",
                    "candidate_id": c["id"], "href": f"/candidates/{c['id']}#refresh",
                })
            else:
                names = ", ".join(c["name"] for c in oldest)
                more = stale_total - len(oldest)
                items.append({
                    "key": "stale:all", "kind": "stale", "rank": 4, "when": oldest[0]["updated_at"],
                    "title": f"{stale_total} profiles may be out of date",
                    "detail": f"Not updated in {days} days. Oldest: {names}" + (f" and {more} more." if more else "."),
                    "candidates": oldest, "href": "/candidates?sort=updated&dir=asc",
                })

    visible = []
    for i in items:
        if i["key"] in snoozed:
            hidden += 1
        else:
            visible.append(i)
    visible.sort(key=lambda i: (i["rank"], i["when"] or ""))
    counts: dict[str, int] = {}
    for i in visible:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    shown_stale = stale_total if any(i["kind"] == "stale" for i in visible) else 0
    return {"items": visible, "counts": counts, "hidden": hidden, "scheduled": scheduled,
            "stale_total": shown_stale, "stale_more": 0}


# ---------------------------------------------------------- status update
def job_status_text(store: Store, job_id: int, now: datetime | None = None) -> str:
    """A plain-text pipeline summary to paste to a hiring manager. No ids, local times."""
    now = now or datetime.now(timezone.utc)
    job = store.get_job(job_id)
    groups = store.pipeline_by_stage(job_id)
    total = sum(len(g["candidates"]) for g in groups)
    active = sum(len(g["candidates"]) for g in groups if g["outcome"] == "open")
    hired = sum(len(g["candidates"]) for g in groups if g["outcome"] == "hired")
    head = job_label(job["title"], job.get("company"))
    if job.get("location"):
        head += f" ({job['location']})"
    status = {"open": "Open", "on_hold": "On hold", "closed": "Closed"}.get(job["status"], job["status"])
    lines = [head, f"Status update for {local_time(now.isoformat(), '%a %b %-d, %Y')}",
             f"{status}. {active} in the running, {hired} hired, {total} in the pipeline.", ""]
    for g in groups:
        if not g["candidates"]:
            continue
        people = ", ".join(f"{a['candidate_name']} ({_in_stage(_days_ago(a['stage_changed_at'], now))})"
                           for a in sorted(g["candidates"], key=lambda a: a["stage_changed_at"]))
        lines.append(f"{g['name']} ({len(g['candidates'])}): {people}")
    if not total:
        lines.append("No one in the pipeline yet.")
    names = {a["candidate_id"]: a["candidate_name"] for g in groups if g["outcome"] == "open" for a in g["candidates"]}
    upcoming = sorted((at, cid) for cid, at in next_interviews(store, job_id, now).items() if cid in names)
    if upcoming:
        lines += ["", "Next interviews:"]
        lines += [f"- {names[cid]}: {local_time(at, '%a %b %-d, %-I:%M %p %Z').strip()}" for at, cid in upcoming]
    return "\n".join(lines)


__all__ = ["FLAGS", "THRESHOLDS", "NotFound", "action_items", "add_followup", "complete_followup", "job_label",
           "job_status_text", "local_time", "next_interviews", "parse_day", "schedule_followup", "twin_hints"]
