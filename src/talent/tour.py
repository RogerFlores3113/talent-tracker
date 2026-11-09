"""The guided tour for first-time users: its steps, and where the user is in it.

Where the user is (active, mode, step, whether the tour loaded the demo people, and whether a tour
ever ended) lives in the settings table, so the tour survives moving between pages and restarting
the app. The browser draws it from the steps this module renders (static/app.js).

Two modes. "full" runs on first use: it loads the demo people, walks the real pages, then clears
the demo data. "short" is the replay: the same walk on the user's own pages, and it never loads or
clears anything.
"""
from __future__ import annotations

from typing import Any

from .sample_data import load_sample_data, remove_sample_data, sample_ids
from .store import Store

FULL, SHORT = "full", "short"
MODES = (FULL, SHORT)

# Each step: id, title, body, path, target, modes, and optionally needs, pulse, action.
#   path: where the step lives. "/x" is a page, "person" and "job" are filled in with a candidate or a
#   job (the demo ones in the full tour, the user's own in the short one), None means wherever the user is.
#   target: CSS selectors. The first one found is lit up; none found means the popover sits in the middle.
#   pulse: a button inside the target to draw the eye to.
STEPS: list[dict[str, Any]] = [
    {"id": "welcome", "title": "Welcome to Talent Search", "path": None, "target": [], "modes": (FULL,),
     "body": "This is your own candidate database, kept on this computer. Your AI app does the typing: you "
             "ask it in plain English and it saves, searches and updates people here. The tour takes about two minutes."},
    {"id": "today", "title": "Today", "path": "/", "target": ["section.today", "main h1"], "modes": (SHORT,),
     "body": "Today lists what needs you: interviews coming up, people stuck in a stage and follow-ups that are due."},
    {"id": "claude", "title": "Connect Claude Desktop", "path": "/connect", "target": ["#claude-panel"],
     "pulse": "#claude-panel .btn.primary", "modes": (FULL, SHORT),
     "body": "One click on this button connects Claude Desktop. It adds Talent Search to Claude's settings and does "
             "nothing else. The tour will not click it for you. Click it now or later, then press Next."},
    {"id": "codex", "title": "Connect Codex", "path": "/connect", "target": ["#codex-panel"],
     "pulse": "#codex-panel .btn.primary", "modes": (FULL, SHORT),
     "body": "Using Codex instead? One click here connects it the same way. LM Studio has its own button just below."},
    {"id": "other", "title": "Other AI apps", "path": "/connect", "target": ["#by-hand"], "modes": (FULL, SHORT),
     "body": "Any other app that runs local tools (Cursor, Windsurf and others) takes the settings in this box. "
             "Press Copy and paste it into that app's tool settings."},
    {"id": "load", "title": "Try it with demo people", "path": None, "target": [], "modes": (FULL,),
     "action": "load",
     "body": "Want something to click on? The tour can add 16 made-up people and 2 jobs, all named Example. "
             "When the tour ends it clears them. Nothing real is touched."},
    {"id": "search", "title": "Search your candidates", "path": "/candidates",
     "target": [".tablebar input[name=q]", "main h1"], "modes": (FULL, SHORT),
     "body": "Type a skill, a company, a title or a name. Try \"rust\" or \"payments\"."},
    {"id": "filters", "title": "Narrow it down", "path": "/candidates", "target": [".tablebar", "main h1"],
     "modes": (FULL, SHORT),
     "body": "Filter by skill, location, job or stage. Click a column heading to sort. Each row is one person."},
    {"id": "person", "title": "A candidate's page", "path": "person", "target": ["main h1"], "modes": (FULL, SHORT),
     "body": "Everything about one person: details, work history, the jobs they are in, and a timeline of calls and "
             "notes. Your AI app can add to the timeline too."},
    {"id": "jobs", "title": "Jobs", "path": "/jobs", "target": ["ul.cards", "main h1"], "modes": (FULL, SHORT),
     "body": "A job holds a pipeline of candidates with stages you can rename. Open one to see who is where."},
    {"id": "pipeline", "title": "A job's pipeline", "path": "job", "target": ["#pipeline"], "modes": (FULL, SHORT),
     "body": "Drag a person to another column, or use the menu on their card, to move them along."},
    {"id": "interviews", "title": "Interviews", "path": "/interviews",
     "target": ["#interviews-view", "main section.panel", "main h1"], "modes": (FULL, SHORT),
     "body": "Every interview across all your jobs, with clashes flagged. Interviews you or your AI app log show up here."},
    {"id": "today-actions", "title": "Back to Today", "path": "/", "target": ["section.today", "main h1"],
     "modes": (FULL,),
     "body": "Today is where you will start each day. Each item has a button to act on it, and you choose what "
             "shows in Settings."},
    {"id": "finish", "title": "That is the tour", "path": None, "target": [], "modes": (FULL, SHORT),
     "action": "finish", "body": "You can replay a shorter tour any time from Settings."},
]


def state(store: Store) -> dict[str, Any]:
    s = store.settings()
    try:
        step = max(0, int(s.get("tour_step") or 0))
    except ValueError:
        step = 0
    mode = s.get("tour_mode")
    return {"active": s.get("tour_active") == "1", "mode": mode if mode in MODES else FULL, "step": step,
            "loaded": s.get("tour_loaded") == "1", "seen": s.get("tour_seen") == "1"}


def has_own_data(store: Store) -> bool:
    """Real candidates or jobs of the user's own. Demo people and jobs don't count."""
    people, jobs = sample_ids(store)
    return (store.count_candidates() - len(people)) > 0 or (len(store.list_jobs()) - len(jobs)) > 0


def should_autostart(store: Store) -> bool:
    """First run only: nothing of the user's own yet, and no tour ever finished or skipped."""
    st = state(store)
    return not st["active"] and not st["seen"] and not has_own_data(store)


def start(store: Store, mode: str = FULL) -> dict[str, Any]:
    """Start a tour. One already running is left as it is, so the demo data it loaded stays accounted for."""
    st = state(store)
    if st["active"]:
        return st
    store.set_settings(tour_active="1", tour_mode=mode if mode in MODES else FULL, tour_step="0", tour_loaded="0")
    return state(store)


def _pick_person(store: Store, mode: str) -> int | None:
    people, _ = sample_ids(store)
    recent = store.recent_candidates(60)
    own = [c["id"] for c in recent if c["id"] not in people]
    demo = [c["id"] for c in recent if c["id"] in people]
    if mode == FULL:
        maya = next((c["id"] for c in recent if c["id"] in people and c["name"] == "Maya Example"), None)
        return maya or (demo or own or [None])[0]
    return (own or demo or [None])[0]


def _pick_job(store: Store, mode: str) -> int | None:
    _, jobs = sample_ids(store)
    every = store.list_jobs("open") or store.list_jobs()
    own = [j["id"] for j in every if j["id"] not in jobs]
    demo = [j for j in every if j["id"] in jobs]
    if mode == FULL:
        pref = next((j["id"] for j in demo if j["title"] == "Senior Backend Engineer"), None)
        return pref or ([j["id"] for j in demo] or own or [None])[0]
    return (own or [j["id"] for j in demo] or [None])[0]


def steps_for(store: Store, mode: str) -> list[dict[str, Any]]:
    """The steps of this mode, each with its page resolved.

    The short tour skips a step that needs a person or a job when nobody has one. The full tour keeps every
    step, and one with nobody to show falls back to the list page.
    """
    people, jobs = sample_ids(store)
    present = bool(people or jobs)
    loaded = state(store)["loaded"]
    person, job = _pick_person(store, mode), _pick_job(store, mode)
    out = []
    for s in STEPS:
        if mode not in s["modes"]:
            continue
        path = s["path"]
        if path == "person":
            if person is None and mode == SHORT:
                continue
            path = f"/candidates/{person}" if person else "/candidates"
        elif path == "job":
            if job is None and mode == SHORT:
                continue
            path = f"/jobs/{job}" if job else "/jobs"
        step = {"id": s["id"], "title": s["title"], "body": s["body"], "path": path, "target": list(s["target"]),
                "pulse": s.get("pulse", ""), "action": s.get("action", ""), "cta": ""}
        if s["id"] == "load" and present:
            step["action"] = ""
            step["body"] = ("The demo people are loaded: made-up people and jobs, all named Example. The tour clears "
                            "them at the end." if loaded else
                            "Demo people are already here, because you loaded them yourself. The tour will leave "
                            "them alone, and you can remove them any time in Settings.")
        if s["id"] == "finish":
            if mode == FULL and loaded:
                step["body"] = ("That is the tour. Clearing the demo data removes the made-up people and jobs. "
                                "A backup is saved first, and any demo person you worked with is kept as yours.")
                step["cta"] = "Clear the demo data and start"
            else:
                step["cta"] = "Finish the tour" if mode == FULL else "Done"
        out.append(step)
    return out


def payload(store: Store) -> dict[str, Any] | None:
    """What the browser needs to draw the tour, or None when no tour is running."""
    st = state(store)
    if not st["active"]:
        return None
    steps = steps_for(store, st["mode"])
    people, jobs = sample_ids(store)
    return {"mode": st["mode"], "step": min(st["step"], len(steps) - 1), "steps": steps,
            "demo": bool(st["loaded"] and (people or jobs))}


def set_step(store: Store, step: int) -> int | None:
    """Remember the step the user is on. None when no tour is running."""
    st = state(store)
    if not st["active"]:
        return None
    n = max(0, min(step, len(steps_for(store, st["mode"])) - 1))
    store.set_settings(tour_step=str(n))
    return n


def load_demo(store: Store) -> int | None:
    """Full tour only: add the demo people, remember that the tour added them, and move to the next step.

    Demo data the user loaded themselves before the tour is never counted as the tour's, so the tour never
    clears it. Returns how many people were added, or None when this isn't a full tour.
    """
    st = state(store)
    if not st["active"] or st["mode"] != FULL:
        return None
    people, jobs = sample_ids(store)
    added = 0 if (people or jobs) else load_sample_data(store)
    steps = steps_for(store, FULL)
    at = next((i for i, s in enumerate(steps) if s["id"] == "load"), st["step"])
    loaded = st["loaded"] or bool(added)
    store.set_settings(tour_loaded="1" if loaded else "0", tour_step=str(min(at + 1, len(steps) - 1)))
    return added


def end(store: Store) -> dict[str, Any] | None:
    """Finish or skip: clear the demo people the tour loaded (full tour only), then close the tour for good.

    Never clears demo data the tour didn't load, and remove_sample_data keeps anything the user worked with.
    Returns remove_sample_data's result, or None when nothing was cleared. If the backup before clearing fails
    this raises ValueError and the tour stays open, so the user can try again.
    """
    st = state(store)
    removed = None
    if st["active"] and st["mode"] == FULL and st["loaded"]:
        removed = remove_sample_data(store)
    store.set_settings(tour_active="0", tour_loaded="0", tour_step="0", tour_seen="1")
    return removed


def reset(store: Store) -> None:
    """Forget that the tour ever ran, so the next first run starts it again. Leaves any demo data alone."""
    store.set_settings(tour_active="0", tour_mode="full", tour_step="0", tour_loaded="0", tour_seen="0")
