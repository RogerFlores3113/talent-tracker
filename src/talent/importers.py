"""Getting candidates in and out: CSV import/export and a quick local read of pasted profile text."""
from __future__ import annotations

import csv
import io
import re
from typing import Any, Iterable

from .store import CANDIDATE_FIELDS, Store, split_tags

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
    ("about", ("about", "summary", "notes", "bio", "description", "comments")),
    ("source", ("source", "found via", "referral", "channel")),
    ("last_contacted_at", ("last contacted", "last contact", "contacted")),
)
FIRST_NAME = ("first name", "firstname", "given name", "first")
LAST_NAME = ("last name", "lastname", "surname", "family name", "last")
FULL_NAME = ("name", "full name", "candidate", "candidate name")

IMPORT_TARGETS = ("", "name", "first_name", "last_name") + tuple(f for f in CANDIDATE_FIELDS if f != "name")


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


def rows_to_candidates(header: list[str], rows: Iterable[list[str]], mapping: list[str]) -> Iterable[dict[str, Any]]:
    for r in rows:
        data: dict[str, Any] = {}
        first = last = ""
        for col, target in enumerate(mapping):
            if not target or col >= len(r):
                continue
            v = r[col].strip()
            if not v:
                continue
            if target == "first_name":
                first = v
            elif target == "last_name":
                last = v
            elif target == "tags":
                data["tags"] = split_tags((data.get("tags") or []) + split_tags(v))
            elif target in data:
                data[target] = f"{data[target]}\n{v}"
            else:
                data[target] = v
        if not data.get("name") and (first or last):
            data["name"] = f"{first} {last}".strip()
        yield data


def import_rows(store: Store, header: list[str], rows: list[list[str]], mapping: list[str],
                source: str = "CSV import") -> dict[str, Any]:
    added = updated = 0
    skipped: list[str] = []
    for i, data in enumerate(rows_to_candidates(header, rows, mapping), start=2):
        if not data.get("name"):
            skipped.append(f"Row {i}: no name")
            continue
        data.setdefault("source", source)
        try:
            _, created = store.upsert_candidate(**data)
        except ValueError as exc:
            skipped.append(f"Row {i}: {exc}")
            continue
        added += created
        updated += not created
    return {"added": added, "updated": updated, "skipped": skipped}


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
_PHONE = re.compile(r"(?:\+?\d[\d\s().-]{7,}\d)")
_LINKEDIN = re.compile(r"(?:https?://)?(?:[a-z]{2,3}\.)?linkedin\.com/in/[\w%-]+/?", re.I)
_URL = re.compile(r"https?://[^\s)>,]+", re.I)


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
        if 9 <= len(digits) <= 15 and not re.fullmatch(r"(19|20)\d{2}\s*[-–]\s*(19|20)\d{2}", m.group(0).strip()):
            out["phone"] = m.group(0).strip()
            break
    for m in _URL.finditer(text):
        if "linkedin.com" not in m.group(0).lower():
            out["website"] = m.group(0)
            break
    lines = [l.strip() for l in text.splitlines() if l.strip()]
    if lines:
        first = lines[0]
        if (len(first) <= 60 and not _EMAIL.search(first) and "http" not in first
                and 1 <= len(first.split()) <= 5 and not any(ch.isdigit() for ch in first)):
            out["name"] = first.title() if first.isupper() else first
            if len(lines) > 1 and len(lines[1]) <= 120 and not _EMAIL.search(lines[1]):
                headline = lines[1]
                if " at " in headline:
                    t, _, c = headline.partition(" at ")
                    out["current_title"], out["current_company"] = t.strip(" |-"), c.strip(" |-")
                else:
                    out["current_title"] = headline.split("|")[0].strip()
    out["about"] = text[:1500]
    return out
