"""SQLite store: the single source of truth for candidates, jobs and pipelines.

Design notes
- One SQLite file, WAL mode, so the desktop UI and the MCP server (a separate
  process launched by Claude Desktop / Codex) can read and write at once.
- Full-text search uses an FTS5 external-content table kept in sync by
  triggers, so every write path (UI, MCP, import) is indexed automatically.
- Every candidate write also lands in `search_outbox` (via triggers). An
  optional external index (Typesense) drains that outbox; with no external
  index configured the outbox is simply pruned.
- Schema version lives in PRAGMA user_version; `_MIGRATIONS` is append-only.
  Each entry is SQL, or (SQL, python_step) when data needs reshaping.
"""
from __future__ import annotations

import json
import re
import sqlite3
import threading
import unicodedata
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable, Iterable

# Default pipeline for a new job: (name, outcome). outcome "open" means the
# candidate is still in play; "hired" and "lost" close the application.
DEFAULT_STAGES: tuple[tuple[str, str], ...] = (
    ("Sourced", "open"),
    ("Contacted", "open"),
    ("Screening", "open"),
    ("Interviewing", "open"),
    ("Offer", "open"),
    ("Hired", "hired"),
    ("Rejected", "lost"),
    ("Withdrawn", "lost"),
)
STAGES: tuple[str, ...] = tuple(n for n, _ in DEFAULT_STAGES)
STAGE_OUTCOMES = ("open", "hired", "lost")
JOB_STATUSES = ("open", "on_hold", "closed")
_STATUS_ALIASES = {"on hold": "on_hold", "hold": "on_hold", "paused": "on_hold", "pause": "on_hold"}

# Stage names that close an application, so custom pipelines count them right.
WON_STAGE_NAMES = frozenset({"hired", "placed", "started", "offer accepted", "accepted", "joined"})
LOST_STAGE_NAMES = frozenset({
    "rejected", "not a fit", "declined", "withdrew", "withdrawn", "lost", "closed", "offer declined",
    "not interested", "no hire", "dropped out", "turned down", "archived",
    "passed", "pass", "no go", "ghosted", "not moving forward", "rejected by client", "client rejected",
    "declined offer", "not selected", "did not proceed",
})
_OUTCOME_ALIASES = {"in play": "open", "active": "open", "won": "hired", "hire": "hired", "placed": "hired",
                    "out": "lost", "no": "lost", "rejected": "lost", "closed": "lost"}


def normalize_job_status(value: str | None) -> str:
    """'On hold', 'on-hold', 'paused' -> 'on_hold'. Raises on anything that isn't a job status."""
    v = re.sub(r"[\s_-]+", " ", (value or "").strip().lower())
    v = _STATUS_ALIASES.get(v, v.replace(" ", "_"))
    if v not in JOB_STATUSES:
        raise ValueError("status must be open, on hold or closed")
    return v


def stage_outcome(name: str) -> str:
    """Guess a stage's outcome from its name: 'hired' for wins, 'lost' for exits, else 'open'."""
    key = re.sub(r"[\W_]+", " ", name or "").strip().lower()
    return "hired" if key in WON_STAGE_NAMES else "lost" if key in LOST_STAGE_NAMES else "open"


def normalize_outcome(value: str | None) -> str | None:
    """'Out', 'lost', 'in play', 'hired' -> a stage outcome. None or blank stays None. Raises on anything else."""
    v = re.sub(r"[\s_-]+", " ", (value or "").strip().lower())
    if not v:
        return None
    v = _OUTCOME_ALIASES.get(v, v)
    if v not in STAGE_OUTCOMES:
        raise ValueError("A stage's outcome must be open (still in play), hired or lost (out).")
    return v

# Things that can go on a candidate's timeline. Contact kinds also move
# "last contacted" when they happened in the past.
TIMELINE_KINDS = ("note", "call", "email", "message", "meeting", "interview", "stage")
CONTACT_KINDS = {"call", "email", "message", "meeting", "interview"}
# "change" entries are the audit trail: written only by the store itself when a
# candidate's fields change (old values kept in timeline.data so they can be undone).
# Nobody can log one by hand, so it is not in TIMELINE_KINDS.
CHANGE_KIND = "change"
ALL_TIMELINE_KINDS = (*TIMELINE_KINDS, CHANGE_KIND)
FIELD_LABELS = {
    "current_title": "title", "current_company": "company", "linkedin_url": "LinkedIn",
    "last_contacted_at": "last contacted", "tags": "skills", "work_history": "work history",
}
# Who made a change -> the timeline entry's source ("from AI app", "from CSV import").
CHANGE_SOURCES = {"app": "App", "ai": "AI app", "import": "CSV import"}
# On a merge (same email or LinkedIn saved again) these take the newer value;
# name, source and about are never overwritten, other fields are only filled when empty.
MERGE_OVERWRITES = ("current_title", "current_company", "location")
# Longest value each field takes, so a pasted 2 MB blob can't land in a profile.
FIELD_LIMITS = {
    "name": 200, "email": 320, "phone": 50, "linkedin_url": 500, "website": 500, "current_title": 200,
    "current_company": 200, "location": 200, "about": 5000, "source": 200, "last_contacted_at": 100,
    "tags": 3000,  # all skills joined into one text
}
# Where the next new candidate and job id must start, so a deleted id is never handed out again
# (an old AI chat that says "candidate 3" must not reach someone new). Kept in the settings table.
ID_MARKS = {"candidates": "last_candidate_id", "jobs": "last_job_id"}

CANDIDATE_FIELDS: tuple[str, ...] = (
    "name",
    "email",
    "phone",
    "linkedin_url",
    "website",
    "current_title",
    "current_company",
    "location",
    "about",
    "tags",
    "source",
    "last_contacted_at",
)

_FTS_COLS = "name, current_title, current_company, location, about, tags, history_text"


def _fts_triggers() -> str:
    new = ", ".join(f"new.{c.strip()}" for c in _FTS_COLS.split(","))
    old = ", ".join(f"old.{c.strip()}" for c in _FTS_COLS.split(","))
    return f"""
    CREATE TRIGGER candidates_ai AFTER INSERT ON candidates BEGIN
        INSERT INTO candidates_fts(rowid, {_FTS_COLS}) VALUES (new.id, {new});
        INSERT INTO search_outbox(candidate_id, op) VALUES (new.id, 'upsert');
    END;
    CREATE TRIGGER candidates_ad AFTER DELETE ON candidates BEGIN
        INSERT INTO candidates_fts(candidates_fts, rowid, {_FTS_COLS}) VALUES ('delete', old.id, {old});
        INSERT INTO search_outbox(candidate_id, op) VALUES (old.id, 'delete');
    END;
    CREATE TRIGGER candidates_au AFTER UPDATE ON candidates BEGIN
        INSERT INTO candidates_fts(candidates_fts, rowid, {_FTS_COLS}) VALUES ('delete', old.id, {old});
        INSERT INTO candidates_fts(rowid, {_FTS_COLS}) VALUES (new.id, {new});
        INSERT INTO search_outbox(candidate_id, op) VALUES (new.id, 'upsert');
    END;
    """


def _migrate_v2_data(conn: sqlite3.Connection) -> None:
    # Fill the new index first: the update trigger below expects it to match the rows.
    conn.execute("INSERT INTO candidates_fts(candidates_fts) VALUES('rebuild')")
    # Tags: comma text -> structured rows (the text column stays as the search copy).
    for row in conn.execute("SELECT id, tags FROM candidates WHERE tags IS NOT NULL").fetchall():
        tags = split_tags(row[1])
        conn.execute("UPDATE candidates SET tags = ? WHERE id = ?", (", ".join(tags) or None, row[0]))
        conn.executemany("INSERT OR IGNORE INTO candidate_tags(candidate_id, tag) VALUES (?, ?)",
                         [(row[0], t) for t in tags])
    # Every existing job gets the default stages; old lowercase stage names become their display names.
    for (job_id,) in conn.execute("SELECT id FROM jobs").fetchall():
        conn.executemany(
            "INSERT INTO job_stages(job_id, name, position, outcome) VALUES (?, ?, ?, ?)",
            [(job_id, n, i, o) for i, (n, o) in enumerate(DEFAULT_STAGES)],
        )
    for name in STAGES:
        conn.execute("UPDATE applications SET stage = ? WHERE lower(stage) = lower(?)", (name, name))
    # Stage history and pipeline notes move to the candidate timeline.
    conn.execute(
        """INSERT INTO timeline (candidate_id, job_id, kind, body, at, created_at)
           SELECT a.candidate_id, a.job_id, CASE ac.kind WHEN 'stage' THEN 'stage' ELSE 'note' END,
                  CASE ac.kind WHEN 'stage' THEN upper(substr(ac.detail, 1, 1)) || substr(ac.detail, 2) ELSE ac.detail END,
                  ac.at, ac.at
           FROM activity ac JOIN applications a ON a.id = ac.application_id"""
    )


def _migrate_v4_data(conn: sqlite3.Connection) -> None:
    # Follow-up days move from settings rows (followup:<id>) onto the to-do itself.
    for key, value in conn.execute("SELECT key, value FROM settings WHERE key LIKE 'followup:%'").fetchall():
        sid = key.split(":", 1)[1]
        if sid.isdigit():
            conn.execute("UPDATE suggestions SET due_on = ? WHERE id = ?", (value[:10], int(sid)))
    conn.execute("DELETE FROM settings WHERE key LIKE 'followup:%'")
    # Exit names that are new to the list ("Passed", "Not moving forward") count as out when they sit
    # after every in-play stage, as they would have if the job were made today.
    for (job_id,) in conn.execute("SELECT id FROM jobs").fetchall():
        stages = conn.execute("SELECT id, name, outcome FROM job_stages WHERE job_id = ? ORDER BY position",
                              (job_id,)).fetchall()
        for sid, name, outcome in reversed(stages):
            if outcome == "open" and stage_outcome(name) == "lost":
                conn.execute("UPDATE job_stages SET outcome = 'lost' WHERE id = ?", (sid,))
            elif outcome == "open":
                break


_MIGRATIONS: tuple[Any, ...] = (
    # v1: core tables, FTS5 index, outbox
    """
    CREATE TABLE candidates (
        id              INTEGER PRIMARY KEY,
        name            TEXT NOT NULL,
        email           TEXT,
        phone           TEXT,
        linkedin_url    TEXT,
        website         TEXT,
        current_title   TEXT,
        current_company TEXT,
        location        TEXT,
        about           TEXT,
        tags            TEXT,
        source          TEXT,
        created_at      TEXT NOT NULL,
        updated_at      TEXT NOT NULL
    );
    CREATE UNIQUE INDEX candidates_email_uq
        ON candidates(lower(email)) WHERE email IS NOT NULL AND email <> '';
    CREATE UNIQUE INDEX candidates_linkedin_uq
        ON candidates(linkedin_url) WHERE linkedin_url IS NOT NULL AND linkedin_url <> '';
    CREATE INDEX candidates_updated_idx ON candidates(updated_at DESC);

    CREATE TABLE search_outbox (
        seq          INTEGER PRIMARY KEY AUTOINCREMENT,
        candidate_id INTEGER NOT NULL,
        op           TEXT NOT NULL CHECK (op IN ('upsert', 'delete'))
    );

    CREATE VIRTUAL TABLE candidates_fts USING fts5(
        name, current_title, current_company, location, about, tags,
        content='candidates', content_rowid='id',
        tokenize='unicode61 remove_diacritics 2'
    );
    CREATE TRIGGER candidates_ai AFTER INSERT ON candidates BEGIN
        INSERT INTO candidates_fts(rowid, name, current_title, current_company, location, about, tags)
        VALUES (new.id, new.name, new.current_title, new.current_company, new.location, new.about, new.tags);
        INSERT INTO search_outbox(candidate_id, op) VALUES (new.id, 'upsert');
    END;
    CREATE TRIGGER candidates_ad AFTER DELETE ON candidates BEGIN
        INSERT INTO candidates_fts(candidates_fts, rowid, name, current_title, current_company, location, about, tags)
        VALUES ('delete', old.id, old.name, old.current_title, old.current_company, old.location, old.about, old.tags);
        INSERT INTO search_outbox(candidate_id, op) VALUES (old.id, 'delete');
    END;
    CREATE TRIGGER candidates_au AFTER UPDATE ON candidates BEGIN
        INSERT INTO candidates_fts(candidates_fts, rowid, name, current_title, current_company, location, about, tags)
        VALUES ('delete', old.id, old.name, old.current_title, old.current_company, old.location, old.about, old.tags);
        INSERT INTO candidates_fts(rowid, name, current_title, current_company, location, about, tags)
        VALUES (new.id, new.name, new.current_title, new.current_company, new.location, new.about, new.tags);
        INSERT INTO search_outbox(candidate_id, op) VALUES (new.id, 'upsert');
    END;

    CREATE TABLE jobs (
        id          INTEGER PRIMARY KEY,
        title       TEXT NOT NULL,
        company     TEXT,
        location    TEXT,
        description TEXT,
        status      TEXT NOT NULL DEFAULT 'open' CHECK (status IN ('open', 'on_hold', 'closed')),
        created_at  TEXT NOT NULL,
        updated_at  TEXT NOT NULL
    );

    CREATE TABLE applications (
        id               INTEGER PRIMARY KEY,
        candidate_id     INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
        job_id           INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
        stage            TEXT NOT NULL DEFAULT 'sourced',
        notes            TEXT,
        created_at       TEXT NOT NULL,
        stage_changed_at TEXT NOT NULL,
        UNIQUE (candidate_id, job_id)
    );
    CREATE INDEX applications_job_idx ON applications(job_id, stage);

    CREATE TABLE activity (
        id             INTEGER PRIMARY KEY,
        application_id INTEGER NOT NULL REFERENCES applications(id) ON DELETE CASCADE,
        kind           TEXT NOT NULL,
        detail         TEXT,
        at             TEXT NOT NULL
    );
    """,
    # v2: skills tags, work history, notes timeline, last contacted, custom stages,
    # dashboard suggestions/snoozes, settings; FTS also covers past roles.
    (
        """
        ALTER TABLE candidates ADD COLUMN last_contacted_at TEXT;
        ALTER TABLE candidates ADD COLUMN history_text TEXT;

        CREATE TABLE candidate_tags (
            candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            tag          TEXT NOT NULL COLLATE NOCASE,
            PRIMARY KEY (candidate_id, tag)
        );
        CREATE INDEX candidate_tags_tag_idx ON candidate_tags(tag);

        CREATE TABLE work_history (
            id           INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            position     INTEGER NOT NULL,
            title        TEXT,
            company      TEXT,
            start        TEXT,
            "end"        TEXT,
            summary      TEXT
        );
        CREATE INDEX work_history_candidate_idx ON work_history(candidate_id, position);

        CREATE TABLE timeline (
            id           INTEGER PRIMARY KEY,
            candidate_id INTEGER NOT NULL REFERENCES candidates(id) ON DELETE CASCADE,
            job_id       INTEGER REFERENCES jobs(id) ON DELETE SET NULL,
            kind         TEXT NOT NULL,
            body         TEXT,
            at           TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            source       TEXT
        );
        CREATE INDEX timeline_candidate_idx ON timeline(candidate_id, at DESC);
        CREATE INDEX timeline_kind_at_idx ON timeline(kind, at);

        CREATE TABLE job_stages (
            id       INTEGER PRIMARY KEY,
            job_id   INTEGER NOT NULL REFERENCES jobs(id) ON DELETE CASCADE,
            name     TEXT NOT NULL,
            position INTEGER NOT NULL,
            outcome  TEXT NOT NULL DEFAULT 'open' CHECK (outcome IN ('open', 'hired', 'lost'))
        );
        CREATE UNIQUE INDEX job_stages_name_uq ON job_stages(job_id, lower(name));

        CREATE TABLE suggestions (
            id           INTEGER PRIMARY KEY,
            candidate_id INTEGER REFERENCES candidates(id) ON DELETE CASCADE,
            job_id       INTEGER REFERENCES jobs(id) ON DELETE CASCADE,
            text         TEXT NOT NULL,
            created_at   TEXT NOT NULL,
            done_at      TEXT,
            source       TEXT
        );
        CREATE TABLE snoozes (key TEXT PRIMARY KEY, until TEXT NOT NULL);
        CREATE TABLE settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);

        DROP TRIGGER candidates_ai;
        DROP TRIGGER candidates_ad;
        DROP TRIGGER candidates_au;
        DROP TABLE candidates_fts;
        CREATE VIRTUAL TABLE candidates_fts USING fts5(
            """ + _FTS_COLS + """,
            content='candidates', content_rowid='id',
            tokenize='unicode61 remove_diacritics 2'
        );
        """ + _fts_triggers(),
        _migrate_v2_data,
    ),
    # v3: audit trail. A "change" timeline entry keeps the old values as JSON so it can be undone.
    """
    ALTER TABLE timeline ADD COLUMN data TEXT;
    """,
    # v4: jobs get a hiring manager, headcount, target date and their own thin-pipeline target;
    # to-dos keep their due day and the note they were logged with (due days used to live in settings).
    (
        """
        ALTER TABLE jobs ADD COLUMN hiring_manager TEXT;
        ALTER TABLE jobs ADD COLUMN hiring_manager_email TEXT;
        ALTER TABLE jobs ADD COLUMN openings INTEGER NOT NULL DEFAULT 1;
        ALTER TABLE jobs ADD COLUMN target_date TEXT;
        ALTER TABLE jobs ADD COLUMN min_active INTEGER;
        ALTER TABLE suggestions ADD COLUMN due_on TEXT;
        ALTER TABLE suggestions ADD COLUMN note TEXT;
        """,
        _migrate_v4_data,
    ),
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


_MONTHS = {m: i for i, m in enumerate(
    ("jan", "feb", "mar", "apr", "may", "jun", "jul", "aug", "sep", "oct", "nov", "dec"), start=1)}
_FULL_MONTHS = ("january", "february", "march", "april", "may", "june", "july", "august", "september",
                "october", "november", "december", "sept")
_TIME = r"(?:[ T,]+(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*(?P<ap>[ap]\.?m\.?)?)?"
_DATE_FORMS = (
    # 11/5/2026, 11/5/26, 5.11.2026 (day first only when the first number can't be a month)
    re.compile(r"(?P<a>\d{1,2})[/.-](?P<b>\d{1,2})[/.-](?P<y>\d{4}|\d{2})" + _TIME + r"$", re.I),
    # 2026/11/05
    re.compile(r"(?P<y>\d{4})[/.](?P<m>\d{1,2})[/.](?P<d>\d{1,2})" + _TIME + r"$", re.I),
    # Mar 3, 2026 / March 3rd 2026
    re.compile(r"(?P<mon>[a-z]{3,9})\.?\s+(?P<d>\d{1,2})(?:st|nd|rd|th)?,?\s+(?P<y>\d{4})" + _TIME + r"$", re.I),
    # 3 Mar 2026 / 3 March, 2026
    re.compile(r"(?P<d>\d{1,2})(?:st|nd|rd|th)?\s+(?P<mon>[a-z]{3,9})\.?,?\s+(?P<y>\d{4})" + _TIME + r"$", re.I),
)


def _parse_loose(s: str) -> datetime | None:
    """The date shapes people type in spreadsheets. None if it isn't one of them."""
    low = s.lower()
    if low in ("today", "now", "yesterday"):
        today = datetime.now().astimezone()
        if low == "now":
            return today
        day = today.replace(hour=0, minute=0, second=0, microsecond=0)
        return day - timedelta(days=1) if low == "yesterday" else day
    for form in _DATE_FORMS:
        m = form.match(s)
        if not m:
            continue
        g = m.groupdict()
        year = int(g["y"])
        if year < 100:
            year += 2000 if year < 70 else 1900
        if g.get("mon"):
            month = _MONTHS.get(g["mon"][:3].lower())
            if month is None or (len(g["mon"]) > 3 and not any(
                    full.startswith(g["mon"].lower()) for full in _FULL_MONTHS)):
                return None
            day = int(g["d"])
        elif g.get("a"):
            a, b = int(g["a"]), int(g["b"])
            month, day = (b, a) if a > 12 >= b else (a, b)  # US order unless that can't be right
        else:
            month, day = int(g["m"]), int(g["d"])
        hour, minute, sec = int(g["h"] or 0), int(g["mi"] or 0), int(g["s"] or 0)
        if g.get("ap"):
            if not 1 <= hour <= 12:
                return None
            hour = hour % 12 + (12 if g["ap"].lower().startswith("p") else 0)
        try:
            return datetime(year, month, day, hour, minute, sec)
        except ValueError:
            return None
    return None


def parse_when(value: str | datetime | None, *, default_now: bool = True) -> str | None:
    """Accept '2026-11-05', '2026-11-05 14:30', ISO with offset, '11/5/2026', '11/5/26',
    '2026/11/05', 'Nov 5, 2026', '5 Nov 2026', 'today', 'yesterday', or a datetime. Returns UTC ISO.

    A time with no zone is taken as this computer's local time, which is what
    a recruiter means when they type one. Slash dates read month first (US),
    unless the first number can't be a month.
    """
    if value is None or (isinstance(value, str) and not value.strip()):
        return now_iso() if default_now else None
    if isinstance(value, datetime):
        dt = value
    else:
        s = re.sub(r"\s+", " ", value.strip())
        try:
            dt = datetime.fromisoformat(s.replace("Z", "+00:00"))
        except ValueError:
            loose = _parse_loose(s)
            if loose is None:
                raise ValueError(f"Couldn't read the date {value!r}. Use a form like 2026-11-05, 11/5/2026 "
                                 "or Nov 5, 2026.") from None
            dt = loose
    if dt.tzinfo is None:
        dt = dt.astimezone()  # local time
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def to_dt(iso: str | None) -> datetime | None:
    if not iso:
        return None
    dt = datetime.fromisoformat(iso)
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


class NotFound(LookupError):
    pass


def fold_text(text: str | None) -> str:
    """Lowercase with accents removed, so 'José Álvarez' compares equal to 'jose alvarez'."""
    decomposed = unicodedata.normalize("NFKD", (text or "").casefold())
    return "".join(ch for ch in decomposed if not unicodedata.combining(ch))


def _name_tokens(name: str | None) -> list[str]:
    return re.sub(r"[^\w\s]|_", " ", fold_text(name)).split()


def fold_name(name: str | None) -> str:
    """A name's comparison key: 'José  Álvarez-Ruiz' -> 'jose alvarez ruiz'."""
    return " ".join(_name_tokens(name))


def phone_key(phone: str | None) -> str | None:
    """The last 10 digits of a phone number, so '+1 (415) 555-0100' and '415.555.0100' match."""
    digits = re.sub(r"\D", "", phone or "")
    return digits[-10:] if len(digits) >= 7 else None


def _compatible(x: str, y: str) -> bool:
    """Same word, or one is an initial or a start of the other ('R' and 'Raman', 'Dan' and 'Daniel')."""
    return x.startswith(y) or y.startswith(x)


def _names_line_up(ta: list[str], tb: list[str]) -> bool:
    if not _compatible(ta[0], tb[0]):
        return False
    if len(ta) == 1 or len(tb) == 1:  # only a first name on one side
        return True
    # Both have a last name: it must be compatible, or appear among the other's later names
    # ('Ana Ruiz' and 'Ana Ruiz-Lopez'). A shared first name alone is not enough.
    return _compatible(ta[-1], tb[-1]) or ta[-1] in tb[1:] or tb[-1] in ta[1:]


def same_person_name(a: str | None, b: str | None) -> bool:
    """Could these two names be the same person? 'Priya R.' and 'Priya Raman', 'P. Raman' and
    'Priya Raman', 'ANA D.' and 'Ana Diaz', 'Raman, Priya' and 'Priya Raman' can; 'Ana Ruiz' and
    'Ben Cole' can't, and neither can 'Maya Smith' and 'Maya Okafor' (same first name, different last)."""
    ta, tb = _name_tokens(a), _name_tokens(b)
    if not ta or not tb or ta == tb or sorted(ta) == sorted(tb):
        return True
    if _names_line_up(ta, tb):
        return True
    # Written last name first ('Raman, P.'): try one side the other way round.
    return len(ta) == 2 and _names_line_up(ta[::-1], tb)


class IdentityConflict(ValueError):
    """A save matched someone by email or LinkedIn, but the name says it's a different person."""

    def __init__(self, existing: dict[str, Any], incoming_name: str, field: str, value: str) -> None:
        self.existing, self.incoming_name, self.field, self.value = existing, incoming_name, field, value
        super().__init__(f"{value} already belongs to {existing['name']} (id {existing['id']}), but this says "
                         f"{incoming_name}, so nothing was changed.")


class DuplicateValue(ValueError):
    """An email or LinkedIn URL that another candidate already has."""

    def __init__(self, field: str, owner: dict[str, Any]) -> None:
        self.field, self.owner = field, owner
        self.label = "email" if field == "email" else "LinkedIn URL"
        super().__init__(f"That {self.label} already belongs to {owner['name']} (id {owner['id']}).")


# Highlight markers inside snippets. Control characters can't come from
# normal typing, so the UI can HTML-escape a snippet first and then turn
# these into <mark> tags without ever trusting candidate text as HTML.
HL_START, HL_END = "\x02", "\x03"


def snippet_html(snippet: str | None) -> str:
    from html import escape

    return escape(snippet or "").replace(HL_START, "<mark>").replace(HL_END, "</mark>")


def snippet_text(snippet: str | None) -> str:
    return (snippet or "").replace(HL_START, "**").replace(HL_END, "**")


@dataclass
class SearchHit:
    candidate: dict[str, Any]
    snippet: str | None = None
    score: float | None = None


@dataclass
class SearchResult:
    hits: list[SearchHit]
    total: int
    engine: str = "fts5"
    query: str = ""
    filters: dict[str, str] = field(default_factory=dict)
    closest: bool = False  # no hit had every word, so these match some of them
    exact: bool = False  # the query was an email or LinkedIn URL that matched one person


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


def split_tags(value: str | Iterable[str] | None) -> list[str]:
    """'Python, sales;  python | ci/cd' -> ['Python', 'sales', 'ci/cd']. Splits on , ; and |, never on /.
    Keeps the first spelling and dedupes ignoring case."""
    if value is None:
        return []
    parts = re.split(r"[,;|\n]", value) if isinstance(value, str) else [str(v) for v in value]
    out: list[str] = []
    seen: set[str] = set()
    for p in parts:
        t = re.sub(r"\s+", " ", p).strip().strip("#").strip()
        if t and t.lower() not in seen:
            seen.add(t.lower())
            out.append(t[:60])
    return out


def normalize_linkedin(url: str | None) -> str | None:
    """Canonical form so the same profile pasted two ways dedupes."""
    url = _clean(url)
    if not url:
        return None
    u = url.lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^(www\.|[a-z]{2}\.)linkedin\.com", "linkedin.com", u)
    u = u.split("?")[0].split("#")[0].rstrip("/")
    return "https://" + u


# Shorthand recruiters type for places and titles. Each expands to an OR group.
ALIASES: dict[str, tuple[str, ...]] = {
    "sfo": ("san francisco",),
    "ny": ("new york",),
    "la": ("los angeles",),
    "dc": ("washington",),
    "bay": ("bay area",),
    "swe": ("software engineer",),
    "sde": ("software engineer",),
    "pm": ("product manager",),
    "ai": ("artificial intelligence",),
    "fe": ("frontend", "front end"),
    "be": ("backend", "back end"),
}
# Spellings that mean the same thing, matched in both directions.
ALIAS_GROUPS: tuple[tuple[str, ...], ...] = (
    ("frontend", "front end"),
    ("backend", "back end"),
    ("fullstack", "full stack"),
    ("ml", "machine learning"),
    ("js", "javascript"),
    ("ts", "typescript"),
    ("k8s", "kubernetes"),
    ("sf", "san francisco"),
    ("nyc", "new york"),
)
for _group in ALIAS_GROUPS:
    for _form in _group:
        ALIASES[_form] = tuple(dict.fromkeys((*ALIASES.get(_form, ()), *(f for f in _group if f != _form))))

# Filler words people type in a natural-language search ("people who know react in sf").
STOPWORDS = frozenset(
    "the and a an of in at with for who people someone or to is are on from that has have any "
    "find me show candidates candidate person knows know".split()
)

_EMAIL_RE = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")
_LINKEDIN_RE = re.compile(r"^(https?://)?([a-z]{2,3}\.)?linkedin\.com/", re.I)


def _query_groups(text: str) -> list[str]:
    """Free text -> one FTS5 group per meaningful word (or alias phrase), all syntax quoted."""
    words = re.findall(r"\w+", (text or "").lower(), flags=re.UNICODE)
    kept = [w for w in words if w not in STOPWORDS] or words
    groups: list[str] = []
    i = 0
    while i < len(kept):
        pair = " ".join(kept[i:i + 2]) if i + 1 < len(kept) else ""
        if pair in ALIASES:  # a two-word phrase like "front end" or "san francisco"
            form, step = pair, 2
        else:
            form, step = kept[i], 1
        last = i + step >= len(kept)
        term = f'"{form}"*' if last and step == 1 else f'"{form}"'
        expansions = ALIASES.get(form)
        if expansions:
            term = "(" + " OR ".join([term, *(f'"{e}"' for e in expansions)]) + ")"
        groups.append(term)
        i += step
    return groups


def fts_query(text: str, *, any_word: bool = False) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match, last word as prefix.

    Users type things like `senior react sf` or `o'brien`; we never pass raw
    syntax through, so stray quotes or operators can't raise errors. Filler
    words (the, who, people...) are dropped, and known shorthand (sf, nyc,
    frontend / front end...) also matches its other forms. any_word=True
    joins the words with OR instead, for a "closest matches" fallback.
    """
    groups = _query_groups(text)
    if not groups:
        return None
    return (" OR " if any_word else " AND ").join(groups)


def _history_text(entries: list[dict[str, Any]]) -> str | None:
    bits = []
    for e in entries:
        t, c = e.get("title"), e.get("company")
        bits.append(" at ".join(x for x in (t, c) if x))
    return "; ".join(b for b in bits if b) or None


def _clean_history(entries: list[dict[str, Any]] | None) -> list[dict[str, Any]]:
    """Same cleaning _write_history does, so a saved list can be compared with a new one."""
    clean = []
    for e in entries or []:
        if not isinstance(e, dict):
            raise ValueError("Each work history entry needs title and/or company.")
        row = {k: _clean(e.get(k)) for k in ("title", "company", "start", "end", "summary")}
        if row["title"] or row["company"]:
            clean.append(row)
    return clean


def _labels(fields: Iterable[str]) -> str:
    return ", ".join(FIELD_LABELS.get(f, f) for f in fields)


def _change_body(old: dict[str, Any], new: dict[str, Any]) -> str:
    cleared = [k for k, v in new.items() if v in (None, "", [])]
    updated = [k for k in new if k not in cleared]
    parts = []
    if updated:
        parts.append("Updated " + _labels(updated))
    if cleared:
        parts.append(("Cleared " if not updated else "cleared ") + _labels(cleared))
    return "; ".join(parts)


DEFAULT_SETTINGS: dict[str, str] = {
    "flag_interviews": "1",
    "flag_stuck": "1",
    "flag_thin": "1",
    "flag_stale": "1",
    "flag_suggestions": "1",
    "stuck_days": "7",
    "thin_min": "5",
    "stale_days": "180",
    "pipeline_view": "board",
}


class Store:
    """Thread-safe wrapper around one SQLite connection."""

    def __init__(self, path: str | Path = ":memory:") -> None:
        self.path = str(path)
        if self.path != ":memory:":
            Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False, timeout=10)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA foreign_keys = ON")
        self._conn.execute("PRAGMA busy_timeout = 10000")
        # Deleted rows are overwritten with zeros, so a deleted person can't be read back out of
        # the file. Some builds (macOS, Windows Python) default to off.
        self._conn.execute("PRAGMA secure_delete = ON")
        self._conn.create_function("fold_name", 1, fold_name, deterministic=True)
        self._conn.create_function("phone_key", 1, phone_key, deterministic=True)
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    # ------------------------------------------------------------------ infra
    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, step in enumerate(_MIGRATIONS[version:], start=version + 1):
                sql, py = (step, None) if isinstance(step, str) else step
                self._conn.executescript("BEGIN;" + sql)
                try:
                    if py is not None:
                        py(self._conn)
                    self._conn.execute(f"PRAGMA user_version = {i}")
                    self._conn.commit()
                except Exception:
                    self._conn.rollback()
                    raise

    @property
    def schema_version(self) -> int:
        return self._conn.execute("PRAGMA user_version").fetchone()[0]

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    def backup_to(self, dest: str | Path) -> None:
        """Consistent copy of the whole database, safe while other processes write."""
        with self._lock:
            target = sqlite3.connect(str(dest))
            try:
                self._conn.backup(target)
            finally:
                target.close()

    def _next_id(self, table: str) -> int:
        """A fresh id for a new candidate or job, above every id ever used (deleted ones too).

        Call inside a write transaction. The first statement takes the write lock, so the app
        window and the MCP server can't pick the same id at once."""
        key = ID_MARKS[table]
        self._conn.execute("INSERT INTO settings(key, value) VALUES (?, '0') ON CONFLICT(key) DO NOTHING", (key,))
        mark = self._conn.execute("SELECT value FROM settings WHERE key = ?", (key,)).fetchone()[0]
        top = self._conn.execute(f"SELECT coalesce(max(id), 0) FROM {table}").fetchone()[0]
        new_id = max(int(mark) if str(mark).isdigit() else 0, top) + 1
        self._conn.execute("UPDATE settings SET value = ? WHERE key = ?", (str(new_id), key))
        return new_id

    # --------------------------------------------------------------- settings
    def settings(self) -> dict[str, str]:
        out = dict(DEFAULT_SETTINGS)
        out.update({r["key"]: r["value"] for r in self._rows("SELECT key, value FROM settings")})
        return out

    def setting(self, key: str) -> str:
        return self.settings().get(key, "")

    def set_settings(self, **values: Any) -> dict[str, str]:
        unknown = set(values) - set(DEFAULT_SETTINGS)
        if unknown:
            raise ValueError(f"Unknown setting(s): {', '.join(sorted(unknown))}")
        with self._lock, self._conn:
            self._conn.executemany(
                "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                [(k, str(v)) for k, v in values.items()],
            )
        return self.settings()

    # ------------------------------------------------------------- candidates
    @staticmethod
    def _hydrate(row: dict[str, Any] | None) -> dict[str, Any] | None:
        if row is not None:
            row["tags"] = split_tags(row.get("tags"))
            row.pop("history_text", None)
        return row

    def _prepare(self, data: dict[str, Any]) -> dict[str, Any]:
        unknown = set(data) - set(CANDIDATE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown candidate field(s): {', '.join(sorted(unknown))}")
        out = {k: _clean(v) for k, v in data.items()}
        if "tags" in out:
            out["tags"] = ", ".join(split_tags(out["tags"])) or None
        if "email" in out and out["email"]:
            out["email"] = out["email"].lower()
        for k, v in out.items():
            limit = FIELD_LIMITS.get(k)
            if limit and isinstance(v, str) and len(v) > limit:
                label = {"tags": "the skills list"}.get(k) or FIELD_LABELS.get(k, k.replace("_", " "))
                raise ValueError(f"{label[:1].upper() + label[1:]} is too long ({len(v):,} characters). "
                                 f"The limit is {limit:,}, so nothing was saved.")
        if "linkedin_url" in out:
            out["linkedin_url"] = normalize_linkedin(out["linkedin_url"])
        if "last_contacted_at" in out and out["last_contacted_at"]:
            out["last_contacted_at"] = parse_when(out["last_contacted_at"])
        return out

    def _canonical_tag(self, tag: str, candidate_id: int) -> str:
        """The spelling other candidates already use for this tag ("React" beats a new "react")."""
        row = self._conn.execute(
            "SELECT tag FROM candidate_tags WHERE tag = ? AND candidate_id <> ?"
            " GROUP BY tag COLLATE BINARY ORDER BY count(*) DESC, min(rowid) LIMIT 1",
            (tag, candidate_id),
        ).fetchone()
        return row[0] if row else tag

    def _sync_tags(self, candidate_id: int, tags_text: str | None) -> None:
        tags = [self._canonical_tag(t, candidate_id) for t in split_tags(tags_text)]
        text = ", ".join(tags) or None
        if text != tags_text:  # keep the search copy in the same spelling
            self._conn.execute("UPDATE candidates SET tags = ? WHERE id = ?", (text, candidate_id))
        self._conn.execute("DELETE FROM candidate_tags WHERE candidate_id = ?", (candidate_id,))
        self._conn.executemany(
            "INSERT OR IGNORE INTO candidate_tags(candidate_id, tag) VALUES (?, ?)",
            [(candidate_id, t) for t in tags],
        )

    def add_candidate(self, work_history: list[dict[str, Any]] | None = None, **data: Any) -> dict[str, Any]:
        fields = self._prepare(data)
        if not fields.get("name"):
            raise ValueError("A candidate needs a name.")
        ts = now_iso()
        fields["created_at"] = fields["updated_at"] = ts
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock, self._conn:
            clash = self._clash(None, fields)
            if clash:
                raise clash
            new_id = self._next_id("candidates")
            self._conn.execute(
                f"INSERT INTO candidates (id, {cols}) VALUES (?, {marks})", (new_id, *fields.values())
            )
            self._sync_tags(new_id, fields.get("tags"))
            if work_history:
                self._write_history(new_id, work_history)
        return self.get_candidate(new_id)

    def find_duplicate(self, email: str | None = None, linkedin_url: str | None = None) -> dict[str, Any] | None:
        email = _clean(email)
        if email:
            hit = self._row("SELECT * FROM candidates WHERE lower(email) = lower(?)", (email,))
            if hit:
                return self._hydrate(hit)
        li = normalize_linkedin(linkedin_url)
        if li:
            return self._hydrate(self._row("SELECT * FROM candidates WHERE linkedin_url = ?", (li,)))
        return None

    def _clash(self, candidate_id: int | None, fields: dict[str, Any]) -> DuplicateValue | None:
        """The first prepared email or LinkedIn value in `fields` that someone else already has."""
        for f, sql in (("email", "lower(email) = lower(?)"), ("linkedin_url", "linkedin_url = ?")):
            if fields.get(f):
                owner = self._row(f"SELECT id, name FROM candidates WHERE {sql} AND id <> ?",
                                  (fields[f], candidate_id or 0))
                if owner:
                    return DuplicateValue(f, owner)
        return None

    def find_by_name(self, name: str) -> list[dict[str, Any]]:
        """Everyone saved under this name, ignoring case, accents and punctuation ('Jose Alvarez'
        finds 'José Álvarez')."""
        key = fold_name(name)
        if not key:
            return [self._hydrate(r) for r in self._rows(
                "SELECT * FROM candidates WHERE lower(name) = lower(?) ORDER BY updated_at DESC",
                (_clean(name) or "",))]
        return [self._hydrate(r) for r in self._rows(
            "SELECT * FROM candidates WHERE fold_name(name) = ? ORDER BY updated_at DESC", (key,))]

    def possible_duplicates(self, candidate_id: int) -> list[dict[str, Any]]:
        """Other people who may be this same person: the same name (ignoring case and accents),
        or the same email, LinkedIn URL or phone number."""
        c = self.get_candidate(candidate_id)
        key, phone = fold_name(c["name"]), phone_key(c.get("phone"))
        rows = self._rows(
            """SELECT * FROM candidates WHERE id <> ? AND (
                   (? <> '' AND fold_name(name) = ?)
                   OR (? IS NOT NULL AND lower(email) = lower(?))
                   OR (? IS NOT NULL AND linkedin_url = ?)
                   OR (? IS NOT NULL AND phone_key(phone) = ?))
               ORDER BY updated_at DESC LIMIT 10""",
            (candidate_id, key, key, c.get("email"), c.get("email"), c.get("linkedin_url"), c.get("linkedin_url"),
             phone, phone))
        return [self._hydrate(r) for r in rows]

    def upsert_candidate(self, work_history: list[dict[str, Any]] | None = None, *, by: str = "app",
                         **data: Any) -> tuple[dict[str, Any], bool]:
        """Add, or merge into an existing candidate matched by email or LinkedIn.

        Merges are conservative, because a CSV row or an AI re-save can be stale or wrong:
        name, source and about are never overwritten (only filled when empty); title, company
        and location take the newer non-empty value; other fields are only filled when empty;
        tags are added to. Blanks never erase anything. Nothing is written (and updated_at
        stays put) when nothing changed. Returns (candidate, created); on a merge the
        candidate dict also carries "_changed" (fields written) and "_kept" (fields that
        had a different saved value that was kept).

        A match whose saved name clearly isn't the incoming one (see same_person_name) raises
        IdentityConflict and changes nothing, so one person's row can't overwrite someone else.
        """
        existing = self.find_duplicate(data.get("email"), data.get("linkedin_url"))
        if existing is None:
            return self.add_candidate(work_history=work_history, **data), True
        incoming_name = _clean(data.get("name"))
        if incoming_name and not same_person_name(existing["name"], incoming_name):
            email = _clean(data.get("email"))
            by_email = bool(email and (existing.get("email") or "").lower() == email.lower())
            raise IdentityConflict(existing, incoming_name, "email" if by_email else "linkedin_url",
                                   email.lower() if by_email else normalize_linkedin(data.get("linkedin_url")))
        incoming = self._prepare(data)
        row = self._row("SELECT * FROM candidates WHERE id = ?", (existing["id"],))
        merged: dict[str, Any] = {}
        kept: list[str] = []
        for k, v in incoming.items():
            if v in (None, ""):
                continue
            cur = row.get(k)
            if k == "tags":
                merged[k] = ", ".join(split_tags(split_tags(cur) + split_tags(v)))
            elif k in MERGE_OVERWRITES:
                merged[k] = v
            elif k == "last_contacted_at":
                if not cur or v > cur:
                    merged[k] = v
            elif not cur:
                merged[k] = v
            elif cur != v:
                kept.append(k)
        history = work_history or self._history_for_new_role(existing["id"], row, merged)
        cand = self._update(existing["id"], merged, history, by=by, merge=True)
        cand["_kept"] = kept
        return cand, False

    def _history_for_new_role(self, candidate_id: int, row: dict[str, Any],
                              merged: dict[str, Any]) -> list[dict[str, Any]] | None:
        """When a save changes the title or company, the saved work history follows, so it doesn't
        still show the old role "to now". Same company: the current role is updated in place.
        New company: the current role gets an end year and the new role goes on top. None when
        nothing needs to change (or there is no saved history to keep in step)."""
        def same(a: Any, b: Any) -> bool:
            return (a or "").strip().casefold() == (b or "").strip().casefold()

        title = merged.get("current_title", row.get("current_title"))
        company = merged.get("current_company", row.get("current_company"))
        if same(title, row.get("current_title")) and same(company, row.get("current_company")):
            return None
        history = self.work_history(candidate_id)
        if not history:
            return None
        top = history[0]
        current = (top.get("end") or "").strip().lower() in ("", "present", "now", "current", "today")
        if current and same(top.get("title"), title) and same(top.get("company"), company):
            return None
        if current and (same(top.get("company"), company) or not top.get("company")):
            return [{**top, "title": title or top.get("title"), "company": company or top.get("company")}, *history[1:]]
        rest = [{**top, "end": str(datetime.now().year)} if current else top, *history[1:]]
        return [{"title": title, "company": company, "start": None, "end": None, "summary": None}, *rest]

    def get_candidate(self, candidate_id: int) -> dict[str, Any]:
        row = self._row("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
        if row is None:
            raise NotFound(f"No candidate with id {candidate_id}")
        return self._hydrate(row)

    def candidate_detail(self, candidate_id: int, timeline_limit: int = 50) -> dict[str, Any]:
        """Everything the candidate page and the AI's get_candidate need."""
        c = self.get_candidate(candidate_id)
        c["work_history"] = self.work_history(candidate_id)
        c["pipelines"] = self.candidate_pipelines(candidate_id)
        c["timeline"] = self.timeline(candidate_id, limit=timeline_limit)
        return c

    def update_candidate(self, candidate_id: int, work_history: list[dict[str, Any]] | None = None, *,
                         by: str = "app", **data: Any) -> dict[str, Any]:
        """Change fields (a blank clears one). Only real changes are written, and each call that
        changes something leaves a "change" entry on the timeline with the old values, so it can
        be undone. `by` is who did it: "app" (the recruiter) or "ai". The returned candidate
        carries "_changed", the fields that actually changed."""
        fields = self._prepare(data)
        if "name" in fields and not fields["name"]:
            raise ValueError("A candidate needs a name.")
        self.get_candidate(candidate_id)
        return self._update(candidate_id, fields, work_history, by=by)

    def _update(self, candidate_id: int, fields: dict[str, Any], work_history: list[dict[str, Any]] | None,
                *, by: str, merge: bool = False) -> dict[str, Any]:
        with self._lock, self._conn:
            old, new = self._diff(candidate_id, fields, work_history)
            clash = self._clash(candidate_id, new)
            if clash:
                raise clash
            if new:
                self._apply(candidate_id, new)
                body = _change_body(old, new) + (" (saved again with new details)" if merge else "")
                self._record_change(candidate_id, old, new, by, body)
        cand = self.get_candidate(candidate_id)
        cand["_changed"] = list(new)
        return cand

    def _diff(self, candidate_id: int, fields: dict[str, Any],
              work_history: list[dict[str, Any]] | None) -> tuple[dict[str, Any], dict[str, Any]]:
        """(old, new) values of just the fields that would really change."""
        row = self._row("SELECT * FROM candidates WHERE id = ?", (candidate_id,)) or {}
        old: dict[str, Any] = {}
        new: dict[str, Any] = {}
        for k, v in fields.items():
            if row.get(k) != v:
                old[k], new[k] = row.get(k), v
        if work_history is not None:
            current, wanted = self.work_history(candidate_id), _clean_history(work_history)
            if current != wanted:
                old["work_history"], new["work_history"] = current, wanted
        return old, new

    def _apply(self, candidate_id: int, new: dict[str, Any]) -> None:
        """Write already-prepared values. Call inside a transaction."""
        cols = {k: v for k, v in new.items() if k != "work_history"}
        cols["updated_at"] = now_iso()
        sets = ", ".join(f"{k} = ?" for k in cols)
        self._conn.execute(f"UPDATE candidates SET {sets} WHERE id = ?", (*cols.values(), candidate_id))
        if "tags" in cols:
            self._sync_tags(candidate_id, cols["tags"])
        if "work_history" in new:
            self._write_history(candidate_id, new["work_history"] or [])

    def _record_change(self, candidate_id: int, old: dict[str, Any], new: dict[str, Any], by: str,
                       body: str) -> int:
        by = by if by in CHANGE_SOURCES else "app"
        ts = now_iso()
        return self._conn.execute(
            "INSERT INTO timeline(candidate_id, kind, body, at, created_at, source, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
            (candidate_id, CHANGE_KIND, body, ts, ts, CHANGE_SOURCES[by],
             json.dumps({"by": by, "old": old, "new": new})),
        ).lastrowid

    def undo_change(self, timeline_id: int, *, candidate_id: int | None = None, by: str = "app") -> dict[str, Any]:
        """Put back the old values from a "change" timeline entry, and log that as a change too.

        Fields edited again since that change are left alone, and so is an email or LinkedIn URL
        that another candidate has now; the returned candidate's "_skipped" says which, as
        "LinkedIn URL (it now belongs to Bob Lee, id 2)". Refuses anything that isn't a change
        entry, or one already undone.
        """
        with self._lock, self._conn:
            entry = self._row("SELECT * FROM timeline WHERE id = ?", (timeline_id,))
            if entry is None or (candidate_id is not None and entry["candidate_id"] != candidate_id):
                raise NotFound(f"No timeline entry with id {timeline_id}")
            data = json.loads(entry["data"]) if entry.get("data") else None
            if entry["kind"] == "stage" and data and data.get("pipeline"):
                if data.get("undone_at"):
                    raise ValueError("That change was already undone.")
                return self._undo_stage(entry, data, by)
            if entry["kind"] != CHANGE_KIND or not data or not data.get("old"):
                raise ValueError("Only a change to a profile can be undone.")
            if data.get("undone_at"):
                raise ValueError("That change was already undone.")
            if data.get("merged"):
                raise ValueError("A merge can't be undone here. The backup taken just before it has both people.")
            cid = entry["candidate_id"]
            row = self._row("SELECT * FROM candidates WHERE id = ?", (cid,)) or {}
            history = self.work_history(cid)

            def current(k: str) -> Any:
                return history if k == "work_history" else row.get(k)

            restore = {k: v for k, v in data["old"].items() if current(k) == data["new"].get(k)}
            if not restore:
                raise ValueError("Those fields were changed again since, so there is nothing to undo.")
            skipped = []
            for k in ("email", "linkedin_url"):
                clash = self._clash(cid, {k: restore.get(k)})
                if clash:
                    restore.pop(k)
                    skipped.append(f"{clash.label} (it now belongs to {clash.owner['name']}, id {clash.owner['id']})")
            if not restore:
                raise ValueError("Can't undo: the " + " and the ".join(skipped) + ".")
            before = {k: current(k) for k in restore}
            self._apply(cid, restore)
            data["undone_at"] = now_iso()
            if skipped:
                data["undo_skipped"] = skipped
            self._conn.execute("UPDATE timeline SET data = ? WHERE id = ?", (json.dumps(data), timeline_id))
            self._record_change(cid, before, restore, by, "Undid a change to " + _labels(restore))
        cand = self.get_candidate(cid)
        cand["_restored"], cand["_skipped"] = list(restore), skipped
        return cand

    def mark_ai_stage(self, candidate_id: int, job_id: int, old_stage: str | None) -> None:
        """Mark the newest stage entry for this candidate and job as the AI's, with what it replaced,
        so the recruiter can undo it (an add takes them back out, a move puts them back)."""
        with self._lock, self._conn:
            entry = self._row("SELECT * FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage'"
                              " ORDER BY id DESC LIMIT 1", (candidate_id, job_id))
            if entry is None or entry.get("data") or (old_stage or "").lower() == (entry["body"] or "").lower():
                return
            data = {"by": "ai", "pipeline": {"job_id": job_id, "old": old_stage, "new": entry["body"]}}
            self._conn.execute("UPDATE timeline SET source = ?, data = ? WHERE id = ?",
                               (CHANGE_SOURCES["ai"], json.dumps(data), entry["id"]))

    def _undo_stage(self, entry: dict[str, Any], data: dict[str, Any], by: str) -> dict[str, Any]:
        """Undo an AI stage move or pipeline add (see mark_ai_stage). Call inside a transaction."""
        cid, move = entry["candidate_id"], data["pipeline"]
        app = self._row("SELECT * FROM applications WHERE candidate_id = ? AND job_id = ?", (cid, move["job_id"]))
        if app is None or app["stage"].lower() != (move["new"] or "").lower():
            raise ValueError("They were moved again since, so there is nothing to undo.")
        ts = now_iso()
        if move.get("old"):
            self._conn.execute("UPDATE applications SET stage = ?, stage_changed_at = ? WHERE id = ?",
                               (move["old"], ts, app["id"]))
            self._conn.execute(
                "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at, source)"
                " VALUES (?, ?, 'stage', ?, ?, ?, ?)", (cid, move["job_id"], move["old"], ts, ts,
                                                        CHANGE_SOURCES.get(by, "App")))
        else:
            self._conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))
        data["undone_at"] = ts
        self._conn.execute("UPDATE timeline SET data = ? WHERE id = ?", (json.dumps(data), entry["id"]))
        cand = self.get_candidate(cid)
        cand["_restored"], cand["_skipped"] = ["job stage" if move.get("old") else "job pipeline"], []
        return cand

    def delete_candidate(self, candidate_id: int) -> None:
        """Only the recruiter can do this, from the app window. The AI has no delete tool."""
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        if cur.rowcount == 0:
            raise NotFound(f"No candidate with id {candidate_id}")

    def merge_candidates(self, keep_id: int, other_id: int, *, by: str = "app") -> dict[str, Any]:
        """App window only (the AI has no merge tool): fold `other` into `keep`, then delete `other`.

        keep's empty fields are filled from other (last contacted takes the later date), skills are
        combined, and other's timeline, to-dos, work history and jobs move to keep. When both are in
        the same job, keep ends up in the further stage. A "change" entry on keep records what came
        from where (it can't be undone with Undo; the backup taken before merging can). The
        returned candidate carries "_merged": {"name", "filled", "timeline", "jobs"}.
        """
        if keep_id == other_id:
            raise ValueError("Pick two different people to merge.")
        self.get_candidate(keep_id)
        other = self.get_candidate(other_id)
        with self._lock, self._conn:
            krow = self._row("SELECT * FROM candidates WHERE id = ?", (keep_id,))
            orow = self._row("SELECT * FROM candidates WHERE id = ?", (other_id,))
            fields: dict[str, Any] = {}
            for k in CANDIDATE_FIELDS:
                kv, ov = krow.get(k), orow.get(k)
                if k == "tags":
                    tags = ", ".join(split_tags(split_tags(kv) + split_tags(ov))) or None
                    if tags != kv:
                        fields[k] = tags
                elif k == "last_contacted_at":
                    if ov and (not kv or ov > kv):
                        fields[k] = ov
                elif ov and not kv:
                    fields[k] = ov
            mine = self.work_history(keep_id)
            seen = {((e.get("title") or "").casefold(), (e.get("company") or "").casefold()) for e in mine}
            extra = [e for e in self.work_history(other_id)
                     if ((e.get("title") or "").casefold(), (e.get("company") or "").casefold()) not in seen]
            if extra:
                fields["work_history"] = mine + extra
            moved = self._conn.execute("UPDATE timeline SET candidate_id = ? WHERE candidate_id = ?",
                                       (keep_id, other_id)).rowcount
            self._conn.execute("UPDATE suggestions SET candidate_id = ? WHERE candidate_id = ?", (keep_id, other_id))
            jobs: list[str] = []
            for app in self._rows("SELECT a.*, j.title FROM applications a JOIN jobs j ON j.id = a.job_id"
                                  " WHERE a.candidate_id = ?", (other_id,)):
                ours = self._row("SELECT * FROM applications WHERE candidate_id = ? AND job_id = ?",
                                 (keep_id, app["job_id"]))
                jobs.append(app["title"])
                if ours is None:
                    self._conn.execute("UPDATE applications SET candidate_id = ? WHERE id = ?", (keep_id, app["id"]))
                    continue
                pos = {s["name"].lower(): s["position"] for s in self.job_stages(app["job_id"])}
                if pos.get(app["stage"].lower(), -1) > pos.get(ours["stage"].lower(), -1):
                    self._conn.execute(
                        "UPDATE applications SET stage = ?, stage_changed_at = ?, notes = coalesce(notes, ?)"
                        " WHERE id = ?", (app["stage"], app["stage_changed_at"], app["notes"], ours["id"]))
            # other's email and LinkedIn URL are unique, so other goes before keep takes them.
            self._conn.execute("DELETE FROM candidates WHERE id = ?", (other_id,))
            created = min(krow["created_at"], orow["created_at"])
            if created != krow["created_at"]:
                self._conn.execute("UPDATE candidates SET created_at = ? WHERE id = ?", (created, keep_id))
            old = {k: (mine if k == "work_history" else krow.get(k)) for k in fields}
            if fields:
                self._apply(keep_id, fields)
            filled = [k for k in fields if k not in ("tags", "work_history")]
            bits = [f"filled {_labels(filled)}"] if filled else []
            if "tags" in fields:
                bits.append("added skills")
            if moved:
                bits.append(f"moved {moved} timeline entr{'y' if moved == 1 else 'ies'}")
            if jobs:
                bits.append(f"{len(jobs)} job{'' if len(jobs) == 1 else 's'}")
            body = f"Merged in {other['name']} (id {other_id})" + (": " + ", ".join(bits) if bits else "") + "."
            ts = now_iso()
            by = by if by in CHANGE_SOURCES else "app"
            info = {"id": other_id, "name": other["name"], "email": orow.get("email"),
                    "linkedin_url": orow.get("linkedin_url"), "phone": orow.get("phone"), "timeline": moved,
                    "jobs": jobs}
            self._conn.execute(
                "INSERT INTO timeline(candidate_id, kind, body, at, created_at, source, data) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (keep_id, CHANGE_KIND, body, ts, ts, CHANGE_SOURCES[by],
                 json.dumps({"by": by, "old": old, "new": fields, "merged": info})))
        cand = self.get_candidate(keep_id)
        cand["_merged"] = {"name": other["name"], "filled": filled, "timeline": moved, "jobs": jobs}
        return cand

    def recent_candidates(self, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
        return [self._hydrate(r) for r in self._rows(
            "SELECT * FROM candidates ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )]

    def count_candidates(self) -> int:
        return self._row("SELECT count(*) AS n FROM candidates")["n"]

    def all_candidates(self) -> Iterable[dict[str, Any]]:
        last = 0
        while True:
            batch = self._rows("SELECT * FROM candidates WHERE id > ? ORDER BY id LIMIT 500", (last,))
            if not batch:
                return
            yield from (self._hydrate(r) for r in batch)
            last = batch[-1]["id"]

    def list_tags(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT tag, count(*) AS count FROM candidate_tags GROUP BY tag COLLATE NOCASE"
            " ORDER BY count DESC, tag COLLATE NOCASE LIMIT ?",
            (limit,),
        )

    # ---------------------------------------------------------- work history
    def _write_history(self, candidate_id: int, entries: list[dict[str, Any]]) -> None:
        clean = []
        for i, e in enumerate(entries or []):
            if not isinstance(e, dict):
                raise ValueError("Each work history entry needs title and/or company.")
            row = {k: _clean(e.get(k)) for k in ("title", "company", "start", "end", "summary")}
            if not (row["title"] or row["company"]):
                continue
            clean.append(row)
        self._conn.execute("DELETE FROM work_history WHERE candidate_id = ?", (candidate_id,))
        self._conn.executemany(
            'INSERT INTO work_history(candidate_id, position, title, company, start, "end", summary)'
            " VALUES (?, ?, ?, ?, ?, ?, ?)",
            [(candidate_id, i, r["title"], r["company"], r["start"], r["end"], r["summary"]) for i, r in enumerate(clean)],
        )
        self._conn.execute("UPDATE candidates SET history_text = ? WHERE id = ?", (_history_text(clean), candidate_id))

    def work_history(self, candidate_id: int) -> list[dict[str, Any]]:
        return self._rows(
            'SELECT title, company, start, "end", summary FROM work_history WHERE candidate_id = ? ORDER BY position',
            (candidate_id,),
        )

    # -------------------------------------------------------------- timeline
    def log(self, candidate_id: int, kind: str, body: str | None = None, *, at: str | datetime | None = None,
            job_id: int | None = None, source: str | None = None) -> dict[str, Any]:
        """Add a note, call, email, message, meeting or interview to the candidate's timeline.

        Logging the same thing twice (same kind, time and text) is a no-op, so
        an AI re-reading an inbox doesn't double up.
        """
        kind = (kind or "").strip().lower()
        if kind not in TIMELINE_KINDS or kind in ("stage", CHANGE_KIND):
            raise ValueError(f"kind must be one of {', '.join(k for k in TIMELINE_KINDS if k != 'stage')}")
        body = _clean(body)
        if kind == "note" and not body:
            raise ValueError("A note needs some text.")
        self.get_candidate(candidate_id)
        if job_id is not None:
            self.get_job(job_id)
        when = parse_when(at)
        with self._lock, self._conn:
            dupe = self._conn.execute(
                "SELECT id FROM timeline WHERE candidate_id = ? AND kind = ? AND at = ? AND coalesce(body, '') = ?",
                (candidate_id, kind, when, body or ""),
            ).fetchone()
            if dupe:
                entry_id = dupe["id"]
            else:
                entry_id = self._conn.execute(
                    "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at, source)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (candidate_id, job_id, kind, body, when, now_iso(), _clean(source)),
                ).lastrowid
                if kind in CONTACT_KINDS and when <= now_iso():
                    self._conn.execute(
                        "UPDATE candidates SET last_contacted_at = ? WHERE id = ?"
                        " AND (last_contacted_at IS NULL OR last_contacted_at < ?)",
                        (when, candidate_id, when),
                    )
        return self._row("SELECT * FROM timeline WHERE id = ?", (entry_id,))

    def timeline(self, candidate_id: int, limit: int = 50) -> list[dict[str, Any]]:
        rows = self._rows(
            """SELECT t.*, j.title AS job_title FROM timeline t LEFT JOIN jobs j ON j.id = t.job_id
               WHERE t.candidate_id = ? ORDER BY t.at DESC, t.id DESC LIMIT ?""",
            (candidate_id, limit),
        )
        for r in rows:
            r["data"] = json.loads(r["data"]) if r.get("data") else None
            r["undoable"] = bool(r["kind"] in (CHANGE_KIND, "stage") and r["data"]
                                 and (r["data"].get("old") or r["data"].get("pipeline"))
                                 and not r["data"].get("undone_at") and not r["data"].get("merged"))
            if r["kind"] == "interview":
                r["status"] = interview_status(r["data"])
        return rows

    def upcoming_interviews(self, days: int = 7, now: datetime | None = None) -> list[dict[str, Any]]:
        """Scheduled interviews from two hours ago to `days` ahead. Cancelled ones, and ones with an outcome
        already recorded, are left out."""
        now = now or datetime.now(timezone.utc)
        start = (now - timedelta(hours=2)).replace(microsecond=0).isoformat()
        end = (now + timedelta(days=days)).replace(microsecond=0).isoformat()
        return self._rows(
            f"""SELECT t.*, c.name AS candidate_name, j.title AS job_title
               FROM timeline t JOIN candidates c ON c.id = t.candidate_id LEFT JOIN jobs j ON j.id = t.job_id
               WHERE t.kind = 'interview' AND t.at >= ? AND t.at <= ? AND {LIVE_INTERVIEW} ORDER BY t.at""",
            (start, end),
        )

    def get_interview(self, entry_id: int, candidate_id: int | None = None) -> dict[str, Any]:
        row = self._row("SELECT t.*, c.name AS candidate_name FROM timeline t JOIN candidates c ON c.id = t.candidate_id"
                        " WHERE t.id = ? AND t.kind = 'interview'", (entry_id,))
        if row is None or (candidate_id is not None and row["candidate_id"] != candidate_id):
            raise NotFound(f"No interview with id {entry_id}" + (" on this profile" if candidate_id else ""))
        row["data"] = json.loads(row["data"]) if row.get("data") else {}
        row["status"] = interview_status(row["data"])
        return row

    def update_interview(self, entry_id: int, *, at: str | datetime | None = None, cancel: bool = False,
                         restore: bool = False, outcome: str | None = None, note: str | None = None,
                         candidate_id: int | None = None, by: str = "app") -> dict[str, Any]:
        """Reschedule, cancel (it stays on the timeline, marked cancelled), restore, or record how it went.

        outcome: passed, not passed or no show, with an optional note. Nothing is ever deleted; the
        old time is kept in the entry's data as `rescheduled_from`.
        """
        iv = self.get_interview(entry_id, candidate_id)
        data = dict(iv["data"])
        ts = now_iso()
        new_at = iv["at"]
        if at is not None and str(at).strip():
            new_at = parse_when(at)
            if new_at != iv["at"]:
                data["rescheduled_from"] = [*data.get("rescheduled_from", []), iv["at"]]
                data["rescheduled_at"] = ts
            data.pop("cancelled_at", None)
            if data.get("status") == "cancelled":
                data["status"] = "scheduled"
        if cancel:
            data["status"], data["cancelled_at"] = "cancelled", ts
        elif restore:
            data["status"] = "scheduled"
            data.pop("cancelled_at", None)
        if outcome is not None:
            key = re.sub(r"[\s_-]+", " ", outcome.strip().lower())
            value = INTERVIEW_OUTCOMES.get(key)
            if value is None:
                raise ValueError("The outcome must be passed, not passed or no show.")
            data["outcome"], data["outcome_at"] = value, ts
            if data.get("status") == "cancelled":
                data["status"] = "scheduled"
        if note is not None:
            data["outcome_note"] = _clean(note)
        if not (at or cancel or restore or outcome is not None or note is not None):
            raise ValueError("Say what to change: a new time, cancel, or an outcome.")
        data["updated_by"] = CHANGE_SOURCES.get(by, by)
        with self._lock, self._conn:
            self._conn.execute("UPDATE timeline SET at = ?, data = ? WHERE id = ?",
                               (new_at, json.dumps(data), entry_id))
            if outcome is not None and data["outcome"] != "no_show" and new_at <= ts:
                self._conn.execute(
                    "UPDATE candidates SET last_contacted_at = ? WHERE id = ?"
                    " AND (last_contacted_at IS NULL OR last_contacted_at < ?)", (new_at, iv["candidate_id"], new_at))
        return self.get_interview(entry_id)

    # ---------------------------------------------------------------- search
    def lookup_exact(self, query: str | None) -> tuple[bool, dict[str, Any] | None]:
        """(looks_like_id, candidate) for a query that is an email address or a LinkedIn URL."""
        q = (query or "").strip()
        if _EMAIL_RE.match(q):
            return True, self.find_duplicate(email=q)
        if _LINKEDIN_RE.match(q):
            return True, self.find_duplicate(linkedin_url=q)
        return False, None

    def search_candidates(
        self,
        query: str = "",
        *,
        location: str | None = None,
        title: str | None = None,
        tag: str | None = None,
        limit: int = 25,
        offset: int = 0,
    ) -> SearchResult:
        """Ranked full-text search, best matches first; empty query lists most recent."""
        where: list[str] = []
        params: list[Any] = []
        filters = {}
        if location:
            where.append("c.location LIKE ?")
            params.append(f"%{location.strip()}%")
            filters["location"] = location
        if title:
            where.append("c.current_title LIKE ?")
            params.append(f"%{title.strip()}%")
            filters["title"] = title
        if tag:
            where.append("c.id IN (SELECT candidate_id FROM candidate_tags WHERE tag = ?)")
            params.append(tag.strip())
            filters["tag"] = tag

        is_id, person = self.lookup_exact(query)
        if is_id:  # emails and LinkedIn URLs aren't in the word index; look them up directly
            hits = [SearchHit(candidate=person)] if person else []
            return SearchResult(hits=hits, total=len(hits), query=query, filters=filters, exact=bool(person))

        match = fts_query(query or "")
        if match is None:
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = self._row(f"SELECT count(*) AS n FROM candidates c {clause}", params)["n"]
            rows = self._rows(
                f"SELECT c.* FROM candidates c {clause} ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            hits = [SearchHit(candidate=self._hydrate(r)) for r in rows]
            return SearchResult(hits=hits, total=total, query=query, filters=filters)

        clause = " AND ".join(["candidates_fts MATCH ?", *where])
        base = f"FROM candidates_fts JOIN candidates c ON c.id = candidates_fts.rowid WHERE {clause}"
        total = self._row(f"SELECT count(*) AS n {base}", (match, *params))["n"]
        closest = False
        if total == 0 and len(_query_groups(query)) > 1:
            # Nobody has every word: show the people who have the most of them.
            match, closest = fts_query(query, any_word=True), True
            total = self._row(f"SELECT count(*) AS n {base}", (match, *params))["n"]
        rows = self._rows(
            # column weights: name, title, company, location, about, tags, past roles
            f"""SELECT c.*,
                       snippet(candidates_fts, 4, char(2), char(3), '…', 16) AS _about_snip,
                       snippet(candidates_fts, -1, char(2), char(3), '…', 16) AS _snippet,
                       bm25(candidates_fts, 10.0, 5.0, 3.0, 2.0, 1.0, 3.0, 2.0) AS _score
                {base}
                ORDER BY _score LIMIT ? OFFSET ?""",
            (match, *params, limit, offset),
        )
        hits = []
        for r in rows:
            about_snip, snippet = r.pop("_about_snip"), r.pop("_snippet")
            if HL_START in (about_snip or ""):
                snippet = about_snip  # the About text is the most useful context to show
            score = r.pop("_score")
            hits.append(SearchHit(candidate=self._hydrate(r), snippet=snippet if HL_START in (snippet or "") else None,
                                  score=score))
        return SearchResult(hits=hits, total=total, query=query, filters=filters, closest=closest and total > 0)

    # ------------------------------------------------------------------ jobs
    def add_job(self, title: str, company: str | None = None, location: str | None = None,
                description: str | None = None, status: str = "open",
                stages: list[str | dict[str, Any]] | None = None, **extra: Any) -> dict[str, Any]:
        """extra: hiring_manager, hiring_manager_email, openings, target_date, min_active."""
        if not _clean(title):
            raise ValueError("A job needs a title.")
        if status not in JOB_STATUSES:
            raise ValueError(f"status must be one of {', '.join(JOB_STATUSES)}")
        unknown = set(extra) - set(JOB_EXTRA_FIELDS)
        if unknown:
            raise ValueError(f"Unknown job field(s): {', '.join(sorted(unknown))}")
        more = _job_extras(extra)
        if more.get("openings") is None:
            more["openings"] = 1
        plan = _stage_plan(stages) if stages else list(DEFAULT_STAGES)
        ts = now_iso()
        cols = ["title", "company", "location", "description", "status", "created_at", "updated_at", *more]
        with self._lock, self._conn:
            job_id = self._next_id("jobs")
            self._conn.execute(
                f"INSERT INTO jobs (id, {', '.join(cols)}) VALUES (?, {', '.join('?' * len(cols))})",
                (job_id, _clean(title), _clean(company), _clean(location), _clean(description), status, ts, ts,
                 *more.values()),
            )
            self._conn.executemany(
                "INSERT INTO job_stages(job_id, name, position, outcome) VALUES (?, ?, ?, ?)",
                [(job_id, n, i, o) for i, (n, o) in enumerate(plan)],
            )
        return self.get_job(job_id)

    def similar_open_jobs(self, title: str | None, company: str | None,
                          exclude: int | None = None) -> list[dict[str, Any]]:
        """Open jobs with the same title and company (ignoring case), to warn about a duplicate."""
        return self._rows(
            "SELECT * FROM jobs WHERE status = 'open' AND lower(title) = lower(?)"
            " AND lower(coalesce(company, '')) = lower(?) AND id <> ? ORDER BY id",
            (_clean(title) or "", _clean(company) or "", exclude or 0))

    def get_job(self, job_id: int) -> dict[str, Any]:
        row = self._row("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFound(f"No job with id {job_id}")
        return row

    def update_job(self, job_id: int, **data: Any) -> dict[str, Any]:
        allowed = {"title", "company", "location", "description", "status", *JOB_EXTRA_FIELDS}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Unknown job field(s): {', '.join(sorted(unknown))}")
        if "status" in data:
            data = {**data, "status": normalize_job_status(data["status"])}
        if "title" in data and not _clean(data["title"]):
            raise ValueError("A job needs a title.")
        self.get_job(job_id)
        extras = _job_extras({k: v for k, v in data.items() if k in JOB_EXTRA_FIELDS})
        if "openings" in extras and extras["openings"] is None:
            extras["openings"] = 1
        fields = {**{k: _clean(v) for k, v in data.items() if k not in JOB_EXTRA_FIELDS}, **extras}
        if fields:
            fields["updated_at"] = now_iso()
            sets = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._conn:
                self._conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
        return self.get_job(job_id)

    def delete_job(self, job_id: int) -> None:
        """App window only: remove a job, its stages and its pipeline. The candidates stay.

        Timeline entries keep their text but lose the job link; to-dos about a
        candidate stay, without the job. The AI has no tool for this.
        """
        with self._lock, self._conn:
            self._conn.execute("UPDATE suggestions SET job_id = NULL WHERE job_id = ? AND candidate_id IS NOT NULL",
                               (job_id,))
            cur = self._conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        if cur.rowcount == 0:
            raise NotFound(f"No job with id {job_id}")

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT j.*,
                        count(a.id) AS pipeline_count,
                        sum(CASE WHEN s.outcome = 'open' THEN 1 ELSE 0 END) AS active_count,
                        sum(CASE WHEN s.outcome = 'hired' THEN 1 ELSE 0 END) AS hired_count
                 FROM jobs j
                 LEFT JOIN applications a ON a.job_id = j.id
                 LEFT JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)"""
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE j.status = ?"
            params = (status,)
        sql += " GROUP BY j.id ORDER BY CASE j.status WHEN 'open' THEN 0 WHEN 'on_hold' THEN 1 ELSE 2 END, j.updated_at DESC"
        rows = self._rows(sql, params)
        counts = self._stage_counts()
        for r in rows:
            r["active_count"] = r["active_count"] or 0
            r["hired_count"] = r["hired_count"] or 0
            r["stage_counts"] = [
                {**s, "count": counts.get((r["id"], s["name"].lower()), 0)} for s in self.job_stages(r["id"])
            ]
        return rows

    def _stage_counts(self) -> dict[tuple[int, str], int]:
        return {(r["job_id"], r["stage"].lower()): r["n"] for r in self._rows(
            "SELECT job_id, stage, count(*) AS n FROM applications GROUP BY job_id, lower(stage)")}

    # ---------------------------------------------------------------- stages
    def job_stages(self, job_id: int) -> list[dict[str, Any]]:
        return self._rows("SELECT id, name, position, outcome FROM job_stages WHERE job_id = ? ORDER BY position",
                          (job_id,))

    def resolve_stage(self, job_id: int, name: str | None) -> dict[str, Any]:
        """Case-insensitive stage lookup; None means the job's first stage."""
        stages = self.job_stages(job_id)
        if not stages:
            raise ValueError("This job has no stages.")
        if not _clean(name):
            return stages[0]
        key = _stage_key(name)
        for s in stages:
            if _stage_key(s["name"]) == key:
                return s
        # An unambiguous start of a name is enough: "tech" finds "Technical interview".
        starts = [s for s in stages if key and _stage_key(s["name"]).startswith(key)]
        if len(starts) == 1:
            return starts[0]
        listed = ", ".join(s["name"] for s in stages)
        if starts:
            raise ValueError(f"{name!r} could mean {' or '.join(s['name'] for s in starts)}. "
                             f"Use the full name. This job's stages are: {listed}.")
        raise ValueError(f"No stage called {name!r} on this job. Its stages are: {listed}.")

    def add_stage(self, job_id: int, name: str, *, after: str | None = None,
                  outcome: str | None = None) -> list[dict[str, Any]]:
        """Add a stage after the named one, or before the closing stages. Outcome defaults from the name."""
        name = _clean(name)
        if not name:
            raise ValueError("A stage needs a name.")
        outcome = normalize_outcome(outcome) or stage_outcome(name)
        if outcome not in STAGE_OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(STAGE_OUTCOMES)}")
        self.get_job(job_id)
        stages = self.job_stages(job_id)
        if any(s["name"].lower() == name.lower() for s in stages):
            raise ValueError(f"This job already has a stage called {name}.")
        if after:
            pos = self.resolve_stage(job_id, after)["position"] + 1
        else:
            # Before the first closing stage, so new working stages land in the flow;
            # a new closing stage goes at the end.
            closing = [s["position"] for s in stages if s["outcome"] != "open"]
            pos = min(closing) if closing and outcome == "open" else len(stages)
        with self._lock, self._conn:
            self._conn.execute("UPDATE job_stages SET position = position + 1 WHERE job_id = ? AND position >= ?",
                               (job_id, pos))
            self._conn.execute("INSERT INTO job_stages(job_id, name, position, outcome) VALUES (?, ?, ?, ?)",
                               (job_id, name, pos, outcome))
        self._touch_job(job_id)
        return self.job_stages(job_id)

    def rename_stage(self, job_id: int, old: str, new: str, outcome: str | None = None) -> list[dict[str, Any]]:
        """Rename a stage (its candidates move with it). An in-play stage with in-play stages after it
        can't take a closing name like "Passed" unless `outcome` says what it should count as."""
        stage = self.resolve_stage(job_id, old)
        new = _clean(new)
        if not new:
            raise ValueError("A stage needs a name.")
        outcome = normalize_outcome(outcome)
        stages = self.job_stages(job_id)
        if new.lower() != stage["name"].lower() and any(s["name"].lower() == new.lower() for s in stages):
            raise ValueError(f"This job already has a stage called {new}.")
        implied = stage_outcome(new)
        if outcome is None and stage["outcome"] == "open" and implied != "open":
            if any(s["position"] > stage["position"] and s["outcome"] == "open" for s in stages):
                word = "a hire" if implied == "hired" else "out"
                raise ValueError(
                    f"{new} sounds like {word}, but {stage['name']} is a working stage with more stages after it. "
                    f"Say what {new} should count as (open, hired or lost; in the app, change Counts as first), "
                    "or pick another name.")
            outcome = implied  # the last working stage can become a closing one, as its new name says
        with self._lock, self._conn:
            self._conn.execute("UPDATE job_stages SET name = ?, outcome = ? WHERE id = ?",
                               (new, outcome or stage["outcome"], stage["id"]))
            self._conn.execute("UPDATE applications SET stage = ? WHERE job_id = ? AND lower(stage) = lower(?)",
                               (new, job_id, stage["name"]))
        self._touch_job(job_id)
        return self.job_stages(job_id)

    def reorder_stages(self, job_id: int, names: list[str]) -> list[dict[str, Any]]:
        stages = self.job_stages(job_id)
        wanted = [self.resolve_stage(job_id, n)["id"] for n in names]
        if len(set(wanted)) != len(wanted):
            raise ValueError("Each stage can only appear once.")
        rest = [s["id"] for s in stages if s["id"] not in wanted]
        with self._lock, self._conn:
            for pos, sid in enumerate(wanted + rest):
                self._conn.execute("UPDATE job_stages SET position = ? WHERE id = ?", (pos, sid))
        self._touch_job(job_id)
        return self.job_stages(job_id)

    def set_stage_outcome(self, job_id: int, name: str, outcome: str) -> list[dict[str, Any]]:
        outcome = normalize_outcome(outcome) or ""
        if outcome not in STAGE_OUTCOMES:
            raise ValueError(f"outcome must be one of {', '.join(STAGE_OUTCOMES)}")
        stage = self.resolve_stage(job_id, name)
        with self._lock, self._conn:
            self._conn.execute("UPDATE job_stages SET outcome = ? WHERE id = ?", (outcome, stage["id"]))
        return self.job_stages(job_id)

    def remove_empty_stage(self, job_id: int, name: str) -> list[dict[str, Any]]:
        """App window only: drop a stage nobody is in. Refuses if it would lose anyone's place."""
        stage = self.resolve_stage(job_id, name)
        n = self._row("SELECT count(*) AS n FROM applications WHERE job_id = ? AND lower(stage) = lower(?)",
                      (job_id, stage["name"]))["n"]
        if n:
            raise ValueError(f"{stage['name']} still has {n} candidate(s). Move them first.")
        if len(self.job_stages(job_id)) <= 1:
            raise ValueError("A job needs at least one stage.")
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM job_stages WHERE id = ?", (stage["id"],))
        return self.reorder_stages(job_id, [s["name"] for s in self.job_stages(job_id)])

    def _touch_job(self, job_id: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("UPDATE jobs SET updated_at = ? WHERE id = ?", (now_iso(), job_id))

    # -------------------------------------------------------------- pipeline
    def add_to_pipeline(self, candidate_id: int, job_id: int, stage: str | None = None,
                        notes: str | None = None) -> dict[str, Any]:
        self.get_candidate(candidate_id)
        self.get_job(job_id)
        s = self.resolve_stage(job_id, stage)
        ts = now_iso()
        with self._lock, self._conn:
            existing = self._conn.execute(
                "SELECT id FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id)
            ).fetchone()
            if existing:
                app_id = existing["id"]
            else:
                cur = self._conn.execute(
                    "INSERT INTO applications (candidate_id, job_id, stage, notes, created_at, stage_changed_at)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (candidate_id, job_id, s["name"], _clean(notes), ts, ts),
                )
                app_id = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at) VALUES (?, ?, 'stage', ?, ?, ?)",
                    (candidate_id, job_id, s["name"], ts, ts),
                )
                if _clean(notes):
                    self._conn.execute(
                        "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at) VALUES (?, ?, 'note', ?, ?, ?)",
                        (candidate_id, job_id, _clean(notes), ts, ts),
                    )
        return self.get_application(app_id)

    def move_stage(self, candidate_id: int, job_id: int, stage: str, notes: str | None = None) -> dict[str, Any]:
        app = self._row(
            "SELECT * FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id)
        )
        if app is None:
            raise NotFound("That candidate isn't in this job's pipeline yet. Add them first.")
        s = self.resolve_stage(job_id, stage)
        ts = now_iso()
        with self._lock, self._conn:
            if s["name"] != app["stage"]:
                self._conn.execute(
                    "UPDATE applications SET stage = ?, stage_changed_at = ? WHERE id = ?", (s["name"], ts, app["id"])
                )
                self._conn.execute(
                    "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at) VALUES (?, ?, 'stage', ?, ?, ?)",
                    (candidate_id, job_id, s["name"], ts, ts),
                )
                if s["name"].lower() == "contacted":
                    self._conn.execute(
                        "UPDATE candidates SET last_contacted_at = ? WHERE id = ?"
                        " AND (last_contacted_at IS NULL OR last_contacted_at < ?)", (ts, candidate_id, ts))
            if _clean(notes):
                self._conn.execute("UPDATE applications SET notes = ? WHERE id = ?", (_clean(notes), app["id"]))
                self._conn.execute(
                    "INSERT INTO timeline(candidate_id, job_id, kind, body, at, created_at) VALUES (?, ?, 'note', ?, ?, ?)",
                    (candidate_id, job_id, _clean(notes), ts, ts),
                )
        return self.get_application(app["id"])

    def get_application(self, app_id: int) -> dict[str, Any]:
        row = self._row(
            """SELECT a.*, c.name AS candidate_name, j.title AS job_title
               FROM applications a JOIN candidates c ON c.id = a.candidate_id JOIN jobs j ON j.id = a.job_id
               WHERE a.id = ?""",
            (app_id,),
        )
        if row is None:
            raise NotFound(f"No pipeline entry {app_id}")
        return row

    def pipeline(self, job_id: int) -> list[dict[str, Any]]:
        self.get_job(job_id)
        return self._rows(
            """SELECT a.*, c.name AS candidate_name, c.current_title, c.current_company, c.location,
                      c.last_contacted_at, c.email, c.linkedin_url
               FROM applications a JOIN candidates c ON c.id = a.candidate_id
               WHERE a.job_id = ? ORDER BY a.stage_changed_at DESC""",
            (job_id,),
        )

    def pipeline_by_stage(self, job_id: int) -> list[dict[str, Any]]:
        """[{stage..., candidates: [...]}] in the job's stage order. Unknown stages are kept, at the end."""
        stages = self.job_stages(job_id)
        by = {s["name"].lower(): {**s, "candidates": []} for s in stages}
        for a in self.pipeline(job_id):
            key = a["stage"].lower()
            if key not in by:
                by[key] = {"id": None, "name": a["stage"], "position": 999, "outcome": "open", "candidates": []}
            by[key]["candidates"].append(a)
        return sorted(by.values(), key=lambda s: s["position"])

    def candidate_pipelines(self, candidate_id: int) -> list[dict[str, Any]]:
        return self._rows(
            """SELECT a.*, j.title AS job_title, j.company AS job_company
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.candidate_id = ? ORDER BY a.stage_changed_at DESC""",
            (candidate_id,),
        )

    def open_applications(self) -> list[dict[str, Any]]:
        """Applications on open jobs that are still in play."""
        return self._rows(
            """SELECT a.*, c.name AS candidate_name, j.title AS job_title, s.position AS stage_position
               FROM applications a
               JOIN candidates c ON c.id = a.candidate_id
               JOIN jobs j ON j.id = a.job_id
               JOIN job_stages s ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
               WHERE j.status = 'open' AND s.outcome = 'open'
               ORDER BY a.stage_changed_at"""
        )

    def next_stage(self, job_id: int, stage: str) -> str | None:
        stages = self.job_stages(job_id)
        cur = self.resolve_stage(job_id, stage)
        later = [s for s in stages if s["position"] > cur["position"] and s["outcome"] != "lost"]
        return later[0]["name"] if later else None

    # ----------------------------------------------------------- suggestions
    def add_suggestion(self, text: str, candidate_id: int | None = None, job_id: int | None = None,
                       source: str | None = None, due_on: str | None = None,
                       note: str | None = None) -> dict[str, Any]:
        """A to-do on Today. due_on (YYYY-MM-DD) hides it until that day; note is what was logged with it."""
        text = _clean(text)
        if not text:
            raise ValueError("A suggestion needs some text.")
        if candidate_id is not None:
            self.get_candidate(candidate_id)
        if job_id is not None:
            self.get_job(job_id)
        due_on = parse_day_text(due_on)
        with self._lock, self._conn:
            dupe = self._conn.execute(
                "SELECT id FROM suggestions WHERE done_at IS NULL AND text = ? AND coalesce(candidate_id, 0) = ?"
                " AND coalesce(job_id, 0) = ?", (text, candidate_id or 0, job_id or 0)).fetchone()
            if dupe:
                sid = dupe["id"]
                if due_on or _clean(note):
                    self._conn.execute(
                        "UPDATE suggestions SET due_on = coalesce(?, due_on), note = coalesce(?, note) WHERE id = ?",
                        (due_on, _clean(note), sid))
            else:
                sid = self._conn.execute(
                    "INSERT INTO suggestions(candidate_id, job_id, text, created_at, source, due_on, note)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?)",
                    (candidate_id, job_id, text, now_iso(), _clean(source), due_on, _clean(note)),
                ).lastrowid
        return self._row("SELECT * FROM suggestions WHERE id = ?", (sid,))

    def open_suggestions(self, job_id: int | None = None) -> list[dict[str, Any]]:
        if job_id is not None:
            return self._rows(
                """SELECT s.*, c.name AS candidate_name, j.title AS job_title FROM suggestions s
                   LEFT JOIN candidates c ON c.id = s.candidate_id LEFT JOIN jobs j ON j.id = s.job_id
                   WHERE s.done_at IS NULL AND s.job_id = ? ORDER BY coalesce(s.due_on, s.created_at)""", (job_id,))
        return self._rows(
            """SELECT s.*, c.name AS candidate_name, j.title AS job_title FROM suggestions s
               LEFT JOIN candidates c ON c.id = s.candidate_id LEFT JOIN jobs j ON j.id = s.job_id
               WHERE s.done_at IS NULL ORDER BY s.created_at"""
        )

    def complete_suggestion(self, suggestion_id: int) -> dict[str, Any]:
        with self._lock, self._conn:
            cur = self._conn.execute("UPDATE suggestions SET done_at = coalesce(done_at, ?) WHERE id = ?",
                                     (now_iso(), suggestion_id))
        if cur.rowcount == 0:
            raise NotFound(f"No suggestion with id {suggestion_id}")
        return self._row("SELECT * FROM suggestions WHERE id = ?", (suggestion_id,))

    # --------------------------------------------------------------- snoozes
    def snooze(self, key: str, days: int = 7) -> None:
        until = (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()
        with self._lock, self._conn:
            self._conn.execute(
                "INSERT INTO snoozes(key, until) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET until = excluded.until",
                (key, until))

    def snoozed_keys(self) -> set[str]:
        return {r["key"] for r in self._rows("SELECT key FROM snoozes WHERE until > ?", (now_iso(),))}

    def stale_candidates(self, days: int, limit: int = 50) -> list[dict[str, Any]]:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()
        rows = self._rows(
            """SELECT c.*, (SELECT count(*) FROM applications a JOIN job_stages s
                              ON s.job_id = a.job_id AND lower(s.name) = lower(a.stage)
                            WHERE a.candidate_id = c.id AND s.outcome = 'open') AS active_pipelines
               FROM candidates c WHERE c.updated_at < ?
               ORDER BY active_pipelines DESC, c.updated_at LIMIT ?""",
            (cutoff, limit),
        )
        return [self._hydrate(r) for r in rows]

    # --------------------------------------------------------------- outbox
    def outbox_batch(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM search_outbox ORDER BY seq LIMIT ?", (limit,))

    def outbox_ack(self, up_to_seq: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM search_outbox WHERE seq <= ?", (up_to_seq,))

    def outbox_clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM search_outbox")


JOB_EXTRA_FIELDS = ("hiring_manager", "hiring_manager_email", "openings", "target_date", "min_active")


def _count(value: Any, label: str) -> int | None:
    """A whole number of 1 or more; blank is None."""
    if value is None or (isinstance(value, str) and not value.strip()):
        return None
    try:
        n = int(str(value).strip())
    except ValueError:
        raise ValueError(f"{label} must be a whole number, like 2.") from None
    if n < 1:
        raise ValueError(f"{label} must be 1 or more.")
    return n


def parse_day_text(value: Any) -> str | None:
    """'2026-12-01', '12/1/2026' or 'Dec 1, 2026' -> '2026-12-01'. Blank is None."""
    v = _clean(value) if isinstance(value, str) else value
    if v is None:
        return None
    if isinstance(v, datetime):
        return v.date().isoformat()
    try:
        return datetime.fromisoformat(str(v)[:10]).date().isoformat()
    except ValueError:
        loose = _parse_loose(re.sub(r"\s+", " ", str(v)))
        if loose is None:
            raise ValueError(f"Couldn't read the date {v!r}. Use a form like 2026-12-01 or Dec 1, 2026.") from None
        return loose.date().isoformat()


def _job_extras(data: dict[str, Any]) -> dict[str, Any]:
    """Check and tidy the hiring manager, openings, target date and thin-pipeline target of a job."""
    out: dict[str, Any] = {}
    for k, v in data.items():
        if k == "openings":
            out[k] = _count(v, "Openings")
        elif k == "min_active":
            out[k] = _count(v, "The active candidate target")
        elif k == "target_date":
            out[k] = parse_day_text(v)
        elif k == "hiring_manager_email":
            v = _clean(v)
            if v and not _EMAIL_RE.match(v):
                raise ValueError(f"{v!r} doesn't look like an email address.")
            out[k] = v
        else:
            out[k] = _clean(v)
    return out


# Interviews are timeline entries; their state lives in timeline.data. One with no data is scheduled.
INTERVIEW_OUTCOMES = {"passed": "passed", "pass": "passed", "not passed": "not_passed", "failed": "not_passed",
                      "did not pass": "not_passed", "no show": "no_show", "noshow": "no_show"}
INTERVIEW_OUTCOME_LABELS = {"passed": "Passed", "not_passed": "Not passed", "no_show": "No show"}
LIVE_INTERVIEW = ("coalesce(json_extract(t.data, '$.status'), 'scheduled') <> 'cancelled'"
                  " AND json_extract(t.data, '$.outcome') IS NULL")


def interview_status(data: dict[str, Any] | None) -> str:
    """'scheduled', 'cancelled', or the recorded outcome ('passed', 'not_passed', 'no_show')."""
    data = data or {}
    if data.get("status") == "cancelled":
        return "cancelled"
    return data.get("outcome") or "scheduled"


def _stage_key(name: str | None) -> str:
    """'Phone-screen ' and 'phone screen' compare equal."""
    return re.sub(r"[\W_]+", " ", name or "").strip().lower()


def _stage_plan(names: list[str | dict[str, Any]]) -> list[tuple[str, str]]:
    """Custom stage list for a new job. Closing names (Hired, Placed, Not a fit, Passed...) close the application.
    A stage can also be {"name": "Passed", "outcome": "lost"} to say so outright."""
    out: list[tuple[str, str]] = []
    seen: set[str] = set()
    for item in names:
        n, outcome = (item.get("name"), item.get("outcome")) if isinstance(item, dict) else (item, None)
        n = _clean(str(n or ""))
        if n and n.lower() not in seen:
            seen.add(n.lower())
            out.append((n, normalize_outcome(outcome) or stage_outcome(n)))
    if not out:
        raise ValueError("Give at least one stage name.")
    return out
