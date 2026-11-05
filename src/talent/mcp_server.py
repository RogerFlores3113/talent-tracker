"""Local MCP server so Claude Desktop, Codex, or a local model can work the database.

Runs over stdio: the AI app launches `Talent Search mcp` itself, so there is
no port, no login and nothing leaves the machine except what the AI app sends
to its own model.

Rules the tools enforce (from the product decisions):
- The AI may search, add, update (including clearing a field), log activity
  and move stages without asking.
- The AI can never delete a candidate, job or record. There is no delete tool.
- Four tools also render a view inside the chat (MCP Apps); every tool's text
  output stands on its own for apps and local models that only read text.
"""
from __future__ import annotations

from typing import Any
from urllib.parse import quote

from mcp.server.fastmcp import FastMCP
from mcp.types import CallToolResult, TextContent, ToolAnnotations
from pydantic import BaseModel, Field

from . import __version__, actions, recipes
from .mcp_views import MIME, URIS, tool_meta, view_html
from .paths import app_url
from .store import JOB_STATUSES, TIMELINE_KINDS, Store, snippet_text
from .typesense_index import SearchService, TypesenseIndex

INSTRUCTIONS = """\
Talent Search is the recruiter's own candidate database on this computer. Nothing in it leaves the computer
except what you read.
- Before adding someone, search by name or email to avoid duplicates. save_candidate also merges automatically
  when the email or LinkedIn URL matches, and warns about same-name matches.
- Put a 2 to 4 sentence summary of the person in `about`; search ranks on it. Fill current_title,
  current_company, location, skills as `tags`, and `work_history` when you know them.
- Log calls, emails, messages, meetings and interviews with log_activity (use the real date/time, with time zone
  for interviews). That keeps "last contacted" and the interview list right. Summarize; don't paste whole emails.
- Each job has its own stages; job_pipeline and list_jobs show them. move_stage accepts any of them.
- You can add, update, clear fields and move stages without asking. You cannot delete anything; if the recruiter
  wants something deleted, tell them to do it in the Talent Search window.
- If a tool fails or the app misbehaves, tell the recruiter, and if they have a GitHub account offer them the
  link from report_problem (it opens a prefilled issue they review; never include candidate details).
"""

READ = ToolAnnotations(readOnlyHint=True, destructiveHint=False, openWorldHint=False)
WRITE = ToolAnnotations(readOnlyHint=False, destructiveHint=False, idempotentHint=True, openWorldHint=False)


class WorkEntry(BaseModel):
    title: str | None = Field(None, description="Job title")
    company: str | None = None
    start: str | None = Field(None, description="e.g. 2021 or 2021-03")
    end: str | None = Field(None, description="Empty or 'present' for a current role")
    summary: str | None = None


def _slim(c: dict[str, Any]) -> dict[str, Any]:
    """Drop empty fields so results stay short in the model's context."""
    return {k: v for k, v in c.items() if v not in (None, "", [])}


def _given(**kw: Any) -> dict[str, Any]:
    """Only the arguments the AI actually passed."""
    return {k: v for k, v in kw.items() if v is not None}


def _history(entries: list[WorkEntry] | None) -> list[dict[str, Any]] | None:
    return None if entries is None else [e.model_dump() for e in entries]


def _line(c: dict[str, Any]) -> str:
    role = " at ".join(x for x in (c.get("current_title"), c.get("current_company")) if x)
    bits = [f"{c['name']} (id {c['id']})", role, c.get("location")]
    return ", ".join(b for b in bits if b)


def _result(text: str, data: dict[str, Any]) -> CallToolResult:
    return CallToolResult(content=[TextContent(type="text", text=text)], structuredContent=data)


def _candidate_text(c: dict[str, Any]) -> str:
    out = [_line(c)]
    for k, label in (("email", "Email"), ("phone", "Phone"), ("linkedin_url", "LinkedIn"), ("website", "Website"),
                     ("source", "Source"), ("last_contacted_at", "Last contacted"), ("updated_at", "Last updated")):
        if c.get(k):
            out.append(f"{label}: {c[k]}")
    if c.get("tags"):
        out.append("Skills: " + ", ".join(c["tags"]))
    if c.get("about"):
        out.append("About: " + c["about"])
    if c.get("work_history"):
        out.append("Work history: " + "; ".join(
            " at ".join(x for x in (w.get("title"), w.get("company")) if x)
            + (f" ({w.get('start') or ''} to {w.get('end') or 'now'})" if w.get("start") or w.get("end") else "")
            for w in c["work_history"]))
    for p in c.get("pipelines", []):
        out.append(f"Job {p['job_id']} {p['job_title']}: {p['stage']} since {p['stage_changed_at'][:10]}")
    for t in c.get("timeline", [])[:10]:
        out.append(f"- {t['at'][:16]} {t['kind']}{': ' + t['body'] if t.get('body') else ''}")
    return "\n".join(out)


def build_server(store: Store, search: SearchService | None = None) -> FastMCP:
    search = search or SearchService(store, TypesenseIndex.from_env())
    mcp = FastMCP("talent-search", instructions=INSTRUCTIONS)

    # ---------------------------------------------------------- UI resources
    for name, uri in URIS.items():
        def make(n: str = name):
            return lambda: view_html(n)
        mcp.resource(uri, name=f"{name}-view", mime_type=MIME, description=f"Talent Search {name} view")(make())

    # ------------------------------------------------------------ candidates
    @mcp.tool(annotations=READ, meta=tool_meta("results"))
    def search_candidates(query: str = "", location: str | None = None, title: str | None = None,
                          tag: str | None = None, limit: int = 10) -> CallToolResult:
        """Search saved candidates by any words: skills, past or current companies, titles, names, places.

        Matches whole words, with the last word as a prefix ("pyth" finds "Python").
        Shorthand like sf, nyc, swe or ml also matches the long form.
        Leave query empty to list the most recently updated candidates.
        location / title narrow by a substring of those fields; tag narrows to one exact skill tag.
        """
        res = search.search(query, location=location, title=title, tag=tag, limit=max(1, min(limit, 50)))
        results = []
        for h in res.hits:
            c = {k: h.candidate.get(k) for k in ("id", "name", "current_title", "current_company", "location",
                                                 "tags", "email", "linkedin_url", "last_contacted_at", "updated_at")}
            if h.snippet:
                c["match"] = snippet_text(h.snippet)
            results.append(_slim(c))
        lines = [f"{res.total} match{'' if res.total == 1 else 'es'}" + (f", showing {len(results)}" if res.total > len(results) else "") + ":"]
        lines += [f"- {_line(c)}" + (f". Match: {c['match']}" if c.get("match") else "") for c in results]
        if not results:
            lines = ["No saved candidates match. Try fewer words, or no filters."]
        return _result("\n".join(lines), {"total": res.total, "results": results,
                                          "app_url": app_url("/?q=" + quote(query or ""))})

    @mcp.tool(annotations=READ, meta=tool_meta("candidate"))
    def get_candidate(candidate_id: int) -> CallToolResult:
        """Full record for one candidate: contact details, skills, work history, jobs and stages, recent activity."""
        c = store.candidate_detail(candidate_id, timeline_limit=15)
        c["pipelines"] = [{"job_id": a["job_id"], "job_title": a["job_title"], "stage": a["stage"],
                           "stage_changed_at": a["stage_changed_at"]} for a in c["pipelines"]]
        c["timeline"] = [_slim({"kind": t["kind"], "body": t["body"], "at": t["at"], "job_title": t.get("job_title")})
                         for t in c["timeline"]]
        c = _slim(c)
        return _result(_candidate_text(c), {**c, "app_url": app_url(f"/candidates/{candidate_id}")})

    @mcp.tool(annotations=WRITE)
    def save_candidate(
        name: str,
        email: str | None = None,
        phone: str | None = None,
        linkedin_url: str | None = None,
        website: str | None = None,
        current_title: str | None = None,
        current_company: str | None = None,
        location: str | None = None,
        about: str | None = None,
        tags: list[str] | None = None,
        work_history: list[WorkEntry] | None = None,
        source: str | None = None,
    ) -> dict[str, Any]:
        """Add a candidate, or update the existing one if the email or LinkedIn URL already exists.

        Only fields you pass are written; blanks never erase what's saved. Tags are skills ("Python", "B2B sales")
        and are added to existing ones. work_history, if given, replaces the saved list (most recent first).
        `source` is where you found them (e.g. "LinkedIn", "referral from Sam").
        """
        data = _given(name=name, email=email, phone=phone, linkedin_url=linkedin_url, website=website,
                      current_title=current_title, current_company=current_company, location=location,
                      about=about, tags=tags, source=source)
        same_name = [] if (email or linkedin_url) else store.find_by_name(name)
        cand, created = store.upsert_candidate(work_history=_history(work_history), **data)
        out: dict[str, Any] = {"created": created, "candidate": _slim(cand)}
        others = [c for c in same_name if c["id"] != cand["id"]]
        if created and others:
            out["warning"] = (f"Saved as new, but {len(others)} other candidate(s) have the same name "
                              f"(ids {', '.join(str(c['id']) for c in others)}). If it's the same person, "
                              f"tell the recruiter so they can merge them in the app.")
        return out

    @mcp.tool(annotations=WRITE)
    def update_candidate(
        candidate_id: int,
        name: str | None = None,
        email: str | None = None,
        phone: str | None = None,
        linkedin_url: str | None = None,
        website: str | None = None,
        current_title: str | None = None,
        current_company: str | None = None,
        location: str | None = None,
        about: str | None = None,
        tags: list[str] | None = None,
        work_history: list[WorkEntry] | None = None,
        source: str | None = None,
        last_contacted_at: str | None = None,
    ) -> dict[str, Any]:
        """Change fields on a candidate by id. Pass an empty string (or empty list) to clear a field.

        tags here replaces the whole skill list. work_history replaces the saved list.
        """
        data = _given(name=name, email=email, phone=phone, linkedin_url=linkedin_url, website=website,
                      current_title=current_title, current_company=current_company, location=location,
                      about=about, tags=tags, source=source, last_contacted_at=last_contacted_at)
        return _slim(store.update_candidate(candidate_id, work_history=_history(work_history), **data))

    @mcp.tool(annotations=WRITE)
    def log_activity(candidate_id: int, kind: str, summary: str | None = None, when: str | None = None,
                     job_id: int | None = None) -> dict[str, Any]:
        """Add something to a candidate's timeline.

        kind: note, call, email, message, meeting or interview. `when` is ISO date/time (with time zone for
        interviews, e.g. 2026-11-12T15:00:00-08:00); leave empty for now. Past calls, emails, messages, meetings
        and interviews update "last contacted". Future interviews show on the recruiter's dashboard.
        Logging the same item twice is harmless.
        """
        entry = store.log(candidate_id, kind, summary, at=when, job_id=job_id, source="AI app")
        return {"logged": _slim(entry), "last_contacted_at": store.get_candidate(candidate_id)["last_contacted_at"]}

    # ------------------------------------------------------------------ jobs
    @mcp.tool(annotations=READ)
    def list_jobs(status: str | None = "open") -> list[dict[str, Any]]:
        """Jobs with their stages and how many candidates are in each. status: open, on_hold, closed, or null for all."""
        return [_slim({**j, "stage_counts": {s["name"]: s["count"] for s in j["stage_counts"]}})
                for j in store.list_jobs(status)]

    @mcp.tool(annotations=WRITE)
    def create_job(title: str, company: str | None = None, location: str | None = None,
                   description: str | None = None, stages: list[str] | None = None) -> dict[str, Any]:
        """Create a job the recruiter is hiring for. stages is optional; the default is
        Sourced, Contacted, Screening, Interviewing, Offer, Hired, Rejected, Withdrawn."""
        job = store.add_job(title, company, location, description, stages=stages)
        return {**_slim(job), "stages": [s["name"] for s in store.job_stages(job["id"])]}

    @mcp.tool(annotations=WRITE)
    def update_job(job_id: int, title: str | None = None, company: str | None = None, location: str | None = None,
                   description: str | None = None, status: str | None = None) -> dict[str, Any]:
        """Change a job's details. status: open, on_hold or closed. Empty string clears a text field."""
        if status is not None and status not in JOB_STATUSES:
            raise ValueError(f"status must be one of {', '.join(JOB_STATUSES)}")
        return _slim(store.update_job(job_id, **_given(title=title, company=company, location=location,
                                                       description=description, status=status)))

    @mcp.tool(annotations=WRITE)
    def edit_job_stages(job_id: int, rename: dict[str, str] | None = None, add: list[str] | None = None,
                        order: list[str] | None = None) -> dict[str, Any]:
        """Customize a job's stages. rename maps old name to new name (candidates stay put); add appends new
        working stages before the closing ones; order lists stage names in the order you want (others keep
        their place after them). Stages can't be removed here."""
        for old, new in (rename or {}).items():
            store.rename_stage(job_id, old, new)
        for name in add or []:
            store.add_stage(job_id, name)
        if order:
            store.reorder_stages(job_id, order)
        return {"job_id": job_id, "stages": [s["name"] for s in store.job_stages(job_id)]}

    # -------------------------------------------------------------- pipeline
    @mcp.tool(annotations=WRITE)
    def add_to_pipeline(candidate_id: int, job_id: int, stage: str | None = None,
                        notes: str | None = None) -> dict[str, Any]:
        """Put a candidate into a job's pipeline, in its first stage unless you name one (no-op if already there)."""
        return _slim(store.add_to_pipeline(candidate_id, job_id, stage, notes))

    @mcp.tool(annotations=WRITE)
    def move_stage(candidate_id: int, job_id: int, stage: str, notes: str | None = None) -> dict[str, Any]:
        """Move a candidate to another of the job's stages, optionally with a note."""
        return _slim(store.move_stage(candidate_id, job_id, stage, notes))

    @mcp.tool(annotations=READ, meta=tool_meta("pipeline"))
    def job_pipeline(job_id: int) -> CallToolResult:
        """Everyone in a job's pipeline, grouped by the job's stages in order."""
        job = store.get_job(job_id)
        stages = [{"name": s["name"], "outcome": s["outcome"], "candidates": [
            _slim({"candidate_id": a["candidate_id"], "name": a["candidate_name"], "current_title": a["current_title"],
                   "since": a["stage_changed_at"]}) for a in s["candidates"]]} for s in store.pipeline_by_stage(job_id)]
        lines = [f"{job['title']} (job {job_id}, {job['status']})"]
        for s in stages:
            names = ", ".join(f"{c['name']} (id {c['candidate_id']})" for c in s["candidates"]) or "none"
            lines.append(f"- {s['name']} ({len(s['candidates'])}): {names}")
        return _result("\n".join(lines), {"job": _slim(job), "stages": stages, "app_url": app_url(f"/jobs/{job_id}")})

    # ------------------------------------------------------------- dashboard
    @mcp.tool(annotations=READ, meta=tool_meta("today"))
    def list_action_items() -> CallToolResult:
        """What needs the recruiter's attention: upcoming interviews, AI suggestions, candidates stuck in a stage,
        jobs with thin pipelines, and stale profiles (only the kinds the recruiter has switched on)."""
        res = actions.action_items(store)
        items = [_slim({k: i.get(k) for k in ("key", "kind", "title", "detail", "when", "candidate_id", "job_id",
                                               "suggestion_id", "stage", "next_stage")}) for i in res["items"]]
        if not items:
            text = "Nothing needs attention right now."
        else:
            text = "\n".join(
                f"- [{i['kind']}] {i['title']}" + (f" ({i['detail']})" if i.get("detail") else "")
                + (f" at {i['when']}" if i["kind"] == "interview" else "")
                + "".join(f" [{k} {i[k]}]" for k in ("candidate_id", "job_id", "suggestion_id") if i.get(k))
                for i in items)
            if res["stale_more"]:
                text += f"\n({res['stale_more']} more stale profiles not shown.)"
        return _result(text, {"items": items, "stale_more": res["stale_more"], "app_url": app_url("/")})

    @mcp.tool(annotations=WRITE)
    def add_suggestion(text: str, candidate_id: int | None = None, job_id: int | None = None) -> dict[str, Any]:
        """Leave the recruiter a to-do on their dashboard, e.g. "Send Priya a follow-up about the offer"."""
        return _slim(store.add_suggestion(text, candidate_id, job_id, source="AI app"))

    @mcp.tool(annotations=WRITE)
    def complete_suggestion(suggestion_id: int) -> dict[str, Any]:
        """Mark a dashboard suggestion as done (it stays in the record)."""
        return _slim(store.complete_suggestion(suggestion_id))

    # --------------------------------------------------------------- support
    @mcp.tool(annotations=READ)
    def report_problem(summary: str, details: str = "") -> dict[str, Any]:
        """Make a link that opens a prefilled GitHub issue about a problem with Talent Search.

        Opening it sends nothing; the recruiter reviews and submits it on GitHub (they need a GitHub account).
        Describe the problem only: never include candidate names, emails or notes.
        """
        return {"url": recipes.issue_url(summary, details, where="MCP / AI app"),
                "tell_the_user": "Open this link to review and submit the report on GitHub. Nothing is sent until you do."}

    # --------------------------------------------------------------- prompts
    @mcp.prompt(title="Plan my day")
    def plan_my_day() -> str:
        """Go through today's action items and suggest what to do first."""
        return recipes.plan_my_day()

    @mcp.prompt(title="Source candidates for a job")
    def source_candidates(job_id: str = "") -> str:
        """Find people who fit a job and save the good ones. Give a Talent Search job id, or leave empty."""
        job = store.get_job(int(job_id)) if job_id.strip().isdigit() else None
        return recipes.source_for_job(job)

    @mcp.prompt(title="Save someone from LinkedIn")
    def save_from_linkedin(url: str) -> str:
        """Save a person from a LinkedIn profile link."""
        return recipes.save_from_linkedin(url)

    @mcp.prompt(title="Save from a resume or profile")
    def save_from_text(text: str) -> str:
        """Paste resume or profile text and let the AI fill every field."""
        return recipes.save_from_text(text)

    @mcp.prompt(title="Log recent emails with candidates")
    def log_recent_emails(days: str = "7") -> str:
        """Use your email connector to log recent candidate emails on their timelines."""
        return recipes.log_recent_emails(int(days) if days.strip().isdigit() else 7)

    @mcp.prompt(title="Log interviews from my calendar")
    def log_calendar_interviews(days: str = "7") -> str:
        """Use your calendar connector to add upcoming interviews to each candidate."""
        return recipes.log_calendar_interviews(int(days) if days.strip().isdigit() else 7)

    @mcp.prompt(title="Refresh stale profiles")
    def refresh_stale_profiles() -> str:
        """Update candidates whose details are months old."""
        return recipes.refresh_stale()

    return mcp


def run_stdio(store: Store) -> None:
    # Errors raised inside tools (NotFound, ValueError) are returned to the
    # AI app as tool errors with our plain-English message.
    build_server(store).run("stdio")


__all__ = ["build_server", "run_stdio", "INSTRUCTIONS", "TIMELINE_KINDS", "__version__"]
