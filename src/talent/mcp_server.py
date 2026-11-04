"""Local MCP server so Claude Desktop, Codex, or a local model can work the database.

Runs over stdio: the AI app launches `Talent Search mcp` itself, so there is
no port, no login and nothing leaves the machine except what the AI app sends
to its own model.
"""
from __future__ import annotations

from typing import Any

from mcp.server.fastmcp import FastMCP

from .store import STAGES, Store, snippet_text
from .typesense_index import SearchService, TypesenseIndex

INSTRUCTIONS = """\
Talent Search is the recruiter's own candidate database on this computer.
- Before adding someone, search by name or email to avoid duplicates.
  save_candidate also merges automatically when the email or LinkedIn URL matches.
- Put a 2-4 sentence summary of the person's background in `about`; it is what search ranks on.
- Fill current_title, current_company and location when known, so filters work.
- Use pipeline tools to track candidates against jobs. Stages: """ + ", ".join(STAGES) + "."


def _slim(c: dict[str, Any]) -> dict[str, Any]:
    """Drop empty fields so results stay short in the model's context."""
    return {k: v for k, v in c.items() if v not in (None, "")}


def _given(**kw: Any) -> dict[str, Any]:
    """Only the arguments the AI actually passed."""
    return {k: v for k, v in kw.items() if v is not None}


def build_server(store: Store, search: SearchService | None = None) -> FastMCP:
    search = search or SearchService(store, TypesenseIndex.from_env())
    mcp = FastMCP("talent-search", instructions=INSTRUCTIONS)

    @mcp.tool()
    def search_candidates(query: str = "", location: str | None = None, title: str | None = None,
                          limit: int = 10) -> dict[str, Any]:
        """Search candidates by any words: skills, past companies, titles, names, places.

        Matches whole words, with the last word as a prefix ("pyth" finds "Python").
        Shorthand like sf, nyc, swe or ml also matches the long form.
        Leave query empty to list the most recently updated candidates.
        location / title narrow results by a substring of those fields.
        """
        res = search.search(query, location=location, title=title, limit=max(1, min(limit, 50)))
        return {
            "total": res.total,
            "results": [
                {**_slim(h.candidate), **({"match": snippet_text(h.snippet)} if h.snippet else {})} for h in res.hits
            ],
        }

    @mcp.tool()
    def get_candidate(candidate_id: int) -> dict[str, Any]:
        """Full record for one candidate, including the jobs they're in and their stage."""
        c = _slim(store.get_candidate(candidate_id))
        c["pipelines"] = [
            {"job_id": a["job_id"], "job_title": a["job_title"], "stage": a["stage"],
             "since": a["stage_changed_at"]}
            for a in store.candidate_pipelines(candidate_id)
        ]
        return c

    @mcp.tool()
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
        source: str | None = None,
    ) -> dict[str, Any]:
        """Add a candidate, or update the existing one if the email or LinkedIn URL already exists.

        Only fields you pass are written; blanks never erase what's saved.
        `source` is where you found them (e.g. "LinkedIn", "referral from Sam").
        """
        data = _given(name=name, email=email, phone=phone, linkedin_url=linkedin_url, website=website,
                      current_title=current_title, current_company=current_company, location=location,
                      about=about, tags=tags, source=source)
        cand, created = store.upsert_candidate(**data)
        return {"created": created, "candidate": _slim(cand)}

    @mcp.tool()
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
        source: str | None = None,
    ) -> dict[str, Any]:
        """Change fields on a candidate by id. Pass an empty string to clear a field."""
        data = _given(name=name, email=email, phone=phone, linkedin_url=linkedin_url, website=website,
                      current_title=current_title, current_company=current_company, location=location,
                      about=about, tags=tags, source=source)
        return _slim(store.update_candidate(candidate_id, **data))

    @mcp.tool()
    def delete_candidate(candidate_id: int) -> str:
        """Permanently delete a candidate and their pipeline history. Confirm with the user first."""
        name = store.get_candidate(candidate_id)["name"]
        store.delete_candidate(candidate_id)
        return f"Deleted {name}."

    @mcp.tool()
    def create_job(title: str, company: str | None = None, location: str | None = None,
                   description: str | None = None) -> dict[str, Any]:
        """Create a job posting the recruiter is hiring for."""
        return _slim(store.add_job(title, company, location, description))

    @mcp.tool()
    def list_jobs(status: str | None = "open") -> list[dict[str, Any]]:
        """List jobs with how many candidates are in each pipeline. status: open, on_hold, closed, or null for all."""
        return [_slim(j) for j in store.list_jobs(status)]

    @mcp.tool()
    def add_to_pipeline(candidate_id: int, job_id: int, stage: str = "sourced",
                        notes: str | None = None) -> dict[str, Any]:
        """Put a candidate into a job's pipeline (no-op if already there)."""
        return _slim(store.add_to_pipeline(candidate_id, job_id, stage, notes))

    @mcp.tool()
    def move_stage(candidate_id: int, job_id: int, stage: str, notes: str | None = None) -> dict[str, Any]:
        """Move a candidate to a new stage for a job, optionally with a note."""
        return _slim(store.move_stage(candidate_id, job_id, stage, notes))

    @mcp.tool()
    def job_pipeline(job_id: int) -> dict[str, Any]:
        """Everyone in a job's pipeline, grouped by stage."""
        job = store.get_job(job_id)
        grouped: dict[str, list[dict[str, Any]]] = {}
        for a in store.pipeline(job_id):
            grouped.setdefault(a["stage"], []).append(
                {"candidate_id": a["candidate_id"], "name": a["candidate_name"], "since": a["stage_changed_at"]}
            )
        return {"job": _slim(job), "stages": grouped}

    return mcp


def run_stdio(store: Store) -> None:
    # Errors raised inside tools (NotFound, ValueError) are returned to the
    # AI app as tool errors with our plain-English message.
    build_server(store).run("stdio")
