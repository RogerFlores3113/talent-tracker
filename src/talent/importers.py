"""Getting candidates in and out: CSV import/export and a quick local read of pasted profile text."""
from __future__ import annotations

import csv
import io
import os
import re
import sqlite3
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable

from .paths import backups_dir
from .store import CANDIDATE_FIELDS, DuplicateValue, IdentityConflict, Store, parse_when, split_tags

EXPORT_FIELDS = (
    "name", "email", "phone", "linkedin_url", "website", "current_title", "current_company", "location",
    "tags", "about", "source", "last_contacted_at", "created_at", "updated_at",
)
EXPORT_HEADERS = {
    "name": "Name", "email": "Email", "phone": "Phone", "linkedin_url": "LinkedIn", "website": "Website",
    "current_title": "Title", "current_company": "Company", "location": "Location", "tags": "Skills",
    "about": "About", "source": "Source", "last_contacted_at": "Last contacted", "created_at": "Added",
    "updated_at": "Last updated", "work_history": "Work history",
}

# Header words people use in spreadsheets, mapped to our fields.
_HEADER_HINTS: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("email", ("email", "e-mail", "mail")),
    ("linkedin_url", ("linkedin", "profile url", "li url")),
    ("phone", ("phone", "mobile", "cell", "tel")),
    ("website", ("website", "github", "portfolio", "site", "url")),
    ("current_title", ("title", "role", "position", "headline", "job")),
    ("current_company", ("company", "employer", "organization", "organisation", "current company")),
    ("location", ("location", "city", "where", "based", "country", "region")),
    ("tags", ("skills", "tags", "keywords", "tech", "stack")),
    ("note", ("notes", "note", "comments", "comment")),
    ("about", ("about", "summary", "bio", "description")),
    ("source", ("source", "found via", "referral", "channel")),
    ("last_contacted_at", ("last contacted", "last contact", "contacted")),
)
FIRST_NAME = ("first name", "firstname", "given name", "first")
LAST_NAME = ("last name", "lastname", "surname", "family name", "last")
FULL_NAME = ("name", "full name", "candidate", "candidate name")

# "note" is not a candidate field: it becomes a note on the candidate's timeline.
IMPORT_TARGETS = ("", "name", "first_name", "last_name") + tuple(f for f in CANDIDATE_FIELDS if f != "name") + ("note",)
BACKUPS_KEPT = 10


def decode(data: bytes) -> str:
    for enc in ("utf-8-sig", "cp1252", "latin-1"):
        try:
            return data.decode(enc)
        except UnicodeDecodeError:
            continue
    return data.decode("utf-8", errors="replace")


def read_csv(text: str) -> tuple[list[str], list[list[str]]]:
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


def guess_mapping(header: list[str]) -> list[str]:
    """For each column, which field it most likely holds ('' = skip)."""
    out: list[str] = []
    used: set[str] = set()
    for h in header:
        low = re.sub(r"[_\-]+", " ", h.lower()).strip()
        pick = ""
        if low in FIRST_NAME:
            pick = "first_name"
        elif low in LAST_NAME:
            pick = "last_name"
        elif low in FULL_NAME:
            pick = "name"
        else:
            for f, hints in _HEADER_HINTS:
                if f not in used and any(low == w or w in low for w in hints):
                    pick = f
                    break
        if pick and pick in used:
            pick = ""
        if pick:
            used.add(pick)
        out.append(pick)
    return out


def _cell(v: str) -> str:
    """Trim, and undo the apostrophe our own export puts before + = - @ so a round trip keeps phones intact."""
    v = v.strip()
    if v[:1] == "'" and v[1:2] in ("=", "+", "-", "@", "\t", "\r"):
        v = v[1:]
    return v


def _rows(header: list[str], rows: Iterable[list[str]],
          mapping: list[str]) -> Iterable[tuple[dict[str, Any], list[str]]]:
    """Each row as (candidate fields, warnings about cells we had to leave out)."""
    for r in rows:
        data: dict[str, Any] = {}
        warnings: list[str] = []
        first = last = ""
        for col, target in enumerate(mapping):
            if not target or col >= len(r):
                continue
            v = _cell(r[col])
            if not v:
                continue
            if target == "first_name":
                first = v
            elif target == "last_name":
                last = v
            elif target == "tags":
                data["tags"] = split_tags((data.get("tags") or []) + split_tags(v))  # , ; | (never / as in ci/cd)
            elif target == "last_contacted_at":
                try:
                    data[target] = parse_when(v, default_now=False)
                except ValueError:
                    col_name = header[col] if col < len(header) and header[col] else "Last contacted"
                    warnings.append(f"left {col_name} blank, couldn't read the date {v!r}")
            elif target in data:
                data[target] = f"{data[target]}\n{v}"
            else:
                data[target] = v
        if not data.get("name") and (first or last):
            data["name"] = f"{first} {last}".strip()
        yield data, warnings


def rows_to_candidates(header: list[str], rows: Iterable[list[str]], mapping: list[str]) -> Iterable[dict[str, Any]]:
    for data, _ in _rows(header, rows, mapping):
        yield data


def backup_before_import(store: Store, keep: int = BACKUPS_KEPT, reason: str = "import") -> Path:
    """Copy the whole database into the data folder's backups/ first. Keeps the newest `keep` of these.

    `reason` names the file ("before-import-...", "before-delete-..."); each reason keeps its own `keep`."""
    folder = backups_dir()
    stamp = datetime.now().strftime("%Y-%m-%d-%H%M%S")
    prefix = "before-" + re.sub(r"[^a-z0-9]+", "-", reason.lower()).strip("-") + "-"
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
    old = sorted(folder.glob(f"{prefix}*.db"), key=lambda f: (f.stat().st_mtime, f.name))
    for f in old[:-keep] if keep > 0 else []:
        f.unlink(missing_ok=True)
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


def _has_note(store: Store, candidate_id: int, body: str) -> bool:
    return any(t["kind"] == "note" and (t.get("body") or "") == body for t in store.timeline(candidate_id, limit=500))


def import_rows(store: Store, header: list[str], rows: list[list[str]], mapping: list[str],
                source: str = "CSV import", backup: bool = True) -> dict[str, Any]:
    """Add or update one candidate per row. A bad row is skipped and listed, never fatal.

    Backs up the database first (see backup_before_import). A row that matched someone and
    changed nothing counts as `unchanged`, not `updated`. `skipped` lists the rows left out, as
    "Row N: reason" (`skipped_rows` counts them); `warnings` lists cells we left blank and notes
    we couldn't save, on rows that were still imported. Changes are logged as made by the import.
    """
    backup_file = backup_before_import(store) if backup else None
    added = updated = unchanged = skipped_rows = notes = 0
    skipped: list[str] = []
    warned: list[str] = []
    for i, (data, warnings) in enumerate(_rows(header, rows, mapping), start=2):
        note = data.pop("note", None)
        if not data.get("name"):
            skipped.append(f"Row {i}: no name")
            skipped_rows += 1
            continue
        data.setdefault("source", source)
        try:
            res = store.upsert_candidate(by="import", **data)
            cand, created = res[0], bool(res[1])
        except Exception as exc:  # one bad row must never stop the rest
            skipped.append(f"Row {i}: {_row_error(exc)}")
            skipped_rows += 1
            continue
        if created:
            added += 1
        elif cand.get("_changed"):
            updated += 1
        else:
            unchanged += 1
        warned.extend(f"Row {i}: {w}" for w in warnings)
        if note:
            try:
                if not _has_note(store, cand["id"], note):
                    store.log(cand["id"], "note", note, source=source)
                    notes += 1
            except Exception as exc:
                warned.append(f"Row {i}: note not saved ({exc})")
    return {"added": added, "updated": updated, "unchanged": unchanged, "skipped": skipped,
            "skipped_rows": skipped_rows, "warnings": warned, "notes": notes,
            "backup": str(backup_file) if backup_file else None}


def export_csv(store: Store) -> str:
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow([EXPORT_HEADERS[f] for f in EXPORT_FIELDS] + [EXPORT_HEADERS["work_history"]])
    for c in store.all_candidates():
        history = "; ".join(
            " at ".join(x for x in (h["title"], h["company"]) if x)
            + (f" ({h['start'] or ''}-{h['end'] or 'now'})" if h["start"] or h["end"] else "")
            for h in store.work_history(c["id"])
        )
        row = [", ".join(c[f]) if f == "tags" else (c.get(f) or "") for f in EXPORT_FIELDS]
        w.writerow([_safe_cell(v) for v in row] + [_safe_cell(history)])
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
    """Drop the LinkedIn bits that ride along on a line: pronouns, '· 2nd', '· Contact info'."""
    parts = [p.strip() for p in re.split(r"\s+[·•]\s+|^[·•]\s*", line)]
    keep = [_PRONOUNS.sub("", p).strip() for p in parts if not _is_noise(p)]
    return " · ".join(p for p in keep if p)


def _looks_like_linkedin(text: str) -> bool:
    if _LINKEDIN.search(text):
        return True
    low = text.lower()
    marks = ("contact info" in low, bool(re.search(r"\b\d[\d,+]* (?:connections|followers)\b", low)),
             bool(re.search(r"(?m)^\s*(?:·\s*)?(?:1st|2nd|3rd\+?)\s*$", low)), "endorse" in low,
             bool(re.search(r"(?m)^\s*experience\s*$", low)) and bool(re.search(r"(?m)^\s*about\s*$", low)))
    return sum(marks) >= 2


def _about_section(lines: list[str]) -> str | None:
    for i, line in enumerate(lines):
        if line.lower() == "about":
            body: list[str] = []
            for nxt in lines[i + 1:]:
                if nxt.lower() in _SECTIONS:
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
            if len(rest) > 1 and _LOCATION.match(rest[1].split(" · ")[0]):
                out["location"] = rest[1].split(" · ")[0]
    if _looks_like_linkedin(text):
        out["source"] = "LinkedIn"
    out["about"] = _about_section(raw) or text[:1500]
    return out
