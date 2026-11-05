"""Ready-made AI prompts ("recipes").

The same text is used two ways: the app window shows them as one-click buttons
that copy the prompt and open Claude, and the MCP server publishes them as
prompts the AI app can list on its own.
"""
from __future__ import annotations

import platform
import re
import sys
from dataclasses import dataclass
from urllib.parse import quote, urlencode

from . import __version__

ISSUES_URL = "https://github.com/RogerFlores3113/talent-tracker/issues/new"


@dataclass(frozen=True)
class Recipe:
    key: str
    title: str
    blurb: str


RECIPES: tuple[Recipe, ...] = (
    Recipe("plan_my_day", "Plan my day", "Go through today's action items and suggest what to do first."),
    Recipe("source_for_job", "Source candidates for a job", "Find people who fit a job and save the good ones."),
    Recipe("save_from_linkedin", "Save someone from LinkedIn", "Paste a LinkedIn link and save that person."),
    Recipe("save_from_text", "Save from a resume or profile", "Paste any profile text and let the AI fill the fields."),
    Recipe("log_recent_emails", "Log recent emails", "Read your recent email with candidates and log it on their timelines."),
    Recipe("log_calendar_interviews", "Log interviews from your calendar", "Add this week's interviews to each candidate."),
    Recipe("refresh_stale", "Refresh stale profiles", "Update candidates whose details are months old."),
)
BY_KEY = {r.key: r for r in RECIPES}


def plan_my_day() -> str:
    return (
        "Use Talent Search. Call list_action_items, then give me a short plan for today: "
        "the 3 to 5 things to do first, and why. For each follow-up, draft the message I could send. "
        "Don't send anything; I'll review."
    )


def source_for_job(job: dict | None = None, count: int = 10) -> str:
    if job:
        role = job["title"] + (f" at {job['company']}" if job.get("company") else "")
        where = f" in or near {job['location']}" if job.get("location") else ""
        ref = f" (Talent Search job id {job['id']})"
        desc = f"\n\nJob description:\n{job['description']}" if job.get("description") else ""
    else:
        role, where, ref, desc = "[describe the role]", "", "", ""
    return (
        f"Help me source candidates for {role}{where}{ref}.{desc}\n\n"
        f"1. First search Talent Search for people I already have who fit, using search_candidates.\n"
        f"2. Then research up to {count} new people who fit. For each, show me why they fit in one line.\n"
        f"3. For the ones I approve, call save_candidate with a 2 to 4 sentence `about`, current title, "
        f"company, location, skills as tags and work history, then add_to_pipeline for this job.\n"
        f"Ask me before saving anyone new."
    )


def save_from_linkedin(url: str = "") -> str:
    return (
        f"Save this person to Talent Search: {url or '[paste LinkedIn URL]'}\n\n"
        "Open or read the profile, then call save_candidate with name, linkedin_url, current title, company, "
        "location, a 2 to 4 sentence `about`, skills as tags, and work history (title, company, start, end). "
        "Set source to \"LinkedIn\". Search first so you don't create a duplicate."
    )


def save_from_text(text: str = "") -> str:
    body = text.strip() or "[paste the resume or profile here]"
    return (
        "Save this person to Talent Search. Read the profile below and call save_candidate with every field you "
        "can fill: name, email, phone, linkedin_url, current title, company, location, a 2 to 4 sentence `about`, "
        "skills as tags, and work history. Search first so you don't create a duplicate.\n\n---\n" + body
    )


def log_recent_emails(days: int = 7) -> str:
    return (
        f"Look through my email from the last {days} days for messages with candidates who are in Talent Search. "
        "For each one, call log_activity with kind \"email\", the date it was sent, and a one-line summary "
        "(no full email text). If someone isn't in Talent Search yet, list them and ask me before saving."
    )


def log_calendar_interviews(days: int = 7) -> str:
    return (
        f"Check my calendar for the next {days} days for interviews or calls with candidates in Talent Search. "
        "For each, call log_activity with kind \"interview\" (or \"call\"), the start time including the time zone, "
        "and the job_id if you can tell which job it's for. Then show me the list."
    )


def refresh_stale(limit: int = 5) -> str:
    return (
        f"Call list_action_items and pick up to {limit} \"stale profile\" candidates. For each, look up their "
        "current role, then call update_candidate with what changed (title, company, location, about, work "
        "history). Tell me what you changed for each person."
    )


def render(key: str, **kw) -> str:
    fn = {
        "plan_my_day": plan_my_day, "source_for_job": source_for_job, "save_from_linkedin": save_from_linkedin,
        "save_from_text": save_from_text, "log_recent_emails": log_recent_emails,
        "log_calendar_interviews": log_calendar_interviews, "refresh_stale": refresh_stale,
    }[key]
    return fn(**kw)


def claude_link(prompt: str) -> str:
    """Opens Claude Desktop with the prompt filled in, where the app supports it."""
    return "claude://claude.ai/new?" + urlencode({"q": prompt}, quote_via=quote)


# ----------------------------------------------------------- problem reports
def environment_line() -> str:
    """App version, OS name and Python version. No kernel release or machine name."""
    return f"Talent Search {__version__} · {platform.system() or 'Unknown OS'} · Python {sys.version.split()[0]}"


_PRIVATE = (
    (re.compile(r"[\w.+'-]+@[\w-]+(?:\.[\w-]+)+"), "[email removed]"),
    (re.compile(r"\b(?:https?://|www\.)\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|dev|co|ai|app|me|in)(?:/\S*)?\b",
                re.I), "[link removed]"),
    # Phone-like digit runs, but not dates such as 2026-11-05.
    (re.compile(r"(?<![\w.])(?!\d{4}-\d{2}-\d{2})\+?\(?\d[\d\s().-]{7,}\d(?![\w.])"), "[phone removed]"),
)


def _scrub(text: str) -> str:
    """Take out anything that looks like an email, phone number or link before it goes in a public issue."""
    for pattern, label in _PRIVATE:
        text = pattern.sub(label, text)
    return text


def issue_url(summary: str, details: str = "", where: str = "app") -> str:
    """A prefilled GitHub 'new issue' link. Opening it sends nothing; the user reviews and submits on GitHub.

    Never put candidate data in here.
    """
    title = _scrub((summary or "Problem report").strip())[:120]
    details = _scrub(details or "")
    body = (
        f"**What happened**\n{details.strip() or title}\n\n"
        f"**Where**\n{where}\n\n"
        f"**Version**\n{environment_line()}\n\n"
        "_Please remove anything private (names, emails, notes) before submitting._"
    )
    return ISSUES_URL + "?" + urlencode({"title": title, "body": body}, quote_via=quote)
