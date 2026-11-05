"""Clients and client reports.

A client is a job company, matched by actions.company_key ('Acme, Inc.' and 'acme' are one client). Its row in
the clients table (contacts, notes, update day, time zone, when the last update went out) is made the first time
the recruiter saves something for it, never on a read, so opening a page changes nothing. In-house, the "client"
is the recruiter's own company.

A report covers a date range (by default since the last update marked as sent, else the last 7 days): where
people stand now by stage group, who moved in the period (from stage history; marked imports and undone moves
don't count), median days in each stage group, upcoming interviews in the client's time zone, and the submittals.
"""
from __future__ import annotations

import csv
import io
import json
import re
import statistics
from datetime import datetime, timedelta, timezone
from typing import Any
from urllib.parse import quote
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from . import actions
from .store import NotFound, Store, _stage_key, now_iso, parse_when, stage_outcome, to_dt, zone_named

DEFAULT_DAYS = 7
MOVES_SHOWN = 40  # moves listed in the text, then "and 12 more"
INTERVIEW_DAYS = 14
RESUBMIT_DAYS = 365
FIELD_LIMITS = {"name": 200, "contacts": 3000, "notes": 5000}
# Stage groups, in funnel order: (key, label). Sourcing and screening are the recruiter's own work.
GROUPS: tuple[tuple[str, str], ...] = (
    ("sourcing", "Not presented yet"), ("screening", "Screening"), ("submitted", "Submitted"),
    ("interview", "Interviewing"), ("offer", "Offer"), ("hired", "Hired"), ("out", "Out"))
GROUP_LABELS = dict(GROUPS)
IN_PLAY = ("sourcing", "screening", "submitted", "interview", "offer")
# How far along each group is, for "reached at least" counts.
FUNNEL = (("submitted", "Submitted or presented"), ("interview", "Interviewed"), ("offer", "Offer"), ("hired", "Hired"))
# Groups the client has seen: a stage in one of these is never left out of their update.
SEEN = ("submitted", "interview", "offer", "hired")
# In-house there's no submitting to a client: those words go, and the first group is the recruiter's sourcing.
INHOUSE_LABELS = {**GROUP_LABELS, "sourcing": "Sourcing"}
_RANK = {"submitted": 1, "interview": 2, "offer": 3, "hired": 4}
SUBMIT_WORDS = frozenset({"submitted", "submittal", "submit", "presented", "present", "presentation"})
INTERVIEW_WORDS = frozenset({"interview", "interviews", "interviewing", "interviewed", "onsite", "panel", "final"})
TZ_ALIASES = {
    "et": "America/New_York", "est": "America/New_York", "edt": "America/New_York", "eastern": "America/New_York",
    "ct": "America/Chicago", "cst": "America/Chicago", "cdt": "America/Chicago", "central": "America/Chicago",
    "mt": "America/Denver", "mst": "America/Denver", "mdt": "America/Denver", "mountain": "America/Denver",
    "pt": "America/Los_Angeles", "pst": "America/Los_Angeles", "pdt": "America/Los_Angeles",
    "pacific": "America/Los_Angeles", "uk": "Europe/London", "london": "Europe/London", "gmt": "UTC", "utc": "UTC",
}
COMMON_ZONES = ("America/New_York", "America/Chicago", "America/Denver", "America/Los_Angeles", "America/Toronto",
                "America/Sao_Paulo", "Europe/London", "Europe/Berlin", "Europe/Paris", "Asia/Kolkata",
                "Asia/Singapore", "Asia/Tokyo", "Australia/Sydney", "UTC")
UPDATE_DAYS = tuple(d.capitalize() for d in actions.WEEKDAYS)
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")


# ------------------------------------------------------------- small helpers
def _fmt(dt: datetime, fmt: str) -> str:
    try:
        return dt.strftime(fmt)
    except ValueError:  # Windows strftime has no %-d
        return dt.strftime(fmt.replace("%-", "%#"))


def _zone(name: str | None) -> Any:
    """The tzinfo for a saved time zone name, or None (this computer's zone) when it's unset or unknown here."""
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except (ZoneInfoNotFoundError, ValueError, OSError):
        return zone_named(name)  # no time zone list here (Windows without tzdata): US zones still work


def in_zone(iso: str | None, tz_name: str | None, fmt: str = "%a %b %-d, %-I:%M %p %Z") -> str:
    """A UTC time in the client's time zone (or this computer's when the client has none)."""
    dt = to_dt(iso)
    if not dt:
        return ""
    return _fmt(dt.astimezone(_zone(tz_name)), fmt).strip()


def normalize_tz(value: str | None) -> str | None:
    """'America/New_York', 'Eastern' or 'ET' -> 'America/New_York'. Blank -> None. Raises on a name we can't use."""
    v = (value or "").strip()
    if not v:
        return None
    v = TZ_ALIASES.get(v.lower(), v)
    if _zone(v) is not None:
        return v
    try:
        ZoneInfo("America/New_York")
        have_db = True
    except (ZoneInfoNotFoundError, ValueError, OSError):
        have_db = False
    if not have_db and re.fullmatch(r"[A-Za-z_]+(?:/[A-Za-z0-9_+-]+){0,2}", v):
        return v  # this computer has no time zone list to check against; times show in its own zone meanwhile
    raise ValueError(f"Couldn't find the time zone {value!r}. Use a name like America/New_York, Europe/London "
                     "or UTC, or ET, CT, MT or PT.")


def slug(key: str) -> str:
    """A client's key as it sits in an address: 'acme payments' -> 'acme-payments'."""
    return key.replace(" ", "-")


def unslug(text: str) -> str:
    return actions.company_key(text.replace("-", " "))


def is_submittal(name: str | None) -> bool:
    """A stage named like Submitted, Presented, Client submitted or Submitted to client (not an offer)."""
    words = _stage_key(name).split()
    return bool(SUBMIT_WORDS & set(words)) and "offer" not in words


def stage_group(name: str | None, outcome: str | None = None) -> str:
    """Which part of the funnel a stage is: sourcing, screening, submitted, interview, offer, hired or out."""
    outcome = outcome or stage_outcome(name or "")
    if outcome == "hired":
        return "hired"
    if outcome == "lost":
        return "out"
    words = _stage_key(name).split()
    if "offer" in words:
        return "offer"
    if is_submittal(name):
        return "submitted"
    if INTERVIEW_WORDS & set(words) or words[:1] == ["hm"] or words[:2] == ["hiring", "manager"]:
        return "interview"
    if actions.internal_like(name):
        return "sourcing"
    return "screening"


def _data(raw: Any) -> dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    try:
        out = json.loads(raw) if raw else {}
    except ValueError:
        return {}
    return out if isinstance(out, dict) else {}


def _is_import(entry: dict[str, Any]) -> bool:
    """A stage entry an import wrote, when the import marked it (source CSV import, by import, or imported)."""
    d = _data(entry.get("data"))
    return entry.get("source") == "CSV import" or d.get("by") == "import" or d.get("imported") is True


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else (many or word + 's')}"


# ------------------------------------------------------------------- clients
def _all_jobs(store: Store) -> dict[str, list[dict[str, Any]]]:
    """Every job with a company, by client key, newest first."""
    out: dict[str, list[dict[str, Any]]] = {}
    for j in store._rows("SELECT * FROM jobs WHERE trim(coalesce(company, '')) <> '' ORDER BY updated_at DESC, id DESC"):
        out.setdefault(actions.company_key(j["company"]), []).append(j)
    return out


def _spelling(jobs: list[dict[str, Any]]) -> str:
    """The company name as the newest open job spells it (else the newest job)."""
    live = [j for j in jobs if j["status"] != "closed"] or jobs
    return live[0]["company"].strip()


def get_row(store: Store, key: str) -> dict[str, Any] | None:
    return store._row("SELECT * FROM clients WHERE key = ?", (key,))


def client(store: Store, key: str) -> dict[str, Any]:
    """A client's saved details (or blanks) with its jobs. NotFound when no job names it and nothing is saved."""
    key = actions.company_key(key)
    jobs = _all_jobs(store).get(key, []) if key else []
    row = get_row(store, key) if key else None
    if not jobs and not row:
        raise NotFound("No client with that name. A client appears here once a job names it as the company.")
    base = {"id": None, "name": _spelling(jobs) if jobs else key, "key": key, "contacts": None, "notes": None,
            "update_day": None, "time_zone": None, "last_update_sent_at": None}
    return {**base, **(row or {}), "slug": slug(key), "jobs": jobs}


def ensure_client(store: Store, key: str) -> dict[str, Any]:
    """The client's row, made from its jobs the first time something is saved for it. One row per key."""
    c = client(store, key)
    if c["id"] is None:
        ts = now_iso()
        with store._lock, store._conn:
            store._conn.execute("INSERT OR IGNORE INTO clients(name, key, created_at, updated_at) VALUES (?, ?, ?, ?)",
                                (c["name"], c["key"], ts, ts))
    return client(store, key)


def save_client(store: Store, key: str, **fields: Any) -> dict[str, Any]:
    """Save a client's name, contacts, notes, update day or time zone. Raises ValueError on a bad value."""
    clean: dict[str, Any] = {}
    for k, v in fields.items():
        if k not in ("name", "contacts", "notes", "update_day", "time_zone"):
            raise ValueError(f"Unknown client field {k}")
        v = (str(v) if v is not None else "").strip() or None
        if k in FIELD_LIMITS and v and len(v) > FIELD_LIMITS[k]:
            raise ValueError(f"The {k} are too long ({len(v):,} characters). The limit is {FIELD_LIMITS[k]:,}."
                             if k != "name" else f"That name is too long. The limit is {FIELD_LIMITS[k]} characters.")
        if k == "update_day" and v:
            v = v.capitalize()
            if v not in UPDATE_DAYS:
                raise ValueError("Pick a day of the week for updates.")
        if k == "time_zone":
            v = normalize_tz(v)
        clean[k] = v
    if "name" in clean and not clean["name"]:
        clean.pop("name")
    c = ensure_client(store, key)
    if clean:
        with store._lock, store._conn:
            store._conn.execute(f"UPDATE clients SET {', '.join(f'{k} = ?' for k in clean)}, updated_at = ? WHERE id = ?",
                                (*clean.values(), now_iso(), c["id"]))
    return client(store, key)


def find_clients(store: Store, query: str | None) -> list[str]:
    """The client keys a name means: the same name (case, punctuation and Inc or LLC don't count), else names
    starting with it, else the closest spelling. Saved clients with no jobs count too."""
    jobs = _all_jobs(store)
    names = [_spelling(js) for js in jobs.values()]
    names += [r["name"] for r in store._rows("SELECT name, key FROM clients") if r["key"] not in jobs]
    wanted = actions.match_companies(names, query)
    return sorted({actions.company_key(n) for n in wanted})


def _contact_email(contacts: str | None) -> str | None:
    m = _EMAIL.search(contacts or "")
    return m.group(0) if m else None


def manager_key(name: str | None) -> str:
    """A hiring manager's name for grouping: any case and spacing. '' for jobs that name none."""
    return re.sub(r"\s+", " ", (name or "").strip().lower())


def managers(store: Store, key: str) -> list[tuple[str, str]]:
    """In-house, the hiring managers of this company's open and on-hold jobs, as (manager_key, name), with
    ('', '') last for jobs that name none. Empty when no job names one, or in agency wording."""
    if not actions.inhouse(store):
        return []
    jobs = [j for j in client(store, key)["jobs"] if j["status"] in ("open", "on_hold")]
    names: dict[str, str] = {}
    for j in jobs:
        names.setdefault(manager_key(j.get("hiring_manager")), (j.get("hiring_manager") or "").strip())
    if not any(names):
        return []
    return sorted(names.items(), key=lambda kv: (kv[0] == "", kv[0]))


def find_manager(store: Store, key: str, query: str | None) -> list[str]:
    """The manager keys a name means among this company's hiring managers, matched as in status_digest."""
    names = [n for _, n in managers(store, key) if n] or [
        (j.get("hiring_manager") or "").strip() for j in client(store, key)["jobs"]]
    return sorted({manager_key(n) for n in actions.match_companies(names, query)})


def _upcoming(store: Store, job_ids: set[int], app_pairs: set[tuple[int, int]], days: int,
              now: datetime) -> list[dict[str, Any]]:
    """Live interviews in the next `days` for people on these jobs (or with no job, while in play on one)."""
    jobs = {j["id"]: j for j in store._rows("SELECT * FROM jobs")}
    rows = actions._live_interviews(store, store.upcoming_interviews(days=days, now=now), jobs)
    in_play = {cid: jid for cid, jid in app_pairs}
    out = []
    for r in rows:
        if r.get("job_id") in job_ids:
            out.append(r)
        elif r.get("job_id") is None and r["candidate_id"] in in_play:
            out.append({**r, "job_id": in_play[r["candidate_id"]], "job_title": jobs[in_play[r["candidate_id"]]]["title"]})
    return out


def _in_play_pairs(store: Store, job_ids: list[int]) -> set[tuple[int, int]]:
    if not job_ids:
        return set()
    marks = ",".join("?" * len(job_ids))
    return {(r["candidate_id"], r["job_id"]) for r in store._rows(
        f"""SELECT a.candidate_id, a.job_id FROM applications a JOIN jobs j ON j.id = a.job_id
            LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
            WHERE a.job_id IN ({marks}) AND j.status = 'open' AND coalesce(s.outcome, 'open') = 'open'""", job_ids)}


def update_due(c: dict[str, Any], now: datetime | None = None) -> bool:
    """The client's update day has come this week and no update went out since it began."""
    if not c.get("update_day"):
        return False
    local = (now or datetime.now(timezone.utc)).astimezone()
    want = UPDATE_DAYS.index(c["update_day"])
    if local.weekday() < want:
        return False
    day_start = (local - timedelta(days=local.weekday() - want)).replace(hour=0, minute=0, second=0, microsecond=0)
    sent = to_dt(c.get("last_update_sent_at"))
    return not sent or sent < day_start


def overview(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """One row per client with jobs: open jobs, people in play, interviews in the next 7 days, last update sent."""
    now = now or datetime.now(timezone.utc)
    rows = {r["key"]: r for r in store._rows("SELECT * FROM clients")}
    out = []
    for key, jobs in _all_jobs(store).items():
        open_ids = [j["id"] for j in jobs if j["status"] == "open"]
        pairs = _in_play_pairs(store, open_ids)
        c = {"id": None, "name": _spelling(jobs), "last_update_sent_at": None, "update_day": None,
             **(rows.get(key) or {}), "key": key, "slug": slug(key)}
        out.append({**c, "open_jobs": len(open_ids), "jobs": len(jobs), "in_play": len({p[0] for p in pairs}),
                    "interviews": len(_upcoming(store, set(open_ids), pairs, 7, now)), "due": update_due(c, now)})
    return sorted(out, key=lambda r: (r["open_jobs"] == 0, r["name"].lower()))


def no_company_jobs(store: Store) -> int:
    return store._row("SELECT count(*) AS n FROM jobs WHERE trim(coalesce(company, '')) = ''")["n"]


# -------------------------------------------------------------------- report
def _range(store: Store, c: dict[str, Any], since: str | datetime | None, until: str | datetime | None,
           now: datetime) -> tuple[str, str, str]:
    """(since, until, why) in UTC ISO. A plain day for `until` means the end of that day."""
    if until is None or (isinstance(until, str) and not until.strip()):
        end = now.replace(microsecond=0).isoformat()
    else:
        end = parse_when(until)
        if isinstance(until, str) and re.fullmatch(r"\s*\d{4}-\d{2}-\d{2}\s*", until):
            end = (to_dt(end) + timedelta(days=1, seconds=-1)).isoformat()
    if since is None or (isinstance(since, str) and not since.strip()):
        if c.get("last_update_sent_at"):
            return c["last_update_sent_at"], end, "last_update"
        return (to_dt(end) - timedelta(days=DEFAULT_DAYS)).isoformat(), end, "default"
    start = parse_when(since)
    if start > end:
        raise ValueError("The start of the range is after its end. Pick an earlier start day.")
    return start, end, "chosen"


def _history(store: Store, job_ids: list[int]) -> dict[tuple[int, int], list[dict[str, Any]]]:
    """Stage entries per (candidate, job), oldest first, without undone moves or the entries that undid them."""
    if not job_ids:
        return {}
    marks = ",".join("?" * len(job_ids))
    out: dict[tuple[int, int], list[dict[str, Any]]] = {}
    for t in store._rows(
            f"""SELECT t.id, t.candidate_id, t.job_id, t.body, t.at, t.source, t.data, c.name AS candidate_name
                FROM timeline t JOIN candidates c ON c.id = t.candidate_id
                WHERE t.kind = 'stage' AND t.job_id IN ({marks}) ORDER BY t.at, t.id""", job_ids):
        d = _data(t["data"])
        if d.get("undone_at") or d.get("undo_of"):
            continue
        t["imported"] = _is_import(t)
        out.setdefault((t["candidate_id"], t["job_id"]), []).append(t)
    return out


def _omit(store: Store, job_id: int) -> set[str]:
    """The stage keys a client update leaves out on this job: its status update's Leave out choice, but never a
    stage the client has seen (named like Submitted, an interview, an offer, or a hire stage)."""
    keys = actions._omitted(store, job_id, None)
    return {_stage_key(s["name"]) for s in store.job_stages(job_id)
            if _stage_key(s["name"]) in keys and stage_group(s["name"], s["outcome"]) not in SEEN}


def report(store: Store, key: str, since: str | datetime | None = None, until: str | datetime | None = None,
           now: datetime | None = None, manager: str | None = None) -> dict[str, Any]:
    """Everything a client update says, as data. report_text turns it into the paste-ready text.

    manager (a manager_key, '' for none) keeps only that hiring manager's jobs, for in-house updates. Counts are
    of people: someone on two of the client's jobs counts once."""
    now = now or datetime.now(timezone.utc)
    c = client(store, key)
    own = actions.inhouse(store)
    start, end, why = _range(store, c, since, until, now)
    jobs = c["jobs"] if manager is None else [j for j in c["jobs"] if manager_key(j.get("hiring_manager")) == manager]
    ids = [j["id"] for j in jobs]
    by_id = {j["id"]: j for j in jobs}
    outcome = {j["id"]: {s["name"].lower(): s["outcome"] for s in store.job_stages(j["id"])} for j in jobs}
    omit = {j["id"]: _omit(store, j["id"]) for j in jobs}
    live = [j for j in jobs if j["status"] in ("open", "on_hold")]
    booked = {j["id"]: actions.booked_hidden(store, j["id"], omit[j["id"]], None, now)
              for j in jobs if j["status"] == "open"}

    def group(job_id: int, stage: str) -> str:
        g = stage_group(stage, outcome[job_id].get((stage or "").lower()))
        return "screening" if own and g == "submitted" else g

    def hidden(job_id: int, stage: str) -> bool:
        return _stage_key(stage) in omit[job_id]

    def in_range(at: str) -> bool:  # an update marked as sent covers its own second, so the next starts after it
        return (start < at if why == "last_update" else start <= at) and at <= end

    hist = _history(store, ids)
    # Where people stand now, on open and on-hold jobs.
    standing: dict[str, set[int]] = {g: set() for g, _ in GROUPS}
    not_shown: dict[str, int] = {}
    booked_names: dict[int, str] = {}
    people: list[dict[str, Any]] = []
    for j in live:
        for a in store.pipeline(j["id"]):
            if hidden(j["id"], a["stage"]):
                if a["candidate_id"] in booked.get(j["id"], {}):  # their interview is listed; told to the recruiter apart
                    booked_names[a["candidate_id"]] = a["candidate_name"]
                else:
                    not_shown[a["stage"]] = not_shown.get(a["stage"], 0) + 1
                continue
            g = group(j["id"], a["stage"])
            if j["status"] == "open":
                standing[g].add(a["candidate_id"])
            entries = hist.get((a["candidate_id"], j["id"]), [])
            subs = [e["at"] for e in entries if is_submittal(e["body"])]
            people.append({"candidate_id": a["candidate_id"], "name": a["candidate_name"], "job_id": j["id"],
                           "job": j["title"], "title": a.get("current_title"), "company": a.get("current_company"),
                           "stage": a["stage"], "group": g, "since": a["stage_changed_at"],
                           "days": max(0, (now - to_dt(a["stage_changed_at"])).days),
                           "submitted_at": subs[-1] if subs else None,
                           "imported": bool(entries and entries[-1]["imported"]
                                            and entries[-1]["body"].lower() == a["stage"].lower())})
    now_counts = {g: len(v) for g, v in standing.items()}
    in_play = len(set().union(*(standing[g] for g in IN_PLAY)))
    # Funnel: how far everyone got (each person once, at their furthest on any of these jobs), and who moved.
    best: dict[int, int] = {}
    period: dict[str, set[int]] = {g: set() for g, _ in GROUPS}
    moves: list[dict[str, Any]] = []
    spans: dict[str, list[float]] = {g: [] for g in IN_PLAY}
    current = {(p["candidate_id"], p["job_id"]): p["stage"] for j in jobs for p in store.pipeline(j["id"])}
    for (cid, jid), entries in hist.items():
        stages = [e["body"] for e in entries]
        if (cid, jid) in current:
            stages.append(current[(cid, jid)])
        best[cid] = max(best.get(cid, 0), max((_RANK.get(group(jid, s), 0) for s in stages), default=0))
        for i, e in enumerate(entries):
            g = group(jid, e["body"])
            nxt = entries[i + 1]["at"] if i + 1 < len(entries) else None
            if nxt is None and current.get((cid, jid), "").lower() == (e["body"] or "").lower() and g in IN_PLAY:
                nxt = now.isoformat()
            if nxt and g in IN_PLAY and not e["imported"] and not hidden(jid, e["body"]):
                spans[g].append((to_dt(nxt) - to_dt(e["at"])).total_seconds() / 86400)
            if not in_range(e["at"]) or e["imported"] or hidden(jid, e["body"]):
                continue
            period[g].add(cid)
            moves.append({"at": e["at"], "candidate_id": cid, "name": e["candidate_name"], "job_id": jid,
                          "job": by_id[jid]["title"], "stage": e["body"], "group": g,
                          "from": entries[i - 1]["body"] if i else None})
    reached = {g: sum(1 for b in best.values() if b >= _RANK[g]) for g, _ in FUNNEL}
    # Back and forth to the same stage on one job is one move: the latest.
    seen_moves: set[tuple[int, int, str]] = set()
    for m in reversed(moves):
        mk = (m["candidate_id"], m["job_id"], _stage_key(m["stage"]))
        m["repeat"] = mk in seen_moves
        seen_moves.add(mk)
    moves = [{k: v for k, v in m.items() if k != "repeat"} for m in moves if not m["repeat"]]
    imported = sum(1 for es in hist.values() for e in es if e["imported"] and in_range(e["at"]))
    medians = {g: round(statistics.median(v), 1) for g, v in spans.items() if v}
    open_ids = {j["id"] for j in jobs if j["status"] == "open"}
    # Everyone still in the running on an open job, whatever their stage: an interview booked while they sit in
    # Sourced is still an interview with the client.
    pairs = _in_play_pairs(store, sorted(open_ids))
    upcoming = [{"interview_id": r["id"], "at": r["at"], "at_client": in_zone(r["at"], c.get("time_zone")),
                 "candidate_id": r["candidate_id"], "name": r["candidate_name"], "job": r.get("job_title"),
                 "summary": r.get("body")}
                for r in _upcoming(store, open_ids, pairs, INTERVIEW_DAYS, now)
                if (r["candidate_id"], r["job_id"]) in pairs]
    still_hidden = [{"candidate_id": cid, "name": booked_names.get(cid, ""), "stage": stage, "job": by_id[jid]["title"]}
                    for jid, b in booked.items() for cid, stage in b.items()]
    open_jobs = [j for j in jobs if j["status"] == "open"]
    openings = sum(j.get("openings") or 1 for j in open_jobs)
    hires = {j["id"]: sum(len(g["candidates"]) for g in store.pipeline_by_stage(j["id"]) if g["outcome"] == "hired")
             for j in open_jobs}
    filled = sum(min(hires[j["id"]], j.get("openings") or 1) for j in open_jobs)
    overfilled = [{"job_id": j["id"], "title": j["title"], "hired": hires[j["id"]], "openings": j.get("openings") or 1,
                   "over": hires[j["id"]] - (j.get("openings") or 1)}
                  for j in open_jobs if hires[j["id"]] > (j.get("openings") or 1)]
    words = {actions.hire_word(store, j["id"]) for j in open_jobs}
    label = None if manager is None else next((j["hiring_manager"].strip() for j in jobs
                                               if (j.get("hiring_manager") or "").strip()), "")
    hm_email = next((j["hiring_manager_email"].strip() for j in jobs if (j.get("hiring_manager_email") or "").strip()),
                    None) if manager else None
    return {"client": {k: c.get(k) for k in ("id", "name", "key", "slug", "contacts", "time_zone", "update_day",
                                             "last_update_sent_at")},
            "inhouse": own, "manager": label, "booked_hidden": still_hidden, "overfilled": overfilled,
            "hired": sum(hires.values()), "hire_word": "placed" if words == {"placed"} else "hired",
            "since": start, "until": end, "range_from": why, "now": now.isoformat(),
            "jobs": [{"id": j["id"], "title": j["title"], "status": j["status"], "location": j.get("location"),
                      "openings": j.get("openings") or 1} for j in jobs],
            "in_play": in_play, "stand": now_counts, "not_shown": not_shown,
            "reached": reached, "period": {g: len(v) for g, v in period.items()}, "moves": moves,
            "imported_moves": imported, "median_days": medians, "upcoming": upcoming, "people": people,
            "openings": openings, "filled": filled, "email": hm_email or _contact_email(c.get("contacts"))}


def reports_for(store: Store, key: str, since: str | datetime | None = None, until: str | datetime | None = None,
                now: datetime | None = None) -> list[dict[str, Any]]:
    """The updates to send: in-house once jobs name hiring managers, one per manager with only their jobs (so a
    manager never reads another's pipeline); otherwise the one client report."""
    now = now or datetime.now(timezone.utc)
    hms = managers(store, key)
    if not hms:
        return [report(store, key, since, until, now)]
    return [report(store, key, since, until, now, manager=k) for k, _ in hms]


def _day(iso: str, tz: str | None = None) -> str:
    return in_zone(iso, tz, "%a %b %-d, %Y")


def title(rep: dict[str, Any]) -> str:
    who = ""
    if rep.get("manager") is not None:
        who = f" for {rep['manager']}" if rep["manager"] else " for jobs with no hiring manager"
    return f"{rep['client']['name']}: hiring update{who}, {_day(rep['since'])} to {_day(rep['until'])}"


def seats_text(rep: dict[str, Any]) -> str:
    """'1 of 2 openings filled', or with more hires than openings '2 hired, 1 opening (over by 1)'."""
    over = rep.get("overfilled") or []
    if not over:
        return actions.filled_text(rep["filled"], rep["openings"])
    seats = _plural(rep["openings"], "opening")
    if len(rep["jobs"]) == 1 or len([j for j in rep["jobs"] if j["status"] == "open"]) == 1:
        return f"{rep['hired']} {rep['hire_word']}, {seats} (over by {over[0]['over']})"
    return (f"{rep['hired']} {rep['hire_word']}, {seats} ("
            + ", ".join(f"{o['title']} over by {o['over']}" for o in over) + ")")


def recruiter_notes(rep: dict[str, Any]) -> list[str]:
    """What the recruiter should know and the client shouldn't read: who the update leaves out, people listed
    though their stage is left out, and jobs with more hires than openings. Never part of the paste text."""
    notes = []
    if rep["not_shown"]:
        notes.append("Not shown: " + ", ".join(f"{s} ({n})" for s, n in rep["not_shown"].items()))
    if rep.get("booked_hidden"):
        notes.append("In Upcoming interviews though their stage is left out, since an interview is booked: " + ", ".join(
            f"{b['name']} ({b['stage']}, {b['job']})" for b in rep["booked_hidden"]))
    for o in rep.get("overfilled") or []:
        notes.append(f"{o['title']} has {o['hired']} {rep.get('hire_word', 'hired')} for "
                     f"{_plural(o['openings'], 'opening')} (over by {o['over']}). Check those hires before you send this")
    return notes


def report_text(rep: dict[str, Any]) -> str:
    """The paste-ready update: no ids, times in the client's time zone, internal stages left out."""
    tz = rep["client"].get("time_zone")
    own = rep.get("inhouse")
    labels = INHOUSE_LABELS if own else GROUP_LABELS
    lines = [title(rep)]
    if rep["range_from"] == "last_update":
        lines.append(f"Changes since the last update on {_day(rep['since'])}.")
    live = [j for j in rep["jobs"] if j["status"] == "open"]
    held = [j for j in rep["jobs"] if j["status"] == "on_hold"]
    if live:
        lines.append(f"{_plural(len(live), 'open job')}: " + ", ".join(j["title"] for j in live)
                     + f". {seats_text(rep)}.")
    else:
        lines.append("No open jobs right now.")
    if held:
        lines.append("On hold: " + ", ".join(j["title"] for j in held) + ".")
    lines += ["", f"In process now: {rep['in_play']}"]
    lines += [f"- {labels[g]}: {rep['stand'][g]}" for g in IN_PLAY if rep["stand"][g]]
    p = rep["period"]
    lines += ["", "This period:"]
    moved = ("interview", "offer", "hired", "out") if own else ("submitted", "interview", "offer", "hired", "out")
    lines.append("- Moved into " + ", ".join(f"{labels[g]} {p[g]}" for g in moved))
    r = rep["reached"]
    lines.append("- All time on these jobs: " + ", ".join(f"{label} {r[g]}" for g, label in funnel(rep)))
    if rep["moves"]:
        lines += ["", "Moves this period:"]
        for m in rep["moves"][:MOVES_SHOWN]:
            lines.append(f"- {_day(m['at'], tz)}: {m['name']} to {m['stage']} ({m['job']})")
        if len(rep["moves"]) > MOVES_SHOWN:
            lines.append(f"- and {len(rep['moves']) - MOVES_SHOWN} more")
    else:
        lines += ["", "Moves this period: none."]
    if rep["median_days"]:
        lines += ["", "Median days in stage: " + ", ".join(
            f"{labels[g]} {_num(v)}" for g, v in rep["median_days"].items())]
    if rep["upcoming"]:
        lines += ["", "Upcoming interviews:"]
        lines += [f"- {u['at_client']}: {u['name']} ({u['job']})" for u in rep["upcoming"]]
    return "\n".join(lines)


def funnel(rep: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """The all-time funnel steps: in-house there's no Submitted or presented step."""
    return FUNNEL[1:] if rep.get("inhouse") else FUNNEL


def _num(v: float) -> str:
    return str(int(v)) if float(v).is_integer() else str(v)


def pipeline_csv(rep: dict[str, Any]) -> str:
    """The per-candidate pipeline behind the report, for a spreadsheet. Left-out stages aren't in it."""
    from .importers import _safe_cell

    tz = rep["client"].get("time_zone")
    buf = io.StringIO()
    w = csv.writer(buf)
    own = rep.get("inhouse")
    labels = INHOUSE_LABELS if own else GROUP_LABELS
    w.writerow(["Job", "Candidate", "Current title", "Current company", "Stage", "Stage group", "In stage since",
                "Days in stage", *([] if own else ["Last submitted"]), "Next interview"])
    nxt: dict[tuple[int, str], str] = {}
    for u in rep["upcoming"]:
        nxt.setdefault((u["candidate_id"], u["job"]), u["at_client"])
    for p in sorted(rep["people"], key=lambda p: (p["job"].lower(), [g for g, _ in GROUPS].index(p["group"]),
                                                  p["name"].lower())):
        sub = [] if own else [in_zone(p["submitted_at"], tz, "%Y-%m-%d") if p["submitted_at"] else ""]
        w.writerow([_safe_cell(v) for v in (
            p["job"], p["name"], p.get("title") or "", p.get("company") or "", p["stage"], labels[p["group"]],
            "imported" if p["imported"] else in_zone(p["since"], tz, "%Y-%m-%d"), "" if p["imported"] else p["days"],
            *sub, nxt.get((p["candidate_id"], p["job"]), ""))])
    return buf.getvalue()


def mailto(rep: dict[str, Any], text: str) -> str | None:
    """A mailto: link with the update filled in, when the client's contacts hold an email. The recruiter's own
    mail app sends it; nothing is sent from here."""
    if not rep.get("email"):
        return None
    return (f"mailto:{quote(rep['email'], safe='@')}?subject={quote(title(rep), safe='')}"
            f"&body={quote(text, safe='')}")


# --------------------------------------------------------- updates sent
def mark_sent(store: Store, key: str, since: str | None = None, until: str | None = None,
              now: datetime | None = None) -> dict[str, Any]:
    """Keep a copy of the update and start the next one from now."""
    now = now or datetime.now(timezone.utc)
    reps = reports_for(store, key, since, until, now)
    rep = reps[0]
    text = "\n\n".join(report_text(r) for r in reps)
    c = ensure_client(store, key)
    ts = now.replace(microsecond=0).isoformat()
    with store._lock, store._conn:
        uid = store._conn.execute("INSERT INTO client_updates(client_id, sent_at, since, body) VALUES (?, ?, ?, ?)",
                                  (c["id"], ts, rep["since"], text)).lastrowid
        store._conn.execute("UPDATE clients SET last_update_sent_at = ?, updated_at = ? WHERE id = ?", (ts, ts, c["id"]))
    return {"id": uid, "sent_at": ts, "since": rep["since"], "body": text}


def updates(store: Store, key: str, limit: int = 20) -> list[dict[str, Any]]:
    row = get_row(store, actions.company_key(key))
    if not row:
        return []
    return store._rows("SELECT * FROM client_updates WHERE client_id = ? ORDER BY sent_at DESC, id DESC LIMIT ?",
                       (row["id"], limit))


def undo_sent(store: Store, key: str, update_id: int) -> dict[str, Any]:
    """Take back the newest "Mark as sent" (a click by mistake): its copy goes and the update starts where it did."""
    row = get_row(store, actions.company_key(key))
    latest = updates(store, key, 1)
    if not row or not latest or latest[0]["id"] != update_id:
        raise ValueError("Only the newest update marked as sent can be taken back.")
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM client_updates WHERE id = ?", (update_id,))
        prev = store._conn.execute("SELECT max(sent_at) FROM client_updates WHERE client_id = ?", (row["id"],)).fetchone()[0]
        store._conn.execute("UPDATE clients SET last_update_sent_at = ?, updated_at = ? WHERE id = ?",
                            (prev, now_iso(), row["id"]))
    return latest[0]


# ------------------------------------------------------------- submittals
def submittals(store: Store, key: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every move into a Submitted or Presented stage on this client's jobs, newest first (undone ones aside)."""
    c = client(store, key)
    ids = [j["id"] for j in c["jobs"]]
    titles = {j["id"]: j["title"] for j in c["jobs"]}
    out = [{"at": e["at"], "candidate_id": e["candidate_id"], "name": e["candidate_name"], "job_id": e["job_id"],
            "job": titles[e["job_id"]], "stage": e["body"], "imported": e["imported"]}
           for es in _history(store, ids).values() for e in es if is_submittal(e["body"])]
    return sorted(out, key=lambda s: s["at"], reverse=True)[:limit]


def submittal_note(store: Store, candidate_id: int, job_id: int, now: datetime | None = None) -> dict[str, Any] | None:
    """After a move: when the person's stage on this job is a Submitted or Presented one, the earlier submittals
    of them to the same client in the last 12 months. {'stage', 'client', 'earlier': [...], 'warning'} or None."""
    now = now or datetime.now(timezone.utc)
    job = store.get_job(job_id)
    app = store._row("SELECT stage FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id))
    if not app or not is_submittal(app["stage"]) or not (job.get("company") or "").strip():
        return None
    key = actions.company_key(job["company"])
    ids = [j["id"] for j in _all_jobs(store).get(key, [])]
    cutoff = (now - timedelta(days=RESUBMIT_DAYS)).replace(microsecond=0).isoformat()
    hist = _history(store, ids)
    entries = hist.get((candidate_id, job_id), [])
    latest = entries[-1]["id"] if entries else None
    earlier = [e for jid in ids for e in hist.get((candidate_id, jid), [])
               if is_submittal(e["body"]) and e["at"] >= cutoff and e["id"] != latest]
    earlier.sort(key=lambda e: e["at"])
    name = store.get_candidate(candidate_id)["name"]
    note = {"stage": app["stage"], "client": job["company"].strip(), "client_key": key,
            "earlier": [{"at": e["at"], "job_id": e["job_id"], "stage": e["body"]} for e in earlier], "warning": None}
    if earlier:
        titles = {j["id"]: j["title"] for j in _all_jobs(store)[key]}
        last = earlier[-1]
        when = in_zone(last["at"], None, "%b %-d, %Y")
        more = f" ({_plural(len(earlier), 'earlier submittal')} in the last 12 months)" if len(earlier) > 1 else ""
        note["warning"] = (f"Heads up: {name} was already submitted to {note['client']} on {when}, for "
                           f"{titles[last['job_id']]}{more}. Check before you send them again.")
    return note


__all__ = ["GROUPS", "client", "ensure_client", "find_clients", "find_manager", "is_submittal", "mailto", "managers",
           "mark_sent", "overview", "pipeline_csv", "recruiter_notes", "report", "report_text", "reports_for",
           "save_client", "stage_group", "submittal_note", "submittals", "undo_sent", "updates"]
