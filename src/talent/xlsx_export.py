"""The spreadsheets Talent Search makes: the Candidates table, the whole list, a client pipeline, and the file a
recruiter forwards to a hiring manager. The writer itself is xlsx.py."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

from . import importers
from .searchquery import years_of_experience
from .store import Store
from .xlsx import Link, Sheet, build_workbook

_LOCAL_HOSTS = {"localhost", "127.0.0.1", "0.0.0.0", "::1", "[::1]"}


def is_public_link(url: str | None) -> bool:
    """A web address a hiring manager could open: http(s) with a real host, never this computer's own."""
    try:
        parts = urlsplit((url or "").strip())
    except ValueError:
        return False
    host = (parts.hostname or "").lower()
    if parts.scheme not in ("http", "https") or not host:
        return False
    return host not in _LOCAL_HOSTS and not host.endswith(".local") and not host.endswith(".localhost")


def _day(value: Any) -> date | None:
    """An ISO date or time text as a date (None when it is empty or can't be read)."""
    s = str(value or "").strip()[:10]
    try:
        return date.fromisoformat(s)
    except ValueError:
        return None


def slug(text: str, fallback: str = "") -> str:
    s = unicodedata.normalize("NFKD", text or "").encode("ascii", "ignore").decode()
    s = re.sub(r"[^a-z0-9]+", "-", s.lower()).strip("-")[:60].strip("-")
    return s or fallback


def stamp(now: datetime | None = None) -> str:
    return (now or datetime.now()).strftime("%Y-%m-%d")


def _link_cell(url: str | None) -> Any:
    url = (url or "").strip()
    return Link(url, url) if is_public_link(url) else ""


def _about_sheet(lines: Iterable[tuple[str, str]], name: str = "About this file") -> Sheet:
    return Sheet(name, ["What", "Details"], [list(x) for x in lines], widths=[26, 80], autofilter=False)


# --------------------------------------------------------------- the Candidates table
TABLE_HEADERS = ["Name", "Title", "Company", "Job", "Stage", "More jobs", "Location", "Email", "Phone", "LinkedIn",
                 "Website", "Other links", "Years of experience", "Skills", "Last contacted", "Updated", "Added",
                 "Color"]


def table_workbook(store: Store, rows: list[dict[str, Any]], total: int, *, filters: str = "", cap: int | None = None,
                   now: datetime | None = None) -> bytes:
    """The rows the Candidates table shows, in the order it shows them (rows come with main_job, main_stage and
    more_jobs, as the table has them). `total` is how many matched, so a cut-off list says so."""
    years = store.experience_years()
    body = []
    for c in rows:
        y = years.get(c["id"])
        body.append([
            c["name"], c.get("current_title") or "", c.get("current_company") or "", c.get("main_job") or "",
            c.get("main_stage") or "", c.get("more_jobs") or None, c.get("location") or "", c.get("email") or "",
            c.get("phone") or "", _link_cell(c.get("linkedin_url")), _link_cell(c.get("website")),
            "\n".join(u for u in (c.get("links") or []) if is_public_link(u)),
            None if y is None else round(y, 1), ", ".join(c.get("tags") or []),
            _day(c.get("last_contacted_at")), _day(c.get("updated_at")), _day(c.get("created_at")),
            c.get("color") or ""])
    made = now or datetime.now()
    about = [("Made on", made.strftime("%Y-%m-%d %H:%M")), ("People in this file", str(len(rows)))]
    if filters:
        about.append(("Filters", filters))
    if total > len(rows):
        about.append(("Note", f"{total} people matched, but this file has the first {len(rows)}"
                              + (f" (the most one file holds is {cap})" if cap else "")
                              + ". Narrow the search to export the rest."))
    return build_workbook([Sheet("Candidates", TABLE_HEADERS, body, widths=[24, 28, 24, 28, 16, 9, 22, 28, 16, 30, 30,
                                                                         36, 12, 40, 14, 14, 14, 9]),
                           _about_sheet(about, "About this export")], title="Talent Search candidates", created=made)


# --------------------------------------------------------------- the file for a hiring manager
SHARE_HEADERS = ["Name", "Title", "Company", "Location", "Years of experience", "Skills", "Stage", "LinkedIn",
                 "Website", "Other links", "About", "Work history"]


def _year(v: str | None) -> str:
    s = (v or "").strip()
    return s[:4] if re.match(r"^\d{4}", s) else s


def history_text(entries: list[dict[str, Any]], limit: int = 8) -> str:
    """'Staff Engineer at Acme (2019 to 2023); Engineer at Beta (2016 to 2019)'."""
    out = []
    for h in entries[:limit]:
        role = " at ".join(x for x in (h.get("title"), h.get("company")) if x)
        if not role:
            continue
        start, end = _year(h.get("start")), _year(h.get("end"))
        if start or end:
            role += f" ({start or '?'} to {end or 'now'})"
        out.append(role)
    return "; ".join(out)


def share_filename(job: dict[str, Any] | None = None, now: datetime | None = None) -> str:
    base = "candidates-for-" + slug(job["title"]) if job and slug(job.get("title") or "") else "candidates"
    return f"{base}-{stamp(now)}.xlsx"


def share_workbook(store: Store, ids: list[int], *, include_contact: bool = False, include_notes: bool = False,
                   job_id: int | None = None, note: str = "", now: datetime | None = None) -> tuple[bytes, str, int]:
    """(file bytes, file name, number of people). Left out unless asked for: email, phone, notes and the timeline.
    Never in it: colors, AI review fields, sample badges, internal ids and this app's own addresses."""
    job = store.get_job(job_id) if job_id else None
    made = now or datetime.now()
    people = []
    for cid in ids:
        try:
            people.append(store.get_candidate(cid))
        except Exception:  # someone removed since the page was shown: skip them
            continue
    stages: dict[int, str] = {}
    if job:
        for p in store._rows("SELECT candidate_id, stage FROM applications WHERE job_id = ?", (job["id"],)):
            stages[p["candidate_id"]] = p["stage"]
    headers = list(SHARE_HEADERS)
    if not job:
        headers.remove("Stage")
    if include_contact:
        headers += ["Email", "Phone"]
    if include_notes:
        headers.append("Notes")
    body = []
    for c in people:
        hist = store.work_history(c["id"])
        y = years_of_experience([(h["start"], h["end"]) for h in hist])
        row = [c["name"], c.get("current_title") or "", c.get("current_company") or "", c.get("location") or "",
               None if y is None else round(y, 1), ", ".join(c.get("tags") or [])]
        if job:
            row.append(stages.get(c["id"], "Not in this job"))
        row += [_link_cell(c.get("linkedin_url")), _link_cell(c.get("website")),
                "\n".join(u for u in (c.get("links") or []) if is_public_link(u)),
                c.get("about") or "", history_text(hist)]
        if include_contact:
            row += [c.get("email") or "", c.get("phone") or ""]
        if include_notes:
            row.append(importers._notes_text(store, c["id"]))
        body.append(row)
    left_out = []
    if not include_contact:
        left_out.append("email and phone")
    if not include_notes:
        left_out.append("notes and timeline")
    about = [("Made on", made.strftime("%Y-%m-%d")), ("People", str(len(body)))]
    if job:
        about.append(("Job", job["title"] + (f" ({job['company']})" if job.get("company") else "")))
    about.append(("Included", "Name, title, company, location, years of experience, skills, links, summary and work history"
                  + (", stage in the job" if job else "") + (", email and phone" if include_contact else "")
                  + (", notes and timeline" if include_notes else "")))
    about.append(("Left out", ", ".join(left_out + ["colors", "internal review marks"])
                  if left_out else "colors, internal review marks"))
    note = " ".join((note or "").split())[:300]
    sheet = Sheet("Candidates", headers, body, preamble=[note] if note else [])
    data = build_workbook([sheet, _about_sheet(about)], title="Candidates", created=made)
    return data, share_filename(job, made), len(body)


# --------------------------------------------------------------- a client's pipeline
def pipeline_workbook(headers: list[str], rows: list[list[Any]], *, now: datetime | None = None) -> bytes:
    return build_workbook([Sheet("Pipeline", headers, rows)], title="Pipeline", created=now)


def save_to_downloads(data: bytes, name: str) -> Path:
    """Write the file into the person's Downloads folder (the one the plugin file uses), never over another file."""
    from .skills import downloads_dir

    folder = Path(downloads_dir())
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / name
    n = 2
    while path.exists():
        path = folder / f"{Path(name).stem}-{n}{Path(name).suffix}"
        n += 1
    path.write_bytes(data)
    return path
