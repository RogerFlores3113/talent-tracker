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
from .store import NotFound, Store, _stage_key, now_iso, parse_when, quoted, stage_outcome, to_dt, zone_named
from .store import USER_ZONE_KEY, as_local, set_shown_zone

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
# Stage words that mean the client or the team is assessing the person: Tech screen, Takehome, Cofounder chat, Loop,
# Reference check. A screen counts as one only with a word like these before it (a Phone screen is the recruiter's).
SCREEN_AS_INTERVIEW = frozenset({"technical", "tech", "coding", "code", "engineering", "system", "design", "skills"})
ASSESS_WORDS = frozenset({"takehome", "assignment", "exercise", "loop", "chat", "challenge", "case", "reference",
                          "references", "debrief", "pairing"})
EARLY_WORDS = frozenset({"intro", "introduction", "coffee", "recruiter", "phone", "initial"})
# The in-play funnel steps a stage can count as (its "Counts as" choice), in funnel order.
FUNNEL_STEPS = ("sourcing", "screening", "submitted", "interview", "offer")
# Kept in the settings table (no new tables): per job, what the recruiter said each stage counts as; per client,
# when each hiring manager's update went out; per update, what its Mark as sent replaced; and the AI moves the
# recruiter has reviewed for client updates.
GROUP_KEY = "stage_groups:{}"
SENT_KEY = "client_sent:{}"
UPDATE_KEY = "client_update:{}"
REVIEWED_KEY = "ai_moves_reviewed"
REVIEWED_KEEP = 2000
MAIL_LIMIT = 1800  # longer mailto: links get cut off by some mail apps, so the page offers Copy instead
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
NOTE_KEY = "client_note:{}"  # per client (and hiring manager), the recruiter's "Note to the client this week"
NOTE_LIMIT = 1500
PEOPLE_SHOWN = 25  # per-person lines in an update, then "and 4 more"


def normalize_day(value: str | None) -> str | None:
    """'fri', 'Fri.', 'FRIDAY', 'thurs' -> 'Friday' or 'Thursday'. Blank -> None. Raises on anything else."""
    v = re.sub(r"[^a-z]", "", (value or "").lower())
    if not v:
        return None
    if v.endswith("s") and len(v) > 4 and v[:-1] in actions.WEEKDAYS:  # "Fridays"
        v = v[:-1]
    v = {"weds": "wed", "thur": "thu", "thurs": "thu"}.get(v, v)
    found = [d for d in actions.WEEKDAYS if len(v) >= 2 and d.startswith(v)]
    if len(found) != 1:
        raise ValueError("Pick a day of the week for updates, like Friday or Fri.")
    return found[0].capitalize()


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
    zone = _zone(tz_name)
    return _fmt(dt.astimezone(zone) if zone is not None else as_local(dt), fmt).strip()


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


def user_zone(store: Store) -> str | None:
    """The time zone the recruiter saved for themselves in Settings, or None (this computer's)."""
    row = store._row("SELECT value FROM settings WHERE key = ?", (USER_ZONE_KEY,))
    return (row["value"] or None) if row else None


def computer_on_utc() -> bool:
    """This computer's clock says UTC: a server or a fresh virtual machine, rarely where a recruiter sits."""
    return (datetime.now().astimezone().tzname() or "").upper() in ("UTC", "Z")


def fallback_zone(store: Store) -> str | None:
    """With no zone saved and a computer on UTC, in-house: the company's own time zone, when one is set."""
    if not actions.inhouse(store) or not computer_on_utc():
        return None
    keys = set(_all_jobs(store))
    zones = {r["time_zone"] for r in store._rows("SELECT key, time_zone FROM clients WHERE time_zone IS NOT NULL")
             if r["key"] in keys}
    return zones.pop() if len(zones) == 1 else None


def apply_zone(store: Store) -> str | None:
    """Show times in the recruiter's zone from now on (Today, timelines, the AI's replies, typed times). Returns
    the zone name used, or None for this computer's."""
    name = user_zone(store) or fallback_zone(store)
    set_shown_zone(name)
    return name


def save_user_zone(store: Store, value: str | None) -> str | None:
    """Save the recruiter's own time zone ('America/Chicago', 'CT'); blank goes back to this computer's."""
    name = normalize_tz(value)
    if name:
        actions._put_setting(store, USER_ZONE_KEY, name)
    else:
        with store._lock, store._conn:
            store._conn.execute("DELETE FROM settings WHERE key = ?", (USER_ZONE_KEY,))
    apply_zone(store)
    return name


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
    key = _stage_key(name)
    words = key.split()
    ws = set(words)
    if "offer" in words:
        return "offer"
    if is_submittal(name):
        return "submitted"
    if "client" in ws and "review" in ws:
        return "submitted"
    if INTERVIEW_WORDS & ws or words[:1] == ["hm"] or words[:2] == ["hiring", "manager"]:
        return "interview"
    if ws & {"screen", "screening", "screened"} and ws & SCREEN_AS_INTERVIEW:
        return "interview"
    if not ws & EARLY_WORDS and (ws & ASSESS_WORDS or "take home" in key):
        return "interview"
    if actions.internal_like(name):
        return "sourcing"
    return "screening"


def _get_json(store: Store, key: str) -> Any:
    row = store._row("SELECT value FROM settings WHERE key = ?", (key,))
    try:
        return json.loads(row["value"]) if row and row["value"] else None
    except ValueError:
        return None


def _put_json(store: Store, key: str, value: Any) -> None:
    actions._put_setting(store, key, json.dumps(value))


# ------------------------------------------------------ what a stage counts as
def funnel_choices(inhouse: bool) -> list[tuple[str, str]]:
    """The in-play funnel steps a stage can count as, with their labels (in-house has no Submitted step)."""
    labels = INHOUSE_LABELS if inhouse else GROUP_LABELS
    return [(g, labels[g]) for g in FUNNEL_STEPS if not (inhouse and g == "submitted")]


def group_overrides(store: Store, job_id: int) -> dict[int, str]:
    """{stage id: funnel step} the recruiter picked for this job's in-play stages (by id, so a rename keeps it)."""
    raw = _get_json(store, GROUP_KEY.format(job_id))
    if not isinstance(raw, dict):
        return {}
    return {int(k): v for k, v in raw.items() if str(k).isdigit() and v in FUNNEL_STEPS}


def stage_groups(store: Store, job_id: int, stages: list[dict[str, Any]] | None = None) -> dict[str, str]:
    """{lowercased stage name: funnel group} for every stage of a job: the recruiter's Counts as pick for an
    in-play stage, else what its name says."""
    over = group_overrides(store, job_id)
    out = {}
    for s in stages if stages is not None else store.job_stages(job_id):
        g = stage_group(s["name"], s["outcome"])
        if s["outcome"] == "open" and s["id"] in over:
            g = over[s["id"]]
        out[s["name"].lower()] = g
    return out


def set_stage_group(store: Store, job_id: int, stage: str, group: str) -> dict[str, Any]:
    """Say which funnel step an in-play stage counts as in client reports ('Tech screen' as Interviewing).
    Picking what its name already says drops the pick. Raises ValueError on an unknown stage or step."""
    if group not in FUNNEL_STEPS:
        raise ValueError("Pick a funnel step: " + ", ".join(GROUP_LABELS[g] for g in FUNNEL_STEPS) + ".")
    s = store.resolve_stage(job_id, stage)
    over = group_overrides(store, job_id)
    if stage_group(s["name"], s["outcome"]) == group:
        over.pop(s["id"], None)
    else:
        over[s["id"]] = group
    _put_json(store, GROUP_KEY.format(job_id), {str(k): v for k, v in over.items()})
    return {"stage": s["name"], "group": group}


def name_hidden(store: Store, job_id: int) -> list[str]:
    """The in-play stages a client update leaves out by default, by what they count as (never by position):
    the recruiter's own work (Sourced, Contacted), and the screening stages of a job that submits people to a
    client. A stage that counts as Submitted, an interview or an offer is always shown."""
    stages = store.job_stages(job_id)
    groups = stage_groups(store, job_id, stages)
    submits = any(groups[s["name"].lower()] == "submitted" for s in stages if s["outcome"] == "open")
    return [s["name"] for s in stages if s["outcome"] == "open" and (
        groups[s["name"].lower()] == "sourcing" or (submits and groups[s["name"].lower()] == "screening"))]


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
            v = normalize_day(v)
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
        if "time_zone" in clean:
            apply_zone(store)  # in-house on a UTC computer, the company's zone is the recruiter's
    return client(store, key)


def create_client(store: Store, name: str, **fields: Any) -> dict[str, Any]:
    """Save a client before its first job (an agency signs the client, then the reqs come). {"client", "created"}:
    created is False when a job or a saved client already has that name ('Acme' and 'Acme, Inc.' are one), and
    then nothing is changed. Raises ValueError on a blank or too long name, or a bad detail."""
    name = re.sub(r"\s+", " ", (name or "").strip())
    if not name or not actions.company_key(name):
        raise ValueError("Give the client a name.")
    if len(name) > FIELD_LIMITS["name"]:
        raise ValueError(f"That name is too long. The limit is {FIELD_LIMITS['name']} characters.")
    key = actions.company_key(name)
    try:
        return {"client": client(store, key), "created": False}
    except NotFound:
        pass
    ts = now_iso()
    with store._lock, store._conn:
        store._conn.execute("INSERT OR IGNORE INTO clients(name, key, created_at, updated_at) VALUES (?, ?, ?, ?)",
                            (name, key, ts, ts))
    try:
        c = save_client(store, key, **{k: v for k, v in fields.items() if v not in (None, "")})
    except ValueError:
        with store._lock, store._conn:  # a bad detail: the new client doesn't stay half made
            store._conn.execute("DELETE FROM clients WHERE key = ? AND last_update_sent_at IS NULL", (key,))
        raise
    return {"client": c, "created": True}


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


def _sent_state(store: Store, key: str) -> dict[str, Any]:
    """{"all": {"at", "id"}, "managers": {manager_key: {"at", "id"}}}: when updates went out, with the newest
    timeline id then, so a move in the same second as Mark as sent lands in the next update."""
    raw = _get_json(store, SENT_KEY.format(key))
    raw = raw if isinstance(raw, dict) else {}
    return {"all": raw.get("all") if isinstance(raw.get("all"), dict) else None,
            "managers": raw.get("managers") if isinstance(raw.get("managers"), dict) else {}}


def sent_mark(store: Store, key: str, manager: str | None = None, c: dict[str, Any] | None = None) -> dict[str, Any]:
    """Where the next update starts: {"at", "id"}. For the client, when it was last marked as sent; for a
    hiring manager, the later of that and their own update."""
    c = c or client(store, key)
    state = _sent_state(store, c["key"])
    whole = {"at": c.get("last_update_sent_at"), "id": None}
    if whole["at"] and state["all"] and state["all"].get("at") == whole["at"]:
        whole["id"] = state["all"].get("id")
    if manager is None:
        return whole
    own = state["managers"].get(manager)
    if isinstance(own, dict) and own.get("at") and (not whole["at"] or own["at"] > whole["at"]):
        return {"at": own["at"], "id": own.get("id")}
    return whole


def last_sent(store: Store, c: dict[str, Any]) -> str | None:
    """When any update for this client last went out (a hiring manager's own counts)."""
    times = [c.get("last_update_sent_at")] + [m.get("at") for m in _sent_state(store, c["key"])["managers"].values()
                                                 if isinstance(m, dict)]
    return max((t for t in times if t), default=None)


def managers(store: Store, key: str, now: datetime | None = None) -> list[tuple[str, str]]:
    """The hiring managers of this company's open and on-hold jobs, as (manager_key, name), with ('', '') last
    for jobs that name none, so each manager's update reads only their own jobs. In-house once any job names one;
    for a client (agency wording) once its jobs name two or more, so one email never carries another manager's
    people. A manager whose job closed since their last update stays until it goes out, so "Hired, req closed"
    reaches them. Empty otherwise."""
    own = actions.inhouse(store)
    c = client(store, key)
    jobs = [j for j in c["jobs"] if j["status"] in ("open", "on_hold")]
    default = ((now or datetime.now(timezone.utc)) - timedelta(days=DEFAULT_DAYS)).replace(microsecond=0).isoformat()
    for j in c["jobs"]:
        if j["status"] == "closed" and (j.get("hiring_manager") or "").strip():
            mark = sent_mark(store, c["key"], manager_key(j["hiring_manager"]), c)["at"] or default
            if (j.get("updated_at") or "") > mark:
                jobs.append(j)
    names: dict[str, str] = {}
    for j in jobs:
        names.setdefault(manager_key(j.get("hiring_manager")), (j.get("hiring_manager") or "").strip())
    if not any(names) or (not own and len([k for k in names if k]) < 2):
        return []
    return sorted(names.items(), key=lambda kv: (kv[0] == "", kv[0]))


def split_note(store: Store, key: str) -> str | None:
    """For the page: why a client's update comes as one per hiring manager, in agency wording. None otherwise."""
    if actions.inhouse(store):
        return None
    named = [n for k, n in managers(store, key) if k]
    if not named:
        return None
    return (f"This client's jobs name {len(named)} hiring managers ({', '.join(named)}), so there is one update for "
            "each. Each one reads only that manager's jobs and goes only to their email.")


def find_manager(store: Store, key: str, query: str | None) -> list[str]:
    """The manager keys a name means among this company's hiring managers, matched as in status_digest."""
    names = [n for _, n in managers(store, key) if n] or [
        (j.get("hiring_manager") or "").strip() for j in client(store, key)["jobs"]]
    return sorted({manager_key(n) for n in actions.match_companies(names, query)})


def _upcoming(store: Store, job_ids: set[int], app_pairs: set[tuple[int, int]], days: int,
              now: datetime) -> list[dict[str, Any]]:
    """Live interviews in the next `days` for people on these jobs. One logged with no job counts only when the
    person is in play on exactly one open job anywhere, and it's one of these: with two, nobody can tell whose
    interview it is, so it stays out of every client's update (see loose_interviews)."""
    jobs = {j["id"]: j for j in store._rows("SELECT * FROM jobs")}
    rows = actions._live_interviews(store, store.upcoming_interviews(days=days, now=now), jobs)
    loose = {r["candidate_id"] for r in rows if r.get("job_id") is None}
    where = _open_jobs_of(store, loose)
    out = []
    for r in rows:
        if r.get("job_id") in job_ids:
            out.append(r)
        elif r.get("job_id") is None and len(where.get(r["candidate_id"], ())) == 1:
            jid = next(iter(where[r["candidate_id"]]))
            if jid in job_ids and (r["candidate_id"], jid) in app_pairs:
                out.append({**r, "job_id": jid, "job_title": jobs[jid]["title"]})
    return out


def _open_jobs_of(store: Store, candidate_ids: set[int]) -> dict[int, set[int]]:
    """{candidate: the open jobs they're in play on}, across every client."""
    if not candidate_ids:
        return {}
    marks = ",".join("?" * len(candidate_ids))
    out: dict[int, set[int]] = {}
    for r in store._rows(
            f"""SELECT a.candidate_id, a.job_id FROM applications a JOIN jobs j ON j.id = a.job_id
                LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
                WHERE a.candidate_id IN ({marks}) AND j.status = 'open' AND coalesce(s.outcome, 'open') = 'open'""",
            tuple(candidate_ids)):
        out.setdefault(r["candidate_id"], set()).add(r["job_id"])
    return out


def loose_interviews(store: Store, job_ids: set[int], days: int = INTERVIEW_DAYS,
                     now: datetime | None = None) -> list[dict[str, Any]]:
    """Upcoming interviews logged with no job for people in play on these jobs that no update can carry, since
    the person is in play on more than one open job: [{"interview_id", "candidate_id", "name", "at", "href"}].
    The recruiter picks the job (/interviews/{id}/job) and the interview joins that client's update."""
    now = now or datetime.now(timezone.utc)
    jobs = {j["id"]: j for j in store._rows("SELECT * FROM jobs")}
    rows = [r for r in actions._live_interviews(store, store.upcoming_interviews(days=days, now=now), jobs)
            if r.get("job_id") is None]
    where = _open_jobs_of(store, {r["candidate_id"] for r in rows})
    return [{"interview_id": r["id"], "candidate_id": r["candidate_id"], "name": r["candidate_name"], "at": r["at"],
             "href": f"/interviews/{r['id']}/job"}
            for r in rows if len(where.get(r["candidate_id"], ())) > 1 and where[r["candidate_id"]] & job_ids]


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
    local = as_local(now or datetime.now(timezone.utc))
    want = UPDATE_DAYS.index(c["update_day"])
    if local.weekday() < want:
        return False
    day_start = (local - timedelta(days=local.weekday() - want)).replace(hour=0, minute=0, second=0, microsecond=0)
    sent = to_dt(c.get("last_update_sent_at"))
    return not sent or sent < day_start


def _day_start(day: str | None, now: datetime) -> datetime | None:
    """When this week's update day began (local), or None when it has no day or the day hasn't come yet."""
    if not day or day not in UPDATE_DAYS:
        return None
    local = as_local(now)
    want = UPDATE_DAYS.index(day)
    if local.weekday() < want:
        return None
    return (local - timedelta(days=local.weekday() - want)).replace(hour=0, minute=0, second=0, microsecond=0)


def due_parts(store: Store, key: str, now: datetime | None = None,
              c: dict[str, Any] | None = None) -> list[tuple[str | None, str | None]]:
    """The updates this client is owed this week and hasn't had: [(manager_key, manager name)] in-house once jobs
    name hiring managers (each manager's update goes out on its own), else [(None, None)]. Empty when none is due."""
    now = now or datetime.now(timezone.utc)
    c = c if c is not None and "jobs" in c else client(store, key)
    start = _day_start(c.get("update_day"), now)
    if start is None:
        return []
    hms: list[tuple[str | None, str | None]] = list(managers(store, c["key"], now)) or [(None, None)]
    out = []
    for mk, name in hms:
        sent = to_dt(sent_mark(store, c["key"], mk, c)["at"])
        if not sent or sent < start:
            out.append((mk, name))
    return out


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
                    "presented": _presented(store, open_ids),
                    # a loop of four sessions is one person interviewing, not four
                    "interviews": len({(r["candidate_id"], r.get("job_id"))
                                       for r in _upcoming(store, set(open_ids), pairs, 7, now)}),
                    "due": bool(due_parts(store, key, now, c)), "last_sent": last_sent(store, c)})
    for key, row in rows.items():  # saved before their first job
        if key not in {r["key"] for r in out}:
            out.append({**row, "key": key, "slug": slug(key), "open_jobs": 0, "jobs": 0, "in_play": 0, "presented": 0,
                        "interviews": 0, "due": False, "last_sent": last_sent(store, {**row, "key": key})})
    return sorted(out, key=lambda r: (r["open_jobs"] == 0, r["name"].lower()))


def _presented(store: Store, job_ids: list[int]) -> int:
    """People in play on these jobs in a stage the client sees (Submitted or later), each once: what the update's
    "In process now" counts, against "in play" that counts Sourced and screening too."""
    seen = set()
    for jid in job_ids:
        groups = stage_groups(store, jid)
        omit = _omit(store, jid)
        for a in store.pipeline(jid):
            g = groups.get(a["stage"].lower()) or stage_group(a["stage"])
            if g in IN_PLAY and _stage_key(a["stage"]) not in omit and g not in ("sourcing",):
                seen.add(a["candidate_id"])
    return len(seen)


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


def _close_out_entries(store: Store, job_ids: list[int]) -> set[int]:
    """Timeline ids of the moves a close-out made on these jobs (an undone one's moves are already left out)."""
    wanted, out = set(job_ids), set()
    for r in store._rows("SELECT value FROM settings WHERE key LIKE ?", (actions.CLOSE_OUT_PREFIX + "%",)):
        try:
            b = json.loads(r["value"] or "{}")
        except ValueError:
            continue
        if isinstance(b, dict) and b.get("job_id") in wanted:
            out |= {m.get("timeline_id") for m in b.get("moves") or [] if isinstance(m, dict) and m.get("timeline_id")}
    return out


def reviewed_ai(store: Store) -> set[int]:
    raw = _get_json(store, REVIEWED_KEY)
    return {int(i) for i in raw if isinstance(i, int)} if isinstance(raw, list) else set()


def mark_ai_reviewed(store: Store, action_ids: list[int]) -> int:
    """The recruiter has looked at these AI moves, so client updates stop flagging them. Only AI stage moves and
    pipeline adds count. Returns how many were new."""
    ids = {int(i) for i in action_ids if str(i).isdigit()}
    if not ids:
        return 0
    marks = ",".join("?" * len(ids))
    real = {r["id"] for r in store._rows(f"SELECT id FROM ai_actions WHERE id IN ({marks})"
                                         " AND kind IN ('stage_move', 'pipeline_add')", tuple(ids))}
    have = reviewed_ai(store)
    new = real - have
    if new:
        _put_json(store, REVIEWED_KEY, sorted(have | new)[-REVIEWED_KEEP:])
    for i in real:  # one reviewed state: Today's "Right?" and Review read the same mark
        try:
            store.confirm_ai_action(i)
        except ValueError:  # undone since: nothing to mark
            pass
    return len(new)


def _unreviewed_ai(store: Store, moves: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The moves in an update that the AI app made and the recruiter hasn't reviewed or undone, so the update
    never carries them silently: [{"action_id", "entry_id", "candidate_id", "name", "stage", "job", "at"}]."""
    by_entry = {m["entry_id"]: m for m in moves if m.get("entry_id")}
    if not by_entry:
        return []
    marks = ",".join("?" * len(by_entry))
    seen = reviewed_ai(store)
    out = []
    for r in store._rows(f"""SELECT id, target_id FROM ai_actions WHERE target_id IN ({marks})
                             AND kind IN ('stage_move', 'pipeline_add') AND undone_at IS NULL
                             AND confirmed_at IS NULL ORDER BY id""",
                         tuple(by_entry)):
        if r["id"] in seen:
            continue
        m = by_entry[r["target_id"]]
        out.append({"action_id": r["id"], "entry_id": r["target_id"], "candidate_id": m["candidate_id"],
                    "name": m["name"], "stage": m["stage"], "job": m["job"], "job_id": m["job_id"], "at": m["at"]})
    return out


HOLD_GROUPS = ("hired", "out")  # an unreviewed AI move into one of these waits for the recruiter


def _held(store: Store, hist: dict[tuple[int, int], list[dict[str, Any]]], current: dict[tuple[int, int], str],
          group: Any, in_range: Any) -> dict[tuple[int, int], dict[str, Any]]:
    """{(candidate, job): their latest stage entry} where it's a move into hired or out that the AI app made in
    this period and the recruiter hasn't reviewed or undone. The update reads as if it hadn't happened yet, so
    nobody hears of a hire or a no before the recruiter has checked it."""
    last: dict[int, tuple[tuple[int, int], dict[str, Any]]] = {}
    for k, es in hist.items():
        e = es[-1]
        if (e["imported"] or not in_range(e["at"], e["id"]) or group(k[1], e["body"]) not in HOLD_GROUPS
                or (current.get(k) or "").lower() != (e["body"] or "").lower()):
            continue
        last[e["id"]] = (k, e)
    if not last:
        return {}
    seen = reviewed_ai(store)
    marks = ",".join("?" * len(last))
    out = {}
    for r in store._rows(f"""SELECT id, target_id FROM ai_actions WHERE target_id IN ({marks})
                             AND kind IN ('stage_move', 'pipeline_add') AND undone_at IS NULL
                             AND confirmed_at IS NULL ORDER BY id""",
                         tuple(last)):
        if r["id"] not in seen:
            k, e = last[r["target_id"]]
            out[k] = {**e, "action_id": r["id"]}
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
    mark = sent_mark(store, c["key"], manager, c)  # a hiring manager's update starts from their own last one
    c = {**c, "last_update_sent_at": mark["at"]}
    start, end, why = _range(store, c, since, until, now)
    jobs = c["jobs"] if manager is None else [j for j in c["jobs"] if manager_key(j.get("hiring_manager")) == manager]
    ids = [j["id"] for j in jobs]
    by_id = {j["id"]: j for j in jobs}
    groups_of = {j["id"]: stage_groups(store, j["id"]) for j in jobs}
    omit = {j["id"]: _omit(store, j["id"]) for j in jobs}
    live = [j for j in jobs if j["status"] in ("open", "on_hold")]
    booked = {j["id"]: actions.booked_hidden(store, j["id"], omit[j["id"]], None, now)
              for j in jobs if j["status"] == "open"}

    def group(job_id: int, stage: str) -> str:
        g = groups_of[job_id].get((stage or "").lower()) or stage_group(stage)
        return "screening" if own and g == "submitted" else g

    def hidden(job_id: int, stage: str) -> bool:
        return _stage_key(stage) in omit[job_id]

    def in_range(at: str, entry_id: int | None = None) -> bool:
        """An update marked as sent covers what happened up to it: in its own second, what came after it (by
        timeline id) is the next update's."""
        if why == "last_update":
            after = start < at or (at == start and mark["id"] is not None and entry_id is not None
                                   and entry_id > mark["id"])
        else:
            after = start <= at
        return after and at <= end

    hist = _history(store, ids)
    current = {(p["candidate_id"], p["job_id"]): p["stage"] for j in jobs for p in store.pipeline(j["id"])}
    held = _held(store, hist, current, group, in_range)
    held_ids = {e["id"] for e in held.values()}
    for k, e in held.items():  # until it's reviewed, they stand where they were before the AI's move
        prev = hist[k][-2] if len(hist[k]) > 1 else None
        if prev:
            current[k] = prev["body"]
        else:
            current.pop(k, None)
    # Where people stand now, on open and on-hold jobs.
    standing: dict[str, set[int]] = {g: set() for g, _ in GROUPS}
    not_shown: dict[str, int] = {}
    booked_names: dict[int, str] = {}
    people: list[dict[str, Any]] = []
    for j in live:
        for a in store.pipeline(j["id"]):
            if (a["candidate_id"], j["id"]) in held:
                if (a["candidate_id"], j["id"]) not in current:
                    continue
                prev = hist[(a["candidate_id"], j["id"])][-2]
                a = {**a, "stage": prev["body"], "stage_changed_at": prev["at"]}
            if hidden(j["id"], a["stage"]):
                if a["candidate_id"] in booked.get(j["id"], {}):  # their interview is listed; told to the recruiter apart
                    booked_names[a["candidate_id"]] = a["candidate_name"]
                else:
                    not_shown[a["stage"]] = not_shown.get(a["stage"], 0) + 1
                continue
            g = group(j["id"], a["stage"])
            if j["status"] == "open":
                standing[g].add(a["candidate_id"])
            entries = [e for e in hist.get((a["candidate_id"], j["id"]), []) if e["id"] not in held_ids]
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
    # Funnel: who entered each step (each person once, on any of these jobs; a step nobody entered stays 0
    # however far they got), and who moved.
    entered: dict[str, set[int]] = {g: set() for g, _ in GROUPS}
    period: dict[str, set[int]] = {g: set() for g, _ in GROUPS}
    moves: list[dict[str, Any]] = []
    spans: dict[str, list[float]] = {g: [] for g in IN_PLAY}
    closing = _close_out_entries(store, ids)
    for (cid, jid), entries in hist.items():
        entries = [e for e in entries if e["id"] not in held_ids]
        stages = [e["body"] for e in entries]
        if (cid, jid) in current:
            stages.append(current[(cid, jid)])
        for s in stages:
            if not hidden(jid, s):
                entered[group(jid, s)].add(cid)
        for i, e in enumerate(entries):
            g = group(jid, e["body"])
            nxt = entries[i + 1]["at"] if i + 1 < len(entries) else None
            if nxt is None and current.get((cid, jid), "").lower() == (e["body"] or "").lower() and g in IN_PLAY:
                nxt = now.isoformat()
            if nxt and g in IN_PLAY and not e["imported"] and not hidden(jid, e["body"]):
                spans[g].append((to_dt(nxt) - to_dt(e["at"])).total_seconds() / 86400)
            if not in_range(e["at"], e["id"]) or e["imported"] or hidden(jid, e["body"]):
                continue
            if e["id"] not in closing:  # a close-out is "position filled", not people the client turned down
                period[g].add(cid)
            moves.append({"at": e["at"], "candidate_id": cid, "name": e["candidate_name"], "job_id": jid,
                          "job": by_id[jid]["title"], "stage": e["body"], "group": g, "entry_id": e["id"],
                          "from": entries[i - 1]["body"] if i else None, "date_only": _date_only(e)})
    reached = {g: len(entered[g]) for g, _ in FUNNEL}
    # Back and forth to the same stage on one job is one move: the latest.
    seen_moves: set[tuple[int, int, str]] = set()
    for m in reversed(moves):
        mk = (m["candidate_id"], m["job_id"], _stage_key(m["stage"]))
        m["repeat"] = mk in seen_moves
        seen_moves.add(mk)
    moves = [{k: v for k, v in m.items() if k != "repeat"} for m in moves if not m["repeat"]]
    imported = sum(1 for es in hist.values() for e in es if e["imported"] and in_range(e["at"], e["id"]))
    # A close-out (everyone left moved out when the job filled) is one line, not one move per person.
    closed_out: dict[int, dict[str, Any]] = {}
    for m in moves:
        if m["entry_id"] in closing:
            co = closed_out.setdefault(m["job_id"], {"job_id": m["job_id"], "job": m["job"], "count": 0, "at": m["at"]})
            co["count"] += 1
            co["at"] = max(co["at"], m["at"])
    moves = [m for m in moves if m["entry_id"] not in closing]
    ai_moves = _unreviewed_ai(store, moves)
    ai_ids = {a["entry_id"] for a in ai_moves}
    held_moves = [{"action_id": e["action_id"], "entry_id": e["id"], "candidate_id": cid, "name": e["candidate_name"],
                   "stage": e["body"], "job": by_id[jid]["title"], "job_id": jid, "at": e["at"], "held": True}
                  for (cid, jid), e in held.items()]
    ai_moves = sorted(ai_moves + held_moves, key=lambda m: m["action_id"])
    for m in moves:
        m["by_ai"] = m["entry_id"] in ai_ids
    medians = {g: round(statistics.median(v), 1) for g, v in spans.items() if v}
    open_ids = {j["id"] for j in jobs if j["status"] == "open"}
    # Everyone still in the running on an open job, whatever their stage: an interview booked while they sit in
    # Sourced is still an interview with the client.
    pairs = _in_play_pairs(store, sorted(open_ids))
    rows = [r for r in _upcoming(store, open_ids, pairs, INTERVIEW_DAYS, now) if (r["candidate_id"], r["job_id"]) in pairs]
    loop_of = {r["id"]: (i, len(g)) for i, g in enumerate(actions.group_loops(rows)) for r in g}
    upcoming = [{"interview_id": r["id"], "at": r["at"], "at_client": in_zone(r["at"], c.get("time_zone")),
                 "candidate_id": r["candidate_id"], "name": r["candidate_name"], "job": r.get("job_title"),
                 "summary": r.get("body"), "label": actions.interview_label(r),
                 "loop": loop_of[r["id"]][0], "loop_size": loop_of[r["id"]][1]} for r in rows]
    shown_people = {(p["candidate_id"], p["job_id"]) for p in people} | {
        (cid, jid) for jid, b in booked.items() for cid in b}
    results = [{"candidate_id": r["candidate_id"], "name": r["name"], "job_id": r["job_id"], "job": r["job_title"],
                "verdict": r["verdict"], "line": r["line"], "at": r["outcome_at"]}
               for jid in ids for r in actions.interview_results(store, jid, now, max(1, (now - to_dt(start)).days + 1))
               if (r["candidate_id"], jid) in shown_people and in_range(r["outcome_at"])]
    still_hidden = [{"candidate_id": cid, "name": booked_names.get(cid, ""), "stage": stage, "job": by_id[jid]["title"]}
                    for jid, b in booked.items() for cid, stage in b.items()]
    open_jobs = [j for j in jobs if j["status"] == "open"]
    openings = sum(j.get("openings") or 1 for j in open_jobs)
    held_hires = [k[1] for k, e in held.items() if group(k[1], e["body"]) == "hired"]
    hires = {j["id"]: sum(len(g["candidates"]) for g in store.pipeline_by_stage(j["id"]) if g["outcome"] == "hired")
             - held_hires.count(j["id"]) for j in open_jobs}
    filled = sum(min(hires[j["id"]], j.get("openings") or 1) for j in open_jobs)
    # An import guesses openings from its hires until the recruiter says: no "22 of 22 filled" from a guess.
    unset = _openings_unset(store, [j["id"] for j in open_jobs])
    overfilled = [{"job_id": j["id"], "title": j["title"], "hired": hires[j["id"]], "openings": j.get("openings") or 1,
                   "over": hires[j["id"]] - (j.get("openings") or 1)}
                  for j in open_jobs if hires[j["id"]] > (j.get("openings") or 1) and j["id"] not in unset]
    words = {actions.hire_word(store, j["id"]) for j in (open_jobs or jobs)}
    # Jobs that closed in the period, with who was hired, so the update says "Hired: Aisha Bello. Req closed."
    # A close whose hire waits for the recruiter's review waits too: no "Req closed, 0 hired" before they've checked.
    held_on = {k[1] for k, e in held.items() if group(k[1], e["body"]) == "hired"}
    closed_jobs = [{"job_id": j["id"], "title": j["title"],
                    "hired": [m["name"] for m in moves if m["job_id"] == j["id"] and m["group"] == "hired"]}
                   for j in jobs if j["status"] == "closed" and in_range(j.get("updated_at") or "")
                   and j["id"] not in held_on]
    held_closed = [{"job_id": j["id"], "title": j["title"]} for j in jobs if j["status"] == "closed"
                   and in_range(j.get("updated_at") or "") and j["id"] in held_on]
    label = None if manager is None else next((j["hiring_manager"].strip() for j in jobs
                                               if (j.get("hiring_manager") or "").strip()), "")
    hm_email = next((j["hiring_manager_email"].strip() for j in jobs if (j.get("hiring_manager_email") or "").strip()),
                    None) if manager else None
    if manager and not own:  # a client's hiring manager: their own email, or a contact line naming them, never another's
        hm_email = hm_email or manager_contact(c.get("contacts"), label) or ""
    loose = loose_interviews(store, {j["id"] for j in jobs if j["status"] == "open"}, now=now)
    labels = _job_labels(store, jobs, own)
    return {"client": {k: c.get(k) for k in ("id", "name", "key", "slug", "contacts", "time_zone", "update_day",
                                             "last_update_sent_at")},
            "inhouse": own, "manager": label, "manager_key": manager, "booked_hidden": still_hidden,
            "overfilled": overfilled, "ai_moves": ai_moves, "held": held_moves, "closed_out": list(closed_out.values()),
            "openings_unset": [{"job_id": j["id"], "title": j["title"]} for j in open_jobs if j["id"] in unset],
            "results": results, "labels": labels,
            "closed_jobs": closed_jobs, "held_closed": held_closed, "loose_interviews": loose,
            "people_lines": person_lines(people, upcoming, labels, c.get("time_zone"), own),
            "client_note": client_note(store, c["key"], manager),
            "hired": sum(hires.values()), "hire_word": "placed" if words == {"placed"} else "hired",
            "since": start, "until": end, "range_from": why, "now": now.isoformat(),
            "jobs": [{"id": j["id"], "title": j["title"], "status": j["status"], "location": j.get("location"),
                      "openings": j.get("openings") or 1} for j in jobs],
            "in_play": in_play, "stand": now_counts, "not_shown": not_shown,
            "reached": reached, "period": {g: len(v) for g, v in period.items()}, "moves": moves,
            "imported_moves": imported, "median_days": medians, "upcoming": upcoming, "people": people,
            "openings": openings, "filled": filled,
            # with no contact email saved yet, a client update goes to a job's hiring manager email
            "email": None if hm_email == "" else hm_email or _contact_email(c.get("contacts")) or (
                None if manager is not None else next(
                (j["hiring_manager_email"].strip() for j in jobs if (j.get("hiring_manager_email") or "").strip()),
                None))}


def manager_contact(contacts: str | None, name: str | None) -> str | None:
    """The email on a client contact line that names this hiring manager ('Dana Reyes <dana@hfl.com>'), or None."""
    words = [w for w in re.split(r"\W+", (name or "").lower()) if len(w) > 1]
    if not words:
        return None
    for line in (contacts or "").splitlines():
        low = line.lower()
        m = _EMAIL.search(line)
        if m and all(re.search(rf"\b{re.escape(w)}\b", low.replace(m.group(0).lower(), " ")) for w in words):
            return m.group(0)
    return None


def _note_key(key: str, manager: str | None) -> str:
    return NOTE_KEY.format(f"{key}|{manager or ''}" if manager is not None else key)


def client_note(store: Store, key: str, manager: str | None = None) -> str:
    """The recruiter's note to the client for the next update (it goes at the top of the text), or ''."""
    raw = _get_json(store, _note_key(key, manager))
    return str(raw.get("text") or "") if isinstance(raw, dict) else ""


def save_client_note(store: Store, key: str, text: str | None, manager: str | None = None) -> str:
    """Save (or with blank text, clear) the note that opens the next update. Marking it as sent clears it."""
    text = re.sub(r"[ \t]+\n", "\n", (text or "").strip())
    if len(text) > NOTE_LIMIT:
        raise ValueError(f"That note is too long ({len(text):,} characters). The limit is {NOTE_LIMIT:,}.")
    ensure_client(store, key)
    if text:
        _put_json(store, _note_key(key, manager), {"text": text, "at": now_iso()})
    else:
        with store._lock, store._conn:
            store._conn.execute("DELETE FROM settings WHERE key = ?", (_note_key(key, manager),))
    return text


def _clear_notes(store: Store, key: str, manager: str | None) -> None:
    """After Mark as sent: the notes that went out with it. Marking the whole client clears every manager's too."""
    with store._lock, store._conn:
        if manager is None:
            store._conn.execute("DELETE FROM settings WHERE key = ? OR key LIKE ?",
                                (_note_key(key, None), NOTE_KEY.format(key + "|") + "%"))
        else:
            store._conn.execute("DELETE FROM settings WHERE key = ?", (_note_key(key, manager),))


def person_lines(people: list[dict[str, Any]], upcoming: list[dict[str, Any]], labels: dict[str, str],
                 tz: str | None, own: bool) -> list[str]:
    """One line per person in play, furthest along first: 'Lena Hoffmann (Staff Engineer at Fetch) for Robotics
    Lead: Submitted to client Nov 5, awaiting your feedback'. A booked interview is named on the line."""
    nxt: dict[tuple[int, str], str] = {}
    for u in upcoming:
        nxt.setdefault((u["candidate_id"], u["job"]), in_zone(u["at"], tz, "%a %b %-d"))
    order = {g: i for i, g in enumerate(IN_PLAY)}
    rows = sorted((p for p in people if p["group"] in IN_PLAY),
                  key=lambda p: (-order[p["group"]], p["name"].lower(), p["job"].lower()))
    out = []
    for p in rows:
        role = " at ".join(x for x in (p.get("title"), p.get("company")) if x) if p.get("title") else ""
        who = p["name"] + (f" ({role})" if role else "")
        when = "" if p.get("imported") else " " + in_zone(p["since"], tz, "%b %-d")
        bits = [f"{p['stage']}{when}" if when.strip() else p["stage"]]
        iv = nxt.get((p["candidate_id"], p["job"]))
        if iv:
            bits.append(f"interview {iv}")
        elif p["group"] == "submitted" and not own:
            bits.append("awaiting your feedback")
        out.append(f"\u2022 {who} for {p['job']}: {', '.join(bits)}")
    return out


def _openings_unset(store: Store, job_ids: list[int]) -> set[int]:
    """The jobs an import made whose openings the recruiter hasn't answered yet (the import's guess stands)."""
    if not job_ids:
        return set()
    from .importers import openings_to_ask

    return set(openings_to_ask(store)) & set(job_ids)


def _job_labels(store: Store, jobs: list[dict[str, Any]], own: bool) -> dict[str, str]:
    """The funnel's words for this update. For a client, each step says the jobs' own stage name when they all
    have just one for it (Placed, Client interview), else the usual word (Hired, Interviewing)."""
    labels = dict(INHOUSE_LABELS if own else GROUP_LABELS)
    if own:
        return labels
    names: dict[str, set[str]] = {}
    spell: dict[str, str] = {}
    for j in jobs:
        for name, g in stage_groups(store, j["id"]).items():
            if g in ("submitted", "interview", "offer", "hired"):
                names.setdefault(g, set()).add(name)
        for st in store.job_stages(j["id"]):
            spell.setdefault(st["name"].lower(), st["name"])
    for g, ns in names.items():
        if len(ns) == 1:
            labels[g] = spell.get(next(iter(ns)), labels[g])
    return labels


def _date_only(entry: dict[str, Any]) -> bool:
    """A stage date from a file that gave a day and no time (stored at the day's midnight or noon here): it is
    shown as that day in every time zone, so a Nov 3 in the ATS never reads as Nov 2 for a client in Chicago."""
    d = _data(entry.get("data"))
    if not (d.get("by") in ("import", "sync") or d.get("imported") is True or "import_run" in d
            or entry.get("source") in ("CSV import", "ATS sync")):
        return False
    if d.get("date_only") is True:
        return True
    dt = to_dt(entry.get("at"))
    t = as_local(dt).time() if dt else None
    return bool(t) and t.second == 0 and t.minute == 0 and t.hour in (0, 12)


def reports_for(store: Store, key: str, since: str | datetime | None = None, until: str | datetime | None = None,
                now: datetime | None = None) -> list[dict[str, Any]]:
    """The updates to send: in-house once jobs name hiring managers, one per manager with only their jobs (so a
    manager never reads another's pipeline); otherwise the one client report."""
    now = now or datetime.now(timezone.utc)
    hms = managers(store, key, now)
    if not hms:
        return [report(store, key, since, until, now)]
    return [report(store, key, since, until, now, manager=k) for k, _ in hms]


def _day(iso: str, tz: str | None = None) -> str:
    return in_zone(iso, tz, "%a %b %-d, %Y")


def span_text(since: str, until: str, tz: str | None = None) -> str:
    """'Thu Nov 5, 2026', or 'Mon Nov 2 to Thu Nov 5, 2026', or with two years both in full."""
    a, b = _day(since, tz), _day(until, tz)
    if a == b:
        return a
    if a[-4:] == b[-4:]:
        a = a[:-6]
    return f"{a} to {b}"


def title(rep: dict[str, Any]) -> str:
    """'Acme: hiring update, Mon Nov 2 to Thu Nov 5, 2026'. In-house it goes to the hiring manager themselves, so
    it never names them: 'Hiring update, Lumen Health, ...'."""
    span = span_text(rep["since"], rep["until"])
    if rep.get("inhouse"):
        return f"Hiring update, {rep['client']['name']}, {span}"
    who = ""
    if rep.get("manager") is not None:
        who = f" for {rep['manager']}" if rep["manager"] else " for jobs with no hiring manager"
    return f"{rep['client']['name']}: hiring update{who}, {span}"


def seats_text(rep: dict[str, Any]) -> str:
    """'1 of 2 openings filled', or with more hires than openings '2 hired, 1 opening (over by 1)'."""
    over = rep.get("overfilled") or []
    if rep.get("openings_unset"):  # openings are still an import's guess: say the hires, not "22 of 22 filled"
        return f"{rep['hired']} {rep.get('hire_word', 'hired')} so far" if rep.get("hired") else (
            f"None {rep.get('hire_word', 'hired')} yet")
    if not over:
        return actions.filled_text(rep["filled"], rep["openings"])
    seats = _plural(rep["openings"], "opening")
    if len(rep["jobs"]) == 1 or len([j for j in rep["jobs"] if j["status"] == "open"]) == 1:
        return f"{rep['hired']} {rep['hire_word']}, {seats} (over by {over[0]['over']})"
    return (f"{rep['hired']} {rep['hire_word']}, {seats} ("
            + ", ".join(f"{o['title']} over by {o['over']}" for o in over) + ")")


def recruiter_notes(rep: dict[str, Any]) -> list[str]:
    """What the recruiter should know and the client shouldn't read: moves the AI app made that they haven't
    reviewed, who the update leaves out, people listed though their stage is left out, and jobs with more hires
    than openings. Never part of the paste text."""
    notes = []
    if rep.get("held"):
        notes.append(held_text(rep["held"]))
    unheld = [m for m in rep.get("ai_moves") or [] if not m.get("held")]
    if unheld:
        notes.append(ai_moves_text(unheld))
    if rep["not_shown"]:
        notes.append("Not shown: " + ", ".join(f"{s} ({n})" for s, n in rep["not_shown"].items()))
    if rep.get("booked_hidden"):
        notes.append("In Upcoming interviews though their stage is left out, since an interview is booked: " + ", ".join(
            f"{b['name']} ({b['stage']}, {b['job']})" for b in rep["booked_hidden"]))
    for o in rep.get("overfilled") or []:
        notes.append(f"{o['title']} has {o['hired']} {rep.get('hire_word', 'hired')} for "
                     f"{_plural(o['openings'], 'opening')} (over by {o['over']}). Check those hires before you send this")
    for j in rep.get("held_closed") or []:
        notes.append(f"{j['title']} is closed, but the hire it waits on isn't reviewed yet, so the update still "
                     "lists it as open until you review that hire")
    loose = rep.get("loose_interviews") or []
    if loose:
        notes.append("Left out, since no job is set and they're in play on more than one: " + ", ".join(
            f"{x['name']}'s interview {in_zone(x['at'], rep['client'].get('time_zone'), '%a %b %-d')}" for x in loose[:5])
            + (f" and {len(loose) - 5} more" if len(loose) > 5 else "") + ". Pick the job on Interviews to add it")
    if rep.get("openings_unset"):
        notes.append("Openings not set yet for " + ", ".join(j["title"] for j in rep["openings_unset"])
                     + ", so the update gives the hires without \"of N openings filled\". Set them on the job's page")
    return notes


def held_text(held: list[dict[str, Any]], shown: int = 5) -> str:
    """'(1 held back, review first) "Maya Chen" to Placed (Staff ML): your AI app made this move and you haven't
    reviewed it, so the update text leaves it out until you do'."""
    listed = ", ".join(f"{quoted(m['name'])} to {m['stage']} ({m['job']})" for m in held[:shown])
    more = f" and {len(held) - shown} more" if len(held) > shown else ""
    it = "it" if len(held) == 1 else "them"
    return (f"({len(held)} held back, review first) {listed}{more}: your AI app made "
            f"{'this move' if len(held) == 1 else 'these moves'} and you haven't reviewed {it}, so the update text "
            f"leaves {it} out until you do")


def ai_moves_text(moves: list[dict[str, Any]], shown: int = 5) -> str:
    """'Your AI app made 2 moves in this update that you haven't reviewed: "Maya Chen" to Placed (Staff ML)...'"""
    listed = ", ".join(f"{quoted(m['name'])} to {m['stage']} ({m['job']})" for m in moves[:shown])
    more = f" and {len(moves) - shown} more" if len(moves) > shown else ""
    return (f"Your AI app made {_plural(len(moves), 'move')} in this update that you haven't reviewed: {listed}{more}. "
            "Check them on Changes, or mark them reviewed, before you send this")


def report_text(rep: dict[str, Any], trim: int = 0) -> str:
    """The paste-ready update: no ids, times in the client's time zone, internal stages left out. AI moves into
    hired or out that the recruiter hasn't reviewed are held back (see _held). trim (1 to 3) shortens it to fit
    an email link: 1 drops the all-time funnel and the median days, 2 lists fewer moves, 3 fewer still."""
    tz = rep["client"].get("time_zone")
    own = rep.get("inhouse")
    labels = rep.get("labels") or (INHOUSE_LABELS if own else GROUP_LABELS)
    shown_moves = (MOVES_SHOWN, MOVES_SHOWN, 10, 5)[min(trim, 3)]
    lines = [title(rep)]
    if rep.get("client_note"):
        lines += ["", rep["client_note"], ""]
    if rep["range_from"] == "last_update":
        lines.append("Changes since the last update.")
    for cj in rep.get("closed_jobs") or []:  # the news a closing manager most needs: who was hired, and it's done
        who = f"{rep.get('hire_word', 'hired').capitalize()}: {', '.join(cj['hired'])}. " if cj["hired"] else ""
        lines.append(f"{who}{'Req' if own else 'Job'} closed: {cj['title']}.")
    live = [j for j in rep["jobs"] if j["status"] == "open"]
    held = [j for j in rep["jobs"] if j["status"] == "on_hold"]
    waiting = [j["title"] for j in rep.get("held_closed") or []]  # closed, but its hire waits for review
    if live:
        lines.append(f"{_plural(len(live) + len(waiting), 'open job')}: " + ", ".join([j["title"] for j in live] + waiting)
                     + f". {seats_text(rep)}.")
    elif waiting:
        lines.append(f"{_plural(len(waiting), 'open job')}: {', '.join(waiting)}.")
    else:
        lines.append("No open jobs right now.")
    if held:
        lines.append("On hold: " + ", ".join(j["title"] for j in held) + ".")
    lines += ["", f"In process now: {rep['in_play']}"]
    lines += [f"- {labels[g]}: {rep['stand'][g]}" for g in IN_PLAY if rep["stand"][g]]
    who = rep.get("people_lines") or []
    if who:
        cap = (PEOPLE_SHOWN, PEOPLE_SHOWN, 10, 5)[min(trim, 3)]
        lines += ["", "Where each person stands:", *who[:cap]]
        if len(who) > cap:
            lines.append(f"\u2022 and {len(who) - cap} more")
    p = rep["period"]
    lines += ["", "This period:"]
    moved = ("interview", "offer", "hired", "out") if own else ("submitted", "interview", "offer", "hired", "out")
    lines.append("- Moved into " + ", ".join(f"{labels[g]} {p[g]}" for g in moved))
    r = rep["reached"]
    if not trim:
        lines.append("- All time on these jobs: " + ", ".join(
            f"{labels[g] if labels[g] != GROUP_LABELS[g] else label} {r[g]}" for g, label in funnel(rep)))
    listed = [(m["at"], f"{m['name']} to {m['stage']} ({m['job']})", m.get("date_only")) for m in rep["moves"]]
    listed += [(co["at"], f"{_plural(co['count'], 'person', 'people')} closed, position filled ({co['job']})", False)
               for co in rep.get("closed_out") or []]
    listed.sort(key=lambda x: x[0])
    if listed:
        lines += ["", "Moves this period:"]
        for at, what, plain in listed[:shown_moves]:
            lines.append(f"- {_plain_day(at) if plain else _day(at, tz)}: {what}")
        if len(listed) > shown_moves:
            lines.append(f"- and {len(listed) - shown_moves} more")
    else:
        lines += ["", "Moves this period: none."]
    if rep.get("results"):
        lines += ["", "Interview results:"]
        many = len({x["job_id"] for x in rep["results"]}) > 1
        lines += [x["line"] + (f", {x['job']}" if many else "") for x in rep["results"]]
    if rep["median_days"] and not trim:
        lines += ["", "Median days in stage: " + ", ".join(
            f"{labels[g]} {_num(v)}" for g, v in rep["median_days"].items())]
    if rep["upcoming"]:
        lines += ["", "Upcoming interviews:", *upcoming_lines(rep)]
    return "\n".join(lines)


def _plain_day(iso: str) -> str:
    """A date-only stage date as its own day, the same in every time zone (see _date_only)."""
    return _fmt(as_local(to_dt(iso)), "%a %b %-d, %Y")


def upcoming_lines(rep: dict[str, Any]) -> list[str]:
    """One line per interview, and one per loop: '- Jordan Ellis (Backend): onsite, 4 sessions, Tue Nov 10 and
    Thu Nov 12'."""
    tz = rep["client"].get("time_zone")
    out, done = [], set()
    for u in rep["upcoming"]:
        if u.get("loop_size", 1) < 2:
            out.append(f"- {u['at_client']}: {u['name']} ({u['job']})" + (f", {u['label']}" if u.get("label") else ""))
            continue
        if u["loop"] in done:
            continue
        done.add(u["loop"])
        group = [x for x in rep["upcoming"] if x.get("loop") == u["loop"]]
        days = list(dict.fromkeys(in_zone(x["at"], tz, "%a %b %-d") for x in group))
        heads = {((x.get("summary") or "").strip().splitlines() or [""])[0].split(":")[0].strip().lower() for x in group}
        kind = next(iter(heads)) if len(heads) == 1 and 0 < len(next(iter(heads))) <= 30 else "interview loop"
        when = days[0] if len(days) == 1 else ", ".join(days[:-1]) + " and " + days[-1]
        out.append(f"- {u['name']} ({u['job']}): {kind}, {len(group)} sessions, {when}")
    return out


def funnel(rep: dict[str, Any]) -> tuple[tuple[str, str], ...]:
    """The all-time funnel steps: in-house there's no Submitted or presented step."""
    return FUNNEL[1:] if rep.get("inhouse") else FUNNEL


def _num(v: float) -> str:
    """Days as the update says them: '3', '2.5' (never '3.0')."""
    return str(int(v)) if float(v).is_integer() else str(v)


def pipeline_csv(rep: dict[str, Any], with_out: bool = False) -> str:
    """The per-candidate pipeline behind the report, for a spreadsheet. Left-out stages aren't in it, nor (unless
    with_out) people who are out, like Not a Fit or Withdrew."""
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
    people = [p for p in rep["people"] if with_out or p["group"] != "out"]
    for p in sorted(people, key=lambda p: (p["job"].lower(), [g for g, _ in GROUPS].index(p["group"]),
                                           p["name"].lower())):
        sub = [] if own else [in_zone(p["submitted_at"], tz, "%Y-%m-%d") if p["submitted_at"] else ""]
        w.writerow([_safe_cell(v) for v in (
            p["job"], p["name"], p.get("title") or "", p.get("company") or "", p["stage"], labels[p["group"]],
            "imported" if p["imported"] else in_zone(p["since"], tz, "%Y-%m-%d"), "" if p["imported"] else p["days"],
            *sub, nxt.get((p["candidate_id"], p["job"]), ""))])
    return buf.getvalue()


def recipients(rep: dict[str, Any]) -> tuple[str | None, list[str]]:
    """(to, cc) for an update: a hiring manager's own update goes to them alone; a client update goes to the
    first contact email, with the client's other contact emails on Cc."""
    if rep.get("manager") is not None:
        return rep.get("email"), []
    found = list(dict.fromkeys(_EMAIL.findall(rep["client"].get("contacts") or "")))
    to = rep.get("email") or (found[0] if found else None)
    return to, [e for e in found if to and e.lower() != to.lower()]


def email_link(rep: dict[str, Any], text: str) -> dict[str, Any] | None:
    """{"href", "to", "cc", "long"} for an Email button, or None with no email to send to. A long update would get
    cut off in a mailto: link, so its link opens the email without the text (long=True) and the page says to
    paste the copied update. The recruiter's own mail app sends it; nothing is sent from here."""
    to, cc = recipients(rep)
    if not to:
        return None
    head = f"mailto:{quote(to, safe='@')}?subject={quote(title(rep), safe='')}"
    if cc:
        head += "&cc=" + quote(",".join(cc), safe="@,")
    full = head + f"&body={quote(text, safe='')}"
    trimmed = False
    if len(full) > MAIL_LIMIT and "moves" in rep and text == report_text(rep):
        for level in (1, 2, 3):  # a shorter body (no all-time funnel or medians, fewer moves) before giving up
            short = head + f"&body={quote(report_text(rep, trim=level), safe='')}"
            if len(short) <= MAIL_LIMIT:
                full, trimmed = short, True
                break
    long = len(full) > MAIL_LIMIT
    return {"href": head if long else full, "to": to, "cc": cc, "long": long, "trimmed": trimmed}


def mailto(rep: dict[str, Any], text: str) -> str | None:
    """A mailto: link with the update filled in (see email_link), when there's an email to send it to."""
    link = email_link(rep, text)
    return link["href"] if link else None


# --------------------------------------------------------- updates sent
def mark_sent(store: Store, key: str, since: str | None = None, until: str | None = None,
              now: datetime | None = None, manager: str | None = None) -> dict[str, Any]:
    """Keep a copy of the update and start the next one from now. manager (a manager_key) marks only that hiring
    manager's update, in-house, so each manager's next update starts from their own."""
    now = now or datetime.now(timezone.utc)
    key = actions.company_key(key)
    if manager is not None and manager not in {k for k, _ in managers(store, key, now)}:
        raise ValueError("That hiring manager has no update here. Reload the page.")
    again = None if since or until else repeat_of(store, key, manager, now)
    if again:  # marked a moment ago with nothing new since (a retry, a double click): the same send
        return again
    watermark = store._row("SELECT coalesce(max(id), 0) AS n FROM timeline")["n"]
    reps = reports_for(store, key, since, until, now) if manager is None else [
        report(store, key, since, until, now, manager=manager)]
    rep = reps[0]
    text = "\n\n".join((f"For {r['manager'] or 'jobs with no hiring manager'}:\n" if len(reps) > 1 else "")
                       + report_text(r) for r in reps)
    c = ensure_client(store, key)
    raw = store._row("SELECT value FROM settings WHERE key = ?", (SENT_KEY.format(c["key"]),))
    state = _sent_state(store, c["key"])
    ts = now.replace(microsecond=0).isoformat()
    if manager is None:
        state["all"] = {"at": ts, "id": watermark}
    else:
        state["managers"][manager] = {"at": ts, "id": watermark}
    put = "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    with store._lock, store._conn:
        uid = store._conn.execute("INSERT INTO client_updates(client_id, sent_at, since, body) VALUES (?, ?, ?, ?)",
                                  (c["id"], ts, rep["since"], text)).lastrowid
        if manager is None:
            store._conn.execute("UPDATE clients SET last_update_sent_at = ?, updated_at = ? WHERE id = ?",
                                (ts, ts, c["id"]))
        store._conn.execute(put, (SENT_KEY.format(c["key"]), json.dumps(state)))
        store._conn.execute(put, (UPDATE_KEY.format(uid), json.dumps(
            {"manager": manager, "state": raw["value"] if raw else None, "last": c.get("last_update_sent_at")})))
    _clear_notes(store, c["key"], manager)  # the note to the client went out with it
    return {"id": uid, "sent_at": ts, "since": rep["since"], "body": text, "manager": rep.get("manager"),
            "client": c["name"], "key": c["key"], "slug": c["slug"], "ai_moves": [m for r in reps for m in r["ai_moves"]],
            "already": False}


REPEAT_HOURS = 12  # a second Mark as sent this soon, with nothing new, is the same send


def last_update_for(store: Store, key: str, manager: str | None) -> dict[str, Any] | None:
    """The newest copy marked as sent for this client's update (or this one hiring manager's)."""
    for u in updates(store, key, 200):
        snap = _get_json(store, UPDATE_KEY.format(u["id"]))
        if (snap.get("manager") if isinstance(snap, dict) else None) == manager:
            return u
    return None


def repeat_of(store: Store, key: str, manager: str | None, now: datetime) -> dict[str, Any] | None:
    """When this update was marked as sent in the last REPEAT_HOURS and nothing on its jobs changed since (no
    move, interview, note or job edit, no new note to the client): that send, as mark_sent returns it, with
    already=True. Else None."""
    try:
        c = client(store, key)
    except NotFound:
        return None
    mark = sent_mark(store, c["key"], manager, c)
    at = to_dt(mark.get("at"))
    if not at or mark.get("id") is None or now - at > timedelta(hours=REPEAT_HOURS) or client_note(store, c["key"], manager):
        return None
    jobs = c["jobs"] if manager is None else [j for j in c["jobs"] if manager_key(j.get("hiring_manager")) == manager]
    ids = [j["id"] for j in jobs]
    if any((j.get("updated_at") or "") > mark["at"] for j in jobs):
        return None
    if ids and store._row(f"SELECT 1 AS x FROM timeline WHERE id > ? AND job_id IN ({','.join('?' * len(ids))}) LIMIT 1",
                          (mark["id"], *ids)):
        return None
    last = last_update_for(store, c["key"], manager)
    if not last or last["sent_at"] != mark["at"]:
        return None
    who = None
    if manager:
        who = next(((j.get("hiring_manager") or "").strip() for j in jobs if (j.get("hiring_manager") or "").strip()), None)
    return {"id": last["id"], "sent_at": last["sent_at"], "since": last["since"], "body": last["body"],
            "manager": "" if manager == "" else who, "client": c["name"], "key": c["key"], "slug": c["slug"],
            "ai_moves": [], "already": True}


def last_sent_to(store: Store, key: str, query: str | None) -> dict[str, Any] | None:
    """For a hiring manager with no update due any more (their req closed and their last one went out): the copy
    of the newest update that went to them, {"manager", "sent_at", "body"}, or None."""
    names = {manager_key(j.get("hiring_manager")): (j.get("hiring_manager") or "").strip()
             for j in client(store, key)["jobs"] if (j.get("hiring_manager") or "").strip()}
    found = sorted({manager_key(n) for n in actions.match_companies(list(names.values()), query)})
    if len(found) != 1:
        return None
    mk = found[0]
    for u in updates(store, key, 200):
        snap = _get_json(store, UPDATE_KEY.format(u["id"]))
        who = snap.get("manager") if isinstance(snap, dict) else None
        if who == mk or (who is None and f"For {names[mk]}:" in (u["body"] or "")):
            return {"manager": names[mk], "sent_at": u["sent_at"], "body": u["body"]}
    return None


def unreviewed_in(store: Store, key: str, manager: str | None = None, since: str | None = None,
                  until: str | None = None, now: datetime | None = None) -> list[dict[str, Any]]:
    """The AI moves in the update Mark as sent would record now that the recruiter hasn't reviewed or undone."""
    key = actions.company_key(key)
    reps = reports_for(store, key, since, until, now) if manager is None else [
        report(store, key, since, until, now, manager=manager)]
    return [m for r in reps for m in r["ai_moves"]]


def ai_moves_list(moves: list[dict[str, Any]], shown: int = 3) -> str:
    """'"Maya Chen" to Placed (Backend Engineer) and 2 more'."""
    listed = ", ".join(f"{quoted(m['name'])} to {m['stage']} ({m['job']})" for m in moves[:shown])
    return listed + (f" and {len(moves) - shown} more" if len(moves) > shown else "")


def updates(store: Store, key: str, limit: int = 20) -> list[dict[str, Any]]:
    row = get_row(store, actions.company_key(key))
    if not row:
        return []
    return store._rows("SELECT * FROM client_updates WHERE client_id = ? ORDER BY sent_at DESC, id DESC LIMIT ?",
                       (row["id"], limit))


def undo_sent(store: Store, key: str, update_id: int, note: str = "taken back in the app") -> dict[str, Any]:
    """Take back the newest "Mark as sent" (a click by mistake): its copy goes and the update starts where it did."""
    row = get_row(store, actions.company_key(key))
    latest = updates(store, key, 1)
    if not row or not latest or latest[0]["id"] != update_id:
        raise ValueError("Only the newest update marked as sent can be taken back.")
    snap = _get_json(store, UPDATE_KEY.format(update_id))
    put = "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value"
    with store._lock, store._conn:
        store._conn.execute("DELETE FROM client_updates WHERE id = ?", (update_id,))
        if isinstance(snap, dict):  # put back exactly what this Mark as sent replaced
            prev = snap.get("last")
            if snap.get("state"):
                store._conn.execute(put, (SENT_KEY.format(row["key"]), snap["state"]))
            else:
                store._conn.execute("DELETE FROM settings WHERE key = ?", (SENT_KEY.format(row["key"]),))
            store._conn.execute("DELETE FROM settings WHERE key = ?", (UPDATE_KEY.format(update_id),))
        else:
            prev = store._conn.execute("SELECT max(sent_at) FROM client_updates WHERE client_id = ?",
                                       (row["id"],)).fetchone()[0]
        store._conn.execute("UPDATE clients SET last_update_sent_at = ?, updated_at = ? WHERE id = ?",
                            (prev, now_iso(), row["id"]))
        store._conn.execute("UPDATE ai_actions SET undone_at = ?, undo_note = coalesce(undo_note, ?)"
                            " WHERE kind = 'client_sent' AND target_id = ? AND undone_at IS NULL",
                            (now_iso(), note, update_id))
    return latest[0]


# ------------------------------------------------------- the Friday send-out
def due_list(store: Store, now: datetime | None = None) -> dict[str, Any]:
    """Which updates are owed this week, cheaply (no reports built): {"due": [{"key", "name", "slug", "day",
    "late_days", "managers": [(manager_key, name)] or None}], "later": [...same, not due yet...], "no_day": n}.
    Clients with nothing open and nothing closed since their last update are left out."""
    now = now or datetime.now(timezone.utc)
    local = as_local(now)
    rows = {r["key"]: r for r in store._rows("SELECT * FROM clients")}
    out: dict[str, Any] = {"due": [], "later": [], "no_day": 0}
    for key, jobs in sorted(_all_jobs(store).items(), key=lambda kv: _spelling(kv[1]).lower()):
        live = any(j["status"] in ("open", "on_hold") for j in jobs)
        row = rows.get(key)
        if not row or not row.get("update_day") or row["update_day"] not in UPDATE_DAYS:
            out["no_day"] += bool(live)
            continue
        c = {**row, "jobs": jobs, "slug": slug(key), "key": key}
        mark = last_sent(store, c)
        if not live and not any(j["status"] == "closed" and (j.get("updated_at") or "") > (mark or "") for j in jobs):
            continue
        want = UPDATE_DAYS.index(row["update_day"])
        base = {"key": key, "name": row["name"] or _spelling(jobs), "slug": slug(key), "day": row["update_day"],
                "late_days": local.weekday() - want}
        if local.weekday() < want:
            out["later"].append({**base, "managers": None})
            continue
        parts = due_parts(store, key, now, c)
        if parts:
            out["due"].append({**base, "managers": None if parts == [(None, None)] else parts})
    return out


def due_updates(store: Store, now: datetime | None = None) -> dict[str, Any]:
    """Everything the Friday send-out page shows: each update due this week (one per hiring manager in-house)
    with its paste text, email link and the recruiter-only warnings, plus the clients due later this week."""
    now = now or datetime.now(timezone.utc)
    lst = due_list(store, now)
    parts = []
    for d in lst["due"]:
        reps = ([report(store, d["key"], now=now, manager=mk) for mk, _ in d["managers"]] if d["managers"]
                else [report(store, d["key"], now=now)])
        for rep in reps:
            text = report_text(rep)
            parts.append({"key": d["key"], "name": d["name"], "slug": d["slug"], "day": d["day"],
                          "late_days": d["late_days"], "manager": rep.get("manager"), "manager_key": rep["manager_key"],
                          "rep": rep, "text": text, "email": email_link(rep, text), "notes": recruiter_notes(rep),
                          "ai_moves": rep["ai_moves"], "overfilled": rep["overfilled"], "not_shown": rep["not_shown"]})
    later = []  # the drafts for later this week, as they stand now, so Thursday's prep can start
    for d in lst["later"]:
        for rep in reports_for(store, d["key"], now=now):
            text = report_text(rep)
            later.append({"key": d["key"], "name": d["name"], "slug": d["slug"], "day": d["day"],
                          "manager": rep.get("manager"), "manager_key": rep["manager_key"], "rep": rep, "text": text,
                          "notes": recruiter_notes(rep), "ai_moves": rep["ai_moves"]})
    return {"parts": parts, "later": lst["later"], "later_parts": later, "no_day": lst["no_day"], "now": now.isoformat()}


def today_items(store: Store, now: datetime | None = None) -> list[dict[str, Any]]:
    """One Today item per client whose update is due (today, or earlier this week and not sent yet)."""
    now = now or datetime.now(timezone.utc)
    own = actions.inhouse(store)
    items = []
    for d in due_list(store, now)["due"]:
        start = _day_start(d["day"], now)
        when = "today" if d["late_days"] == 0 else f"since {d['day']}"
        who = ""
        if d["managers"]:
            names = [n or "jobs with no hiring manager" for _, n in d["managers"]]
            who = "For " + (names[0] if len(names) == 1 else ", ".join(names[:-1]) + " and " + names[-1]) + ". "
        items.append({"key": f"update_due:{d['key']}:{start.date().isoformat() if start else ''}", "kind": "update_due",
                      "rank": 1, "when": start.astimezone(timezone.utc).replace(microsecond=0).isoformat() if start else None,
                      "title": f"{'Hiring update' if own else 'Client update'} due {when}: {d['name']}",
                      "detail": who + "Copy, email and mark it sent from the send-out page.", "client": d["name"],
                      "href": "/clients/due"})
    return items


# ------------------------------------------------- client changes the AI made
CLIENT_AI_FIELDS = ("contacts", "update_day", "time_zone", "notes")


def ai_save_client(store: Store, key: str, **fields: Any) -> dict[str, Any]:
    """save_client for the AI app: only contacts, update day, time zone and notes, logged on Changes with what
    they replaced so the recruiter can undo it. Returns {"client", "changed": {field: (old, new)}, "action_id"}."""
    unknown = [k for k in fields if k not in CLIENT_AI_FIELDS]
    if unknown:
        raise ValueError(f"Unknown client field {unknown[0]}. It accepts: {', '.join(CLIENT_AI_FIELDS)}.")
    before = client(store, key)
    after = save_client(store, key, **fields)
    changed = {k: (before.get(k), after.get(k)) for k in fields if before.get(k) != after.get(k)}
    action_id = None
    if changed:
        with store._write():
            action_id = store._log_ai("client_edit", target_id=after["id"], body=", ".join(changed),
                                      old={k: v[0] for k, v in changed.items()},
                                      new={**{k: v[1] for k, v in changed.items()}, "_client": after["name"],
                                           "_key": after["key"]})
    return {"client": after, "changed": changed, "action_id": action_id}


def ai_create_client(store: Store, name: str) -> dict[str, Any]:
    """create_client for the AI app: the name only (details go through ai_save_client), logged on Changes so the
    recruiter can undo it while no job names the client. {"client", "created", "action_id"}."""
    got = create_client(store, name)
    got["action_id"] = None
    if got["created"]:
        c = got["client"]
        with store._write():
            got["action_id"] = store._log_ai("client_create", target_id=c["id"], body=c["name"],
                                             new={"_client": c["name"], "_key": c["key"]})
    return got


def ai_mark_sent(store: Store, key: str, manager: str | None = None, since: str | None = None) -> dict[str, Any]:
    """mark_sent for the AI app, logged on Changes so the recruiter can take it back."""
    out = mark_sent(store, key, since, None, manager=manager)
    if out.get("already"):  # nothing new since the last Mark as sent: nothing to log or undo
        out["action_id"] = None
        return out
    with store._write():
        out["action_id"] = store._log_ai("client_sent", target_id=out["id"], body=out["manager"],
                                         new={"_client": out["client"], "_key": out["key"], "manager": out["manager"],
                                              "sent_at": out["sent_at"]})
    return out


def ai_client_blocker(store: Store, a: dict[str, Any]) -> str | None:
    """Why an AI client change can't be undone any more, or None."""
    new = a.get("new_data") or {}
    row = store._row("SELECT * FROM clients WHERE key = ?", (new.get("_key") or "",))
    if row is None:
        return "that client is gone"
    if a["kind"] == "client_create":
        if _all_jobs(store).get(row["key"]):
            return "a job names that client now"
        if updates(store, row["key"], 1):
            return "an update to them was marked as sent since"
        return None
    if a["kind"] == "client_sent":
        latest = updates(store, row["key"], 1)
        if not latest or latest[0]["id"] != a["target_id"]:
            return "a newer update was marked as sent since" if latest else "it was taken back already"
        return None
    fields = [k for k in new if not k.startswith("_")]
    return None if all(row.get(k) == new[k] for k in fields) else "those details were changed again since"


def ai_client_undo(store: Store, a: dict[str, Any], by: str = "app") -> str:
    """Undo an AI client change: take back its Mark as sent, or put the client's details back."""
    reason = ai_client_blocker(store, a)
    if reason:
        raise ValueError(reason[:1].upper() + reason[1:] + ".")
    new = a.get("new_data") or {}
    if a["kind"] == "client_sent":
        undo_sent(store, new["_key"], a["target_id"], note=None)  # marks this action undone too
        return a["summary"]
    if a["kind"] == "client_create":  # a client the AI app saved, with no job and no update yet: it goes
        with store._write():
            store._conn.execute("DELETE FROM clients WHERE key = ?", (new["_key"],))
            store._conn.execute("DELETE FROM settings WHERE key IN (?, ?) OR key LIKE ? OR key LIKE ?",
                                (NOTE_KEY.format(new["_key"]), SENT_KEY.format(new["_key"]),
                                 NOTE_KEY.format(new["_key"] + "|") + "%", "client_chased:" + new["_key"] + "|%"))
            store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?", (now_iso(), a["id"]))
        return a["summary"]
    old = {k: v for k, v in (a.get("old_data") or {}).items() if k in CLIENT_AI_FIELDS}
    with store._write():
        if old:
            store._conn.execute(f"UPDATE clients SET {', '.join(f'{k} = ?' for k in old)}, updated_at = ? WHERE key = ?",
                                (*old.values(), now_iso(), new["_key"]))
        store._conn.execute("UPDATE ai_actions SET undone_at = ? WHERE id = ?", (now_iso(), a["id"]))
    return a["summary"]


# ------------------------------------------------------------- submittals
def submittals(store: Store, key: str, limit: int = 200) -> list[dict[str, Any]]:
    """Every move into a Submitted or Presented stage on this client's jobs, newest first (undone ones aside)."""
    c = client(store, key)
    ids = [j["id"] for j in c["jobs"]]
    titles = {j["id"]: j["title"] for j in c["jobs"]}
    out = [{"at": e["at"], "candidate_id": e["candidate_id"], "name": e["candidate_name"], "job_id": e["job_id"],
            "job": titles[e["job_id"]], "stage": e["body"], "imported": e["imported"],
            # an import with no date for it carries the import day, not the day they were submitted
            "undated": _data(e.get("data")).get("imported") is True}
           for es in _history(store, ids).values() for e in es if is_submittal(e["body"])]
    out.sort(key=lambda s: s["at"], reverse=True)
    return sorted(out, key=lambda s: s["undated"])[:limit]  # dated ones first, newest first; date unknown last


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
        # An undated import's entry carries the import day, not the day they were submitted.
        imported = last.get("imported") or _data(last.get("data")).get("imported")
        when = "(imported, date unknown)" if imported else "on " + in_zone(last["at"], None, "%b %-d, %Y")
        more = f" ({_plural(len(earlier), 'earlier submittal')} in the last 12 months)" if len(earlier) > 1 else ""
        note["warning"] = (f"Heads up: {name} was already submitted to {note['client']} {when}, for "
                           f"{titles[last['job_id']]}{more}. Check before you send them again.")
    return note


__all__ = ["GROUPS", "client", "ensure_client", "find_clients", "find_manager", "is_submittal", "mailto", "managers",
           "mark_sent", "overview", "pipeline_csv", "recruiter_notes", "report", "report_text", "reports_for",
           "save_client", "stage_group", "submittal_note", "submittals", "undo_sent", "updates"]
