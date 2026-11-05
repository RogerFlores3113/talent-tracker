"""Getting candidates in and out: CSV import/export and a quick local read of pasted profile text."""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
import unicodedata
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable

from .paths import backups_dir
from .store import (CANDIDATE_FIELDS, CHANGE_SOURCES, FIELD_LABELS, MERGE_OVERWRITES, DuplicateValue,
                    IdentityConflict, Store, normalize_linkedin, parse_when, same_person_name, split_tags,
                    stage_outcome)

EXPORT_FIELDS = (
    "name", "email", "phone", "linkedin_url", "website", "current_title", "current_company", "location",
    "tags", "about", "source", "last_contacted_at", "created_at", "updated_at",
)
EXPORT_HEADERS = {
    "name": "Name", "email": "Email", "phone": "Phone", "linkedin_url": "LinkedIn", "website": "Website",
    "current_title": "Title", "current_company": "Company", "location": "Location", "tags": "Skills",
    "about": "About", "source": "Source", "last_contacted_at": "Last contacted", "created_at": "Added",
    "updated_at": "Last updated", "jobs": "Jobs", "notes": "Notes", "work_history": "Work history",
}
EXPORT_NOTES_KEPT = 20      # newest notes per person in the export's Notes column
EXPORT_NOTES_CHARS = 5000   # and at most this much text

# Header words people use in spreadsheets, mapped to our fields. An exact header match beats a
# partial one, and a longer hint beats a shorter one ("Job Title" is a title before "Role" is).
_HEADER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("email", ("email", "e-mail", "mail", "email address", "e-mail address", "work email", "personal email")),
    ("linkedin_url", ("linkedin", "profile url", "li url", "linkedin url", "linkedin profile")),
    ("phone", ("phone", "mobile", "cell", "tel", "telephone", "phone number", "cellphone")),
    ("website", ("website", "github", "portfolio", "site", "url", "homepage")),
    ("current_title", ("title", "role", "position", "headline", "job title", "current title", "current role",
                       "current position", "job")),
    ("current_company", ("company", "employer", "organization", "organisation", "current company",
                         "current employer", "company name")),
    ("location", ("location", "city", "based", "based in", "country", "region", "state", "province")),
    ("tags", ("skills", "tags", "keywords", "tech", "stack", "tech stack")),
    ("note", ("notes", "note", "comments", "comment", "salary", "salary expectation", "notice", "notice period",
              "feedback", "connected on", "date connected")),
    ("about", ("about", "summary", "bio", "description")),
    ("source", ("source", "found via", "referral", "channel", "where we met", "met at", "how we met", "met via",
                "how found", "found through", "lead source")),
    ("last_contacted_at", ("last contacted", "last contact", "contacted", "last spoke", "last spoken",
                           "last touch", "last reached out")),
    # Pipeline columns: which job a row is for, at which client, and where they are in it.
    ("job_title", ("requisition", "req", "job req", "opening", "job opening", "position applied", "applied for",
                   "role", "job")),
    ("job_company", ("client", "client company", "client name", "hiring company", "account")),
    ("stage", ("status", "stage", "pipeline stage", "pipeline status", "candidate status")),
    # Dates: when the person was first added, and when they reached their stage. Exact headers only, so a
    # "Created by" or "Added by" column is never read as a date.
    ("created_at", ("date added", "added", "added on", "created", "created at", "created on", "date created",
                    "date entered")),
    ("stage_changed_at", ("stage date", "date moved", "last stage change", "stage changed", "stage changed at",
                          "status date", "status changed", "date of last stage change", "moved on")),
)
_EXACT_ONLY = {"created_at", "stage_changed_at"}
# "Role" or "Job" means the job they're up for only on a sheet that also tracks a client or a status.
_PIPELINE_ONLY_HINTS = {"role", "job"}
_MULTI_TARGETS = {"location", "note", "tag_column"}  # several columns may feed these
# A note column with one of these headers holds the note itself; any other note column ("Connected On",
# "Salary") keeps its header in front of the value, so the note still says what it is.
_PLAIN_NOTE_HEADERS = {"notes", "note", "comments", "comment", "feedback", "remarks"}
# A note column holding the day something happened: the note is dated that day.
_DATED_NOTE_HEADERS = {"connected on", "date connected"}
FIRST_NAME = ("first name", "firstname", "given name", "first")
LAST_NAME = ("last name", "lastname", "surname", "family name", "last")
FULL_NAME = ("name", "full name", "candidate", "candidate name", "contact", "contact name")

# "note" is not a candidate field: it becomes a note on the candidate's timeline. "notes" is our own
# export's Notes column (several notes split by " | "). job_title, job_company and stage put the person
# in a job's pipeline; "jobs" is our export's Jobs column ("Title (Company): Stage; ...").
# "tag_column" turns a column like Owner into a skill tag holding the header too ("Owner: Marcus").
# created_at and stage_changed_at date the person and their place in the job.
IMPORT_TARGETS = (("", "name", "first_name", "last_name") + tuple(f for f in CANDIDATE_FIELDS if f != "name")
                  + ("tag_column", "created_at", "note", "notes", "job_title", "job_company", "stage",
                     "stage_changed_at", "jobs"))
TARGET_LABELS = {
    "": "Skip this column", "name": "Full name", "first_name": "First name", "last_name": "Last name",
    "email": "Email", "phone": "Phone", "linkedin_url": "LinkedIn URL", "website": "Website",
    "current_title": "Current title", "current_company": "Current company", "location": "Location",
    "about": "About", "note": "Note on their timeline", "notes": "Notes, split at | (from an export)",
    "tags": "Skills", "source": "Source", "last_contacted_at": "Last contacted",
    "job_title": "Job they're up for", "job_company": "Client (the job's company)", "stage": "Stage in that job",
    "jobs": "Jobs, as exported", "tag_column": "A tag, as Column: value (like Owner: Marcus)",
    "created_at": "Date added", "stage_changed_at": "Date they reached that stage",
}
BACKUPS_KEPT = 10

_RELATIVE_DATE = re.compile(
    r"^(?:today|tonight|now|yesterday|tomorrow|(?:last|next|this) \w+|(?:a|an|\d+|a few|few) "
    r"(?:min(?:ute)?s?|hours?|days?|weeks?|wks?|months?|mos?|years?|yrs?) ago|"
    r"(?:mon|tues?|wed(?:nes)?|thu(?:rs?)?|fri|sat(?:ur)?|sun)(?:day)?)\.?"
    r"(?:,?\s+(?:at\s+)?\d{1,2}(?::\d{2})?\s*(?:[ap]\.?m\.?)?(?:\s+[a-z/_]+)?)?$", re.I)  # 'yesterday 3pm CT'


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def _norm_header(h: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"[_\-:#*]+", " ", (h or "").lower())).strip()


_KNOWN_HEADERS = ({*FIRST_NAME, *FULL_NAME, *LAST_NAME} - {"first", "last", "contact"}) | {
    _norm_header(w) for _, ws in _HEADER_HINTS for w in ws}


def _header_score(line: str) -> int:
    """How many cells of this line read like column headers ('First Name', 'URL', 'Email Address')."""
    best = 0
    for delim in (",", ";", "\t"):
        try:
            cells = next(csv.reader([line], delimiter=delim))
        except (csv.Error, StopIteration):
            continue
        if len(cells) >= 2:
            best = max(best, sum(1 for c in cells if _norm_header(c) in _KNOWN_HEADERS))
    return best


def _skip_preamble(text: str) -> str:
    """LinkedIn's Connections.csv starts with a few 'Notes:' lines before the real header. Drop them."""
    lines = text.splitlines(keepends=True)
    seen = 0
    for i, line in enumerate(lines[:30]):
        if not line.strip():
            continue
        if _header_score(line) >= 2:
            return "".join(lines[i:]) if seen else text
        seen += 1
    return text


def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
    text = _skip_preamble(text)
    try:
        dialect = csv.Sniffer().sniff(text[:4096], delimiters=",;\t")
    except csv.Error:
        dialect = csv.excel
    rows = [r for r in csv.reader(io.StringIO(text), dialect) if any(c.strip() for c in r)]
    if not rows:
        raise ValueError("That file looks empty.")
    header = [h.strip() for h in rows[0]]
    body = [r + [""] * (len(header) - len(r)) for r in rows[1:]]
    return header, body


def looks_like_linkedin_export(header: list[str]) -> bool:
    low = {_norm_header(h) for h in header}
    return {"first name", "last name", "url"} <= low and ("connected on" in low or "position" in low)


def looks_like_our_export(header: list[str]) -> bool:
    return {EXPORT_HEADERS[k] for k in ("name", "jobs", "notes", "created_at", "updated_at")} <= set(header)


def default_source(header: list[str]) -> str:
    return "LinkedIn connections" if looks_like_linkedin_export(header) else "CSV import"


def _hint_score(low: str, hint: str) -> int:
    hint = _norm_header(hint)
    if low == hint:
        return 1000 + len(hint)
    if re.search(rf"(?<![a-z]){re.escape(hint)}(?![a-z])", low):
        return 10 * len(hint) + 5
    return 10 * len(hint) if len(hint) >= 4 and hint in low else 0


def guess_mapping(header: list[str], rows: list[list[str]] | None = None) -> list[str]:
    """For each column, which field it most likely holds ('' = skip).

    Every (column, field) pair gets a score and the best pairs are picked first, so an exact or longer
    header match wins ("Job Title" is the title even when a "Role" column comes first). A column whose
    values are linkedin.com links is the LinkedIn URL whatever its header says.
    """
    lows = [_norm_header(h) for h in header]
    pipeline_sheet = any(_hint_score(low, w) >= 1000 for low in lows
                         for f, ws in _HEADER_HINTS if f in ("job_company", "stage") for w in ws)
    ours = looks_like_our_export(header)
    out = [""] * len(header)
    pairs: list[tuple[int, int, str]] = []
    for col, low in enumerate(lows):
        if low in FIRST_NAME:
            pairs.append((5000, col, "first_name"))
        elif low in LAST_NAME:
            pairs.append((5000, col, "last_name"))
        elif low in FULL_NAME:
            pairs.append((5000, col, "name"))
        if ours and header[col] == EXPORT_HEADERS["jobs"]:
            pairs.append((5000, col, "jobs"))
        if ours and header[col] == EXPORT_HEADERS["notes"]:
            pairs.append((5000, col, "notes"))
        values = [r[col].strip() for r in (rows or [])[:50] if col < len(r) and r[col].strip()]
        if values and sum("linkedin.com/" in v.lower() for v in values) * 2 >= len(values):
            pairs.append((3000, col, "linkedin_url"))
        for f, hints in _HEADER_HINTS:
            for w in hints:
                if f == "job_title" and w in _PIPELINE_ONLY_HINTS and not pipeline_sheet:
                    continue
                if f == "current_title" and w in _PIPELINE_ONLY_HINTS and pipeline_sheet and low == w:
                    continue  # on a pipeline sheet a bare "Role" is the job they're up for
                s = _hint_score(low, w)
                if f in _EXACT_ONLY and s < 1000:
                    continue
                if s:
                    pairs.append((s, col, f))
    used: set[str] = set()
    for score, col, f in sorted(pairs, key=lambda p: (-p[0], p[1])):
        if out[col] or (f in used and f not in _MULTI_TARGETS):
            continue
        out[col] = f
        used.add(f)
    if "job_title" not in used:  # a client or status means nothing without the job itself
        out = ["" if f in ("job_company", "stage") else f for f in out]
    return out


def _cell(v: str) -> str:
    """Trim, and undo the apostrophe our own export puts before + = - @ so a round trip keeps phones intact."""
    v = v.strip()
    if v[:1] == "'" and v[1:2] in ("=", "+", "-", "@", "\t", "\r"):
        v = v[1:]
    return v


def _fold(s: str | None) -> str:
    """Lower case, no accents, single spaces: 'José  Álvarez' -> 'jose alvarez'."""
    s = unicodedata.normalize("NFKD", (s or "").casefold())
    s = "".join(ch for ch in s if not unicodedata.combining(ch))
    return re.sub(r"\s+", " ", re.sub(r"[^\w\s]", " ", s)).strip()


def _digits(phone: str | None) -> str:
    d = re.sub(r"\D", "", phone or "")
    return d[-10:] if len(d) >= 7 else ""


def _split_role(title: str) -> tuple[str, str] | None:
    """'Frontend Lead at Ramp' -> ('Frontend Lead', 'Ramp')."""
    for sep in (" at ", " @ "):
        if sep in title:
            t, _, c = title.partition(sep)
            if t.strip() and c.strip():
                return t.strip(" ,|-"), c.strip(" ,|-")
    return None


def _parse_jobs_cell(v: str) -> list[tuple[str, str, str]]:
    """Our export's Jobs cell: 'Senior Backend Engineer (Acme): Screening; Designer: Offer'."""
    out = []
    for part in (p.strip() for p in v.split(";")):
        m = re.match(r"^(?P<t>.+?)(?: \((?P<c>[^()]*)\))?: (?P<s>[^:]+)$", part)
        if m:
            out.append((m["t"].strip(), (m["c"] or "").strip(), m["s"].strip()))
        elif part:
            out.append((part, "", ""))
    return out


def _split_notes_cell(v: str) -> list[str]:
    """Our export's Notes cell: newest first, ' | ' between notes, a | inside a note written as \\|."""
    parts = re.split(r"(?<!\\) \| ", v)
    return [p.replace("\\|", "|").strip() for p in parts if p.strip()]


def _records(header: list[str], rows: Iterable[list[str]], mapping: list[str]) -> Iterable[dict[str, Any]]:
    """Each row as {"rows": [row number], "data": candidate fields, "notes", "note_at", "jobs", "warnings"}.

    Each job is (title, company, stage, from our export, row number); note_at gives some notes a date."""
    note_cols = sum(1 for m in mapping if m == "note")
    for n, r in enumerate(rows, start=2):
        data: dict[str, Any] = {}
        warnings: list[str] = []
        notes: list[str] = []
        note_at: dict[str, str] = {}
        jobs: list[tuple[str, str, str, bool, int]] = []
        dates: dict[str, str] = {}
        job = {"job_title": "", "job_company": "", "stage": ""}
        first = last = ""
        for col, target in enumerate(mapping):
            if not target or col >= len(r):
                continue
            col_name = header[col] if col < len(header) and header[col] else TARGET_LABELS.get(target, target)
            raw = r[col].strip()
            v = _cell(r[col])
            if not v:
                continue
            if raw.startswith("=") and len(raw) > 1:
                warnings.append(f"left {col_name} blank, {raw[:40]!r} looks like a spreadsheet formula, not a value")
                continue
            if target == "first_name":
                first = v
            elif target == "last_name":
                last = v
            elif target == "tags":
                data["tags"] = split_tags((data.get("tags") or []) + split_tags(v))  # , ; | (never / as in ci/cd)
            elif target == "tag_column":
                data["tags"] = split_tags((data.get("tags") or []) + [f"{col_name}: {v}"])
            elif target in ("created_at", "stage_changed_at"):
                when = _date_cell(v, col_name, warnings)
                if when:
                    dates[target] = when
            elif target == "last_contacted_at":
                if _RELATIVE_DATE.match(v.strip()):
                    warnings.append(f"left {col_name} blank, {v!r} depends on when the sheet was written. "
                                    "Use a date like 2026-11-05")
                    continue
                try:
                    data[target] = parse_when(v, default_now=False)
                except ValueError:
                    warnings.append(f"left {col_name} blank, couldn't read the date {v!r}")
            elif target == "note":
                head = _norm_header(col_name)
                text = f"{col_name}: {v}" if note_cols > 1 or head not in _PLAIN_NOTE_HEADERS else v
                notes.append(text)
                if head in _DATED_NOTE_HEADERS:
                    try:
                        note_at[text] = parse_when(v, default_now=False)
                    except ValueError:
                        pass
            elif target == "notes":
                notes.extend(reversed(_split_notes_cell(v)))  # oldest first, so the newest ends up on top
            elif target in job:
                job[target] = v
            elif target == "jobs":
                jobs.extend((t, c, s, True, n) for t, c, s in _parse_jobs_cell(v))
            elif target == "location" and target in data:
                data[target] = f"{data[target]}, {v}"
            elif target == "about" and target in data:
                data[target] = f"{data[target]}\n{v}"
            elif target not in data:
                data[target] = v
        if not data.get("name") and (first or last):
            data["name"] = f"{first} {last}".strip()
        if data.get("current_title") and not data.get("current_company"):
            split = _split_role(data["current_title"])
            if split:
                data["current_title"], data["current_company"] = split
        if job["job_title"]:
            jobs.append((job["job_title"], job["job_company"], job["stage"], False, n))
        elif job["job_company"] or job["stage"]:
            warnings.append("left the client and stage out, there's no job title on this row")
        yield {"rows": [n], "data": data, "notes": notes, "note_at": note_at, "jobs": jobs, "warnings": warnings,
               "differs": {}, "created_at": dates.get("created_at"),
               "stage_at": {n: dates["stage_changed_at"]} if "stage_changed_at" in dates else {}}


def _date_cell(v: str, col_name: str, warnings: list[str]) -> str | None:
    """A date column's cell as UTC ISO, or None with a warning when it isn't a date we can trust."""
    if _RELATIVE_DATE.match(v.strip()):
        warnings.append(f"left {col_name} blank, {v!r} depends on when the sheet was written. Use a date like 2026-11-05")
        return None
    try:
        when = parse_when(v, default_now=False)
    except ValueError:
        warnings.append(f"left {col_name} blank, couldn't read the date {v!r}")
        return None
    if when and when > datetime.now(timezone.utc).isoformat():
        warnings.append(f"left {col_name} blank, {v!r} is in the future")
        return None
    return when


def _rows(header: list[str], rows: Iterable[list[str]],
          mapping: list[str]) -> Iterable[tuple[dict[str, Any], list[str]]]:
    """Each row as (candidate fields, warnings about cells we had to leave out)."""
    for rec in _records(header, rows, mapping):
        data = dict(rec["data"])
        if rec["notes"]:
            data["note"] = "\n".join(rec["notes"])
        yield data, rec["warnings"]


def rows_to_candidates(header: list[str], rows: Iterable[list[str]], mapping: list[str]) -> Iterable[dict[str, Any]]:
    for data, _ in _rows(header, rows, mapping):
        data.pop("note", None)
        yield data


def _merge_records(a: dict[str, Any], b: dict[str, Any]) -> None:
    """Fold row b into row a (same person, same file): later non-empty values win, notes add up.
    a["differs"] remembers which fields the rows disagreed on, and the row whose value was kept."""
    for k, v in b["data"].items():
        cur = a["data"].get(k)
        if k == "tags":
            a["data"][k] = split_tags((cur or []) + v)
        elif k == "last_contacted_at":
            a["data"][k] = max(cur or "", v)
        else:
            if cur not in (None, "") and _fold(str(cur)) != _fold(str(v)):
                a["differs"][k] = b["rows"][0]
            a["data"][k] = v
    a["rows"] += b["rows"]
    a["notes"] += [x for x in b["notes"] if x not in a["notes"]]
    a["note_at"] = {**b["note_at"], **a["note_at"]}
    a["jobs"] += b["jobs"]
    a["warnings"] += b["warnings"]
    a["stage_at"] = {**a["stage_at"], **b["stage_at"]}
    if b["created_at"] and (not a["created_at"] or b["created_at"] < a["created_at"]):
        a["created_at"] = b["created_at"]


def _collapse(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Rows with the same email or LinkedIn in one file become one row, so they don't flip-flop."""
    out: list[dict[str, Any]] = []
    owner: dict[tuple[str, str], dict[str, Any]] = {}
    for rec in records:
        d = rec["data"]
        keys = [k for k in (("email", (d.get("email") or "").strip().lower()),
                            ("li", normalize_linkedin(d.get("linkedin_url")) or "")) if k[1]]
        hit = next((owner[k] for k in keys if k in owner), None)
        if hit is not None and d.get("name") and hit["data"].get("name") \
                and not same_person_name(hit["data"]["name"], d["name"]):
            hit = None  # a different person on the same email: let the identity check report it
        if hit is None:
            out.append(rec)
            hit = rec
        else:
            _merge_records(hit, rec)
        for k in keys:
            owner.setdefault(k, hit)
    return out


def _label(rows: list[int]) -> str:
    if len(rows) == 1:
        return f"Row {rows[0]}"
    nums = [str(n) for n in rows]
    return "Rows " + ", ".join(nums[:-1]) + " and " + nums[-1]


def _backup_prefix(reason: str) -> str:
    return "before-" + re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") + "-"


def backup_label_path(backup: Path) -> Path:
    """The small file next to a backup that says what it was taken for (which sheet, how many rows)."""
    return backup.with_suffix(".json")


def prune_backups(reason: str = "import", keep: int = BACKUPS_KEPT) -> None:
    """Keep the newest `keep` backups taken for this reason, and their labels."""
    old = sorted(backups_dir().glob(f"{_backup_prefix(reason)}*.db"), key=lambda f: (f.stat().st_mtime, f.name))
    for f in old[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)
        backup_label_path(f).unlink(missing_ok=True)


def drop_backup(backup: Path) -> None:
    backup.unlink(missing_ok=True)
    backup_label_path(backup).unlink(missing_ok=True)


def label_backup(backup: Path, file: str | None, rows: int) -> None:
    """Remember which file an import backup was taken for, so Settings can say "Before importing a.csv"."""
    import json

    try:
        backup_label_path(backup).write_text(json.dumps({"file": file or "", "rows": rows}), encoding="utf-8")
    except OSError:
        pass  # a missing label only makes the list less specific


def backup_before_import(store: Store, keep: int = BACKUPS_KEPT, reason: str = "import", *,
                         prune: bool = True) -> Path:
    """Copy the whole database into the data folder's backups/ first. Keeps the newest `keep` of these.

    `reason` names the file ("before-import-...", "before-delete-..."); each reason keeps its own `keep`.
    prune=False leaves the older ones for now, so an import that changes nothing can drop its own backup
    without pushing a real one out (prune_backups then runs once the import did change something)."""
    folder = backups_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    prefix = _backup_prefix(reason)
    dest, n = folder / f"{prefix}{stamp}.db", 1
    while dest.exists():
        dest, n = folder / f"{prefix}{stamp}-{n}.db", n + 1
    try:
        os.close(os.open(dest, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
        store.backup_to(dest)
    except (OSError, sqlite3.Error) as exc:
        dest.unlink(missing_ok=True)
        doing = "importing" if reason == "import" else "making this change"
        raise ValueError(f"Couldn't save a backup before {doing}, so nothing was changed ({exc}).") from exc
    if prune:
        prune_backups(reason, keep)
    return dest


def _row_error(exc: Exception) -> str:
    if isinstance(exc, IdentityConflict):
        return (f"{exc.value} belongs to {exc.existing['name']} but this row says {exc.incoming_name}, "
                "so it was left alone")
    if isinstance(exc, DuplicateValue):
        return (f"its email and LinkedIn match two different people already here (that {exc.label} belongs to "
                f"{exc.owner['name']}), so it was left alone")
    if isinstance(exc, sqlite3.IntegrityError):
        if "linkedin" in str(exc).lower() or "email" in str(exc).lower():
            return "its email and LinkedIn match two different people already here, so it was left alone"
        return "it clashes with someone already here, so it was left alone"
    if isinstance(exc, ValueError):
        return str(exc)
    return f"couldn't be saved ({exc.__class__.__name__}: {exc})"


def _is_conflict(exc: Exception) -> bool:
    return isinstance(exc, (IdentityConflict, DuplicateValue, sqlite3.IntegrityError))


# Timeline kinds the export's Notes column carries. Notes go in as they are; the others get their kind
# in front ("call: Left a voicemail"), and come back in as notes with that text.
EXPORT_NOTE_KINDS = ("note", "call", "email", "message", "meeting", "interview")


def _export_note(t: dict[str, Any]) -> str | None:
    body = t.get("body") or ""
    if t["kind"] not in EXPORT_NOTE_KINDS or not body.strip():
        return None
    return body if t["kind"] == "note" else f"{t['kind']}: {body.strip()}"


def _note_bodies(store: Store, candidate_id: int) -> set[str]:
    """Every text a re-imported Notes cell could match: notes as they are, and the "call: ..." forms."""
    out: set[str] = set()
    for t in store.timeline(candidate_id, limit=1000):
        if t["kind"] == "note":
            out.add(t.get("body") or "")
        elif (text := _export_note(t)) is not None:
            out.add(text)
    return out


def _has_note(store: Store, candidate_id: int, body: str) -> bool:
    return body in _note_bodies(store, candidate_id)


class _People:
    """Saved people by folded name, so a row with no email or LinkedIn can find them."""

    def __init__(self, store: Store) -> None:
        self.by_name: dict[str, list[dict[str, Any]]] = {}
        for c in store.all_candidates():
            self.add(c)

    def add(self, c: dict[str, Any]) -> None:
        entry = {k: c.get(k) for k in ("id", "name", "email", "linkedin_url", "current_company", "phone")}
        entries = self.by_name.setdefault(_fold(c.get("name")), [])
        entries[:] = [e for e in entries if e["id"] != c["id"]] + [entry]

    def match(self, data: dict[str, Any]) -> tuple[dict[str, Any] | None, list[dict[str, Any]]]:
        """(the one person this row is, or None; everyone else with the same name)."""
        same = self.by_name.get(_fold(data.get("name")), [])
        company, phone = _fold(data.get("current_company")), _digits(data.get("phone"))
        for e in reversed(same):
            if (company and _fold(e["current_company"]) == company) or (phone and _digits(e["phone"]) == phone):
                return e, []
        return None, list(same)


def _merge_fields(store: Store, candidate_id: int, data: dict[str, Any]) -> dict[str, Any]:
    """The same merge rules as a save by email or LinkedIn, for someone matched by name."""
    cur = store.get_candidate(candidate_id)
    out: dict[str, Any] = {}
    for k, v in data.items():
        if v in (None, "", []):
            continue
        if k == "tags":
            out[k] = split_tags(cur["tags"] + split_tags(v))
        elif k in MERGE_OVERWRITES:
            out[k] = v
        elif k == "last_contacted_at":
            if not cur.get(k) or v > cur[k]:
                out[k] = v
        elif not cur.get(k):
            out[k] = v
    return out


class _Jobs:
    """Find a job by title and company (ignoring case), or make it. One lookup per import."""

    def __init__(self, store: Store) -> None:
        self.store = store
        self.jobs = store.list_jobs()
        self.created: list[str] = []

    def get(self, title: str, company: str, any_status: bool) -> dict[str, Any]:
        key = (_fold(title), _fold(company))
        rank = {"open": 0, "on_hold": 1, "closed": 2}
        found = sorted((j for j in self.jobs if (_fold(j["title"]), _fold(j.get("company"))) == key
                        and (any_status or j["status"] == "open")), key=lambda j: rank.get(j["status"], 3))
        if found:
            return found[0]
        job = self.store.add_job(title, company or None)
        self.jobs.append(job)
        self.created.append(job_label(job))
        return job


def job_label(job: dict[str, Any]) -> str:
    company = job.get("company") or job.get("job_company")
    title = job.get("title") or job.get("job_title")
    return f"{title} ({company})" if company else str(title)


def _rename_change_source(store: Store, candidate_id: int, source: str) -> None:
    """Say which sheet made the change ("from Old agency sheet"), not just "CSV import"."""
    if not source or source == CHANGE_SOURCES["import"]:
        return
    with store._lock, store._conn:
        store._conn.execute(
            "UPDATE timeline SET source = ? WHERE id = (SELECT max(id) FROM timeline"
            " WHERE candidate_id = ? AND kind = 'change')", (source, candidate_id))


def _labels(fields: Iterable[str]) -> str:
    return ", ".join(FIELD_LABELS.get(f, f.replace("_", " ")) for f in fields)


def import_rows(store: Store, header: list[str], rows: list[list[str]], mapping: list[str],
                source: str = "CSV import", backup: bool = True, add_stages: bool = False,
                file_name: str | None = None) -> dict[str, Any]:
    """Add or update one candidate per row. A bad row is skipped and listed, never fatal.

    Backs up the database first (see backup_before_import). Rows with the same email or LinkedIn
    in one file are folded into one first. A row with neither is matched to a saved person by
    name plus company or phone; if only the name matches, it's added and listed in
    `possible_duplicates`. A row that matched someone and changed nothing counts as `unchanged`,
    not `updated`. `skipped` lists the rows left out, as "Row N: reason" (`skipped_rows` counts
    them, `conflicts` the ones that clashed with someone already here); `warnings` lists cells we
    left blank and notes we couldn't save, on rows that were still imported; `changes` says what
    each updated row changed. Job, client and stage columns put people in pipelines.

    A Status the job has a stage for (ignoring case) goes there. With add_stages=True any other Status is
    added to that job as its own stage, in pipeline order (see _gap_after); a "Placed" on a job whose win is
    "Hired" still goes to Hired. Without it, a Status goes to the closest stage by name ("Phone screen" to
    Screening), else the job's first stage. `missing_stages` lists the stages ticking add_stages would add,
    `stage_fallbacks` says who went where, and `stage_moves` counts them ("Phone Screen to Screening on
    X": 198). `already_in` lists people already in that job at another stage, who were left there.
    `merged_rows` lists rows folded into one person, and which row won where they differ. A row whose email
    and LinkedIn belong to two different people here is left alone as a conflict.

    Date added and stage date columns date the person and their stage. Pipeline entries with no stage date
    are marked as imported, so status updates don't call them new this week. The backup is labelled with
    `file_name` and the row count, and dropped again when the import changes nothing.
    """
    backup_file = backup_before_import(store, prune=False) if backup else None
    try:
        result = _import(store, header, rows, mapping, source, add_stages)
    except BaseException:
        if backup_file:
            prune_backups()
        raise
    if backup_file and not result.pop("_changed"):
        drop_backup(backup_file)  # nothing changed, so keep the older backups instead
        backup_file = None
    elif backup_file:
        label_backup(backup_file, file_name, len(rows))
        prune_backups()
    result.pop("_changed", None)
    result["backup"] = str(backup_file) if backup_file else None
    return result


def _order_hints(records: list[dict[str, Any]]) -> dict[tuple[str, str], list[str]]:
    """For each job in the file, its Status values in pipeline order as far as the file can tell: by the
    middle stage date when every Status has one, else in the order the file first shows them."""
    seen: dict[tuple[str, str], dict[str, list[Any]]] = {}
    for rec in records:
        for title, company, stage, _, row in rec["jobs"]:
            if not stage.strip():
                continue
            by = seen.setdefault((_fold(title), _fold(company)), {})
            entry = by.setdefault(stage.strip().casefold(), [len(by), stage.strip(), []])
            if rec["stage_at"].get(row):
                entry[2].append(rec["stage_at"][row])
    out: dict[tuple[str, str], list[str]] = {}
    for key, by in seen.items():
        entries = list(by.values())
        if all(e[2] for e in entries):
            entries.sort(key=lambda e: (sorted(e[2])[len(e[2]) // 2], e[0]))
        else:
            entries.sort(key=lambda e: e[0])
        out[key] = [e[1] for e in entries]
    return out


def _gap_after(stages: list[dict[str, Any]], order: list[str] | None, new: str) -> str | None:
    """The stage a new in-play stage should go right after, so the job's stages agree with the order the
    file gives (`order`) as closely as they can: "Submitted" seen between Screened and Client interview goes
    between them. None when the file gives no hint (the stage then goes before the closing stages)."""
    keys = [k.casefold() for k in order or []]
    if new.casefold() not in keys or stage_outcome(new) != "open":
        return None
    pos = {s["name"].casefold(): i for i, s in enumerate(stages)}
    known = [pos[k] for k in keys if k in pos]
    if known != sorted(known):
        return None  # the file lists the job's own stages out of order (sorted by name, say): no hint
    me = keys.index(new.casefold())
    before = [pos[k] for k in keys[:me] if k in pos]
    after = [pos[k] for k in keys[me + 1:] if k in pos]
    if not before and not after:
        return None
    best: tuple[int, int] | None = None
    for gap, s in enumerate(stages):  # gap = right after stage number `gap`
        if s["outcome"] != "open":
            break  # an in-play stage never goes after a closing one
        cost = sum(1 for p in before if p > gap) + sum(1 for p in after if p <= gap)
        if best is None or cost < best[0] or (cost == best[0] and not before):
            best = (cost, gap)  # with only later stages to go by, sit right before them
    return stages[best[1]]["name"] if best else None


def _import(store: Store, header: list[str], rows: list[list[str]], mapping: list[str], source: str,
            add_stages: bool) -> dict[str, Any]:
    added = updated = unchanged = skipped_rows = notes = conflicts = pipelines = dated = 0
    skipped: list[str] = []
    warned: list[str] = []
    changes: list[str] = []
    possible: list[str] = []
    fallbacks: list[str] = []
    already: list[str] = []
    moves: Counter[str] = Counter()
    merged_rows: list[str] = []
    missing: dict[tuple[int, str], dict[str, str]] = {}
    stages_made: list[str] = []
    field_counts: Counter[str] = Counter()
    records = _collapse(list(_records(header, rows, mapping)))
    hints = _order_hints(records) if add_stages else {}
    people: _People | None = None
    jobs: _Jobs | None = None
    tag_case = {t["tag"].casefold(): t["tag"] for t in store.list_tags(100000)}
    for rec in records:
        label, data = _label(rec["rows"]), rec["data"]
        if not data.get("name"):
            skipped.append(f"{label}: no name")
            skipped_rows += len(rec["rows"])
            continue
        if data.get("tags"):
            data["tags"] = [tag_case.setdefault(t.casefold(), t) for t in data["tags"]]
        others: list[dict[str, Any]] = []
        if len(rec["rows"]) > 1:
            line = f"{label} are the same person ({data['name']}), so they were saved as one"
            if rec["differs"]:
                by_row: dict[int, list[str]] = {}
                for field, row in rec["differs"].items():
                    by_row.setdefault(row, []).append(field)
                line += ". Where they differ, " + ", ".join(f"row {row} won ({_labels(fields)})"
                                                            for row, fields in sorted(by_row.items()))
            merged_rows.append(line)
        by_email = store.find_duplicate(email=data.get("email")) if data.get("email") else None
        by_li = store.find_duplicate(linkedin_url=data.get("linkedin_url")) if data.get("linkedin_url") else None
        if by_email and by_li and by_email["id"] != by_li["id"]:
            skipped.append(f"{label}: its email and LinkedIn match two different people here (the email is "
                           f"{by_email['name']}'s, id {by_email['id']}, and the LinkedIn is {by_li['name']}'s, id "
                           f"{by_li['id']}), so it was left alone")
            skipped_rows += len(rec["rows"])
            conflicts += 1
            continue
        try:
            match = None
            if not (data.get("email") or data.get("linkedin_url")):
                people = people or _People(store)
                match, others = people.match(data)
            if not match and not store.find_duplicate(data.get("email"), data.get("linkedin_url")):
                data.setdefault("source", source)  # only new people get the sheet as where they came from
            if match and not (match["email"] or match["linkedin_url"]):
                cand = store.update_candidate(match["id"], by="import", **_merge_fields(store, match["id"], data))
                created = False
            else:
                if match:  # same name and company or phone: save into them through their email or LinkedIn
                    data = {**data, "email": match["email"], "linkedin_url": match["linkedin_url"]}
                    data = {k: v for k, v in data.items() if v not in (None, "")}
                res = store.upsert_candidate(by="import", **data)
                cand, created = res[0], bool(res[1])
        except Exception as exc:  # one bad row must never stop the rest
            skipped.append(f"{label}: {_row_error(exc)}")
            skipped_rows += len(rec["rows"])
            conflicts += _is_conflict(exc)
            continue
        if people is not None:
            people.add(cand)
        if rec["created_at"] and (created or rec["created_at"] < (cand.get("created_at") or "")):
            with store._lock, store._conn:  # the sheet knows when they were first added
                store._conn.execute("UPDATE candidates SET created_at = ? WHERE id = ?", (rec["created_at"], cand["id"]))
            dated += 1
        if created:
            added += 1
            if others:
                ids = ", ".join(f"id {o['id']}" for o in others[:3])
                possible.append(f"{label}: {cand['name']} was added, but someone with that name is already "
                                f"here ({ids}). It has no email or LinkedIn to tell them apart.")
        elif cand.get("_changed"):
            updated += 1
            field_counts.update(cand["_changed"])
            changes.append(f"{label} ({cand['name']}): {_labels(cand['_changed'])}")
            _rename_change_source(store, cand["id"], source)
        else:
            unchanged += 1
        warned.extend(f"{label}: {w}" for w in rec["warnings"])
        if rec["notes"]:
            try:
                have = _note_bodies(store, cand["id"])
                for note in rec["notes"]:
                    if note not in have:
                        store.log(cand["id"], "note", note, source=source,
                                  at=rec["note_at"].get(note) or data.get("last_contacted_at"))
                        have.add(note)
                        notes += 1
            except Exception as exc:
                warned.append(f"{label}: note not saved ({exc})")
        for title, company, stage, any_status, row in rec["jobs"]:
            jobs = jobs or _Jobs(store)
            try:
                pipelines += _place(store, jobs, cand, title, company, stage, any_status, f"Row {row}", warned,
                                    fallbacks=fallbacks, missing=missing, add_stages=add_stages, already=already,
                                    moves=moves, order=hints.get((_fold(title), _fold(company))),
                                    stage_at=rec["stage_at"].get(row), made=stages_made)
            except Exception as exc:
                warned.append(f"Row {row}: not added to {title} ({exc})")
    mapped_cols = {i for i, m in enumerate(mapping) if m}
    not_imported = [h for i, h in enumerate(header) if i not in mapped_cols and h.strip()]
    jobs_created = jobs.created if jobs else []
    return {"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped,
            "skipped_rows": skipped_rows, "conflicts": conflicts, "warnings": warned, "notes": notes,
            "changes": changes, "changed_fields": dict(field_counts.most_common()),
            "possible_duplicates": possible, "pipelines_added": pipelines, "stage_fallbacks": fallbacks,
            "stage_moves": dict(moves.most_common()), "already_in": already,
            "merged_rows": merged_rows, "missing_stages": list(missing.values()), "stages_added": add_stages,
            "jobs_created": jobs_created, "not_imported": not_imported, "dates_set": dated,
            "_changed": bool(added or updated or notes or pipelines or dated or jobs_created or stages_made),
            "dry_run": False}


def _stage_words(name: str) -> list[str]:
    return [w for w in re.split(r"[^a-z0-9]+", _fold(name)) if len(w) >= 3]


def closest_stage(name: str, stages: list[dict[str, Any]]) -> dict[str, Any] | None:
    """The one stage whose name shares a word with this one ('Phone screen' and 'Screening',
    'Client interview' and 'Interviewing'), or None when no stage, or more than one, is as close."""
    words = _stage_words(name)
    scores = []
    for s in stages:
        theirs = _stage_words(s["name"])
        scores.append((sum(1 for w in words if any(t.startswith(w) or (len(t) >= 4 and w.startswith(t))
                                                   for t in theirs)), s))
    best = max((n for n, _ in scores), default=0)
    top = [s for n, s in scores if n == best]
    return top[0] if best and len(top) == 1 else None


def _place(store: Store, jobs: _Jobs, cand: dict[str, Any], title: str, company: str, stage: str,
           any_status: bool, label: str, warned: list[str], *, fallbacks: list[str] | None = None,
           missing: dict[tuple[int, str], dict[str, str]] | None = None, add_stages: bool = False,
           already: list[str] | None = None, moves: Counter[str] | None = None, order: list[str] | None = None,
           stage_at: str | None = None, made: list[str] | None = None) -> int:
    """Put the person in that job's pipeline at that stage. 1 if they were added, 0 if already there."""
    fallbacks = warned if fallbacks is None else fallbacks
    already = warned if already is None else already
    job = jobs.get(title, company, any_status)
    stages = store.job_stages(job["id"])
    want = stages[0]
    if stage:
        key = stage.strip().casefold()
        exact = [s for s in stages if s["name"].casefold() == key]
        starts = [s for s in stages if s["name"].casefold().startswith(key)]
        outcome = stage_outcome(stage)
        by_outcome = [s for s in stages if outcome != "open" and s["outcome"] == outcome]
        if exact:
            want = exact[0]
        elif add_stages and outcome == "hired" and by_outcome:  # "Placed" on a job whose win stage is "Hired"
            want = by_outcome[0]
        elif add_stages:  # its own stage, never merged into a near one ("HM Screen" is not Screening)
            if missing is not None:
                missing.setdefault((job["id"], key), {"job": job_label(job), "stage": stage.strip()})
            store.add_stage(job["id"], stage.strip(), after=_gap_after(stages, order, stage.strip()))
            want = store.resolve_stage(job["id"], stage.strip())
            if made is not None:
                made.append(want["name"])
        else:
            near = None if len(starts) == 1 or by_outcome else closest_stage(stage, stages)
            if missing is not None and not (outcome == "hired" and by_outcome):  # what ticking "Add the missing stages" would add
                missing.setdefault((job["id"], key), {"job": job_label(job), "stage": stage.strip()})
            if len(starts) == 1:
                want = starts[0]
            elif by_outcome:  # "Placed" on a job whose win stage is "Hired"
                want = by_outcome[0]
            elif near:
                want = near
                fallbacks.append(f"{label}: put in {near['name']}, the closest stage to {stage!r} on {job_label(job)}")
            else:
                fallbacks.append(f"{label}: put in {want['name']}, since {job_label(job)} has no stage called "
                                 f"{stage!r}")
            if moves is not None and not (len(starts) == 1 or by_outcome):
                moves[f"{stage.strip()} to {want['name']} on {job_label(job)}"] += 1
    current = next((p for p in store.candidate_pipelines(cand["id"]) if p["job_id"] == job["id"]), None)
    if current:
        if current["stage"].casefold() != want["name"].casefold():
            already.append(f"{label}: already in {job_label(job)} at {current['stage']}, so they were left there")
        return 0
    store.add_to_pipeline(cand["id"], job["id"], stage=want["name"])
    _date_placement(store, cand["id"], job["id"], stage_at)
    return 1


def _date_placement(store: Store, candidate_id: int, job_id: int, stage_at: str | None) -> None:
    """Date a new pipeline entry from the sheet's stage date. With no date, mark its stage entry as imported,
    so a status update says "(imported)" and doesn't count it as this week's news."""
    with store._lock, store._conn:
        entry = ("(SELECT max(id) FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage')")
        if stage_at:
            store._conn.execute("UPDATE applications SET stage_changed_at = ?, created_at = min(created_at, ?)"
                                " WHERE candidate_id = ? AND job_id = ?", (stage_at, stage_at, candidate_id, job_id))
            store._conn.execute(f"UPDATE timeline SET at = ? WHERE id = {entry}", (stage_at, candidate_id, job_id))
        else:
            store._conn.execute(f"UPDATE timeline SET data = ? WHERE id = {entry}",
                                ('{"imported": true}', candidate_id, job_id))


def imported_placements(store: Store, job_id: int) -> set[int]:
    """People on this job still sitting where an undated import put them (their newest stage entry is it)."""
    rows = store._rows(
        """SELECT t.candidate_id FROM timeline t
           WHERE t.job_id = ? AND t.kind = 'stage' AND json_valid(t.data) AND json_extract(t.data, '$.imported') = 1
             AND t.id = (SELECT max(l.id) FROM timeline l WHERE l.candidate_id = t.candidate_id
                         AND l.job_id = t.job_id AND l.kind = 'stage')""", (job_id,))
    return {r["candidate_id"] for r in rows}


def preview_import(store: Store, header: list[str], rows: list[list[str]], mapping: list[str],
                   source: str = "CSV import", add_stages: bool = False) -> dict[str, Any]:
    """What import_rows would do, worked out on a private copy of the database. Nothing here changes."""
    scratch = Store(":memory:")
    try:
        try:
            with store._lock:
                store._conn.backup(scratch._conn)
        except sqlite3.Error:  # page sizes differ: fall back to a copy on disk
            scratch.close()
            return _preview_on_disk(store, header, rows, mapping, source, add_stages)
        result = import_rows(scratch, header, rows, mapping, source=source, backup=False, add_stages=add_stages)
    finally:
        try:
            scratch.close()
        except sqlite3.Error:
            pass
    return _will(result)


def _preview_on_disk(store: Store, header: list[str], rows: list[list[str]], mapping: list[str],
                     source: str, add_stages: bool = False) -> dict[str, Any]:
    import tempfile

    folder = Path(tempfile.mkdtemp(prefix="preview-", dir=backups_dir()))
    copy = folder / "preview.db"
    try:
        store.backup_to(copy)
        scratch = Store(copy)
        try:
            result = import_rows(scratch, header, rows, mapping, source=source, backup=False, add_stages=add_stages)
        finally:
            scratch.close()
    finally:
        for f in folder.glob("*"):
            f.unlink(missing_ok=True)
        folder.rmdir()
    return _will(result)


def _will(result: dict[str, Any]) -> dict[str, Any]:
    """A dry run speaks about what will happen: "so it will be left alone"."""
    for key in ("skipped", "warnings", "stage_fallbacks", "merged_rows", "already_in"):
        if result.get(key):
            result[key] = [x.replace("so it was left alone", "so it will be left alone")
                           .replace("so they were left there", "so they will be left there")
                           .replace("so they were saved as one", "so they will be saved as one")
                           .replace(": put in ", ": will be put in ", 1)
                           for x in result[key]]
    result["dry_run"] = True
    return result


def button_label(result: dict[str, Any] | None, total: int) -> str:
    """The import button says what it does: 'Import 3 new people and update 2'."""
    if result:
        added, updated = result.get("added", 0), result.get("updated", 0)
        if added and updated:
            return f"Import {_plural(added, 'new person', 'new people')} and update {updated}"
        if added:
            return f"Import {_plural(added, 'new person', 'new people')}"
        if updated:
            return f"Update {_plural(updated, 'person', 'people')}"
    return f"Import {_plural(total, 'row')}"


def _plural(n: int, word: str, many: str | None = None) -> str:
    return f"{n} {word if n == 1 else many or word + 's'}"


def preview_lines(result: dict[str, Any]) -> list[str]:
    """What a dry run found, in plain lines. The first line is the headline."""
    fields = _labels(result.get("changed_fields") or {})
    parts = [f"add {result['added']}",
             f"update {result['updated']}" + (f" ({fields})" if fields and result["updated"] else "")]
    if result.get("conflicts"):
        parts.append(f"leave {result['conflicts']} alone because of conflicts")
    other_skips = result["skipped_rows"] - result.get("conflicts", 0)
    if other_skips:
        parts.append(f"skip {_plural(other_skips, 'row')}")
    if result.get("possible_duplicates"):
        n = len(result["possible_duplicates"])
        parts.append(f"flag {_plural(n, 'possible duplicate')} (no email or LinkedIn, and the name matches "
                     "someone here)")
    lines = ["Will " + ", ".join(parts) + "."]
    extra = []
    if result["unchanged"]:
        extra.append(f"{result['unchanged']} already up to date")
    if result.get("notes"):
        extra.append(f"{_plural(result['notes'], 'note')} to add")
    if result.get("pipelines_added"):
        extra.append(f"{_plural(result['pipelines_added'], 'person', 'people')} to put in job pipelines")
    if result.get("jobs_created"):
        extra.append(f"{_plural(len(result['jobs_created']), 'new job')}: " + ", ".join(result["jobs_created"][:5]))
    if result.get("missing_stages") and result.get("stages_added"):
        extra.append(f"{_plural(len(result['missing_stages']), 'new stage')}: " + ", ".join(
            f"{m['stage']} on {m['job']}" for m in result["missing_stages"][:5]))
    if extra:
        lines.append("Also: " + "; ".join(extra) + ".")
    return lines


PANEL_LINES = 20  # lines a result panel shows before "and N more"
# Result panels: (key in the result, heading). Possible duplicates start open.
PANELS = (("skipped", "Skipped rows"), ("already_in", "Already in that job"),
          ("warnings", "Imported, with a cell left blank"), ("stage_fallbacks", "Put in a different stage"),
          ("merged_rows", "Rows saved as one person"), ("changes", "What changed on updated people"),
          ("possible_duplicates", "Possible duplicates to check"))
_ROW_LEAD = re.compile(r"^Rows? [\d, and]+(?: \([^)]*\))?: ")


def group_lines(lines: list[str]) -> list[str]:
    """Lines that say the same thing about different rows, counted: "no name: 12". Empty when every line
    says something different, since a count of 1 each adds nothing."""
    counts = Counter(_ROW_LEAD.sub("", line) for line in lines)
    if len(counts) == len(lines):
        return []
    return [f"{reason}: {n:,}" for reason, n in counts.most_common(PANEL_LINES)]


def result_panels(result: dict[str, Any]) -> list[dict[str, Any]]:
    """The detail lists of an import or a dry run, each capped at PANEL_LINES. A longer list whose lines repeat
    the same reason shows the reasons with counts instead ("Phone Screen to Screening on X: 198")."""
    out = []
    for key, title in PANELS:
        lines = result.get(key) or []
        if not lines:
            continue
        groups: list[str] = []
        if len(lines) > PANEL_LINES:  # a long list reads better as reasons with counts
            if key == "stage_fallbacks" and result.get("stage_moves"):
                groups = [f"{k}: {n:,}" for k, n in list(result["stage_moves"].items())[:PANEL_LINES]]
            else:
                groups = group_lines(lines)
        out.append({"key": key, "title": title, "count": len(lines), "groups": groups,
                    "lines": lines[:PANEL_LINES], "more": max(0, len(lines) - PANEL_LINES),
                    "open": key == "possible_duplicates"})
    return out


def panel_csv(title: str, lines: list[str]) -> str:
    """One result panel as a CSV: the row number(s) and what happened."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["Rows", title])
    for line in lines:
        m = _ROW_LEAD.match(line)
        lead = line[:m.end() - 2] if m else ""
        w.writerow([_safe_cell(lead), _safe_cell(line[m.end():] if m else line)])
    return buf.getvalue()


def _jobs_text(store: Store, candidate_id: int) -> str:
    return "; ".join(f"{job_label(p)}: {p['stage']}" for p in store.candidate_pipelines(candidate_id))


def _notes_text(store: Store, candidate_id: int) -> str:
    out: list[str] = []
    size = 0
    for t in store.timeline(candidate_id, limit=200):  # newest first
        text = _export_note(t)
        if text is None:
            continue
        body = text.replace("|", "\\|")
        if len(out) >= EXPORT_NOTES_KEPT or size + len(body) > EXPORT_NOTES_CHARS:
            break
        out.append(body)
        size += len(body) + 3
    return " | ".join(out)


def export_csv(store: Store) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([EXPORT_HEADERS[f] for f in EXPORT_FIELDS]
               + [EXPORT_HEADERS["jobs"], EXPORT_HEADERS["notes"], EXPORT_HEADERS["work_history"]])
    for c in store.all_candidates():
        history = "; ".join(
            " at ".join(x for x in (h["title"], h["company"]) if x)
            + (f" ({h['start'] or ''}-{h['end'] or 'now'})" if h["start"] or h["end"] else "")
            for h in store.work_history(c["id"])
        )
        row = [", ".join(c[f]) if f == "tags" else (c.get(f) or "") for f in EXPORT_FIELDS]
        row += [_jobs_text(store, c["id"]), _notes_text(store, c["id"]), history]
        w.writerow([_safe_cell(v) for v in row])
    return buf.getvalue()


def _safe_cell(v: Any) -> str:
    """Stop spreadsheet apps from running a cell as a formula (CSV injection)."""
    s = str(v)
    return "'" + s if s[:1] in ("=", "+", "-", "@", "\t", "\r") else s


# ------------------------------------------------------- pasted profile text
_EMAIL = re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+")
_PHONE = re.compile(r"(?:\+?\(?\d[\d\s().-]{7,}\d)")
_LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_URL = re.compile(r"https?://[^\s)>,]+", re.I)
_TLDS = r"(?:com|dev|io|me|org|net|co|ai|app|xyz|tech|sh|page|site|info|us|uk|ca|de|eu|so|gg|codes|design)"
_BARE = re.compile(r"(?<![\w@/.:-])((?:www\.)?[a-z0-9][a-z0-9-]*(?:\.[a-z0-9-]+)*\." + _TLDS + r")(/[^\s)>,]*)?(?![\w@.-])")
_PRONOUNS = re.compile(r"\(?\b(?:he|she|they|xe|ze)\s*/\s*(?:him|her|them|his|hers|their|theirs|xem|zir)"
                       r"(?:\s*/\s*\w+)?\b\)?", re.I)
_DEGREE = re.compile(r"^(?:·\s*)?(?:1st|2nd|3rd\+?)(?:\s+degree connection)?$", re.I)
_NOISE = {"contact info", "connect", "message", "more", "follow", "following", "pending", "open to", "share",
          "add profile section", "enhance profile", "resources", "save to pdf", "view in sales navigator",
          "show all", "see more", "...see more", "…see more", "send profile in a message"}
_SECTIONS = {"about", "experience", "education", "activity", "skills", "featured", "licenses & certifications",
             "licenses and certifications", "volunteering", "volunteer experience", "projects", "recommendations",
             "interests", "languages", "honors & awards", "honors and awards", "publications", "courses",
             "organizations", "people also viewed", "people you may know", "causes", "services", "highlights"}
_LOCATION = re.compile(r"^[A-Z][\w.'-]*(?: [\w.'-]+){0,3}, ?[A-Z][\w.'-]*(?: [\w.'-]+){0,3}"
                       r"(?:, ?[A-Z][\w.'-]*(?: [\w.'-]+){0,3})?$"
                       r"|^(?:Greater )?[A-Z][\w.'-]*(?: [\w.'-]+){0,3} (?:Bay |Metropolitan |Metro )?Area$")


def _is_noise(part: str) -> bool:
    low = part.lower().strip(" ·•|")
    return (not low or low in _NOISE or bool(_DEGREE.match(low)) or bool(_PRONOUNS.fullmatch(low))
            or bool(re.search(r"\b(?:followers|connections)\b", low)) or low.startswith("talks about"))


def _clean_line(line: str) -> str:
    """Drop the LinkedIn bits that ride along on a line: pronouns, '· 2nd', '· Contact info', and a
    'Contact info' link copied onto the end of the location line ('Brooklyn, New York  Contact info')."""
    line = re.sub(r"(?:\s+[·•])?\s+contact info\s*$", "", line, flags=re.I)
    parts = [p.strip() for p in re.split(r"\s+[·•]\s+|^[·•]\s*", line)]
    keep = [_PRONOUNS.sub("", p).strip() for p in parts if not _is_noise(p)]
    return " · ".join(p for p in keep if p)


def _linkedin_marks(text: str) -> int:
    """How many things in the text only a LinkedIn profile page has. A LinkedIn link alone isn't one:
    resumes carry those too."""
    low = text.lower()
    marks = ("contact info" in low, bool(re.search(r"\b\d[\d,+]* (?:connections|followers)\b", low)),
             bool(re.search(r"(?m)^\s*(?:·\s*)?(?:1st|2nd|3rd\+?)(?: degree connection)?\s*$", low)
                  or re.search(r"·\s*(?:1st|2nd|3rd\+?)\b", low)), "endorse" in low,
             bool(re.search(r"(?m)^\s*experience\s*$", low)) and bool(re.search(r"(?m)^\s*about\s*$", low)),
             bool(re.search(r"(?m)^\s*(?:message|connect|follow)\s*$", low)),
             bool(re.search(r"(?m)^\s*(?:full-time|part-time|self-employed)\b|· (?:full-time|part-time)\b", low)))
    return sum(marks)


def _looks_like_linkedin(text: str) -> bool:
    return _linkedin_marks(text) >= 2


_SKILL_HEADS = re.compile(r"^(?:technical |core |key )?(?:skills|skills & tools|skills and tools|tech stack|"
                          r"technologies|tools|competencies)\s*:?\s*(?P<rest>.*)$", re.I)
_EXP_HEADS = {"experience", "work experience", "professional experience", "employment", "employment history",
              "work history", "career history", "experience:", "work experience:", "employment:"}
_RESUME_SECTIONS = _SECTIONS | {"education:", "skills:", "projects:", "summary", "summary:", "profile", "objective",
                                "certifications", "awards", "references", "contact", "technical skills",
                                "publications:", "languages:", "interests:"} | _EXP_HEADS
_MON = r"(?:jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec)[a-z]*\.?"
_WHEN = rf"(?:{_MON}\s+)?(?:\d{{1,2}}/)?(?:19|20)\d{{2}}"
_RANGE = re.compile(rf"(?P<start>{_WHEN})\s*(?:-|–|—|to|until)\s*(?P<end>{_WHEN}|present|now|current|today)", re.I)


def _is_section(line: str) -> bool:
    low = line.lower().strip()
    return low in _RESUME_SECTIONS or bool(_SKILL_HEADS.match(line) and not _SKILL_HEADS.match(line)["rest"])


def _skills_section(lines: list[str]) -> list[str]:
    """'Skills: Python, Go' or a Skills heading with the skills under it."""
    for i, line in enumerate(lines):
        m = _SKILL_HEADS.match(line)
        if not m:
            continue
        body = [m["rest"]] if m["rest"] else []
        if not body:
            for nxt in lines[i + 1:i + 30]:
                if _is_section(nxt) or _RANGE.search(nxt):
                    break
                body.append(nxt)
        tags = []
        for part in re.split(r"[,;|•·]|\s{2,}", " , ".join(body)):
            t = re.sub(r"^[-*–\s]+|\s*\(?\d+\s*endorsements?\)?$", "", part.strip(), flags=re.I).strip(" .")
            if t and len(t) <= 40 and len(t.split()) <= 4 and not re.search(r"endorse|\d{3,}", t, re.I):
                tags.append(t)
        if tags:
            return split_tags(tags)[:40]
    return []


def _experience_section(lines: list[str]) -> list[dict[str, str]]:
    """Roles under an Experience heading, only where each one has a clear date range.

    Takes 'Senior Engineer at Stripe (2021 - Present)', 'Senior Engineer | Stripe | 2021 - now', and
    LinkedIn's three lines (title, 'Stripe · Full-time', 'Jan 2021 - Present · 3 yrs')."""
    start = next((i for i, l in enumerate(lines) if l.lower().strip() in _EXP_HEADS), None)
    if start is None:
        return []
    body: list[str] = []
    for line in lines[start + 1:]:
        if _is_section(line):
            break
        body.append(line)
    out: list[dict[str, str]] = []
    used = -1
    for i, line in enumerate(body):
        m = _RANGE.search(line)
        if not m:
            continue
        span = {"start": m["start"].strip(), "end": "now" if m["end"].lower() in ("present", "now", "current", "today")
                else m["end"].strip()}
        rest = (line[:m.start()] + " " + line[m.end():]).strip()
        rest = re.sub(r"\s*·\s*\d+\s*(?:yrs?|mos?|years?|months?)\b.*$", "", rest, flags=re.I)
        rest = re.sub(r"[()\[\]]", " ", rest).strip(" ,|·-–")
        title = company = ""
        if rest:
            split = _split_role(rest)
            parts = [p.strip() for p in re.split(r"\s+\|\s+|\s+·\s+|,\s+|\s+[-–]\s+", rest) if p.strip()]
            if split:
                title, company = split
            elif len(parts) >= 2:
                title, company = parts[0], parts[1]
            else:
                continue  # one bare phrase and a date: not clear enough
        else:
            prev = [l for l in body[max(used + 1, i - 2):i] if not _RANGE.search(l)]
            if len(prev) < 2:
                continue
            title, company = prev[-2], re.split(r"\s+·\s+", prev[-1])[0]
        title, company = title.strip(), company.strip()
        if (len(title) > 80 or len(company) > 80 or not title
                or any(re.match(r"^[-*•–]", x) for x in (title, company))
                or re.search(r"\b\d+\s*(?:yrs?|mos?|years?|months?)\b", f"{title} {company}", re.I)):
            continue  # a bullet or a "5 yrs" line: not clearly a role
        out.append({"title": title, "company": company, **span})
        used = i
    return out[:15]


def _header_location(headline: str) -> str | None:
    """'Senior Backend Engineer | Austin, TX' -> 'Austin, TX'."""
    for part in [p.strip() for p in re.split(r"\s+[|·•]\s+", headline)][1:]:
        if _LOCATION.match(part):
            return part
    return None


def _about_section(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if line.lower().rstrip(":") in ("about", "summary", "professional summary", "profile"):
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.lower() in _RESUME_SECTIONS or _is_section(nxt):
                    break
                nxt = re.sub(r"\s*(?:…|\.\.\.)?\s*see more$", "", nxt, flags=re.I).strip()
                if nxt and nxt.lower() not in _NOISE:
                    body.append(nxt)
            return "\n".join(body)[:1500] or None
    return None


def _website(text: str) -> str | None:
    for m in _URL.finditer(text):
        if "linkedin.com" not in m.group(0).lower():
            return m.group(0)
    bare = [m for m in _BARE.finditer(text) if "linkedin.com" not in m.group(0).lower()]
    # A bare domain counts when it has a path or www (github.com/x, www.site.com), or stands on its own.
    for m in bare:
        if m.group(2) or m.group(1).startswith("www."):
            return "https://" + m.group(0)
    for m in bare:
        line_start = text.rfind("\n", 0, m.start()) + 1
        line_end = text.find("\n", m.end())
        line = text[line_start:line_end if line_end >= 0 else len(text)]
        tokens = (re.sub(r"^[\w ]{1,20}:\s*", "", t.strip()) for t in re.split(r"[·•|]", line))
        if m.group(0) in tokens:  # on its own, or after a label like "Site:"
            return "https://" + m.group(0)
    return None


def _is_name(line: str) -> bool:
    return (len(line) <= 60 and not _EMAIL.search(line) and "http" not in line and not _BARE.search(line.lower())
            and 1 <= len(line.split()) <= 5 and not any(ch.isdigit() for ch in line))


def quick_parse(text: str) -> dict[str, Any]:
    """Pull the obvious bits out of pasted text without any AI. The recruiter reviews before saving."""
    text = text.strip()
    out: dict[str, Any] = {}
    if not text:
        return out
    if m := _EMAIL.search(text):
        out["email"] = m.group(0).lower()
    if m := _LINKEDIN.search(text):
        out["linkedin_url"] = m.group(0)
    for m in _PHONE.finditer(text):
        digits = re.sub(r"\D", "", m.group(0))
        if 9 <= len(digits) <= 15 and not re.fullmatch(r"\(?(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}", m.group(0).strip()):
            phone = m.group(0).strip()
            if phone.startswith("(") and ")" not in phone:
                phone = phone[1:]
            out["phone"] = phone
            break
    if site := _website(text):
        out["website"] = site
    raw = [l.strip() for l in text.splitlines() if l.strip()]
    lines = [c for c in (_clean_line(l) for l in raw) if c]
    if lines and _is_name(lines[0]):
        name = lines[0]
        out["name"] = name.title() if name.isupper() else name
        rest = [l for l in lines[1:] if l.lower() != name.lower()]  # LinkedIn often repeats the name
        if rest and len(rest[0]) <= 160 and not _EMAIL.search(rest[0]) and rest[0].lower() not in _SECTIONS:
            headline = rest[0]
            sep = " at " if " at " in headline else " @ " if " @ " in headline else None
            if sep:
                t, _, c = headline.partition(sep)
                out["current_title"], out["current_company"] = t.strip(" |-"), c.split("|")[0].strip(" |-")
            else:
                out["current_title"] = headline.split("|")[0].strip()
            if loc := _header_location(headline):
                out["location"] = loc
            elif len(rest) > 1 and _LOCATION.match(rest[1].split(" · ")[0]):
                out["location"] = rest[1].split(" · ")[0]
    marks = _linkedin_marks(text)
    if marks >= 2:
        out["source"] = "LinkedIn"
    elif marks == 0:
        out["source"] = "Resume"
    # A link pasted under Skills (often the profile URL LinkedIn puts at the end) is never a skill.
    url_like = re.compile(r"(?i)://|^www\.|@|^[\w-]+(\.[\w-]+)*\.[a-z]{2,}/|"
                          r"^[\w-]+(\.[\w-]+)*\.(com|org|net|edu|gov|co\.uk)$")
    if tags := [t for t in _skills_section(raw) if not url_like.search(t.strip())]:
        out["tags"] = tags
    if history := _experience_section(raw):
        out["work_history"] = history
        # The current role in Experience beats the headline ("Founding Engineer @ Vercel alum" is a tagline).
        now = next((h for h in history if h.get("end") == "now" and h.get("title")), None)
        if now:
            out["current_title"] = now["title"]
            if now.get("company"):
                out["current_company"] = now["company"]
    out["about"] = _about_section(raw) or text[:1500]
    return out
