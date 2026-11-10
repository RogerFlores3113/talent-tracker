"""The spreadsheets Talent Search makes: the Candidates table, the whole list, a client pipeline, and the file a
recruiter forwards to a hiring manager. The writer itself is xlsx.py."""
from __future__ import annotations

import re
import unicodedata
from datetime import date, datetime
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import urlsplit

import csv
import io

from . import importers, searchquery
from .searchquery import years_of_experience
from .store import Store, stage_outcome
from .xlsx import Colored, Link, Sheet, build_workbook

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


def half_years(y: float | None) -> float | None:
    """Years of experience to the nearest half year (the same as the compare page)."""
    return None if y is None else round(y * 2) / 2


def hl_words(hl: Iterable[tuple[str, str]]) -> str:
    """[('python', 'red'), ('data engineer', 'yellow')] -> 'python (red), data engineer (yellow)'."""
    return ", ".join(f"{n} ({c})" for n, c in hl)


def matched_skills(tags: Iterable[str], hl: list[tuple[str, str]]) -> Any:
    """'python, data engineer (2 of 2)' in the color of the first highlighted skill this person has; '' when none."""
    mine = {t.lower(): t for t in tags}
    found = [(mine[n], c) for n, c in hl if n in mine]
    if not found:
        return ""
    return Colored(f"{', '.join(n for n, _ in found)} ({len(found)} of {len(hl)})", found[0][1])


def _blank(v: Any) -> bool:
    return v is None or v == "" or (isinstance(v, Colored) and not v.text)


def _assemble(cols: list[tuple[str, float]], records: list[dict[str, Any]], keep: Iterable[str] = ()) -> tuple[list[str], list[list[Any]], list[float]]:
    """Headers, rows and widths for the columns in `cols` that are not empty for everyone (the names in `keep`
    stay whatever they hold)."""
    keep = set(keep)
    use = [(h, w) for h, w in cols if h in keep or any(not _blank(r.get(h)) for r in records)]
    return [h for h, _ in use], [[r.get(h) for h, _ in use] for r in records], [w for _, w in use]


# --------------------------------------------------------------- the Candidates table
TABLE_COLS: list[tuple[str, float]] = [
    ("Name", 24), ("Matched skills", 28), ("Title", 28), ("Company", 24), ("Job", 28), ("Stage", 16),
    ("More jobs", 9), ("Location", 22), ("Email", 28), ("Phone", 16), ("LinkedIn", 30), ("Website", 30),
    ("Other links", 36), ("Years of experience", 12), ("Skills", 40), ("Last contacted", 14), ("Updated", 14),
    ("Added", 14), ("Label color", 11)]
TABLE_HEADERS = [h for h, _ in TABLE_COLS]


def table_workbook(store: Store, rows: list[dict[str, Any]], total: int, *, filters: Any = (), cap: int | None = None,
                   hl: Iterable[tuple[str, str]] = (), now: datetime | None = None) -> bytes:
    """The rows the Candidates table shows, in the order it shows them (rows come with main_job, main_stage and
    more_jobs, as the table has them). `total` is how many matched, so a cut-off list says so. `filters` is a list
    of (what, value) pairs in words, or one line of text. `hl` adds a Matched skills column."""
    years = store.experience_years()
    hl = list(hl)
    records = []
    for c in rows:
        records.append({
            "Name": c["name"], "Matched skills": matched_skills(c.get("tags") or [], hl) if hl else "",
            "Title": c.get("current_title") or "", "Company": c.get("current_company") or "",
            "Job": c.get("main_job") or "", "Stage": c.get("main_stage") or "", "More jobs": c.get("more_jobs") or None,
            "Location": c.get("location") or "", "Email": c.get("email") or "", "Phone": c.get("phone") or "",
            "LinkedIn": _link_cell(c.get("linkedin_url")), "Website": _link_cell(c.get("website")),
            "Other links": "\n".join(u for u in (c.get("links") or []) if is_public_link(u)),
            "Years of experience": half_years(years.get(c["id"])), "Skills": ", ".join(c.get("tags") or []),
            "Last contacted": _day(c.get("last_contacted_at")), "Updated": _day(c.get("updated_at")),
            "Added": _day(c.get("created_at")), "Label color": c.get("color") or ""})
    keep = [h for h, _ in TABLE_COLS if h not in ("Matched skills", "Label color")]  # a plain dump keeps its columns
    headers, body, widths = _assemble(TABLE_COLS, records, keep)
    made = now or datetime.now()
    about = [("Made on", made.strftime("%Y-%m-%d %H:%M")), ("People in this file", f"{len(rows):,}")]
    if isinstance(filters, str):
        filters = [("Filters", filters)] if filters else []
    about += [(k, v) for k, v in filters]
    if hl:
        about.append(("Highlighted skills", hl_words(hl)))
    if total > len(rows):
        about.append(("Note", f"This file has the first {len(rows):,} of {total:,} people"
                              + (f" (the most one file holds is {cap:,})" if cap else "")
                              + ". Narrow the search to export the rest."))
    about.append(("Not in this file", "Notes and the timeline. The CSV export has them."))
    return build_workbook([Sheet("Candidates", headers, body, widths=widths),
                           _about_sheet(about, "About this export")], title="Talent Search candidates", created=made)


def csv_in_order(store: Store, ids: list[int]) -> bytes:
    """The importer's CSV for these people, in the order of `ids` (the CSV export itself lists people by id).
    Cells that look like formulas keep their leading apostrophe."""
    wanted = list(dict.fromkeys(ids))
    raw = importers.export_csv(store, wanted)
    parsed = list(csv.reader(io.StringIO(raw)))
    head, body = parsed[0], parsed[1:]
    asked = set(wanted)
    found = [c["id"] for c in store.all_candidates() if c["id"] in asked]  # the order the rows came out in
    by_id = dict(zip(found, body))
    out = io.StringIO()
    w = csv.writer(out)
    w.writerow(head)
    for i in wanted:
        if i in by_id:
            w.writerow(by_id[i])
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


# --------------------------------------------------------------- the file for a hiring manager
NOTES_MODES = ("none", "latest", "all")
SHARE_COLS: list[tuple[str, float]] = [
    ("Name", 20), ("Matched skills", 28), ("Title", 22), ("Company", 18), ("Location", 16),
    ("Years of experience", 11), ("Skills", 32), ("Stage", 13), ("LinkedIn", 24), ("Website", 24),
    ("Other links", 28), ("About", 50), ("Work history", 42), ("Email", 24), ("Phone", 15), ("Notes", 50)]
SHARE_HEADERS = [h for h, _ in SHARE_COLS if h not in ("Matched skills", "Email", "Phone", "Notes")]


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


def share_filename(job: dict[str, Any] | None = None, now: datetime | None = None, note: str = "") -> str:
    """shortlist-<job title>-YYYY-MM-DD.xlsx, or shortlist-YYYY-MM-DD.xlsx with no job. The note at the top is free
    text (it may say anything), so it is never part of a file name; `note` is only kept so older callers still work."""
    name = slug((job or {}).get("title") or "")
    name = re.sub(r"^shortlist(-for)?(-|$)", "", name)  # "Shortlist for X" would say shortlist twice
    return f"shortlist-{name + '-' if name else ''}{stamp(now)}.xlsx"


def _latest_note(store: Store, candidate_id: int) -> str:
    for t in store.timeline(candidate_id, limit=200):  # newest first
        if t["kind"] == "note" and (t.get("body") or "").strip():
            return t["body"]
    return ""


def _pick_people(store: Store, people: list[dict[str, Any]], job: dict[str, Any] | None, include_rejected: bool,
                 ) -> tuple[list[dict[str, Any]], dict[int, str], int]:
    """(who goes in the file in file order, their stage in the job, how many rejected people were left out)."""
    stage_of: dict[int, str] = {}
    info: dict[str, tuple[int, str]] = {}
    if job:
        for p in store._rows("SELECT candidate_id, stage FROM applications WHERE job_id = ?", (job["id"],)):
            stage_of[p["candidate_id"]] = p["stage"]
        info = {s["name"].lower(): (s["position"], s["outcome"]) for s in store.job_stages(job["id"])}

    def outcome(cid: int) -> str:
        name = stage_of[cid]
        return info[name.lower()][1] if name.lower() in info else stage_outcome(name)

    left_out = 0
    if job:
        if not include_rejected:
            kept = [c for c in people if c["id"] not in stage_of or outcome(c["id"]) != "lost"]
            left_out = len(people) - len(kept)
            people = kept

        def rank(c: dict[str, Any]) -> tuple[int, int]:  # furthest along first; people not in the job, then the rejected
            cid = c["id"]
            if cid not in stage_of:
                return (2, 0)
            o = outcome(cid)
            pos = info.get(stage_of[cid].lower(), (0, ""))[0]
            return (3, -pos) if o == "lost" else (0, 0) if o == "hired" else (1, -pos)
        people = sorted(people, key=rank)  # a stable sort: ties keep the order the people came in
    return people, stage_of, left_out


def share_table(store: Store, ids: list[int], *, include_contact: bool = False, notes: str = "none",
                job_id: int | None = None, hl: Iterable[tuple[str, str]] = (), include_rejected: bool = False,
                ) -> dict[str, Any]:
    """What the hiring manager's sheet holds: headers, rows, widths, and how many people were left out as rejected."""
    job = store.get_job(job_id) if job_id else None
    hl = list(hl)
    people = []
    for cid in dict.fromkeys(ids):
        try:
            people.append(store.get_candidate(cid))
        except Exception:  # someone removed since the page was shown: skip them
            continue
    people, stage_of, left_out = _pick_people(store, people, job, include_rejected)
    records = []
    for c in people:
        hist = store.work_history(c["id"])
        y = half_years(years_of_experience([(h["start"], h["end"]) for h in hist]))
        rec = {"Name": c["name"], "Matched skills": matched_skills(c.get("tags") or [], hl) if hl else "",
               "Title": c.get("current_title") or "", "Company": c.get("current_company") or "",
               "Location": c.get("location") or "", "Years of experience": y, "Skills": ", ".join(c.get("tags") or []),
               "Stage": stage_of.get(c["id"], "") if job else "",
               "LinkedIn": _link_cell(c.get("linkedin_url")), "Website": _link_cell(c.get("website")),
               "Other links": "\n".join(u for u in (c.get("links") or []) if is_public_link(u)),
               "About": c.get("about") or "", "Work history": history_text(hist)}
        if include_contact:
            rec["Email"], rec["Phone"] = c.get("email") or "", c.get("phone") or ""
        if notes == "all":
            rec["Notes"] = importers._notes_text(store, c["id"])
        elif notes == "latest":
            rec["Notes"] = _latest_note(store, c["id"])
        records.append(rec)
    cols = [(h, w) for h, w in SHARE_COLS
            if (h != "Stage" or (job and any(c["id"] in stage_of for c in people))) and (h != "Matched skills" or hl) and (h not in ("Email", "Phone") or include_contact)
            and (h != "Notes" or notes in ("latest", "all"))]
    headers, rows, widths = _assemble(cols, records, keep=["Name"])
    return {"headers": headers, "rows": rows, "widths": widths, "job": job, "left_out_rejected": left_out,
            "hl": hl, "contact": include_contact, "notes": notes}


def share_workbook(store: Store, ids: list[int], *, include_contact: bool = False, include_notes: bool = False,
                   notes: str = "", job_id: int | None = None, note: str = "", hl: Iterable[tuple[str, str]] = (),
                   include_rejected: bool = False, now: datetime | None = None) -> tuple[bytes, str, int]:
    """(file bytes, file name, number of people). Left out unless asked for: email, phone, notes and the timeline.
    Never in it: colors, AI review fields, sample badges, internal ids and this app's own addresses. `notes` is
    "none", "latest" (the newest note only) or "all" (notes and the timeline); include_notes=True means "all"."""
    notes = notes if notes in NOTES_MODES else ("all" if include_notes else "none")
    made = now or datetime.now()
    t = share_table(store, ids, include_contact=include_contact, notes=notes, job_id=job_id, hl=hl,
                    include_rejected=include_rejected)
    job, headers, body = t["job"], t["headers"], t["rows"]
    left_out = []
    if not include_contact:
        left_out.append("email and phone")
    if notes == "none":
        left_out.append("notes and timeline")
    elif notes == "latest":
        left_out.append("older notes and the timeline")
    about = [("Made on", made.strftime("%Y-%m-%d")), ("People", f"{len(body):,}")]
    if job:
        about.append(("Job", job["title"] + (f" ({job['company']})" if job.get("company") else "")))
    if t["hl"]:
        about.append(("Highlighted skills", hl_words(t["hl"])))
    if t["left_out_rejected"]:
        n = t["left_out_rejected"]
        about.append(("People left out", f"{n} {'person' if n == 1 else 'people'} marked rejected in this job"))
    about.append(("Included", ", ".join(headers)))
    about.append(("Left out", ", ".join(left_out + ["colors", "internal review marks", "columns with nothing in them"])))
    about.append(("Copied as written", "About, work history" + (", notes" if notes != "none" else "")
                  + ". Read them before you forward the file."))
    note = " ".join((note or "").split())[:300]
    sheet = Sheet("Candidates", headers, body, preamble=[note] if note else [], widths=t["widths"], big_preamble=True)
    data = build_workbook([sheet, _about_sheet(about)], title="Candidates", created=made)
    return data, share_filename(job, made, note), len(body)


def share_preview(store: Store, ids: list[int], *, hl: Iterable[tuple[str, str]]) -> dict[str, Any]:
    """For the Share page: which columns could be in the file and when (base, stage, contact, notes), and, for each
    job the person could pick (key "" is no job), who the file would hold: the first names, how many people, and
    whether the Stage column is in it, with rejected people left out ("out") and put back in ("in"). Worked out here,
    so the page can show it exactly without asking the server again."""
    hl = list(hl)
    full = share_table(store, ids, include_contact=True, notes="all", job_id=None, hl=hl)
    cols = []
    for h in full["headers"]:
        kind = "contact" if h in ("Email", "Phone") else "notes" if h == "Notes" else "base"
        cols.append({"name": h, "kind": kind})
        if h == "Skills":
            cols.append({"name": "Stage", "kind": "stage"})
    people = []
    for cid in dict.fromkeys(ids):
        try:
            people.append(store.get_candidate(cid))
        except Exception:
            continue

    def view(job: dict[str, Any] | None, rejected: bool) -> dict[str, Any]:
        kept, stage_of, _ = _pick_people(store, people, job, rejected)
        return {"names": [c["name"] for c in kept[:3]], "count": len(kept),
                "stage": bool(job and any(c["id"] in stage_of for c in kept))}

    plain = view(None, True)
    jobs: dict[str, Any] = {"": {"out": plain, "in": plain}}
    mine = {c["id"] for c in people}
    in_jobs = {r["job_id"] for r in store._rows("SELECT DISTINCT job_id, candidate_id FROM applications")
               if r["candidate_id"] in mine}
    for j in store.membership_choices()["job_choices"]:
        if j["id"] not in in_jobs:  # nobody here is in it: the file is the same as with no job
            jobs[str(j["id"])] = {"out": plain, "in": plain}
            continue
        job = store.get_job(j["id"])
        jobs[str(j["id"])] = {"out": view(job, False), "in": view(job, True)}
    return {"cols": cols, "names": plain["names"], "jobs": jobs}


# --------------------------------------------------------------- a client's pipeline
def pipeline_workbook(headers: list[str], rows: list[list[Any]], *, client: str = "", now: datetime | None = None) -> bytes:
    made = now or datetime.now()
    title = f"{client} pipeline, as of {stamp(made)}" if client else f"Pipeline, as of {stamp(made)}"
    return build_workbook([Sheet("Pipeline", headers, rows, preamble=[title], big_preamble=True)],
                          title="Pipeline", created=now)


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
