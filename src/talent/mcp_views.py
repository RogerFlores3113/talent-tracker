"""In-chat views for MCP Apps hosts (Claude and others that support `text/html;profile=mcp-app`).

The view code lives in static/mcp_views/ (kit.css, kit.js, people.css and one <view>.js each), is read when a view
is asked for, and is put together into one self-contained page here.

Each view is a self-contained HTML page with no external requests. It talks to
the host over postMessage (JSON-RPC): `ui/initialize`, then waits for the
`ui/notifications/tool-result` carrying the tool's structuredContent, and
renders it with DOM calls only (never innerHTML with data), so candidate text
can't inject markup. Hosts without MCP Apps just show the tool's text content.
"""
from __future__ import annotations

import json
from pathlib import Path

from . import __version__
from .links import labels_json

MIME = "text/html;profile=mcp-app"
URIS = {
    "people": "ui://talent-search/people.html",
    "candidate": "ui://talent-search/candidate.html",  # aliases of people (older hosts and saved links keep working)
    "results": "ui://talent-search/search-results.html",
    "pipeline": "ui://talent-search/pipeline-board.html",
    "today": "ui://talent-search/action-items.html",
    "shortlist": "ui://talent-search/shortlist.html",
    "saved": "ui://talent-search/batch-save.html",
    "decide": "ui://talent-search/finalists.html",
    "jobs": "ui://talent-search/jobs.html",
    "searches": "ui://talent-search/saved-searches.html",
    "followups": "ui://talent-search/followups.html",
    "digest": "ui://talent-search/digest.html",
    "interviews": "ui://talent-search/interviews.html",
    "owed": "ui://talent-search/feedback-owed.html",
    "duplicates": "ui://talent-search/duplicates.html",
}

# The People view serves search results, one candidate and a saved candidate; the older names are aliases of it.
ALIASES = {"candidate": "people", "results": "people"}
# Views that draw the people table (or one like it) also load people.css.
WITH_TABLE = ("people", "decide", "shortlist")
# The host draws a border around the view (it sits inside the chat like a card).
RESOURCE_META = {"ui": {"prefersBorder": True}}
_DIR = Path(__file__).parent / "static" / "mcp_views"


def _read(name: str) -> str:
    return (_DIR / name).read_text(encoding="utf-8")


def view_html(name: str) -> str:
    name = ALIASES.get(name, name)
    if name not in URIS:
        raise KeyError(name)
    own = _DIR / (name + ".css")  # a view may bring its own styles (interviews.css ...)
    table = name in WITH_TABLE or name == "searches"  # these show the People card or table, so they need people.css
    css = (_read("kit.css") + ("\n" + _read("people.css") if table else "")
           + ("\n" + own.read_text(encoding="utf-8") if own.exists() and own.name != "people.css" else ""))
    kit = (_read("kit.js").replace("__VERSION__", json.dumps(__version__))
           .replace("__LINK_LABELS__", labels_json()))  # one table of link names, shared with the app
    # Saved searches shows the People view inside itself: people.js loads first, its two entry points are kept,
    # then searches.js takes over render and draw (see searches.js).
    scripts = (_read("people.js") + "\n</script><script>const renderPeople=render,drawPeople=draw;</script><script>\n"
               if name == "searches" else "") + _read(name + ".js")
    return (
        "<!doctype html><html lang='en'><head><meta charset='utf-8'><meta name='viewport' content='width=device-width'>"
        "<title>Talent Search</title>"
        f"<style>{css}</style></head><body><main id='main'><p class='empty'>Loading...</p></main>"
        "<div id='drawer' hidden></div><div id='linknote'></div>"
        "<div id='live' class='sr' role='status' aria-live='polite'></div>"
        f"<script>{kit}\n{scripts}</script></body></html>"
    )


def tool_meta(name: str) -> dict:
    uri = URIS[name]
    # "ui/resourceUri" is the older flat key some hosts still read.
    return {"ui": {"resourceUri": uri}, "ui/resourceUri": uri}
