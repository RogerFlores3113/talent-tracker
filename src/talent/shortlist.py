"""A Shortlist per job: people the AI app researched and proposed, waiting for the recruiter.

The AI app can only propose (propose_candidates). Keeping someone (saving them, or linking the person
already saved, and adding them to the job's first stage) and passing on someone are the recruiter's call,
in the app. A pass is remembered with its profile link, so the same person isn't proposed for that job
again, and other jobs' proposals say they were passed on before.
"""
from __future__ import annotations

import re
from typing import Any

from .actions import job_label
from .store import NotFound, Store, fold_name, normalize_linkedin, now_iso

MAX_BATCH = 25
STATUSES = ("proposed", "kept", "passed")
LIMITS = {"name": 200, "title": 200, "company": 200, "location": 200, "fit": 1000, "source": 200}
REASON_LIMIT = 300
_PROFILE = re.compile(r"^https://linkedin\.com/in/([^/?#]+)")


class NeedsChoice(ValueError):
    """Keep can't tell whether this is someone already saved under the same name."""

    def __init__(self, prospect: dict[str, Any], matches: list[dict[str, Any]]) -> None:
        self.prospect, self.matches = prospect, matches
        n = len(matches)
        super().__init__(f"{n} saved {'person has' if n == 1 else 'people have'} the name {prospect['name']}. "
                         "Choose the same person or someone new on the Shortlist.")


def _clip(value: Any, limit: int) -> str | None:
    text = " ".join(str(value or "").split())
    return text[:limit].rstrip() or None


def profile_url(url: Any) -> tuple[str | None, str | None]:
    """(the canonical linkedin.com/in/ link, None), (None, None) when no link was given, or (None, why not)."""
    raw = _clip(url, 500)
    if not raw:
        return None, None
    m = _PROFILE.match(normalize_linkedin(raw) or "")
    if not m:
        return None, "not a linkedin.com/in/ profile link (Sales Navigator and Recruiter links can't be checked)"
    return "https://linkedin.com/in/" + m.group(1), None


def _job_name(job: dict[str, Any]) -> str:
    return f"{job['title']} ({job['company']})" if job.get("company") else job["title"]


def _brief(c: dict[str, Any]) -> dict[str, Any]:
    return {k: c.get(k) for k in ("id", "name", "current_title", "current_company") if c.get(k)}


def _pipelines(store: Store, candidate_id: int) -> list[dict[str, Any]]:
    return [{"job_id": p["job_id"], "job": job_label(p["job_title"], p.get("job_company")), "stage": p["stage"]}
            for p in store.candidate_pipelines(candidate_id)]


def checks(store: Store, p: dict[str, Any]) -> dict[str, Any]:
    """What the recruiter should know before keeping someone: the saved person with this profile link
    (with their jobs), people saved under the same name, and other jobs where they were passed on."""
    out: dict[str, Any] = {}
    saved = store.find_duplicate(linkedin_url=p["linkedin_url"]) if p.get("linkedin_url") else None
    if saved:
        out["saved"] = {**_brief(saved), "pipelines": _pipelines(store, saved["id"]),
                        "same_name": fold_name(saved["name"]) == fold_name(p["name"])}
    else:
        same = same_name(store, p)
        if same:
            out["same_name"] = [{**_brief(c), "pipelines": _pipelines(store, c["id"])} for c in same[:5]]
    if p.get("linkedin_url"):
        passed = store._rows(
            """SELECT p.job_id, p.pass_reason, p.decided_at, j.title, j.company FROM prospects p
               JOIN jobs j ON j.id = p.job_id WHERE p.linkedin_url = ? AND p.status = 'passed' AND p.job_id <> ?
               ORDER BY p.decided_at DESC LIMIT 3""", (p["linkedin_url"], p.get("job_id") or 0))
        if passed:
            out["passed_elsewhere"] = [{"job_id": r["job_id"], "job": job_label(r["title"], r.get("company")),
                                        "reason": r["pass_reason"]} for r in passed]
    return out


def same_name(store: Store, p: dict[str, Any]) -> list[dict[str, Any]]:
    """People saved under this name who could be them: not someone with a different LinkedIn link."""
    return [c for c in store.find_by_name(p["name"]) if not (p.get("linkedin_url") and c.get("linkedin_url"))]


def check_lines(c: dict[str, Any]) -> list[str]:
    """The checks as short plain lines, for the AI's reply and the Shortlist page."""
    lines = []
    if c.get("saved"):
        s = c["saved"]
        jobs = "; ".join(f"{p['job']}: {p['stage']}" for p in s["pipelines"])
        lines.append(f"Already saved as {s['name']} (id {s['id']})" + ("" if s["same_name"] else ", a different name")
                     + (f". In {jobs}" if jobs else ". Not in any job"))
    for m in c.get("same_name") or []:
        lines.append(f"Same name as saved {m['name']} (id {m['id']})"
                     + (f", {m['current_title']}" if m.get("current_title") else "")
                     + (f" at {m['current_company']}" if m.get("current_company") else ""))
    for x in c.get("passed_elsewhere") or []:
        lines.append(f"Passed on for {x['job']}" + (f": {x['reason']}" if x.get("reason") else ""))
    return lines


def _skip_reason(prior: dict[str, Any]) -> str:
    if prior["status"] == "proposed":
        return "already on this Shortlist"
    if prior["status"] == "kept":
        return "already kept for this job"
    return "passed on for this job" + (f" ({prior['pass_reason']})" if prior.get("pass_reason") else "")


def propose(store: Store, job_id: int, people: list[dict[str, Any]], *, by: str = "ai") -> dict[str, Any]:
    """Put people on a job's Shortlist. Skips (and says why) anyone already proposed, kept or passed on for
    this job, or already in its pipeline. by="ai" logs the batch as one AI action that Undo takes back."""
    job = store.get_job(job_id)
    if not people:
        raise ValueError("No people given. Send each person's name, LinkedIn link and a line on why they fit.")
    if len(people) > MAX_BATCH:
        raise ValueError(f"Nothing added. Send at most {MAX_BATCH} people per call ({len(people)} given); "
                         "split them into several calls.")
    in_job = {a["candidate_id"]: a["stage"] for a in store.pipeline(job_id)}
    prior = store._rows("SELECT * FROM prospects WHERE job_id = ?", (job_id,))
    by_url = {r["linkedin_url"]: r for r in prior if r["linkedin_url"]}
    by_name = {fold_name(r["name"]): r for r in prior if not r["linkedin_url"]}
    seen: set[str] = set()
    rows, skipped = [], []
    for raw in people:
        name = _clip(raw.get("name"), LIMITS["name"])
        if not name:
            skipped.append({"name": "(no name)", "reason": "no name given"})
            continue
        url, problem = profile_url(raw.get("linkedin_url"))
        if problem:
            skipped.append({"name": name, "linkedin_url": raw.get("linkedin_url"), "reason": problem})
            continue
        key = url or "name:" + fold_name(name)
        if key in seen:
            skipped.append({"name": name, "reason": "listed twice in this call"})
            continue
        seen.add(key)
        before = by_url.get(url) if url else by_name.get(fold_name(name))
        if before:
            skipped.append({"name": name, "linkedin_url": url, "reason": _skip_reason(before)})
            continue
        saved = store.find_duplicate(linkedin_url=url) if url else None
        if saved and saved["id"] in in_job:
            skipped.append({"name": name, "linkedin_url": url, "candidate_id": saved["id"],
                            "reason": f"already in this job's pipeline, in {in_job[saved['id']]}"})
            continue
        row = {"job_id": job_id, "linkedin_url": url, "name": name,
               **{k: _clip(raw.get(k), LIMITS[k]) for k in ("title", "company", "location", "fit", "source")}}
        row["source"] = row["source"] or ("LinkedIn" if url else None)
        rows.append(row)
    added = []
    if rows:
        with store._write():
            ts = now_iso()
            for r in rows:
                pid = store._conn.execute(
                    "INSERT INTO prospects(job_id, linkedin_url, name, title, company, location, fit, source,"
                    " proposed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, r["linkedin_url"], r["name"], r["title"], r["company"], r["location"], r["fit"],
                     r["source"], by, ts, ts)).lastrowid
                added.append({"id": pid, **r})
            if by == "ai":
                ids = [a["id"] for a in added]
                batch = store._log_ai("prospects_proposed", job_id=job_id, body=str(len(ids)),
                                      new={"prospect_ids": ids})
                store._conn.execute(f"UPDATE prospects SET batch_id = ? WHERE id IN ({', '.join('?' * len(ids))})",
                                    (batch, *ids))
    for a in added:
        a["checks"] = checks(store, a)
    return {"job": job, "added": added, "skipped": skipped, "counts": counts(store, job_id)}


def counts(store: Store, job_id: int) -> dict[str, int]:
    out = {s: 0 for s in STATUSES}
    for r in store._rows("SELECT status, count(*) AS n FROM prospects WHERE job_id = ? GROUP BY status", (job_id,)):
        out[r["status"]] = r["n"]
    return out


def waiting(store: Store, job_id: int) -> int:
    """How many people wait on this job's Shortlist (the badge on the job)."""
    return store._row("SELECT count(*) AS n FROM prospects WHERE job_id = ? AND status = 'proposed'", (job_id,))["n"]


def listed(store: Store, job_id: int, status: str | None = "proposed", with_checks: bool = True) -> list[dict[str, Any]]:
    """A job's Shortlist rows, oldest first for waiting ones, newest decision first otherwise."""
    status = (status or "all").strip().lower()
    if status not in (*STATUSES, "all"):
        raise ValueError("status is proposed, kept, passed or all.")
    order = "id" if status == "proposed" else "coalesce(decided_at, created_at) DESC, id DESC"
    rows = store._rows(f"SELECT * FROM prospects WHERE job_id = ? AND (? = 'all' OR status = ?) ORDER BY {order}",
                       (job_id, status, status))
    if with_checks:
        for r in rows:
            if r["status"] == "proposed":
                r["checks"] = checks(store, r)
    return rows


def get(store: Store, job_id: int, prospect_id: int) -> dict[str, Any]:
    row = store._row("SELECT * FROM prospects WHERE id = ? AND job_id = ?", (prospect_id, job_id))
    if row is None:
        raise NotFound("That person is no longer on this Shortlist. Reload the page.")
    return row


def keep(store: Store, job_id: int, prospect_id: int, *, link_id: int | None = None,
         as_new: bool = False) -> dict[str, Any]:
    """The recruiter keeps someone: link the person already saved with this profile link (or the one they
    picked), or save them, then add them to the job's first stage. Returns {"candidate", "created", "stage",
    "already"}. Raises NeedsChoice when someone with the same name is saved and the recruiter hasn't chosen."""
    p = get(store, job_id, prospect_id)
    if p["status"] != "proposed":
        raise ValueError(f"{p['name']} was already {p['status']}.")
    target = store.get_candidate(link_id) if link_id else None
    if target is None and p["linkedin_url"]:
        target = store.find_duplicate(linkedin_url=p["linkedin_url"])
    if target is None and not as_new:
        same = same_name(store, p)
        if same:
            raise NeedsChoice(p, same)
    created = target is None
    if created:
        target = store.add_candidate(name=p["name"], linkedin_url=p["linkedin_url"], current_title=p["title"],
                                     current_company=p["company"], location=p["location"], source=p["source"])
    elif p["linkedin_url"] and not target.get("linkedin_url"):
        try:  # the recruiter said it's the same person: their empty LinkedIn takes this one
            target = store.update_candidate(target["id"], linkedin_url=p["linkedin_url"])
        except ValueError:
            pass
    already = next((a["stage"] for a in store.pipeline(job_id) if a["candidate_id"] == target["id"]), None)
    stage = already
    if not already:
        note = f"Kept from the Shortlist. Why they fit: {p['fit']}" if p.get("fit") else "Kept from the Shortlist."
        stage = store.add_to_pipeline(target["id"], job_id, None, note)["stage"]
    with store._write():
        ts = now_iso()
        store._conn.execute("UPDATE prospects SET status = 'kept', candidate_id = ?, decided_at = ?, updated_at = ?"
                            " WHERE id = ?", (target["id"], ts, ts, prospect_id))
    return {"candidate": target, "created": created, "stage": stage, "already": bool(already)}


def keep_all(store: Store, job_id: int) -> dict[str, Any]:
    """Keep everyone waiting, except people whose same-name match needs the recruiter's choice."""
    kept, left = [], []
    for p in listed(store, job_id, "proposed", with_checks=False):
        try:
            kept.append(keep(store, job_id, p["id"]))
        except NeedsChoice:
            left.append(p["name"])
        except (ValueError, NotFound):
            left.append(p["name"])
    return {"kept": kept, "left": left}


def pass_on(store: Store, job_id: int, prospect_id: int, reason: str | None = None) -> dict[str, Any]:
    p = get(store, job_id, prospect_id)
    if p["status"] != "proposed":
        raise ValueError(f"{p['name']} was already {p['status']}.")
    return _pass(store, [p["id"]], reason)[0] | {"name": p["name"]}


def pass_all(store: Store, job_id: int, reason: str | None = None) -> int:
    ids = [p["id"] for p in listed(store, job_id, "proposed", with_checks=False)]
    return len(_pass(store, ids, reason)) if ids else 0


def _pass(store: Store, ids: list[int], reason: str | None) -> list[dict[str, Any]]:
    why = _clip(reason, REASON_LIMIT)
    with store._write():
        ts = now_iso()
        store._conn.execute(
            f"UPDATE prospects SET status = 'passed', pass_reason = ?, decided_at = ?, updated_at = ?"
            f" WHERE status = 'proposed' AND id IN ({', '.join('?' * len(ids))})", (why, ts, ts, *ids))
    return [{"id": i, "reason": why} for i in ids]


def put_back(store: Store, job_id: int, prospect_id: int) -> dict[str, Any]:
    """Undo a pass: the person waits on the Shortlist again."""
    p = get(store, job_id, prospect_id)
    if p["status"] != "passed":
        raise ValueError(f"{p['name']} isn't passed on.")
    with store._write():
        store._conn.execute("UPDATE prospects SET status = 'proposed', pass_reason = NULL, decided_at = NULL,"
                            " updated_at = ? WHERE id = ?", (now_iso(), prospect_id))
    return p


def today_items(store: Store) -> list[dict[str, Any]]:
    """One Today line per open job with people waiting on its Shortlist."""
    items = []
    for r in store._rows(
            """SELECT p.job_id, j.title, j.company, count(*) AS n, max(p.id) AS last, max(p.created_at) AS at,
                      sum(p.proposed_by = 'ai') AS by_ai
               FROM prospects p JOIN jobs j ON j.id = p.job_id
               WHERE p.status = 'proposed' AND j.status = 'open' GROUP BY p.job_id ORDER BY max(p.id) DESC"""):
        n, job = r["n"], job_label(r["title"], r.get("company"))
        people = f"{n} {'person' if n == 1 else 'people'}"
        title = (f"Your AI app proposed {people} for {job}" if r["by_ai"] == n
                 else f"{people} {'waits' if n == 1 else 'wait'} on the Shortlist for {job}")
        items.append({"key": f"shortlist:{r['job_id']}:{r['last']}", "kind": "shortlist", "rank": 2, "when": r["at"],
                      "title": title, "detail": "Keep or pass each one on the job's Shortlist.",
                      "job_id": r["job_id"], "href": f"/jobs/{r['job_id']}/shortlist"})
    return items


def row_view(p: dict[str, Any]) -> dict[str, Any]:
    """One Shortlist row as the AI app and the in-chat view see it."""
    out = {k: p.get(k) for k in ("id", "name", "linkedin_url", "title", "company", "location", "fit", "status",
                                 "pass_reason", "candidate_id") if p.get(k) not in (None, "")}
    if p.get("checks"):
        out["checks"] = p["checks"]
        out["check_lines"] = check_lines(p["checks"])
    return out


__all__ = ["MAX_BATCH", "NeedsChoice", "propose", "listed", "keep", "keep_all", "pass_on", "pass_all", "put_back",
           "counts", "waiting", "today_items", "checks", "check_lines", "row_view", "profile_url",
           "same_name"]
