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
"""
from __future__ import annotations

import re
import sqlite3
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

STAGES: tuple[str, ...] = (
    "sourced",
    "contacted",
    "screening",
    "interviewing",
    "offer",
    "hired",
    "rejected",
    "withdrawn",
)
CLOSED_STAGES = {"hired", "rejected", "withdrawn"}
JOB_STATUSES = ("open", "on_hold", "closed")

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
)

_MIGRATIONS: tuple[str, ...] = (
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
)


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


class NotFound(LookupError):
    pass


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


def _clean(value: Any) -> Any:
    if isinstance(value, str):
        value = value.strip()
        return value or None
    return value


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
    "sf": ("san francisco",),
    "sfo": ("san francisco",),
    "nyc": ("new york",),
    "ny": ("new york",),
    "la": ("los angeles",),
    "dc": ("washington",),
    "bay": ("bay area",),
    "swe": ("software engineer",),
    "sde": ("software engineer",),
    "pm": ("product manager",),
    "ml": ("machine learning",),
    "ai": ("artificial intelligence",),
    "fe": ("frontend", "front end"),
    "be": ("backend", "back end"),
}


def fts_query(text: str) -> str | None:
    """Turn free text into a safe FTS5 query: every word must match, last word as prefix.

    Users type things like `senior react sf` or `o'brien`; we never pass raw
    syntax through, so stray quotes or operators can't raise errors. Known
    shorthand (sf, nyc, swe...) also matches its long form.
    """
    words = re.findall(r"\w+", text.lower(), flags=re.UNICODE)
    if not words:
        return None
    parts = []
    for i, w in enumerate(words):
        term = f'"{w}"*' if i == len(words) - 1 else f'"{w}"'
        expansions = ALIASES.get(w)
        if expansions:
            term = "(" + " OR ".join([term, *(f'"{e}"' for e in expansions)]) + ")"
        parts.append(term)
    return " AND ".join(parts)


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
        if self.path != ":memory:":
            self._conn.execute("PRAGMA journal_mode = WAL")
            self._conn.execute("PRAGMA synchronous = NORMAL")
        self._migrate()

    # ------------------------------------------------------------------ infra
    def _migrate(self) -> None:
        with self._lock:
            version = self._conn.execute("PRAGMA user_version").fetchone()[0]
            for i, sql in enumerate(_MIGRATIONS[version:], start=version + 1):
                self._conn.executescript("BEGIN;" + sql + f"PRAGMA user_version = {i}; COMMIT;")

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    def _rows(self, sql: str, params: Iterable[Any] = ()) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(r) for r in self._conn.execute(sql, tuple(params)).fetchall()]

    def _row(self, sql: str, params: Iterable[Any] = ()) -> dict[str, Any] | None:
        rows = self._rows(sql, params)
        return rows[0] if rows else None

    # ------------------------------------------------------------- candidates
    def _prepare(self, data: dict[str, Any]) -> dict[str, Any]:
        unknown = set(data) - set(CANDIDATE_FIELDS)
        if unknown:
            raise ValueError(f"Unknown candidate field(s): {', '.join(sorted(unknown))}")
        out = {k: _clean(v) for k, v in data.items()}
        if "tags" in out and isinstance(out["tags"], (list, tuple)):
            out["tags"] = ", ".join(t.strip() for t in out["tags"] if t and t.strip()) or None
        if "email" in out and out["email"]:
            out["email"] = out["email"].lower()
        if "linkedin_url" in out:
            out["linkedin_url"] = normalize_linkedin(out["linkedin_url"])
        return out

    def add_candidate(self, **data: Any) -> dict[str, Any]:
        fields = self._prepare(data)
        if not fields.get("name"):
            raise ValueError("A candidate needs a name.")
        ts = now_iso()
        fields["created_at"] = fields["updated_at"] = ts
        cols = ", ".join(fields)
        marks = ", ".join("?" for _ in fields)
        with self._lock, self._conn:
            cur = self._conn.execute(
                f"INSERT INTO candidates ({cols}) VALUES ({marks})", tuple(fields.values())
            )
            new_id = cur.lastrowid
        return self.get_candidate(new_id)

    def find_duplicate(self, email: str | None = None, linkedin_url: str | None = None) -> dict[str, Any] | None:
        email = _clean(email)
        if email:
            hit = self._row("SELECT * FROM candidates WHERE lower(email) = lower(?)", (email,))
            if hit:
                return hit
        li = normalize_linkedin(linkedin_url)
        if li:
            return self._row("SELECT * FROM candidates WHERE linkedin_url = ?", (li,))
        return None

    def upsert_candidate(self, **data: Any) -> tuple[dict[str, Any], bool]:
        """Add, or merge into an existing candidate matched by email or LinkedIn.

        Only non-empty incoming values overwrite, so an AI client re-saving a
        sparse profile never blanks out fields a recruiter filled in.
        Returns (candidate, created).
        """
        existing = self.find_duplicate(data.get("email"), data.get("linkedin_url"))
        if existing is None:
            return self.add_candidate(**data), True
        changes = {k: v for k, v in data.items() if _clean(v) not in (None, [], ())}
        return self.update_candidate(existing["id"], **changes), False

    def get_candidate(self, candidate_id: int) -> dict[str, Any]:
        row = self._row("SELECT * FROM candidates WHERE id = ?", (candidate_id,))
        if row is None:
            raise NotFound(f"No candidate with id {candidate_id}")
        return row

    def update_candidate(self, candidate_id: int, **data: Any) -> dict[str, Any]:
        fields = self._prepare(data)
        if "name" in fields and not fields["name"]:
            raise ValueError("A candidate needs a name.")
        self.get_candidate(candidate_id)
        if fields:
            fields["updated_at"] = now_iso()
            sets = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._conn:
                self._conn.execute(
                    f"UPDATE candidates SET {sets} WHERE id = ?", (*fields.values(), candidate_id)
                )
        return self.get_candidate(candidate_id)

    def delete_candidate(self, candidate_id: int) -> None:
        with self._lock, self._conn:
            cur = self._conn.execute("DELETE FROM candidates WHERE id = ?", (candidate_id,))
        if cur.rowcount == 0:
            raise NotFound(f"No candidate with id {candidate_id}")

    def recent_candidates(self, limit: int = 25, offset: int = 0) -> list[dict[str, Any]]:
        return self._rows(
            "SELECT * FROM candidates ORDER BY updated_at DESC, id DESC LIMIT ? OFFSET ?",
            (limit, offset),
        )

    def count_candidates(self) -> int:
        return self._row("SELECT count(*) AS n FROM candidates")["n"]

    def all_candidates(self) -> Iterable[dict[str, Any]]:
        last = 0
        while True:
            batch = self._rows("SELECT * FROM candidates WHERE id > ? ORDER BY id LIMIT 500", (last,))
            if not batch:
                return
            yield from batch
            last = batch[-1]["id"]

    def search_candidates(
        self,
        query: str = "",
        *,
        location: str | None = None,
        title: str | None = None,
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

        match = fts_query(query or "")
        if match is None:
            clause = ("WHERE " + " AND ".join(where)) if where else ""
            total = self._row(f"SELECT count(*) AS n FROM candidates c {clause}", params)["n"]
            rows = self._rows(
                f"SELECT c.* FROM candidates c {clause} ORDER BY c.updated_at DESC, c.id DESC LIMIT ? OFFSET ?",
                (*params, limit, offset),
            )
            hits = [SearchHit(candidate=r) for r in rows]
            return SearchResult(hits=hits, total=total, query=query, filters=filters)

        clause = " AND ".join(["candidates_fts MATCH ?", *where])
        base = f"FROM candidates_fts JOIN candidates c ON c.id = candidates_fts.rowid WHERE {clause}"
        total = self._row(f"SELECT count(*) AS n {base}", (match, *params))["n"]
        rows = self._rows(
            # column weights: name, title, company, location, about, tags
            f"""SELECT c.*,
                       snippet(candidates_fts, 4, char(2), char(3), '…', 16) AS _snippet,
                       bm25(candidates_fts, 10.0, 5.0, 3.0, 2.0, 1.0, 3.0) AS _score
                {base}
                ORDER BY _score LIMIT ? OFFSET ?""",
            (match, *params, limit, offset),
        )
        hits = []
        for r in rows:
            snippet = r.pop("_snippet")
            score = r.pop("_score")
            hits.append(SearchHit(candidate=r, snippet=snippet if HL_START in (snippet or "") else None, score=score))
        return SearchResult(hits=hits, total=total, query=query, filters=filters)

    # ------------------------------------------------------------------ jobs
    def add_job(self, title: str, company: str | None = None, location: str | None = None,
                description: str | None = None, status: str = "open") -> dict[str, Any]:
        if not _clean(title):
            raise ValueError("A job needs a title.")
        if status not in JOB_STATUSES:
            raise ValueError(f"status must be one of {', '.join(JOB_STATUSES)}")
        ts = now_iso()
        with self._lock, self._conn:
            cur = self._conn.execute(
                "INSERT INTO jobs (title, company, location, description, status, created_at, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?)",
                (_clean(title), _clean(company), _clean(location), _clean(description), status, ts, ts),
            )
        return self.get_job(cur.lastrowid)

    def get_job(self, job_id: int) -> dict[str, Any]:
        row = self._row("SELECT * FROM jobs WHERE id = ?", (job_id,))
        if row is None:
            raise NotFound(f"No job with id {job_id}")
        return row

    def update_job(self, job_id: int, **data: Any) -> dict[str, Any]:
        allowed = {"title", "company", "location", "description", "status"}
        unknown = set(data) - allowed
        if unknown:
            raise ValueError(f"Unknown job field(s): {', '.join(sorted(unknown))}")
        if "status" in data and data["status"] not in JOB_STATUSES:
            raise ValueError(f"status must be one of {', '.join(JOB_STATUSES)}")
        self.get_job(job_id)
        fields = {k: _clean(v) for k, v in data.items()}
        if fields:
            fields["updated_at"] = now_iso()
            sets = ", ".join(f"{k} = ?" for k in fields)
            with self._lock, self._conn:
                self._conn.execute(f"UPDATE jobs SET {sets} WHERE id = ?", (*fields.values(), job_id))
        return self.get_job(job_id)

    def list_jobs(self, status: str | None = None) -> list[dict[str, Any]]:
        sql = """SELECT j.*,
                        count(a.id) AS pipeline_count,
                        sum(CASE WHEN a.stage NOT IN ('hired','rejected','withdrawn') THEN 1 ELSE 0 END) AS active_count
                 FROM jobs j LEFT JOIN applications a ON a.job_id = j.id"""
        params: tuple[Any, ...] = ()
        if status:
            sql += " WHERE j.status = ?"
            params = (status,)
        sql += " GROUP BY j.id ORDER BY j.updated_at DESC"
        rows = self._rows(sql, params)
        for r in rows:
            r["active_count"] = r["active_count"] or 0
        return rows

    # -------------------------------------------------------------- pipeline
    def add_to_pipeline(self, candidate_id: int, job_id: int, stage: str = "sourced",
                        notes: str | None = None) -> dict[str, Any]:
        self._check_stage(stage)
        self.get_candidate(candidate_id)
        self.get_job(job_id)
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
                    (candidate_id, job_id, stage, _clean(notes), ts, ts),
                )
                app_id = cur.lastrowid
                self._conn.execute(
                    "INSERT INTO activity (application_id, kind, detail, at) VALUES (?, 'stage', ?, ?)",
                    (app_id, stage, ts),
                )
        return self.get_application(app_id)

    def move_stage(self, candidate_id: int, job_id: int, stage: str, notes: str | None = None) -> dict[str, Any]:
        self._check_stage(stage)
        app = self._row(
            "SELECT * FROM applications WHERE candidate_id = ? AND job_id = ?", (candidate_id, job_id)
        )
        if app is None:
            raise NotFound("That candidate isn't in this job's pipeline yet. Add them first.")
        ts = now_iso()
        with self._lock, self._conn:
            if stage != app["stage"]:
                self._conn.execute(
                    "UPDATE applications SET stage = ?, stage_changed_at = ? WHERE id = ?", (stage, ts, app["id"])
                )
                self._conn.execute(
                    "INSERT INTO activity (application_id, kind, detail, at) VALUES (?, 'stage', ?, ?)",
                    (app["id"], stage, ts),
                )
            if _clean(notes):
                self._conn.execute("UPDATE applications SET notes = ? WHERE id = ?", (_clean(notes), app["id"]))
                self._conn.execute(
                    "INSERT INTO activity (application_id, kind, detail, at) VALUES (?, 'note', ?, ?)",
                    (app["id"], _clean(notes), ts),
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
            """SELECT a.*, c.name AS candidate_name, c.current_title, c.location
               FROM applications a JOIN candidates c ON c.id = a.candidate_id
               WHERE a.job_id = ? ORDER BY a.stage_changed_at DESC""",
            (job_id,),
        )

    def candidate_pipelines(self, candidate_id: int) -> list[dict[str, Any]]:
        return self._rows(
            """SELECT a.*, j.title AS job_title, j.company AS job_company
               FROM applications a JOIN jobs j ON j.id = a.job_id
               WHERE a.candidate_id = ? ORDER BY a.stage_changed_at DESC""",
            (candidate_id,),
        )

    @staticmethod
    def _check_stage(stage: str) -> None:
        if stage not in STAGES:
            raise ValueError(f"stage must be one of {', '.join(STAGES)}")

    # --------------------------------------------------------------- outbox
    def outbox_batch(self, limit: int = 200) -> list[dict[str, Any]]:
        return self._rows("SELECT * FROM search_outbox ORDER BY seq LIMIT ?", (limit,))

    def outbox_ack(self, up_to_seq: int) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM search_outbox WHERE seq <= ?", (up_to_seq,))

    def outbox_clear(self) -> None:
        with self._lock, self._conn:
            self._conn.execute("DELETE FROM search_outbox")
