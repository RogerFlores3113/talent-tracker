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

from . import importers, picks, searchquery
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
    """[('python', 'red'), ('title:data engineer', 'yellow')] -> 'python (red), title: Data Engineer (yellow)'."""
    return ", ".join(f"{picks.label(n)} ({c})" for n, c in hl)


def matched_picks(c: dict[str, Any], hl: list[tuple[str, str]], aliases: dict[str, str] | None = None) -> Any:
    """'python, title: Data Engineer (2 of 2)' in the color of the first pick this person matches; '' when none.
    A skill is written as the person has it; look-alike names they merged count as the one pick."""
    am = aliases or {}
    tags = c.get("tags") or []
    hits = picks.person_hits(tags, c.get("current_title"), [n for n, _ in hl], am)
    found = []
    for (n, col), hit in zip(hl, hits):
        if not hit:
            continue
        if picks.is_title(n):
            found.append((picks.label(n), col))
        else:
            found.append((next((t for t in tags if picks.canon(t, am) == picks.canon(n, am)), n), col))
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
    ("Name", 24), ("Matched picks", 28), ("Title", 28), ("Company", 24), ("Job", 28), ("Stage", 16),
    ("More jobs", 9), ("Location", 22), ("Email", 28), ("Phone", 16), ("LinkedIn", 30), ("Website", 30),
    ("Other links", 36), ("Years of experience", 12), ("Skills", 40), ("Last contacted", 14), ("Updated", 14),
    ("Added", 14), ("Label color", 11)]
TABLE_HEADERS = [h for h, _ in TABLE_COLS]


def table_workbook(store: Store, rows: list[dict[str, Any]], total: int, *, filters: Any = (), cap: int | None = None,
                   hl: Iterable[tuple[str, str]] = (), now: datetime | None = None, drop_empty: bool = False) -> bytes:
    """The rows the Candidates table shows, in the order it shows them (rows come with main_job, main_stage and
    more_jobs, as the table has them). `total` is how many matched, so a cut-off list says so. `filters` is a list
    of (what, value) pairs in words, or one line of text. `hl` adds a Matched picks column. `drop_empty` leaves out
    the columns nothing is in for anyone in the file (Name stays), like the file for a hiring manager."""
    years = store.experience_years()
    aliases = store.skill_alias_map()
    hl = picks.canon_pairs(hl, aliases)
    records = []
    for c in rows:
        records.append({
            "Name": c["name"], "Matched picks": matched_picks(c, hl, aliases) if hl else "",
            "Title": c.get("current_title") or "", "Company": c.get("current_company") or "",
            "Job": c.get("main_job") or "", "Stage": c.get("main_stage") or "", "More jobs": c.get("more_jobs") or None,
            "Location": c.get("location") or "", "Email": c.get("email") or "", "Phone": c.get("phone") or "",
            "LinkedIn": _link_cell(c.get("linkedin_url")), "Website": _link_cell(c.get("website")),
            "Other links": "\n".join(u for u in (c.get("links") or []) if is_public_link(u)),
            "Years of experience": half_years(years.get(c["id"])), "Skills": ", ".join(c.get("tags") or []),
            "Last contacted": _day(c.get("last_contacted_at")), "Updated": _day(c.get("updated_at")),
            "Added": _day(c.get("created_at")), "Label color": c.get("color") or ""})
    keep = ["Name"] if drop_empty else [h for h, _ in TABLE_COLS if h not in ("Matched picks", "Label color")]
    # (a plain dump keeps its columns)
    headers, body, widths = _assemble(TABLE_COLS, records, keep)
    made = now or datetime.now()
    about = [("Made on", made.strftime("%Y-%m-%d %H:%M")), ("People in this file", f"{len(rows):,}")]
    if isinstance(filters, str):
        filters = [("Filters", filters)] if filters else []
    about += [(k, v) for k, v in filters]
    if hl:
        about.append(("Highlighted picks", hl_words(hl)))
    if total > len(rows):
        about.append(("Note", f"This file has the first {len(rows):,} of {total:,} people"
                              + (f" (the most one file holds is {cap:,})" if cap else "")
                              + ". Narrow the search to export the rest."))
    about.append(("Not in this file", "Notes and the timeline. The CSV export has them when you tick Include notes."))
    return build_workbook([Sheet("Candidates", headers, body, widths=widths),
                           _about_sheet(about, "About this export")], title="Talent Search candidates", created=made)


_CSV_DATE_COLS = ("Last contacted", "Added", "Last updated")


def csv_in_order(store: Store, ids: list[int], hl: Iterable[tuple[str, str]] = (), include_notes: bool = False) -> bytes:
    """The importer's CSV for these people, in the order of `ids` (the CSV export itself lists people by id).
    Dates are plain yyyy-mm-dd. The private notes and the timeline are left out unless `include_notes`. (The full
    backup in Settings keeps everything: it does not come through here.) Cells that look like formulas keep their
    leading apostrophe. With `hl`, a "Matched picks" column follows
    Name, with the same words as the xlsx ("python, sql (2 of 2)"); the importer skips a column it doesn't know."""
    wanted = list(dict.fromkeys(ids))
    aliases = store.skill_alias_map()
    hl = picks.canon_pairs(hl, aliases)
    raw = importers.export_csv(store, wanted)
    parsed = list(csv.reader(io.StringIO(raw)))
    head, body = parsed[0], parsed[1:]
    date_at = [i for i, h in enumerate(head) if h in _CSV_DATE_COLS]
    stage_at = head.index("Stage changed") if "Stage changed" in head else -1
    notes_at = head.index("Notes") if "Notes" in head else -1

    def tidy(row: list[str]) -> list[str]:
        row = list(row)
        for i in date_at:
            if i < len(row):
                row[i] = row[i][:10] if _day(row[i]) else row[i]
        if 0 <= stage_at < len(row):
            row[stage_at] = "; ".join((_day(v) and v.strip()[:10]) or v.strip() for v in row[stage_at].split(";"))
        return row

    if not include_notes and notes_at >= 0:
        head = head[:notes_at] + head[notes_at + 1:]
    asked = set(wanted)
    mine = [c for c in store.all_candidates() if c["id"] in asked]  # the order the rows came out in
    body = [tidy(r) for r in body]
    if not include_notes and notes_at >= 0:
        body = [r[:notes_at] + r[notes_at + 1:] for r in body]
    by_id = dict(zip((c["id"] for c in mine), body))
    by_person = {c["id"]: c for c in mine}
    out = io.StringIO()
    w = csv.writer(out)
    if hl:
        w.writerow([head[0], "Matched picks", *head[1:]])
    else:
        w.writerow(head)
    for i in wanted:
        if i in by_id:
            row = by_id[i]
            if hl:
                m = matched_picks(by_person[i], hl, aliases)
                row = [row[0], importers._safe_cell(m.text if isinstance(m, Colored) else m), *row[1:]]
            w.writerow(row)
    return b"\xef\xbb\xbf" + out.getvalue().encode("utf-8")


# --------------------------------------------------------------- the file for a hiring manager
NOTES_MODES = ("none", "latest", "all")
SHARE_COLS: list[tuple[str, float]] = [
    ("Name", 20), ("Matched picks", 28), ("Title", 22), ("Company", 18), ("Location", 16),
    ("Years of experience", 11), ("Skills", 32), ("Stage", 13), ("LinkedIn", 24), ("Website", 24),
    ("Other links", 28), ("About", 50), ("Work history", 42), ("Email", 24), ("Phone", 15), ("Notes", 50)]
SHARE_HEADERS = [h for h, _ in SHARE_COLS if h not in ("Matched picks", "Email", "Phone", "Notes")]


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
    """shortlist-<company>-<job title>-YYYY-MM-DD.xlsx when a job is picked (no company part when it has none), or
    shortlist-YYYY-MM-DD.xlsx with no job. The note at the top is free text (it may say anything), so it is never
    part of a file name; `note` is only kept so older callers still work."""
    name = slug((job or {}).get("title") or "")
    name = re.sub(r"^shortlist(-for)?(-|$)", "", name)  # "Shortlist for X" would say shortlist twice
    company = slug((job or {}).get("company") or "") if job else ""
    parts = [p for p in (company, name) if p]
    return f"shortlist-{'-'.join(parts) + '-' if parts else ''}{stamp(now)}.xlsx"


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
                leave_out: Iterable[int] = (), skip_notes: Iterable[int] = ()) -> dict[str, Any]:
    """What the hiring manager's sheet holds: headers, rows, widths, and how many people were left out as rejected.
    `leave_out` names people who stay out of the file; `skip_notes` names people whose note stays out of it."""
    job = store.get_job(job_id) if job_id else None
    aliases = store.skill_alias_map()
    hl = picks.canon_pairs(hl, aliases)
    out_ids, no_notes = set(leave_out), set(skip_notes)
    people = []
    for cid in dict.fromkeys(i for i in ids if i not in out_ids):
        try:
            people.append(store.get_candidate(cid))
        except Exception:  # someone removed since the page was shown: skip them
            continue
    people, stage_of, left_out = _pick_people(store, people, job, include_rejected)
    records = []
    for c in people:
        hist = store.work_history(c["id"])
        y = half_years(years_of_experience([(h["start"], h["end"]) for h in hist]))
        rec = {"Name": c["name"], "Matched picks": matched_picks(c, hl, aliases) if hl else "",
               "Title": c.get("current_title") or "", "Company": c.get("current_company") or "",
               "Location": c.get("location") or "", "Years of experience": y, "Skills": ", ".join(c.get("tags") or []),
               "Stage": stage_of.get(c["id"], "") if job else "",
               "LinkedIn": _link_cell(c.get("linkedin_url")), "Website": _link_cell(c.get("website")),
               "Other links": "\n".join(u for u in (c.get("links") or []) if is_public_link(u)),
               "About": c.get("about") or "", "Work history": history_text(hist)}
        if include_contact:
            rec["Email"], rec["Phone"] = c.get("email") or "", c.get("phone") or ""
        if c["id"] in no_notes:
            rec["Notes"] = ""
        elif notes == "all":
            rec["Notes"] = importers._notes_text(store, c["id"])
        elif notes == "latest":
            rec["Notes"] = _latest_note(store, c["id"])
        records.append(rec)
    cols = [(h, w) for h, w in SHARE_COLS
            if (h != "Stage" or (job and any(c["id"] in stage_of for c in people))) and (h != "Matched picks" or hl) and (h not in ("Email", "Phone") or include_contact)
            and (h != "Notes" or notes in ("latest", "all"))]
    headers, rows, widths = _assemble(cols, records, keep=["Name"])
    return {"headers": headers, "rows": rows, "widths": widths, "job": job, "left_out_rejected": left_out,
            "hl": hl, "contact": include_contact, "notes": notes}


def share_workbook(store: Store, ids: list[int], *, include_contact: bool = False, include_notes: bool = False,
                   notes: str = "", job_id: int | None = None, note: str = "", hl: Iterable[tuple[str, str]] = (),
                   include_rejected: bool = False, now: datetime | None = None, leave_out: Iterable[int] = (),
                   skip_notes: Iterable[int] = ()) -> tuple[bytes, str, int]:
    """(file bytes, file name, number of people). Left out unless asked for: email, phone, notes and the timeline.
    Never in it: colors, AI review fields, sample badges, internal ids and this app's own addresses. `notes` is
    "none", "latest" (the newest note only) or "all" (notes and the timeline); include_notes=True means "all"."""
    notes = notes if notes in NOTES_MODES else ("all" if include_notes else "none")
    made = now or datetime.now()
    t = share_table(store, ids, include_contact=include_contact, notes=notes, job_id=job_id, hl=hl,
                    include_rejected=include_rejected, leave_out=leave_out, skip_notes=skip_notes)
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
        about.append(("Highlighted picks", hl_words(t["hl"])))
    if t["left_out_rejected"]:
        n = t["left_out_rejected"]
        about.append(("People left out", f"{n} {'person' if n == 1 else 'people'} marked rejected in this job"))
    about.append(("Included", ", ".join(headers)))
    about.append(("Left out", ", ".join(left_out + ["colors", "internal review marks", "columns with nothing in them"])))
    about.append(("Copied as written", "About, work history" + (", notes" if notes != "none" else "")
                  + ". Read them before you forward the file."))
    note = " ".join((note or "").split())[:300]
    title = f"{note}, as of {stamp(made)}" if note else ""  # the title row is the note: no note, no title row
    sheet = Sheet("Candidates", headers, body, preamble=[title] if title else [], widths=t["widths"], big_preamble=True)
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
                "people": [{"id": c["id"], "name": c["name"]} for c in kept],
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


def _cut(text: str, limit: int = 80) -> str:
    text = text or ""
    return text if len(text) <= limit else text[:limit] + "..."


def note_preview(store: Store, ids: Iterable[int], limit: int = 80) -> dict[int, dict[str, str]]:
    """For the Share page: for each person the first `limit` characters of the note text that would go in the file,
    for "latest" (the newest note) and "all" (notes and the timeline), exactly as they would be written."""
    out: dict[int, dict[str, str]] = {}
    for cid in dict.fromkeys(ids):
        try:
            store.get_candidate(cid)
        except Exception:
            continue
        out[cid] = {"latest": _cut(_latest_note(store, cid), limit), "all": _cut(importers._notes_text(store, cid), limit)}
    return out


def nobody_matches(store: Store, ids: Iterable[int], hl: Iterable[tuple[str, str]]) -> bool:
    """True when picks are highlighted but nobody in `ids` matches any of them (the Matched picks column would be empty)."""
    aliases = store.skill_alias_map()
    hl = picks.canon_pairs(hl, aliases)
    if not hl:
        return False
    for cid in dict.fromkeys(ids):
        try:
            c = store.get_candidate(cid)
        except Exception:
            continue
        if not _blank(matched_picks(c, hl, aliases)):
            return False
    return True


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
