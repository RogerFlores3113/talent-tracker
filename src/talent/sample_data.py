"""Made-up candidates so a new user can try search before adding real people. Every one is called
"<first name> Example", so a sample person is never mistaken for, or matched to, someone a real import brings."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any

from .importers import backup_before_import
from .store import NotFound, Store

SAMPLE_SOURCE = "Sample data"  # the source on sample people, notes and to-dos

SAMPLES = [
    ("Maya Example", "Senior Backend Engineer", "Ledgerline", "Austin, TX",
     "Eight years building payment systems in Go and Rust. Led the migration of a card-processing service to event sourcing; mentors two junior engineers.", "go, rust, payments"),
    ("Daniel Example", "Staff Frontend Engineer", "Brightpath", "San Francisco, CA",
     "React and TypeScript specialist who built a design system used by 40 product teams. Strong on accessibility and performance budgets.", "react, typescript, design systems"),
    ("Priya Example", "Data Science Lead", "Harvest Analytics", "New York, NY",
     "Runs a team of six doing forecasting and causal inference in Python. Previously an economist; publishes on experiment design.", "python, causal inference, leadership"),
    ("Tomás Example", "Machine Learning Engineer", "Northwind AI", "Seattle, WA",
     "Trains and serves ranking models with PyTorch on Kubernetes. Cut inference cost 40% with distillation and batching.", "pytorch, mlops, kubernetes"),
    ("Aisha Example", "Product Manager", "Cartwheel", "Chicago, IL",
     "B2B SaaS PM focused on onboarding and activation. Shipped a self-serve trial that doubled conversion; former support lead.", "saas, growth, onboarding"),
    ("Kenji Example", "Site Reliability Engineer", "Cloudmere", "Remote (Portland, OR)",
     "On-call lead for a 99.99% uptime API platform. Terraform, Prometheus and incident command; writes great postmortems.", "sre, terraform, observability"),
    ("Sofia Example", "Engineering Manager", "Ledgerline", "Austin, TX",
     "Manages two backend teams (14 engineers). Grew the org from 5 and introduced a career ladder. Hands-on background in Java.", "management, java, hiring"),
    ("Liam Example", "iOS Engineer", "Fitloop", "Boston, MA",
     "Swift and SwiftUI developer on a top-50 fitness app. Owns HealthKit integrations and offline sync.", "swift, ios, mobile"),
    ("Grace Example", "Security Engineer", "Vaultline", "Washington, DC",
     "Application security for fintech: threat modeling, secure code review and SOC 2 programs. Former penetration tester.", "appsec, soc2, fintech"),
    ("Arjun Example", "Full Stack Engineer", "Shoplane", "San Jose, CA",
     "Next.js, Node and Postgres generalist at an e-commerce startup. Built the checkout flow and an internal admin panel.", "nextjs, node, postgres"),
    ("Hannah Example", "Technical Recruiter", "Talentforge", "Denver, CO",
     "Agency recruiter placing infrastructure and data engineers. Strong network in the Rockies startup scene.", "recruiting, infrastructure"),
    ("Omar Example", "Data Engineer", "Streamwise", "New York, NY",
     "Builds batch and streaming pipelines with Spark, Kafka and dbt. Owns the company's data warehouse on Snowflake.", "spark, kafka, dbt, snowflake"),
    ("Chloe Example", "Product Designer", "Brightpath", "Los Angeles, CA",
     "Systems-minded designer working across web and mobile. Ran the research program behind a full navigation redesign.", "figma, research, design systems"),
    ("Rafael Example", "Solutions Engineer", "Northwind AI", "Miami, FL",
     "Pre-sales engineer for an AI platform. Runs technical demos and pilots with enterprise customers; Python and SQL.", "pre-sales, python, enterprise"),
    ("Emily Example", "Senior Software Engineer", "Medisight", "Boston, MA",
     "Healthcare software in Python and Django; HL7/FHIR integrations with hospital systems. Cares about data privacy.", "python, django, healthcare"),
    ("Noah Example", "DevOps Engineer", "Shoplane", "Remote (Atlanta, GA)",
     "CI/CD and AWS cost work. Moved builds from Jenkins to GitHub Actions and halved deploy times.", "aws, ci/cd, github actions"),
]


HISTORY = {
    "Maya Example": [("Senior Backend Engineer", "Ledgerline", "2021", ""), ("Backend Engineer", "Stripe", "2017", "2021")],
    "Priya Example": [("Data Science Lead", "Harvest Analytics", "2020", ""), ("Economist", "Federal Reserve Bank of NY", "2015", "2020")],
    "Kenji Example": [("Site Reliability Engineer", "Cloudmere", "2019", ""), ("Systems Engineer", "Rackspace", "2014", "2019")],
    "Omar Example": [("Data Engineer", "Streamwise", "2022", ""), ("Analytics Engineer", "Squarespace", "2019", "2022")],
}

JOBS = [
    ("Senior Backend Engineer", "Acme Payments", "Austin, TX or remote",
     "Go or Rust, payments or ledger experience, 6+ years. Owns the settlement service.",
     [("Maya Example", "Interviewing", 3), ("Kenji Example", "Screening", 12), ("Noah Example", "Contacted", 4),
      ("Arjun Example", "Sourced", 20), ("Sofia Example", "Rejected", 9)]),
    ("Data Lead", "Acme Payments", "New York, NY",
     "Leads a small data science team. Forecasting, experiments, Python and SQL.",
     [("Priya Example", "Offer", 2), ("Omar Example", "Sourced", 1)]),
]


def _ago(days: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(days=days)).replace(microsecond=0).isoformat()


def load_sample_data(store: Store) -> int:
    """Add the sample people, two jobs with pipelines, and enough history to fill the Today page.

    Running it twice doesn't duplicate anything.
    """
    added = 0
    ids: dict[str, int] = {}
    new_people: list[int] = []
    new_jobs: list[int] = []
    for name, title, company, location, about, tags in SAMPLES:
        slug = name.lower().replace(" ", ".").replace("'", "")
        history = [{"title": t, "company": c, "start": a, "end": b} for t, c, a, b in HISTORY.get(name, [])]
        cand, created = store.upsert_candidate(
            work_history=history or None, name=name, current_title=title, current_company=company,
            location=location, about=about, tags=tags, email=f"{slug}@example.com", source=SAMPLE_SOURCE,
        )
        ids[name] = cand["id"]
        added += created
        if created:
            new_people.append(cand["id"])
    if not added:
        return 0

    db = store._conn
    # A couple of profiles that haven't been touched in a long time, so "stale profile" has something to show.
    for name, days in (("Liam Example", 400), ("Chloe Example", 250)):
        db.execute("UPDATE candidates SET updated_at = ?, created_at = ? WHERE id = ?", (_ago(days), _ago(days), ids[name]))

    existing = {j["title"] for j in store.list_jobs()}
    for title, company, location, description, people in JOBS:
        if title in existing:
            continue
        job = store.add_job(title, company=company, location=location, description=description)
        new_jobs.append(job["id"])
        for name, stage, days in people:
            store.add_to_pipeline(ids[name], job["id"])
            if stage != "Sourced":
                store.move_stage(ids[name], job["id"], stage)
            # Backdate the stage change so some candidates show as stuck.
            db.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                       (_ago(days), ids[name], job["id"]))
            db.execute("UPDATE timeline SET at = ? WHERE candidate_id = ? AND job_id = ? AND kind = 'stage'",
                       (_ago(days), ids[name], job["id"]))
        if title == "Senior Backend Engineer":
            when = (datetime.now(timezone.utc) + timedelta(days=2)).replace(hour=17, minute=0, second=0, microsecond=0)
            store.log(ids["Maya Example"], "interview", "Systems design round with the payments team",
                      at=when, job_id=job["id"], source=SAMPLE_SOURCE)
            store.log(ids["Kenji Example"], "call", "Intro call. Open to Austin, wants to stay hands-on.",
                      at=_ago(12), job_id=job["id"], source=SAMPLE_SOURCE)
            store.add_suggestion("Follow up with Noah Example about the backend role", candidate_id=ids["Noah Example"],
                                 job_id=job["id"], source=SAMPLE_SOURCE)
    db.commit()
    people, jobs = sample_ids(store)
    _save_ids(store, people | set(new_people), jobs | set(new_jobs))
    return added


# Which rows are sample data, so the app can badge them and remove exactly those.
# Kept in the settings table as JSON lists of ids.
CANDIDATE_KEY, JOB_KEY = "sample_candidate_ids", "sample_job_ids"


def _ids(raw: str | None) -> set[int]:
    try:
        return {int(i) for i in json.loads(raw or "[]")}
    except (ValueError, TypeError):
        return set()


def _save_ids(store: Store, people: set[int], jobs: set[int]) -> None:
    with store._lock, store._conn:
        for key, ids in ((CANDIDATE_KEY, people), (JOB_KEY, jobs)):
            if ids:
                store._conn.execute(
                    "INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
                    (key, json.dumps(sorted(ids))))
            else:
                store._conn.execute("DELETE FROM settings WHERE key = ?", (key,))


def sample_ids(store: Store) -> tuple[set[int], set[int]]:
    """(candidate ids, job ids) that came from the sample data and still exist."""
    cfg = store.settings()
    people, jobs = _ids(cfg.get(CANDIDATE_KEY)), _ids(cfg.get(JOB_KEY))
    if people:
        marks = ",".join("?" * len(people))
        people = {r["id"] for r in store._rows(f"SELECT id FROM candidates WHERE id IN ({marks})", sorted(people))}
    if jobs:
        marks = ",".join("?" * len(jobs))
        jobs = {r["id"] for r in store._rows(f"SELECT id FROM jobs WHERE id IN ({marks})", sorted(jobs))}
    return people, jobs


def _sample_path(name: str, job_title: str) -> set[str] | None:
    """The stages the sample put this person through in this job (lowercase), or None if it didn't."""
    for title, _, _, _, people in JOBS:
        if title == job_title:
            for who, stage, _ in people:
                if who == name:
                    return {"sourced", stage.lower()}
    return None


def _touched_people(store: Store, people: set[int], jobs: set[int]) -> dict[int, str]:
    """Sample people the recruiter (or their AI app, or an import) has worked with: id -> name."""
    touched: dict[int, str] = {}
    for cid in people:
        c = store.get_candidate(cid)
        sample_jobs = {a["job_id"]: a for a in store.candidate_pipelines(cid) if a["job_id"] in jobs}
        if len(sample_jobs) < len(store.candidate_pipelines(cid)):
            touched[cid] = c["name"]  # added to one of the recruiter's own jobs
            continue
        for a in sample_jobs.values():
            path = _sample_path(c["name"], a["job_title"])
            if path is None or a["stage"].lower() not in path or a.get("notes"):
                touched[cid] = c["name"]  # moved to another stage, or a note was added
        for t in store._rows("SELECT * FROM timeline WHERE candidate_id = ?", (cid,)):
            if t["source"] == SAMPLE_SOURCE:
                continue
            if t["kind"] == "stage" and t["job_id"] in sample_jobs:
                path = _sample_path(c["name"], sample_jobs[t["job_id"]]["job_title"]) or set()
                if (t["body"] or "").lower() in path:
                    continue
            touched[cid] = c["name"]  # a note, call, edit, merge or import of their own
        if store._row("SELECT 1 AS x FROM suggestions WHERE candidate_id = ? AND coalesce(source, '') <> ?",
                      (cid, SAMPLE_SOURCE)):
            touched[cid] = c["name"]
    return touched


def _searches_on_sample(store: Store, people: set[int]) -> list[dict[str, Any]]:
    """The recruiter's own saved searches (not the AI app's) made after the sample people were added that match
    somebody now. Removing the sample data deletes those that then match nobody, so no dead chip is left. Searches
    that matched nobody to begin with, were saved earlier, or match a real person always stay."""
    if not people:
        return []
    marks = ",".join("?" * len(people))
    r = store._row(f"SELECT max(created_at) AS t FROM candidates WHERE id IN ({marks})", tuple(sorted(people)))
    since = (r or {}).get("t")
    if not since:
        return []
    return [row for row in store.list_saved_searches()
            if row.get("created_by") != "ai" and row["created_at"] >= since and store.saved_search_counts(row)[0]]


def remove_sample_data(store: Store, *, backup: bool = True) -> dict[str, Any]:
    """Remove the sample people and jobs the recruiter never worked with, after a backup.

    A sample person with anything of the recruiter's own (a note or call, an edit, a merge, an
    import, a stage move, a to-do, a place in one of their own jobs) is kept and stops being marked
    Sample, and so is a sample job that was edited or has any real or kept person in its pipeline.
    Returns {"people", "jobs"} removed, {"kept_people", "kept_jobs"} names, and "backup" (a path).
    """
    people, jobs = sample_ids(store)
    out: dict[str, Any] = {"people": 0, "jobs": 0, "kept_people": [], "kept_jobs": [], "backup": None,
                           "deleted_searches": []}
    if not people and not jobs:
        _save_ids(store, set(), set())
        return out
    if backup:
        out["backup"] = str(backup_before_import(store, reason="sample removal"))
    kept = _touched_people(store, people, jobs)
    made = _searches_on_sample(store, people)  # saved while the sample people were here, matching someone
    kept_jobs: dict[int, str] = {}
    for jid in jobs:
        job = store.get_job(jid)
        label = job["title"] + (f" ({job['company']})" if job.get("company") else "")
        in_pipeline = {a["candidate_id"] for a in store.pipeline(jid)}
        own_todo = store._row("SELECT 1 AS x FROM suggestions WHERE job_id = ? AND coalesce(source, '') <> ?",
                              (jid, SAMPLE_SOURCE))
        if (in_pipeline - (people - set(kept))) or job["updated_at"] != job["created_at"] or own_todo:
            kept_jobs[jid] = label
    for cid in people - set(kept):
        try:
            store.delete_candidate(cid)
            out["people"] += 1
        except NotFound:
            pass
    for jid in jobs - set(kept_jobs):
        try:
            store.delete_job(jid)
            out["jobs"] += 1
        except NotFound:
            pass
    if kept:
        # Kept people lose what the sample made up for them (the fake interview, call and to-do), so Today
        # doesn't show it as real, and stop saying they were found via the sample.
        marks = ",".join("?" * len(kept))
        ids = sorted(kept)
        with store._lock, store._conn:
            store._conn.execute(f"DELETE FROM timeline WHERE source = ? AND candidate_id IN ({marks})",
                                (SAMPLE_SOURCE, *ids))
            store._conn.execute(f"DELETE FROM suggestions WHERE source = ? AND candidate_id IN ({marks})",
                                (SAMPLE_SOURCE, *ids))
            store._conn.execute(f"UPDATE candidates SET source = NULL WHERE source = ? AND id IN ({marks})",
                                (SAMPLE_SOURCE, *ids))
    _save_ids(store, set(), set())  # whatever is left is the recruiter's own now
    for row in made:  # a saved search that matched only sample people would now open on nobody: remove it
        try:
            if not store.saved_search_counts(row)[0]:
                store.delete_saved_search(row["id"])
                out["deleted_searches"].append(row["name"])
        except NotFound:
            pass
    out["kept_people"], out["kept_jobs"] = sorted(kept.values()), sorted(kept_jobs.values())
    return out
