"""Today's action items, computed on read from the database.

Nothing here is stored except snoozes and AI suggestions. Each kind of item can
be switched off in Settings, and its threshold changed.
"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from .store import Store, to_dt

FLAGS: tuple[tuple[str, str, str], ...] = (
    # (setting key, label, help)
    ("flag_interviews", "Upcoming interviews", "Interviews in the next 7 days."),
    ("flag_stuck", "Stuck in a stage", "Candidates who haven't moved stage in a while."),
    ("flag_thin", "Thin pipelines", "Open jobs with too few active candidates."),
    ("flag_stale", "Stale profiles", "Candidates whose details haven't been updated in months."),
    ("flag_suggestions", "AI suggestions", "Follow-ups your AI app wrote for you."),
)

MAX_STALE = 5


def _days_ago(iso: str | None, now: datetime) -> int:
    dt = to_dt(iso)
    return (now - dt).days if dt else 0


def action_items(store: Store, now: datetime | None = None) -> dict[str, Any]:
    """{'items': [...], 'counts': {...}, 'hidden': n}. Items are ordered most urgent first."""
    now = now or datetime.now(timezone.utc)
    cfg = store.settings()
    on = lambda k: cfg.get(k, "1") == "1"  # noqa: E731
    snoozed = store.snoozed_keys()
    items: list[dict[str, Any]] = []

    if on("flag_interviews"):
        for t in store.upcoming_interviews(days=7, now=now):
            items.append({
                "key": f"interview:{t['id']}", "kind": "interview", "rank": 0, "when": t["at"],
                "title": f"Interview with {t['candidate_name']}",
                "detail": " · ".join(x for x in (t.get("job_title"), t.get("body")) if x),
                "candidate_id": t["candidate_id"], "job_id": t.get("job_id"),
            })

    if on("flag_suggestions"):
        for s in store.open_suggestions():
            items.append({
                "key": f"suggestion:{s['id']}", "kind": "suggestion", "rank": 1, "when": s["created_at"],
                "title": s["text"], "detail": "Suggested by your AI app",
                "candidate_id": s.get("candidate_id"), "job_id": s.get("job_id"), "suggestion_id": s["id"],
            })

    if on("flag_stuck"):
        limit = int(cfg.get("stuck_days") or 7)
        for a in store.open_applications():
            days = _days_ago(a["stage_changed_at"], now)
            if days >= limit:
                nxt = store.next_stage(a["job_id"], a["stage"])
                items.append({
                    "key": f"stuck:{a['id']}", "kind": "stuck", "rank": 2, "when": a["stage_changed_at"],
                    "title": f"{a['candidate_name']} has been in {a['stage']} for {days} days",
                    "detail": a["job_title"], "candidate_id": a["candidate_id"], "job_id": a["job_id"],
                    "stage": a["stage"], "next_stage": nxt, "days": days,
                })

    if on("flag_thin"):
        minimum = int(cfg.get("thin_min") or 5)
        for j in store.list_jobs("open"):
            if j["active_count"] < minimum:
                n = j["active_count"]
                items.append({
                    "key": f"thin:{j['id']}", "kind": "thin", "rank": 3, "when": j["updated_at"],
                    "title": f"{j['title']} has {'no' if n == 0 else 'only ' + str(n)} active candidate{'' if n == 1 else 's'}",
                    "detail": f"Aim for at least {minimum}." + (f" {j['company']}" if j.get("company") else ""),
                    "job_id": j["id"],
                })

    stale_more = 0
    if on("flag_stale"):
        days = int(cfg.get("stale_days") or 180)
        stale = [c for c in store.stale_candidates(days, limit=200) if f"stale:{c['id']}" not in snoozed]
        for c in stale[:MAX_STALE]:
            items.append({
                "key": f"stale:{c['id']}", "kind": "stale", "rank": 4, "when": c["updated_at"],
                "title": f"Refresh {c['name']}'s profile",
                "detail": f"Last updated {_days_ago(c['updated_at'], now)} days ago",
                "candidate_id": c["id"],
            })
        stale_more = max(0, len(stale) - MAX_STALE)

    visible = [i for i in items if i["key"] not in snoozed]
    visible.sort(key=lambda i: (i["rank"], i["when"] or ""))
    counts: dict[str, int] = {}
    for i in visible:
        counts[i["kind"]] = counts.get(i["kind"], 0) + 1
    return {"items": visible, "counts": counts, "hidden": len(items) - len(visible), "stale_more": stale_more}
