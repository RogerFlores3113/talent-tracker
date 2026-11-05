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
from .store import NotFound, Store, now_iso, to_dt

DAYS_KEY = "chase_days"
DEFAULT_DAYS = 5
CHASED_KEY = "client_chased:{}"
WRITEUP_NOTES = 3  # screen notes in a write-up, newest first
NOTE_CHARS = 400
SHOWN = 10  # people per nudge (oldest first), then "and 3 more" with a count per job and stage
TODAY_SHOWN = 5  # names on a Today chase card: the worst few, then "and 30 more"
TABLE_ROWS = 12  # job and stage rows in a nudge's count table
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


MAX_KEY = "chase_max_days"
STAGES_KEY = "chase_stages"
PAGE_SHOWN = 25  # people per manager on the chase page (then "Show all"), and per nudge in client_chase's data
TOOL_GROUPS = 30  # nudges client_chase gives at most; the page has the rest
# Stages the chase leaves out unless the recruiter says to count them: the client is busy with these, not sitting on
# a decision (an onsite loop, reference or background checks).
_SKIP_WORDS = re.compile(r"\b(on\W?site|references?|ref check|background check|background)\b", re.I)


def chase_max_days(store: Store) -> int | None:
    """The optional upper limit: people waiting longer than this are left out (very old ATS rows). None: no limit."""
    raw = store._row("SELECT value FROM settings WHERE key = ?", (MAX_KEY,))
    try:
        n = int(raw["value"]) if raw and raw["value"] else 0
    except (TypeError, ValueError):
        n = 0
    return n if n > 0 else None


def set_chase_max_days(store: Store, value: Any) -> int | None:
    """Set the upper limit in days (empty or 0 takes it off). It has to be more than the chase days."""
    text = str(value if value is not None else "").strip()
    if text in ("", "0"):
        actions._drop_setting(store, MAX_KEY)
        return None
    try:
        n = int(text)
    except ValueError:
        raise ValueError("Give the oldest wait as a number of days, or leave it empty for no limit.") from None
    days = chase_days(store)
    if not days < n <= 3650:
        raise ValueError(f"Give the oldest wait as more than {days} days (the chase days) and at most 3650, or leave "
                         "it empty for no limit.")
    actions._put_setting(store, MAX_KEY, str(n))
    return n


def skipped_by_default(stage: str) -> bool:
    """Onsite and reference or background check stages stay off the chase unless the recruiter counts them."""
    return bool(_SKIP_WORDS.search(stage or ""))


def _stage_picks(store: Store) -> dict[str, bool]:
    raw = reports._get_json(store, STAGES_KEY)
    return {str(k).lower(): bool(v) for k, v in raw.items()} if isinstance(raw, dict) else {}


def counts_stage(store: Store, stage: str, picks: dict[str, bool] | None = None) -> bool:
    """Whether the chase counts people in this stage: the recruiter's pick, else the default."""
    picks = _stage_picks(store) if picks is None else picks
    name = (stage or "").strip().lower()
    return picks[name] if name in picks else not skipped_by_default(name)


def stage_choices(store: Store) -> list[dict[str, Any]]:
    """The client-facing stages on open jobs, for the chase page's pick: [{"name", "counted", "default"}]."""
    own = actions.inhouse(store)
    picks = _stage_picks(store)
    seen: dict[str, str] = {}
    for jobs in reports._all_jobs(store).values():
        for j in jobs:
            if j["status"] != "open":
                continue
            groups = reports.stage_groups(store, j["id"])
            want = _waiting_groups(store, j["id"], own)
            for st in store.job_stages(j["id"]):
                if st["outcome"] == "open" and groups.get(st["name"].lower()) in want:
                    seen.setdefault(st["name"].lower(), st["name"])
    return [{"name": name, "counted": counts_stage(store, name, picks), "default": not skipped_by_default(name)}
            for low, name in sorted(seen.items())]


def set_stage_picks(store: Store, shown: list[str], counted: list[str]) -> dict[str, bool]:
    """Save the chase page's stage pick: of the stages shown, the ticked ones count. A pick that matches the default
    is dropped, so new defaults still apply."""
    picks = _stage_picks(store)
    ticked = {c.strip().lower() for c in counted if c and c.strip()}
    for name in {x.strip().lower() for x in shown if x and x.strip()}:
        on = name in ticked
        if on == (not skipped_by_default(name)):
            picks.pop(name, None)
        else:
            picks[name] = on
    reports._put_json(store, STAGES_KEY, dict(sorted(picks.items())[:200]))
    return picks


def _chased_key(key: str, manager: str) -> str:
    return CHASED_KEY.format(f"{key}|{manager}")


def chased_at(store: Store, key: str, manager: str) -> str | None:
    raw = reports._get_json(store, _chased_key(key, manager))
    return raw.get("at") if isinstance(raw, dict) else None


def mark_chased(store: Store, key: str, manager: str, now: datetime | None = None,
                snooze_days: int | None = None) -> str:
    """The recruiter sent this manager's nudge: Today stops asking until the next one is due, or for snooze_days."""
    reports.client(store, key)  # NotFound for a client that isn't here
    when = (now or datetime.now(timezone.utc)).replace(microsecond=0)
    row: dict[str, Any] = {"at": when.isoformat()}
    if snooze_days:
        row["until"] = (when + timedelta(days=snooze_days)).isoformat()
    reports._put_json(store, _chased_key(key, manager), row)
    return row["at"]


def snooze_days_from(value: Any) -> int | None:
    """The snooze box: empty means the chase days; otherwise 1 to 365 days."""
    text = str(value if value is not None else "").strip()
    if not text:
        return None
    try:
        n = int(text)
    except ValueError:
        raise ValueError("Give the snooze as a number of days from 1 to 365.") from None
    if not 1 <= n <= 365:
        raise ValueError("Give the snooze as a number of days from 1 to 365.")
    return n


def mark_client_chased(store: Store, key: str, snooze_days: int | None = None,
                       now: datetime | None = None) -> int:
    """Mark every hiring manager's nudge for this client as chased in one go, with an optional snooze of N days.
    Returns how many nudges that covered (at least one, so a client with nothing waiting still snoozes)."""
    reports.client(store, key)
    managers = {g["manager_key"] for g in chase_list(store, key, now=now)} or {""}
    for mk in managers:
        mark_chased(store, key, mk, now, snooze_days)
    return len(managers)


def snoozed(store: Store, key: str, manager: str, now: datetime) -> bool:
    """Whether Today should stay quiet about this nudge: chased within the chase days, or snoozed until later."""
    raw = reports._get_json(store, _chased_key(key, manager))
    if not isinstance(raw, dict):
        return False
    until, last = to_dt(raw.get("until")), to_dt(raw.get("at"))
    if until:
        return now < until
    return bool(last and now - last < timedelta(days=chase_days(store)))


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


def cc_main_link(rep: dict[str, Any], text: str) -> dict[str, Any] | None:
    """For an agency's per-manager update: the same email with the client's main contact on Cc, as an offer next
    to the manager-only one. {"href", "to", "cc", "long"} or None (in-house, a client update, no main contact, or
    the manager is the main contact)."""
    if rep.get("manager") is None or rep.get("inhouse"):
        return None
    to = rep.get("email")
    main = reports._contact_email((rep.get("client") or {}).get("contacts"))
    if not to or not main or main.lower() == to.lower():
        return None
    link = _mailto(to, reports.title(rep), text, cc=[main])
    return {**link, "cc": [main]} if link else None


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
_KEPT = re.compile(r"^kept from the shortlist\.\s*(why they fit:\s*)?", re.I)
_SYSTEM_NOTES = (re.compile(r"^[\w .]{1,40} link \(it only opens inside "), re.compile(r"^reason: position filled$", re.I))


def screen_note(text: str | None) -> str | None:
    """A note as the write-up's "From my screen" shows it: the recruiter's own words and the fit line, never the
    app's own notes ("Kept from the Shortlist.", a lead link, a close reason). None to leave it out."""
    t = (text or "").strip()
    m = _KEPT.match(t)
    if m:
        t = t[m.end():].strip()
    if not t or any(p.match(t) for p in _SYSTEM_NOTES):
        return None
    return t


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
           ORDER BY (job_id IS NULL), at DESC, id DESC LIMIT ?""", (candidate_id, job_id, WRITEUP_NOTES * 3))]
    if app and (app.get("notes") or "").strip() and app["notes"].strip() not in notes:
        notes.insert(0, app["notes"].strip())
    notes = list(dict.fromkeys(n for n in (screen_note(x) for x in notes) if n))[:WRITEUP_NOTES]
    texts = [*notes, cand.get("about") or ""]
    comp, avail = _found(texts, _COMP), _found(texts, _AVAIL)
    hist = store.work_history(candidate_id)[:3]
    lines = [f"Hi {_first(manager) or 'there'},", "",
             f"I'd like to put {cand['name']} forward for {job['title']}.", ""]
    now_role = ", ".join(x for x in (
        " at ".join(x for x in (cand.get("current_title"), cand.get("current_company")) if x), cand.get("location")) if x)
    if now_role:
        lines.append(f"{'Now' if cand.get('current_title') or cand.get('current_company') else 'Based in'}: {now_role}")
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
               days: int | None = None, left_out: dict[str, int] | None = None) -> list[dict[str, Any]]:
    """Everyone in a client-facing stage (Submitted, Client review, a hiring manager interview) for at least `days`
    by their stage date, with no interview booked on that job, one group per client and hiring manager:
    [{"client", "key", "slug", "manager", "manager_key", "to", "people": [...], "chased_at", "text", "email"}].
    Oldest first within a group; groups with the longest wait first. Stages the chase doesn't count (Onsite and
    reference checks by default) and waits past the optional upper limit are left out, counted in `left_out`
    ({"stages", "too_old"}) when given."""
    from .importers import imported_placements

    now = now or datetime.now(timezone.utc)
    days = days or chase_days(store)
    own = actions.inhouse(store)
    cutoff = now - timedelta(days=days)
    oldest = chase_max_days(store)
    too_old = now - timedelta(days=oldest) if oldest else None
    picks = _stage_picks(store)
    left = left_out if left_out is not None else {}
    left.setdefault("stages", 0)
    left.setdefault("too_old", 0)
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
                if not counts_stage(store, a["stage"], picks):
                    left["stages"] += 1
                    continue
                if too_old and since < too_old and a["candidate_id"] not in undated:
                    left["too_old"] += 1
                    continue
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
        grp["by_job_stage"] = by_job_stage(grp["people"])
        out.append(grp)
    return sorted(out, key=lambda g: (-g["longest"], g["client"].lower(), g["manager_key"]))


def by_job_stage(people: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """[{"job", "job_id", "stage", "count", "longest"}] for these people, biggest wait first."""
    rows: dict[tuple[int, str], dict[str, Any]] = {}
    for p in people:
        r = rows.setdefault((p["job_id"], p["stage"]), {"job": p["job"], "job_id": p["job_id"], "stage": p["stage"],
                                                        "count": 0, "longest": 0})
        r["count"] += 1
        r["longest"] = max(r["longest"], p["days"])
    return sorted(rows.values(), key=lambda r: (-r["longest"], -r["count"], r["job"].lower(), r["stage"].lower()))


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
        rest = by_job_stage(people[SHOWN:])
        lines.append(f"- and {len(people) - SHOWN} more" + (", by job and stage:" if rest else ""))
        lines += [f"  {r['job']}, {r['stage']}: {r['count']}" for r in rest[:TABLE_ROWS]]
        if len(rest) > TABLE_ROWS:
            lines.append(f"  other jobs and stages: {sum(r['count'] for r in rest[TABLE_ROWS:])}")
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
        if snoozed(store, g["key"], g["manager_key"], now):
            continue
        n = len(g["people"])
        who = f"{g['manager']} at {g['client']}" if g["manager"] else g["client"]
        items.append({"key": f"client_chase:{g['key']}:{g['manager_key']}:{now.date().isocalendar()[1]}",
                      "kind": "client_chase", "rank": 2, "when": g["people"][0]["since"],
                      "title": f"Chase {who} for feedback: {n} {'person' if n == 1 else 'people'} waiting {days}+ days",
                      "detail": ", ".join(f"{p['name']} ({p['days']} days)" for p in g["people"][:TODAY_SHOWN])
                      + (f" and {n - TODAY_SHOWN} more" if n > TODAY_SHOWN else ""), "client": g["client"],
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


def attach_interview_job(store: Store, entry: dict[str, Any], job_id: int | None) -> dict[str, Any]:
    """After the AI app logs an interview with job_id: when the log matched one already there with no job (the same
    interview logged again, as the reply asks), give it that job, as an AI change Undo takes back.
    {"job_id": the job it has now, "set": True when this call set it, "skipped": why the job wasn't saved}."""
    if entry.get("kind") != "interview" or job_id is None:
        return {"job_id": entry.get("job_id"), "set": False, "skipped": None}
    have = entry.get("job_id")
    if have == job_id:
        return {"job_id": have, "set": False, "skipped": None}
    job = store.get_job(job_id)
    if have is not None:
        other = store.get_job(have)
        return {"job_id": have, "set": False, "skipped": (
            f"That interview was already logged for {other['title']}, so it stays there and wasn't moved to "
            f"{job['title']}.")}
    if job_id not in {p["job_id"] for p in store.candidate_pipelines(entry["candidate_id"])}:
        return {"job_id": None, "set": False, "skipped": (
            f"That interview was already logged with no job, and it still has none: this person isn't on "
            f"{job['title']}. Add them with add_to_pipeline, then log it again with job_id.")}
    with store._write():
        store._conn.execute("UPDATE timeline SET job_id = ? WHERE id = ? AND kind = 'interview' AND job_id IS NULL",
                            (job_id, entry["id"]))
        store._log_ai("interview_job", candidate_id=entry["candidate_id"], job_id=job_id, target_id=entry["id"],
                      old={"job_id": None}, new={"job_id": job_id, "summary": entry.get("body")})
    store._touch_job(job_id)
    entry["job_id"] = job_id
    return {"job_id": job_id, "set": True, "skipped": None}


def ai_job_blocker(store: Store, a: dict[str, Any]) -> str | None:
    """Why the AI app's job for an interview can't be taken back (short, for Changes), or None."""
    t = store._row("SELECT job_id, data FROM timeline WHERE id = ? AND kind = 'interview'", (a["target_id"],))
    if t is None:
        return "that interview is gone from the timeline"
    if t["job_id"] != (a.get("new_data") or {}).get("job_id"):
        return "its job was changed again since"
    return None


def ai_job_undo(store: Store, a: dict[str, Any]) -> str:
    """Undo the AI app setting an interview's job: it goes back to having no job."""
    reason = ai_job_blocker(store, a)
    if reason:
        raise ValueError(reason[:1].upper() + reason[1:] + ".")
    old = (a.get("old_data") or {}).get("job_id")
    with store._write():
        store._conn.execute("UPDATE timeline SET job_id = ? WHERE id = ?", (old, a["target_id"]))
        store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?",
                            (now_iso(), a["id"]))
    if a.get("job_id"):
        store._touch_job(a["job_id"])
    return a["summary"]


__all__ = ["attach_interview_job", "by_job_stage", "mark_client_chased", "snooze_days_from", "snoozed", "chase_days", "chase_max_days", "counts_stage", "set_chase_max_days",
           "set_stage_picks", "stage_choices", "chase_list", "chase_text", "interview_jobs", "mark_chased", "nudge_text",
           "set_chase_days", "set_interview_job", "submit", "submit_stage", "today_items", "writeup"]
