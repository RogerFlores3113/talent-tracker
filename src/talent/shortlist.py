"""A Shortlist per job: people the AI app researched and proposed, waiting for the recruiter.

The AI app can only propose (propose_candidates). Keeping someone (saving them, or linking the person
already saved, and adding them to the job's first stage) and passing on someone are the recruiter's call,
in the app. A pass is remembered with its profile link, so the same person isn't proposed for that job
again, and other jobs' proposals say they were passed on before.
"""
from __future__ import annotations

import json
import re
from typing import Any, Callable

from .actions import job_label
from .store import NotFound, Store, fold_name, linkedin_slug_ok, normalize_linkedin, now_iso, quoted, split_tags

MAX_BATCH = 25
STATUSES = ("proposed", "kept", "passed")
LIMITS = {"name": 200, "title": 200, "company": 200, "location": 200, "fit": 1000, "source": 200}
REASON_LIMIT = 300
KEEP_PREFIX = "shortlist_keep:"  # settings key of one Keep or Keep all, so Changes can undo it as one
# settings key of one proposed person's about, skills and work history, so Keep saves a full record
EXTRA_PREFIX = "prospect_extra:"
ABOUT_LIMIT, TAG_LIMIT, HISTORY_LIMIT = 5000, 30, 15
SENTENCE_NAME = "the name reads like a sentence, not a person's name"
_EMAIL = re.compile(r"^[^@\s,;<>]+@[^@\s,;<>]+\.[^@\s,;<>]+$")
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
    if not linkedin_slug_ok(raw):
        return None, "the link has no real profile name after /in/ (only dots, spaces or quotes)"
    return "https://linkedin.com/in/" + m.group(1), None


def _extra_in(raw: dict[str, Any]) -> dict[str, Any]:
    """The about, skills and work history the AI sent with a proposed person, cleaned and capped."""
    out: dict[str, Any] = {}
    about = " ".join(str(raw.get("about") or "").split())[:ABOUT_LIMIT].rstrip()
    if about:
        out["about"] = about
    email = " ".join(str(raw.get("email") or "").split()).lower()
    if _EMAIL.match(email) and len(email) <= 200:
        out["email"] = email
    tags = raw.get("tags") or raw.get("skills")
    tags = [t[:60] for t in split_tags(tags if isinstance(tags, (str, list, tuple)) else None)][:TAG_LIMIT]
    if tags:
        out["tags"] = tags
    history = []
    for e in raw.get("work_history") or []:
        if not isinstance(e, dict):
            continue
        row = {k: _clip(e.get(k), 1000 if k == "summary" else 200) for k in ("title", "company", "start", "end",
                                                                                "summary")}
        if row["title"] or row["company"]:
            history.append({k: v for k, v in row.items() if v})
    if history:
        out["work_history"] = history[:HISTORY_LIMIT]
    return out


def _attach_extras(store: Store, rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Add each row's about, tags and work_history from settings (only for the row it was written for)."""
    if not rows:
        return rows
    keys = [f"{EXTRA_PREFIX}{r['id']}" for r in rows]
    found = {r["key"]: r["value"] for r in store._rows(
        f"SELECT key, value FROM settings WHERE key IN ({', '.join('?' * len(keys))})", tuple(keys))}
    for r, k in zip(rows, keys):
        try:
            x = json.loads(found[k]) if k in found else None
        except ValueError:
            x = None
        if isinstance(x, dict) and x.get("name") == r["name"] and x.get("created_at") == r["created_at"]:
            r.update({f: x[f] for f in ("about", "tags", "work_history", "email") if x.get(f)})
    return rows


def _sentence_like(name: str) -> bool:
    from .mcp_server import sentence_like_name  # the same check save_candidate holds names with

    return sentence_like_name(name)


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
    out: dict[str, Any] = {} if p.get("linkedin_url") else {"no_link": True}
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
    lines = [f"Passed on for {x['job']}" + (f": {x['reason']}" if x.get("reason") else "")
             for x in c.get("passed_elsewhere") or []]
    if c.get("no_link"):
        lines.append("No LinkedIn link, so it can't be checked against people already saved")
    if c.get("saved"):
        s = c["saved"]
        jobs = "; ".join(f"{p['job']}: {p['stage']}" for p in s["pipelines"])
        lines.append(f"Already saved as {s['name']} (id {s['id']})" + ("" if s["same_name"] else ", a different name")
                     + (f". In {jobs}" if jobs else ". Not in any job"))
    for m in c.get("same_name") or []:
        jobs = "; ".join(f"{p['job']}: {p['stage']}" for p in m.get("pipelines") or [])
        lines.append(f"Same name as saved {m['name']} (id {m['id']})"
                     + (f", {m['current_title']}" if m.get("current_title") else "")
                     + (f" at {m['current_company']}" if m.get("current_company") else "")
                     + (f". In {jobs}" if jobs else ""))
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
        if _sentence_like(name):
            skipped.append({"name": name[:80], "reason": SENTENCE_NAME + "; ask the recruiter what they are called"})
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
        rows.append({**row, "_extra": _extra_in(raw)})
    added = []
    if rows:
        with store._write():
            ts = now_iso()
            for r in rows:
                extra = r.pop("_extra")
                pid = store._conn.execute(
                    "INSERT INTO prospects(job_id, linkedin_url, name, title, company, location, fit, source,"
                    " proposed_by, created_at, updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (job_id, r["linkedin_url"], r["name"], r["title"], r["company"], r["location"], r["fit"],
                     r["source"], by, ts, ts)).lastrowid
                if extra:
                    store._conn.execute("INSERT OR REPLACE INTO settings(key, value) VALUES (?, ?)", (
                        f"{EXTRA_PREFIX}{pid}", json.dumps({"name": r["name"], "created_at": ts, **extra})))
                added.append({"id": pid, **r, **extra})
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
    rows = _attach_extras(store, store._rows(
        f"SELECT * FROM prospects WHERE job_id = ? AND (? = 'all' OR status = ?) ORDER BY {order}",
        (job_id, status, status)))
    if with_checks:
        for r in rows:
            if r["status"] == "proposed":
                r["checks"] = checks(store, r)
    kept = [r for r in rows if r["status"] == "kept" and r.get("candidate_id")]
    if kept:  # where each kept person is now, and whether their record is still thin (no about, no skills)
        now = {a["candidate_id"]: a["stage"] for a in store.pipeline(job_id)}
        for r in kept:
            c = store._row("SELECT about, tags FROM candidates WHERE id = ?", (r["candidate_id"],))
            r["stage"] = now.get(r["candidate_id"])
            r["thin"] = bool(c) and not (c.get("about") or c.get("tags"))
    return rows


def get(store: Store, job_id: int, prospect_id: int) -> dict[str, Any]:
    row = store._row("SELECT * FROM prospects WHERE id = ? AND job_id = ?", (prospect_id, job_id))
    if row is None:
        raise NotFound("That person is no longer on this Shortlist. Reload the page.")
    return _attach_extras(store, [row])[0]


def _last_timeline_id(store: Store) -> int:
    return store._row("SELECT coalesce(max(id), 0) AS n FROM timeline")["n"]


def keep_stage(store: Store, job_id: int, stage: str | None) -> str | None:
    """The stage a Keep adds people to: None (the job's first stage) or one still in play, by name."""
    if not (stage or "").strip():
        return None
    s = store.resolve_stage(job_id, stage)
    if s["outcome"] != "open":
        raise ValueError(f"Keep adds people to a stage still in play, not {s['name']}.")
    return s["name"]


def keep_stages(store: Store, job_id: int) -> list[str]:
    """Where a Keep can land: the job's first stage, and its Contacted stage for people already messaged."""
    from .outreach import contact_stage  # here: outreach imports actions, as this module does

    stages = store.job_stages(job_id)
    out = [stages[0]["name"]] if stages and stages[0]["outcome"] == "open" else []
    c = contact_stage(store, job_id)
    if c and c["name"] not in out:
        out.append(c["name"])
    return out


def keep(store: Store, job_id: int, prospect_id: int, *, link_id: int | None = None,
         as_new: bool = False, log: bool = True, via: str | None = None, stage: str | None = None) -> dict[str, Any]:
    """The recruiter keeps someone: link the person already saved with this profile link (or the one they
    picked), or save them, then add them to the job's first stage. Returns {"candidate", "created", "stage",
    "already", "record"}. Raises NeedsChoice when someone with the same name is saved and the recruiter hasn't
    chosen. With log, the keep is listed on Changes with its own Undo (keep_all logs its keeps as one).
    Linking someone saved fills their blank fields from the Shortlist row. via="claude": pressed in Claude's view.
    stage: a stage still in play to add them to (Contacted, when the recruiter already messaged them)."""
    stage_name = keep_stage(store, job_id, stage)
    mark = _last_timeline_id(store)
    p = get(store, job_id, prospect_id)
    if p["status"] != "proposed":
        raise ValueError(f"{p['name']} was already {p['status']}.")
    target = store.get_candidate(link_id) if link_id else None
    if target is None and p["linkedin_url"]:
        target = store.find_duplicate(linkedin_url=p["linkedin_url"])
    email = p.get("email")
    owner = store.find_duplicate(email=email) if email else None  # who already has the email the AI app sent
    if owner is not None and target is None and fold_name(owner["name"]) == fold_name(p["name"]):
        target = owner
    if owner is not None and (target is None or owner["id"] != target["id"]):
        email = None  # it belongs to someone else: don't save it on this person
    if target is None and not as_new:
        same = same_name(store, p)
        if same:
            raise NeedsChoice(p, same)
    created = target is None
    if created:
        target = store.add_candidate(p.get("work_history"), name=p["name"], linkedin_url=p["linkedin_url"],
                                     current_title=p["title"], current_company=p["company"], location=p["location"],
                                     source=p["source"], about=p.get("about"), email=email,
                                     tags=", ".join(p.get("tags") or []) or None)
    linked = None
    if not created:  # the recruiter said it's the same person: their blank fields take what the row has
        fill = {dst: p[src] for src, dst in (("linkedin_url", "linkedin_url"), ("title", "current_title"),
                                             ("company", "current_company"), ("location", "location"),
                                             ("about", "about")) if p.get(src) and not target.get(dst)}
        if email and not target.get("email"):
            fill["email"] = email
        if p.get("tags") and not target.get("tags"):
            fill["tags"] = ", ".join(p["tags"])
        history = p.get("work_history") if p.get("work_history") and not store.work_history(target["id"]) else None
        for attempt in (fill, {k: v for k, v in fill.items() if k != "linkedin_url"}):
            if not attempt and not history:
                break
            try:
                target = store.update_candidate(target["id"], history, via=via, **attempt)
            except ValueError:  # their LinkedIn link belongs to someone else: fill the rest
                continue
            if target.get("_changed"):
                linked = store._row("SELECT id FROM timeline WHERE candidate_id = ? AND kind = 'change' AND id > ?"
                                    " ORDER BY id DESC LIMIT 1", (target["id"], mark))
            break
    already = next((a["stage"] for a in store.pipeline(job_id) if a["candidate_id"] == target["id"]), None)
    stage = already
    if not already:
        note = f"Kept from the Shortlist. Why they fit: {p['fit']}" if p.get("fit") else "Kept from the Shortlist."
        stage = store.add_to_pipeline(target["id"], job_id, stage_name, note)["stage"]
    with store._write():
        ts = now_iso()
        store._conn.execute("UPDATE prospects SET status = 'kept', candidate_id = ?, decided_at = ?, updated_at = ?"
                            " WHERE id = ?", (target["id"], ts, ts, prospect_id))
    entry = None if already else store._row(
        "SELECT id FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage' AND id > ?"
        " ORDER BY id DESC LIMIT 1", (target["id"], job_id, mark))
    own = [r["id"] for r in store._rows("SELECT id FROM timeline WHERE candidate_id = ? AND id > ?",
                                        (target["id"], mark))]
    record = {"prospect_id": prospect_id, "candidate_id": target["id"], "name": target["name"], "created": created,
              "stage": stage, "stage_entry": entry["id"] if entry else None, "linked": linked["id"] if linked else None,
              "own": own, **({"via": via} if via else {})}
    if log:
        _log_keeps(store, job_id, [record])
    from .outreach import other_clients_text  # here: outreach imports actions, as this module does

    return {"candidate": target, "created": created, "stage": stage, "already": bool(already), "record": record,
            "elsewhere": other_clients_text(store, target["id"], job_id)}


def keep_all(store: Store, job_id: int, ids: list[int] | None = None, *, stage: str | None = None,
             via: str | None = None) -> dict[str, Any]:
    """Keep everyone waiting (or the waiting ones among ids), except people whose same-name match needs the
    recruiter's choice and names that read like a sentence (keep those one at a time, if they really are
    someone). Logged as one change."""
    keep_stage(store, job_id, stage)  # a stage out of play is refused before anyone is kept
    kept, left, held = [], [], []
    rows = listed(store, job_id, "proposed", with_checks=False)
    if ids is not None:
        rows = [p for p in rows if p["id"] in set(ids)]
    for p in rows:
        if _sentence_like(p["name"]):
            held.append(p["name"])
            continue
        try:
            kept.append(keep(store, job_id, p["id"], log=False, stage=stage, via=via))
        except NeedsChoice:
            left.append(p["name"])
        except (ValueError, NotFound):
            left.append(p["name"])
    batch = _log_keeps(store, job_id, [k["record"] for k in kept]) if kept else None
    return {"kept": kept, "left": left, "held": held, "batch": batch}


def _log_keeps(store: Store, job_id: int, records: list[dict[str, Any]]) -> int:
    with store._write():
        rows = store._rows(f"SELECT key FROM settings WHERE key LIKE '{KEEP_PREFIX}%'")
        n = 1 + max((int(r["key"][len(KEEP_PREFIX):]) for r in rows if r["key"][len(KEEP_PREFIX):].isdigit()),
                    default=0)
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?)", (f"{KEEP_PREFIX}{n}", json.dumps(
            {"job_id": job_id, "at": now_iso(), "kept": records})))
    return n


def keep_batches(store: Store, since: str | None = None) -> list[dict[str, Any]]:
    """Keeps from the Shortlist, newest first, each with its Changes line and whether Undo can still run.
    Ones whose job is gone are left out."""
    out = []
    for r in store._rows(f"SELECT key, value FROM settings WHERE key LIKE '{KEEP_PREFIX}%'"):
        bid = r["key"][len(KEEP_PREFIX):]
        try:
            b = json.loads(r["value"])
        except ValueError:
            continue
        if not bid.isdigit() or not isinstance(b, dict) or (since and (b.get("at") or "") < since):
            continue
        job = store._row("SELECT title, company FROM jobs WHERE id = ?", (b.get("job_id"),))
        if not job:
            continue
        kept = b.get("kept") or []
        n = len(kept)
        who = quoted(kept[0]["name"]) if n == 1 else f"{n} people"
        stages = sorted({k["stage"] for k in kept if k.get("stage")})
        text = (f"Kept {who} from the Shortlist for {job_label(job['title'], job.get('company'))}"
                + (f", in {' and '.join(stages)}" if stages else "")
                + (", in your AI app" if kept and all(k.get("via") == "claude" for k in kept) else ""))
        out.append({**b, "id": int(bid), "job_title": job["title"], "text": text, "undoable": not b.get("undone_at"),
                    "note": b.get("undo_note")})
    return sorted(out, key=lambda b: (b["at"], b["id"]), reverse=True)


def _touched_since(store: Store, rec: dict[str, Any]) -> bool:
    """Whether anyone worked on a person a keep saved, apart from the keep itself."""
    cid, own = rec["candidate_id"], [i for i in rec.get("own") or [] if isinstance(i, int)]
    marks = ", ".join("?" * len(own)) or "NULL"
    return bool(store._row(f"SELECT 1 FROM timeline WHERE candidate_id = ? AND id NOT IN ({marks}) LIMIT 1",
                           (cid, *own))
                or store._row("SELECT 1 FROM applications WHERE candidate_id = ?", (cid,))
                or store._row("SELECT 1 FROM suggestions WHERE candidate_id = ?", (cid,)))


def undo_keep(store: Store, batch_id: int, before_delete: Callable[[], Any] | None = None) -> str:
    """Take back a Keep or Keep all: each person goes back on the Shortlist, waiting, and out of the job (unless
    they were moved since). Someone the keep saved is taken out again when nobody worked on them since (the app
    backs up first); otherwise they stay saved. Returns the notice. Raises NotFound or ValueError."""
    key = f"{KEEP_PREFIX}{batch_id}"
    raw = store._row("SELECT value FROM settings WHERE key = ?", (key,))
    if raw is None:
        raise NotFound("That keep is gone. Reload the page.")
    b = json.loads(raw["value"])
    if b.get("undone_at"):
        raise ValueError("That keep was already undone.")
    job_id, back, stayed, saved_still = b["job_id"], 0, [], []
    for rec in reversed(b.get("kept") or []):
        p = store._row("SELECT status, candidate_id FROM prospects WHERE id = ? AND job_id = ?",
                       (rec["prospect_id"], job_id))
        if p is None or p["status"] != "kept":
            stayed.append(rec["name"])
            continue
        if rec.get("stage_entry"):
            app = store._row("SELECT id, stage FROM applications WHERE candidate_id = ? AND job_id = ?",
                             (rec["candidate_id"], job_id))
            later = store._row("SELECT 1 FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage'"
                               " AND id > ?", (rec["candidate_id"], job_id, rec["stage_entry"]))
            if app is not None and (later or app["stage"].lower() != (rec.get("stage") or "").lower()):
                stayed.append(rec["name"])
                continue
            if app is not None:
                with store._write():
                    store._conn.execute("DELETE FROM applications WHERE id = ?", (app["id"],))
        if rec.get("linked"):
            try:
                store.undo_change(rec["linked"], by="app")
            except (ValueError, NotFound):
                pass
        own = [i for i in rec.get("own") or [] if isinstance(i, int)]
        if not rec.get("created") and rec.get("stage_entry") and own:  # their timeline loses the keep's add and note
            with store._write():
                store._conn.execute(f"DELETE FROM timeline WHERE kind IN ('stage', 'note') AND candidate_id = ?"
                                    f" AND id IN ({', '.join('?' * len(own))})", (rec["candidate_id"], *own))
        with store._write():
            store._conn.execute("UPDATE prospects SET status = 'proposed', candidate_id = NULL, decided_at = NULL,"
                                " updated_at = ? WHERE id = ?", (now_iso(), rec["prospect_id"]))
        back += 1
        if rec.get("created"):
            try:
                if _touched_since(store, rec):
                    saved_still.append(rec["name"])
                else:
                    if before_delete:
                        before_delete()
                    store.delete_candidate(rec["candidate_id"])
            except NotFound:
                pass
    if not back:
        raise ValueError("Nothing to undo: " + ("everyone in it was moved or decided again since."
                                                if stayed else "they are gone."))
    note = "; ".join(x for x in (
        f"left {len(stayed)} as they are, moved or decided since" if stayed else "",
        f"kept {len(saved_still)} saved because you worked on them since" if saved_still else "") if x)
    with store._write():
        store._conn.execute("UPDATE settings SET value = ? WHERE key = ?",
                            (json.dumps({**b, "undone_at": now_iso(), "undo_note": note or None}), key))
    who = quoted(b["kept"][0]["name"]) if back == 1 and len(b.get("kept") or []) == 1 else \
        f"{back} {'person' if back == 1 else 'people'}"
    return (f"Undid the keep: {who} {'waits' if back == 1 else 'wait'} on the Shortlist again."
            + (f" ({note[:1].upper() + note[1:]}.)" if note else ""))


def pass_on(store: Store, job_id: int, prospect_id: int, reason: str | None = None) -> dict[str, Any]:
    p = get(store, job_id, prospect_id)
    if p["status"] != "proposed":
        raise ValueError(f"{p['name']} was already {p['status']}.")
    return _pass(store, [p["id"]], reason)[0] | {"name": p["name"]}


def pass_all(store: Store, job_id: int, reason: str | None = None, ids: list[int] | None = None) -> int:
    """Pass on everyone waiting (or the waiting ones among ids), with one reason."""
    return len(pass_waiting(store, job_id, reason, ids))


def pass_waiting(store: Store, job_id: int, reason: str | None = None,
                 ids: list[int] | None = None) -> list[dict[str, Any]]:
    """pass_all, returning the people passed on: [{"id", "name", "reason"}]."""
    rows = [p for p in listed(store, job_id, "proposed", with_checks=False) if ids is None or p["id"] in set(ids)]
    names = {p["id"]: p["name"] for p in rows}
    return [{**r, "name": names[r["id"]]} for r in _pass(store, list(names), reason)] if rows else []


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
    out = {k: p.get(k) for k in ("id", "name", "linkedin_url", "title", "company", "location", "fit", "about",
                                 "email", "tags", "work_history", "status", "pass_reason", "candidate_id", "stage")
           if p.get(k) not in (None, "", [])}
    if p.get("checks"):
        out["checks"] = p["checks"]
        out["check_lines"] = check_lines(p["checks"])
    return out


__all__ = ["MAX_BATCH", "NeedsChoice", "propose", "listed", "keep", "keep_all", "keep_stage", "keep_stages", "keep_batches", "undo_keep",
           "pass_on", "pass_all", "put_back",
           "counts", "waiting", "today_items", "checks", "check_lines", "row_view", "profile_url",
           "same_name"]
