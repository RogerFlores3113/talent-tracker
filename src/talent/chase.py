"""Submittals and the client feedback chase.

Submit to client moves someone into the job's Submitted stage (its stage history keeps the date) and drafts a
short write-up for the recruiter to send from their own mail: a mailto: link, nothing is sent from here. The chase
list is everyone sitting in a stage the client acts on (Submitted, Client review, a hiring manager interview)
longer than a few days by the stage date, grouped per client and hiring manager, with one paste-ready nudge each.
Settings rows only (no new tables): the days before someone counts as waiting, and when each manager was chased.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote

from . import actions, reports
from .store import NotFound, Store, to_dt

DAYS_KEY = "chase_days"
DEFAULT_DAYS = 5
CHASED_KEY = "client_chased:{}"
WRITEUP_NOTES = 3  # screen notes in a write-up, newest first
NOTE_CHARS = 400
SHOWN = 15  # people per nudge, then "and 3 more"
_COMP = re.compile(r"\b(comp|compensation|salary|base|ote|total comp|tc|rate|expecting|expects|\$\s?\d)", re.I)
_AVAIL = re.compile(r"\b(availab\w*|notice|start date|can start|start in|starting|open to start)\b", re.I)


# ------------------------------------------------------------------ settings
def chase_days(store: Store) -> int:
    """How many days in a client-facing stage before someone shows on the chase list (default 5)."""
    raw = store._row("SELECT value FROM settings WHERE key = ?", (DAYS_KEY,))
    try:
        n = int(raw["value"]) if raw else DEFAULT_DAYS
    except (TypeError, ValueError):
        n = DEFAULT_DAYS
    return n if 1 <= n <= 90 else DEFAULT_DAYS


def set_chase_days(store: Store, value: Any) -> int:
    try:
        n = int(str(value).strip())
    except (TypeError, ValueError):
        raise ValueError("Give the days as a number from 1 to 90.") from None
    if not 1 <= n <= 90:
        raise ValueError("Give the days as a number from 1 to 90.")
    actions._put_setting(store, DAYS_KEY, str(n))
    return n


def _chased_key(key: str, manager: str) -> str:
    return CHASED_KEY.format(f"{key}|{manager}")


def chased_at(store: Store, key: str, manager: str) -> str | None:
    raw = reports._get_json(store, _chased_key(key, manager))
    return raw.get("at") if isinstance(raw, dict) else None


def mark_chased(store: Store, key: str, manager: str, now: datetime | None = None) -> str:
    """The recruiter sent this manager's nudge: Today stops asking until the next one is due."""
    reports.client(store, key)  # NotFound for a client that isn't here
    ts = (now or datetime.now(timezone.utc)).replace(microsecond=0).isoformat()
    reports._put_json(store, _chased_key(key, manager), {"at": ts})
    return ts


# ------------------------------------------------------------------ helpers
def _first(name: str | None) -> str:
    return (name or "").strip().split(" ")[0] if (name or "").strip() else ""


def _mailto(to: str | None, subject: str, body: str, cc: list[str] | None = None) -> dict[str, Any] | None:
    """{"href", "to", "long"}: the recruiter's own mail app opens with it filled in. A long body would be cut off
    in the link, so then it opens without it and the page says to paste the copied text."""
    if not to:
        return None
    head = f"mailto:{quote(to, safe='@')}?subject={quote(subject, safe='')}"
    if cc:
        head += "&cc=" + quote(",".join(cc), safe="@,")
    full = head + f"&body={quote(body, safe='')}"
    long = len(full) > reports.MAIL_LIMIT
    return {"href": head if long else full, "to": to, "long": long}


def manager_email(c: dict[str, Any], jobs: list[dict[str, Any]], manager: str | None, own: bool) -> str | None:
    """Where a note to this hiring manager goes: their email on a job, else a contact line naming them, else (with
    no manager named, or in-house) the client's first contact."""
    named = next(((j.get("hiring_manager") or "").strip() for j in jobs if (j.get("hiring_manager") or "").strip()), "")
    hm = next(((j.get("hiring_manager_email") or "").strip() for j in jobs
               if (j.get("hiring_manager_email") or "").strip()), None)
    if hm:
        return hm
    if named:
        found = reports.manager_contact(c.get("contacts"), named)
        if found or not own:
            return found
    return reports._contact_email(c.get("contacts"))


def submit_stage(store: Store, job_id: int) -> dict[str, Any] | None:
    """The job's first in-play stage that counts as Submitted (by its Counts as pick or its name), or None."""
    groups = reports.stage_groups(store, job_id)
    return next((s for s in store.job_stages(job_id)
                 if s["outcome"] == "open" and groups.get(s["name"].lower()) == "submitted"), None)


def _lines(text: str | None) -> list[str]:
    return [x.strip(" -*•\t") for x in re.split(r"[\r\n]+|(?<=[.;])\s+", text or "") if x.strip(" -*•\t")]


def _found(texts: list[str], pattern: re.Pattern[str]) -> str | None:
    for t in texts:
        for line in _lines(t):
            if pattern.search(line):
                return line[:200]
    return None


def other_clients(store: Store, candidate_id: int, job_id: int) -> list[dict[str, Any]]:
    """Where else this person is in play on an open job for another company: [{"client", "job", "stage"}]."""
    job = store.get_job(job_id)
    mine = actions.company_key(job.get("company"))
    out = []
    for p in store.candidate_pipelines(candidate_id):
        if p["job_id"] == job_id or not (p.get("job_company") or "").strip():
            continue
        if actions.company_key(p["job_company"]) == mine:
            continue
        other = store.get_job(p["job_id"])
        s = next((x for x in store.job_stages(p["job_id"]) if x["name"].lower() == p["stage"].lower()), None)
        if other["status"] == "open" and (s is None or s["outcome"] == "open"):
            out.append({"client": p["job_company"].strip(), "job": p["job_title"], "job_id": p["job_id"],
                        "stage": p["stage"], "since": p["stage_changed_at"]})
    return out


def earlier_to_client(store: Store, candidate_id: int, job_id: int, now: datetime | None = None) -> str | None:
    """Before a submit: 'Heads up: ... already submitted to Acme on Mar 3, 2026, for Backend.' or None."""
    now = now or datetime.now(timezone.utc)
    job = store.get_job(job_id)
    if not (job.get("company") or "").strip():
        return None
    key = actions.company_key(job["company"])
    cutoff = (now - timedelta(days=reports.RESUBMIT_DAYS)).replace(microsecond=0).isoformat()
    jobs = reports._all_jobs(store).get(key, [])
    hist = reports._history(store, [j["id"] for j in jobs])
    earlier = sorted((e for j in jobs for e in hist.get((candidate_id, j["id"]), [])
                      if reports.is_submittal(e["body"]) and e["at"] >= cutoff), key=lambda e: e["at"])
    if not earlier:
        return None
    last = earlier[-1]
    titles = {j["id"]: j["title"] for j in jobs}
    undated = reports._data(last.get("data")).get("imported") is True
    when = "(imported, date unknown)" if undated else "on " + reports.in_zone(last["at"], None, "%b %-d, %Y")
    name = store.get_candidate(candidate_id)["name"]
    return (f"Heads up: {name} was already submitted to {job['company'].strip()} {when}, for "
            f"{titles[last['job_id']]}. Check before you send them again.")


# ------------------------------------------------------------------ submittal
def writeup(store: Store, candidate_id: int, job_id: int) -> dict[str, Any]:
    """The submittal draft for one person and job: {"subject", "body", "to", "email", "client", "manager"}. Built from
    their profile, the newest screen notes on this job, and comp and availability when a note or the profile says
    them. The recruiter reads and sends it from their own mail."""
    cand = store.get_candidate(candidate_id)
    job = store.get_job(job_id)
    own = actions.inhouse(store)
    company = (job.get("company") or "").strip()
    c = reports.client(store, company) if company else {"contacts": None, "name": "", "key": "", "slug": ""}
    manager = (job.get("hiring_manager") or "").strip()
    app = store._row("SELECT notes FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    notes = [r["body"] for r in store._rows(
        """SELECT body FROM timeline WHERE candidate_id = ? AND kind IN ('note', 'call', 'meeting')
           AND (job_id = ? OR job_id IS NULL) AND coalesce(body, '') <> ''
           AND NOT (json_valid(data) AND json_extract(data, '$.struck_at') IS NOT NULL)
           ORDER BY (job_id IS NULL), at DESC, id DESC LIMIT ?""", (candidate_id, job_id, WRITEUP_NOTES))]
    if app and (app.get("notes") or "").strip() and app["notes"].strip() not in notes:
        notes.insert(0, app["notes"].strip())
    notes = notes[:WRITEUP_NOTES]
    texts = [*notes, cand.get("about") or ""]
    comp, avail = _found(texts, _COMP), _found(texts, _AVAIL)
    hist = store.work_history(candidate_id)[:3]
    lines = [f"Hi {_first(manager) or 'there'},", "",
             f"I'd like to put {cand['name']} forward for {job['title']}.", ""]
    now_role = ", ".join(x for x in (
        " at ".join(x for x in (cand.get("current_title"), cand.get("current_company")) if x), cand.get("location")) if x)
    if now_role:
        lines.append(f"Now: {now_role}")
    if hist:
        lines.append("Before: " + "; ".join(
            " at ".join(x for x in (h.get("title"), h.get("company")) if x) for h in hist if h.get("title") or h.get("company")))
    if cand.get("linkedin_url"):
        lines.append(f"Profile: {cand['linkedin_url']}")
    if notes:
        lines += ["", "From my screen:"]
        lines += [f"- {n[:NOTE_CHARS].strip()}{'...' if len(n) > NOTE_CHARS else ''}" for n in notes]
    if comp or avail:
        lines.append("")
        if comp:
            lines.append(f"Comp: {comp}")
        if avail:
            lines.append(f"Availability: {avail}")
    lines += ["", "Let me know if you'd like to meet them, and when works.", "", "Thanks"]
    subject = f"{cand['name']} for {job['title']}"
    to = manager_email(c, [job], manager or None, own) if company or job.get("hiring_manager_email") else None
    body = "\n".join(lines)
    return {"subject": subject, "body": body, "to": to, "email": _mailto(to, subject, body), "client": c.get("name"),
            "client_slug": c.get("slug"), "manager": manager or None, "has_comp": bool(comp), "has_availability": bool(avail)}


def submit(store: Store, candidate_id: int, job_id: int, by: str = "app") -> dict[str, Any]:
    """Move someone into the job's Submitted stage and draft their write-up. {"moved", "stage", "before", "at",
    "writeup", "warning", "elsewhere"}. A second submit while they're already there moves nothing. Raises
    ValueError when they aren't on the job, are hired or out, or the job has no Submitted stage."""
    job = store.get_job(job_id)
    app = store._row("SELECT stage, stage_changed_at FROM applications WHERE candidate_id = ? AND job_id = ?",
                     (candidate_id, job_id))
    name = store.get_candidate(candidate_id)["name"]
    if app is None:
        raise ValueError(f"{name} isn't on {job['title']} yet. Add them to the job first.")
    target = submit_stage(store, job_id)
    if target is None:
        raise ValueError(f"{job['title']} has no stage that counts as Submitted. Add one (like Submitted to client), "
                         "or set a stage's Counts as to Submitted on the job's Edit stages page.")
    here = next((s for s in store.job_stages(job_id) if s["name"].lower() == app["stage"].lower()), None)
    if here is not None and here["outcome"] != "open":
        raise ValueError(f"{name} is in {app['stage']} on {job['title']}, which counts as "
                         f"{'hired' if here['outcome'] == 'hired' else 'out'}. Move them back first if they're in play.")
    warning = earlier_to_client(store, candidate_id, job_id)
    moved = app["stage"].lower() != target["name"].lower()
    at = app["stage_changed_at"]
    if moved:
        if here is not None and here["position"] > target["position"]:
            raise ValueError(f"{name} is already past Submitted on {job['title']} ({app['stage']}).")
        out = store.move_stage(candidate_id, job_id, target["name"], by=by)
        if by == "ai":
            store.mark_ai_stage(candidate_id, job_id, app["stage"])
        at = out["stage_changed_at"]
    else:
        warning = None  # already there: nothing new to warn about
    return {"moved": moved, "stage": target["name"], "before": app["stage"], "at": at, "warning": warning,
            "elsewhere": other_clients(store, candidate_id, job_id), "writeup": writeup(store, candidate_id, job_id)}


# ------------------------------------------------------------------ the chase
def _waiting_groups(store: Store, job_id: int, own: bool) -> set[str]:
    return {"interview"} if own else {"submitted", "interview"}


def chase_list(store: Store, key: str | None = None, now: datetime | None = None,
               days: int | None = None) -> list[dict[str, Any]]:
    """Everyone in a client-facing stage (Submitted, Client review, a hiring manager interview) for at least `days`
    by their stage date, with no interview booked on that job, one group per client and hiring manager:
    [{"client", "key", "slug", "manager", "manager_key", "to", "people": [...], "chased_at", "text", "email"}].
    Oldest first within a group; groups with the longest wait first."""
    from .importers import imported_placements

    now = now or datetime.now(timezone.utc)
    days = days or chase_days(store)
    own = actions.inhouse(store)
    cutoff = now - timedelta(days=days)
    booked = {(r["candidate_id"], r.get("job_id")) for r in store.upcoming_interviews(days=60, now=now)}
    booked_any = {cid for cid, jid in booked if jid is None}
    groups: dict[tuple[str, str], dict[str, Any]] = {}
    clients = reports._all_jobs(store)
    for ck, jobs in sorted(clients.items(), key=lambda kv: reports._spelling(kv[1]).lower()):
        if key is not None and ck != key:
            continue
        c = None
        for j in jobs:
            if j["status"] != "open":
                continue
            stage_of = reports.stage_groups(store, j["id"])
            want = _waiting_groups(store, j["id"], own)
            undated = imported_placements(store, j["id"])
            for a in store.pipeline(j["id"]):
                g = stage_of.get(a["stage"].lower()) or reports.stage_group(a["stage"])
                since = to_dt(a["stage_changed_at"])
                if g not in want or not since or since > cutoff:
                    continue
                if (a["candidate_id"], j["id"]) in booked or a["candidate_id"] in booked_any:
                    continue  # an interview is booked: they're not waiting on an answer yet
                c = c or reports.client(store, ck)
                mk = reports.manager_key(j.get("hiring_manager"))
                grp = groups.setdefault((ck, mk), {
                    "client": c["name"], "key": ck, "slug": c["slug"], "manager": (j.get("hiring_manager") or "").strip()
                    or None, "manager_key": mk, "people": [], "jobs": [], "_c": c})
                if j not in grp["jobs"]:
                    grp["jobs"].append(j)
                grp["people"].append({"candidate_id": a["candidate_id"], "name": a["candidate_name"],
                                      "job_id": j["id"], "job": j["title"], "stage": a["stage"],
                                      "since": a["stage_changed_at"], "days": (now - since).days,
                                      "date_unknown": a["candidate_id"] in undated})
    out = []
    for (ck, mk), grp in groups.items():
        c = grp.pop("_c")
        grp["people"].sort(key=lambda p: (p["date_unknown"], p["since"]))
        grp["to"] = manager_email(c, grp["jobs"], grp["manager"], own)
        grp["job_ids"] = [j["id"] for j in grp.pop("jobs")]
        grp["chased_at"] = chased_at(store, ck, mk)
        grp["text"] = nudge_text(grp, own, c.get("time_zone"))
        grp["subject"] = ("Feedback on " + ("candidates" if len(grp["people"]) > 1 else grp["people"][0]["name"])
                          + (f" for {c['name']}" if not own else ""))
        grp["email"] = _mailto(grp["to"], grp["subject"], grp["text"])
        grp["longest"] = max(p["days"] for p in grp["people"])
        out.append(grp)
    return sorted(out, key=lambda g: (-g["longest"], g["client"].lower(), g["manager_key"]))


def nudge_text(grp: dict[str, Any], own: bool, tz: str | None = None) -> str:
    """The paste-ready nudge to one hiring manager: who is waiting, since when, and the ask."""
    people = grp["people"]
    lines = [f"Hi {_first(grp.get('manager')) or 'there'},", "",
             ("A quick check on the people in your interview stages that I haven't heard back on yet:" if own else
              "A quick check on the people I sent over that I haven't heard back on yet:")]
    for p in people[:SHOWN]:
        when = "" if p["date_unknown"] else f" since {reports.in_zone(p['since'], tz, '%b %-d')} ({p['days']} days)"
        lines.append(f"- {p['name']} ({p['job']}): {p['stage']}{when}")
    if len(people) > SHOWN:
        lines.append(f"- and {len(people) - SHOWN} more")
    lines += ["", "Could you let me know where each one stands, or if you'd like to meet any of them?", "", "Thanks"]
    return "\n".join(lines)


def chase_text(groups: list[dict[str, Any]], days: int) -> str:
    """The chase list as plain text (for the AI): one block per manager, then their nudge."""
    if not groups:
        return f"Nobody has waited {days} or more days in a client-facing stage. Nothing to chase."
    n = sum(len(g["people"]) for g in groups)
    lines = [f"{n} {'person' if n == 1 else 'people'} waiting {days}+ days on client feedback, "
             f"{len(groups)} {'nudge' if len(groups) == 1 else 'nudges'}:"]
    for i, g in enumerate(groups, 1):
        who = g["client"] + (f", {g['manager']}" if g["manager"] else "")
        lines.append(f"\nNudge {i}: {who}" + (f" (email {g['to']})" if g["to"] else " (no email saved)")
                     + (f". Last chased {reports.in_zone(g['chased_at'], None, '%b %-d')}" if g["chased_at"] else ""))
        lines.append(g["text"])
    return "\n".join(lines)


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """One Today item per client and hiring manager with people waiting on feedback, unless chased lately."""
    now = now or datetime.now(timezone.utc)
    days = chase_days(store)
    items = []
    for g in chase_list(store, now=now, days=days):
        last = to_dt(g["chased_at"])
        if last and now - last < timedelta(days=days):
            continue
        n = len(g["people"])
        who = f"{g['manager']} at {g['client']}" if g["manager"] else g["client"]
        items.append({"key": f"client_chase:{g['key']}:{g['manager_key']}:{now.date().isocalendar()[1]}",
                      "kind": "client_chase", "rank": 2, "when": g["people"][0]["since"],
                      "title": f"Chase {who} for feedback: {n} {'person' if n == 1 else 'people'} waiting {days}+ days",
                      "detail": ", ".join(f"{p['name']} ({p['days']} days)" for p in g["people"][:3])
                      + (f" and {n - 3} more" if n > 3 else ""), "client": g["client"],
                      "href": f"/clients/chase#chase-{reports.slug(g['key'])}-{reports.slug(g['manager_key']) or 'none'}"})
    return items


# ------------------------------------------------------- an interview's job
def interview_jobs(store: Store, interview_id: int) -> dict[str, Any]:
    """{"interview", "jobs"}: the jobs an interview logged with no job could be for (the person's pipelines)."""
    iv = store.get_interview(interview_id)
    if iv.get("kind") != "interview":
        raise NotFound("That isn't an interview.")
    jobs = [{"job_id": p["job_id"], "title": p["job_title"], "company": p.get("job_company"), "stage": p["stage"]}
            for p in store.candidate_pipelines(iv["candidate_id"])]
    return {"interview": iv, "jobs": jobs}


def set_interview_job(store: Store, interview_id: int, job_id: int) -> dict[str, Any]:
    """Say which job an interview is for. Only a job the person is on; the recruiter's own pick (the app)."""
    got = interview_jobs(store, interview_id)
    if job_id not in {j["job_id"] for j in got["jobs"]}:
        raise ValueError("Pick one of the jobs this person is on.")
    with store._write():
        store._conn.execute("UPDATE timeline SET job_id = ? WHERE id = ? AND kind = 'interview'", (job_id, interview_id))
    store._touch_job(job_id)
    return store.get_interview(interview_id)


__all__ = ["chase_days", "chase_list", "chase_text", "interview_jobs", "mark_chased", "nudge_text",
           "set_chase_days", "set_interview_job", "submit", "submit_stage", "today_items", "writeup"]
