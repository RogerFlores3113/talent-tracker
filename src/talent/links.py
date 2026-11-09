"""Other links on a person (GitHub, portfolio, Scholar...): one list of web addresses, and one rule for what each
is called. The candidate page, the CSV export and the in-chat views all use this table, so a link reads the same
everywhere. Nothing here makes a request: it only looks at the text of an address."""
from __future__ import annotations

import json
import re
from typing import Any
from urllib.parse import urlsplit

MAX_LINKS = 10
LINK_LIMIT = 500

# host (without www.) -> what to call it. A host also matches when it ends with ".<host>" (gist.github.com).
LINK_LABELS: dict[str, str] = {
    "github.com": "GitHub", "gist.github.com": "GitHub", "gitlab.com": "GitLab", "bitbucket.org": "Bitbucket",
    "x.com": "X", "twitter.com": "X", "mastodon.social": "Mastodon", "bsky.app": "Bluesky",
    "scholar.google.com": "Google Scholar", "orcid.org": "ORCID", "semanticscholar.org": "Semantic Scholar",
    "researchgate.net": "ResearchGate", "arxiv.org": "arXiv",
    "stackoverflow.com": "Stack Overflow", "stackexchange.com": "Stack Exchange",
    "medium.com": "Medium", "substack.com": "Substack", "dev.to": "DEV", "hashnode.dev": "Hashnode",
    "dribbble.com": "Dribbble", "behance.net": "Behance", "kaggle.com": "Kaggle",
    "huggingface.co": "Hugging Face", "youtube.com": "YouTube", "youtu.be": "YouTube",
    "notion.site": "Notion", "notion.so": "Notion", "linkedin.com": "LinkedIn",
    "leetcode.com": "LeetCode", "codepen.io": "CodePen", "figma.com": "Figma", "angel.co": "AngelList",
    "wellfound.com": "Wellfound", "facebook.com": "Facebook", "instagram.com": "Instagram",
    "calendly.com": "Calendly", "linktr.ee": "Linktree", "about.me": "about.me",
}
_ALIASES = {"github": "GitHub", "gitlab": "GitLab", "twitter": "X", "x": "X", "scholar": "Google Scholar",
            "portfolio": "Portfolio"}


def _host(url: str) -> str:
    try:
        host = (urlsplit(url).hostname or "").lower()
    except ValueError:
        return ""
    return host[4:] if host.startswith("www.") else host


def link_label(url: str | None) -> str:
    """What to call this link: "GitHub", "Google Scholar"... and "Portfolio" for a site we don't know."""
    host = _host(url or "")
    if not host:
        return "Portfolio"
    parts = host.split(".")
    for i in range(len(parts) - 1):
        name = LINK_LABELS.get(".".join(parts[i:]))
        if name:
            return name
    return "Portfolio"


def link_key(url: str) -> str:
    """Two spellings of one address share a key: host case, www, a trailing slash and #fragment are ignored."""
    try:
        p = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = _host(url)
    path = p.path.rstrip("/")
    return f"{host}{path}" + (f"?{p.query}" if p.query else "")


def split_links(value: Any) -> list[str]:
    """Raw text or a list into separate entries: split on new lines, spaces, commas and semicolons."""
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        out: list[str] = []
        for v in value:
            out.extend(split_links(v))
        return out
    return [p for p in re.split(r"[\s,;]+", str(value)) if p]


def clean_links(value: Any, *, exclude: tuple[str | None, ...] = (), limit_to: int = MAX_LINKS) -> list[str]:
    """The list of web addresses as saved. Each one must be an http(s) address (a bare "maya.dev" gets https://),
    at most 500 characters. Duplicates and any address in `exclude` (the LinkedIn and Website already saved) are
    dropped. Raises ValueError, in plain words, for an entry that isn't a web address or for too many links."""
    from .store import web_link  # late: store imports this module

    skip = {link_key(e) for e in exclude if e}
    out: list[str] = []
    seen = set(skip)
    for raw in split_links(value):
        try:
            url = web_link(raw)
        except ValueError:
            raise ValueError(f"{raw[:60]!r} isn't a web address, so no links were saved. Use links that start with "
                             "https://, like https://github.com/maya.") from None
        if len(url) > LINK_LIMIT:
            raise ValueError(f"A link is too long ({len(url):,} characters). The limit is {LINK_LIMIT}, "
                             "so no links were saved.")
        k = link_key(url)
        if k in seen:
            continue
        seen.add(k)
        out.append(url)
    if len(out) > limit_to:
        raise ValueError(f"That is {len(out)} other links. A person can have up to {limit_to}, "
                         "so no links were saved.")
    return out


def merge_links(*lists: Any, exclude: tuple[str | None, ...] = ()) -> list[str]:
    """Combine lists of links, keeping the first spelling of each and never dropping any (past the cap, the
    newest are left out). Entries that are not web addresses are skipped, not refused."""
    from .store import web_link

    seen = {link_key(e) for e in exclude if e}
    out: list[str] = []
    for lst in lists:
        for raw in split_links(lst):
            try:
                url = web_link(raw)
            except ValueError:
                continue
            k = link_key(url)
            if k in seen or len(url) > LINK_LIMIT or len(out) >= MAX_LINKS:
                continue
            seen.add(k)
            out.append(url)
    return out


def stored_links(value: Any) -> list[str]:
    """The saved column (JSON text, or None) as a list."""
    if isinstance(value, list):
        return [str(v) for v in value]
    if not value:
        return []
    try:
        data = json.loads(value)
    except (TypeError, ValueError):
        return []
    return [str(v) for v in data] if isinstance(data, list) else []


def labelled(urls: list[str]) -> list[dict[str, str]]:
    return [{"label": link_label(u), "url": u} for u in urls]


def text_line(urls: list[str]) -> str:
    """"Links: GitHub https://..., Portfolio https://..." (empty when none)."""
    return ("Links: " + ", ".join(f"{link_label(u)} {u}" for u in urls)) if urls else ""


# Other names AI apps send for "links": folded into one list (a github or portfolio value is one link).
LINK_ARG_NAMES = ("other_links", "urls", "github", "portfolio", "website_links", "websites", "profiles")


def fold_link_args(args: dict[str, Any]) -> dict[str, Any]:
    """A copy of `args` where links, other_links, urls, github, portfolio... are one `links` list."""
    if not any(k in args for k in LINK_ARG_NAMES):
        return args
    out = dict(args)
    merged: list[str] = split_links(out.get("links"))
    for k in LINK_ARG_NAMES:
        if k in out:
            merged.extend(split_links(out.pop(k)))
    out["links"] = merged or None
    return out


def labels_json() -> str:
    """LINK_LABELS as JSON, for a view that names links the same way link_label does (match a host, or a
    parent of it, and fall back to "Portfolio")."""
    return json.dumps(LINK_LABELS, separators=(",", ":"))
