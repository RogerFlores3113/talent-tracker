"""Text parity for the tools' replies: Codex and Claude Code show only the text, so the first time a person or a job
is named in a reply it carries the link to open it in Talent Search.

`link_names(store, text, data)` finds the people and jobs a reply is about from its structured result (only ones the
database confirms by id and name, so a saved search called "Ana" is not a person), then puts the app link after the
first mention of each: `Ana Ray (id 3, http://127.0.0.1:8765/candidates/3)`. Text inside a candidate-provided fence is
read but never rewritten, and a reply that already holds a link is left alone. At most LIMIT links go into one
reply, so a long list stays lean.
"""
from __future__ import annotations

import re
from typing import Any

from .paths import app_url
from .store import NotFound, Store

LIMIT = 60
INLINE_ABOVE = 6
_FENCE = re.compile(r"(<(data-[0-9a-f]+)>.*?</\2>)", re.S)


def _job_label(job: dict[str, Any]) -> str:
    title = job.get("title") or "that job"
    return f"{title} ({job['company']})" if job.get("company") else str(title)


def entities(store: Store, data: Any) -> tuple[dict[int, str], dict[int, str]]:
    """(candidate id -> name, job id -> "Title (Company)") for what a structured result is about, in order."""
    people: dict[int, str] = {}
    jobs: dict[int, str] = {}

    def person(cid: Any, name: Any = None) -> None:
        if not isinstance(cid, int) or isinstance(cid, bool) or cid in people or len(people) >= LIMIT:
            return
        try:
            real = store.get_candidate(cid)["name"]
        except (NotFound, LookupError, ValueError, KeyError, TypeError):
            return
        if name is None or name == real:
            people[cid] = real

    def job(jid: Any, title: Any = None) -> None:
        if not isinstance(jid, int) or isinstance(jid, bool) or jid in jobs or len(jobs) >= LIMIT:
            return
        try:
            real = store.get_job(jid)
        except (NotFound, LookupError, ValueError, KeyError, TypeError):
            return
        if title is None or title == real.get("title"):
            jobs[jid] = _job_label(real)

    def walk(v: Any, depth: int = 0) -> None:
        if depth > 5:
            return
        if isinstance(v, dict):
            if "candidate_id" in v:
                person(v.get("candidate_id"))
            if "job_id" in v:
                job(v.get("job_id"))
            if isinstance(v.get("id"), int):
                if isinstance(v.get("name"), str) and "title" not in v:
                    person(v["id"], v["name"])
                elif isinstance(v.get("title"), str) and "name" not in v:
                    job(v["id"], v["title"])
            for x in v.values():
                if isinstance(x, (dict, list)):
                    walk(x, depth + 1)
        elif isinstance(v, list):
            for x in v[:200]:
                walk(x, depth + 1)

    walk(data)
    return people, jobs


def _put(text: str, name: str, url: str, ident: str) -> str:
    """The link after the first mention of `name`. An id label that follows ("(id 3)", "[job_id 4]") takes it inside."""
    m = re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text)
    if not m:
        return text
    pos = m.end()
    if text[pos:pos + 1] in ('"', "”"):
        pos += 1
    if text.startswith(("'s", "’s"), pos):
        pos += 2
    label = re.match(rf" [\[(]((?:job[ _])?id[ _]?{ident})[\])]", text[pos:])
    if label:
        close = text[pos + label.end() - 1]
        return text[:pos + label.end() - 1] + f", {url}" + text[pos + label.end() - 1:] if close else text
    return text[:pos] + f" ({url})" + text[pos:]


def link_names(store: Store, text: str, data: Any) -> str:
    """The reply with a link for each person and job it names. With up to INLINE_ABOVE of them the links come in one
    closing "Open in Talent Search" block, so the sentences stay as they read; with more (a long list), each link
    sits right after the first mention of the name."""
    people, jobs = entities(store, data)
    if not people and not jobs:
        return text
    todo: list[tuple[str, int, str, str]] = []  # (path kind, id, name, url)
    for kind, found in (("candidates", people), ("jobs", jobs)):
        for ident, name in found.items():
            if list(found.values()).count(name) > 1:
                continue  # two saved people (or jobs) share the name: a link after it would name the wrong one
            url = app_url(f"/{kind}/{ident}")
            if url not in text and re.search(r"(?<!\w)" + re.escape(name) + r"(?!\w)", text):
                todo.append((kind, ident, name, url))
    if not todo:
        return text
    if len(todo) <= INLINE_ABOVE:
        return text.rstrip("\n") + "\nOpen in Talent Search:\n" + "\n".join(f"- {name}: {url}" for _k, _i, name, url in todo)
    pieces = _FENCE.split(text)
    plain_idx = list(range(0, len(pieces), 3))  # split() with two groups: [plain, fence, marker, plain, ...]
    for _kind, ident, name, url in todo:
        for i in plain_idx:
            new = _put(pieces[i], name, url, str(ident))
            if new != pieces[i]:
                pieces[i] = new
                break
    return "".join(p for k, p in enumerate(pieces) if k % 3 != 2)
