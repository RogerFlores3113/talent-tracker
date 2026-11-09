"""Optional Typesense index for power users and the later hosted mode.

The desktop app never needs this: SQLite FTS5 handles search out of the box.
When TYPESENSE_URL and TYPESENSE_API_KEY are set, writes are mirrored to a
Typesense collection by draining the store's outbox, and search goes to
Typesense first (typo tolerance, faceting) with FTS5 as the fallback if the
server is unreachable.

Talks to Typesense's REST API with httpx directly, so there's no extra
client dependency to bundle.
"""
from __future__ import annotations

import ipaddress
import json
import logging
import os
import time
from datetime import datetime
from typing import Any
from urllib.parse import urlsplit

import httpx

from .store import HL_END, HL_START, SearchHit, SearchResult, Store

log = logging.getLogger(__name__)

COLLECTION = "candidates"
QUERY_BY = "name,current_title,current_company,location,about,tags"
QUERY_WEIGHTS = "10,5,3,2,1,3"

SCHEMA = {
    "name": COLLECTION,
    "fields": [
        {"name": "name", "type": "string"},
        {"name": "current_title", "type": "string", "optional": True, "facet": True},
        {"name": "current_company", "type": "string", "optional": True, "facet": True},
        {"name": "location", "type": "string", "optional": True, "facet": True},
        {"name": "about", "type": "string", "optional": True},
        {"name": "tags", "type": "string", "optional": True},
        {"name": "updated_ts", "type": "int64"},
    ],
    "default_sorting_field": "updated_ts",
}


def _loopback(host: str) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False


def url_allowed(url: str) -> bool:
    """https anywhere, or http on this computer only."""
    try:
        parts = urlsplit(url.strip())
        host = (parts.hostname or "").lower()
    except ValueError:
        return False
    return bool(host) and (parts.scheme == "https" or (parts.scheme == "http" and _loopback(host)))


def shown_url(url: str) -> str:
    """The address without any user name or password in it, for showing and logging."""
    try:
        parts = urlsplit(url.strip())
        host = parts.hostname or ""
        port = f":{parts.port}" if parts.port else ""
    except ValueError:
        return "(not a valid address)"
    host = f"[{host}]" if ":" in host else host
    return f"{parts.scheme}://{host}{port}{parts.path}"


def env_status() -> dict[str, Any] | None:
    """What TYPESENSE_URL is set to, for the Privacy and Settings pages. None when unset."""
    url = os.environ.get("TYPESENSE_URL")
    if not url:
        return None
    return {"url": shown_url(url), "refused": not url_allowed(url), "has_key": bool(os.environ.get("TYPESENSE_API_KEY"))}


def to_document(c: dict[str, Any]) -> dict[str, Any]:
    doc = {"id": str(c["id"]), "name": c["name"], "updated_ts": int(datetime.fromisoformat(c["updated_at"]).timestamp())}
    for f in ("current_title", "current_company", "location", "about", "tags"):
        if c.get(f):
            doc[f] = ", ".join(c[f]) if isinstance(c[f], list) else c[f]
    return doc


class TypesenseIndex:
    def __init__(self, url: str, api_key: str, *, client: httpx.Client | None = None, timeout: float = 2.0):
        self.url = url.rstrip("/")
        self.client = client or httpx.Client(timeout=timeout)
        self.headers = {"X-TYPESENSE-API-KEY": api_key}

    @classmethod
    def from_env(cls) -> "TypesenseIndex | None":
        """The index from TYPESENSE_URL and TYPESENSE_API_KEY. A plain-http address is only
        used on this computer: candidate data never crosses the network unencrypted."""
        url, key = os.environ.get("TYPESENSE_URL"), os.environ.get("TYPESENSE_API_KEY")
        if not (url and key):
            return None
        if not url_allowed(url):
            log.warning("Ignoring TYPESENSE_URL %s: it must use https unless it's on this computer.", shown_url(url))
            return None
        return cls(url, key)

    def _req(self, method: str, path: str, *, headers: dict[str, str] | None = None, **kw: Any) -> httpx.Response:
        return self.client.request(method, f"{self.url}{path}", headers={**self.headers, **(headers or {})}, **kw)

    def healthy(self) -> bool:
        try:
            return self._req("GET", "/health").json().get("ok") is True
        except (httpx.HTTPError, ValueError):
            return False

    def ensure_collection(self) -> None:
        r = self._req("GET", f"/collections/{COLLECTION}")
        if r.status_code == 404:
            self._req("POST", "/collections", json=SCHEMA).raise_for_status()
        else:
            r.raise_for_status()

    def sync(self, store: Store, batch_size: int = 200) -> int:
        """Push pending outbox changes. Returns how many were applied."""
        applied = 0
        while batch := store.outbox_batch(batch_size):
            # Collapse to the last op per candidate; re-read current rows.
            last_op: dict[int, str] = {}
            for item in batch:
                last_op[item["candidate_id"]] = item["op"]
            upserts = []
            for cid, op in last_op.items():
                if op == "delete":
                    r = self._req("DELETE", f"/collections/{COLLECTION}/documents/{cid}")
                    if r.status_code not in (200, 404):
                        r.raise_for_status()
                else:
                    try:
                        upserts.append(to_document(store.get_candidate(cid)))
                    except LookupError:
                        pass  # deleted after it was queued; a later delete op handles it
            if upserts:
                body = "\n".join(json.dumps(d) for d in upserts)
                r = self._req(
                    "POST",
                    f"/collections/{COLLECTION}/documents/import",
                    params={"action": "upsert"},
                    content=body,
                    headers={"Content-Type": "text/plain"},
                )
                r.raise_for_status()
                failed = [line for line in r.text.splitlines() if line and not json.loads(line).get("success")]
                if failed:
                    raise RuntimeError(f"Typesense rejected {len(failed)} document(s): {failed[0]}")
            store.outbox_ack(batch[-1]["seq"])
            applied += len(last_op)
        return applied

    def rebuild(self, store: Store) -> int:
        r = self._req("DELETE", f"/collections/{COLLECTION}")
        if r.status_code not in (200, 404):
            r.raise_for_status()
        self.ensure_collection()
        store.outbox_clear()
        docs = [to_document(c) for c in store.all_candidates()]
        for i in range(0, len(docs), 500):
            chunk = "\n".join(json.dumps(d) for d in docs[i : i + 500])
            self._req(
                "POST",
                f"/collections/{COLLECTION}/documents/import",
                params={"action": "upsert"},
                content=chunk,
                headers={"Content-Type": "text/plain"},
            ).raise_for_status()
        return len(docs)

    def search(self, store: Store, query: str, *, location: str | None = None, title: str | None = None,
               limit: int = 25, offset: int = 0) -> SearchResult:
        filters = []
        if location:
            filters.append(f"location:`{location.replace('`', '')}`")
        if title:
            filters.append(f"current_title:`{title.replace('`', '')}`")
        params = {
            "q": query or "*",
            "query_by": QUERY_BY,
            "query_by_weights": QUERY_WEIGHTS,
            "per_page": limit,
            "page": offset // max(limit, 1) + 1,
            "highlight_fields": "about",
            "highlight_start_tag": HL_START,
            "highlight_end_tag": HL_END,
        }
        if filters:
            params["filter_by"] = " && ".join(filters)
        if not query:
            params["sort_by"] = "updated_ts:desc"
        r = self._req("GET", f"/collections/{COLLECTION}/documents/search", params=params)
        r.raise_for_status()
        data = r.json()
        hits = []
        for h in data.get("hits", []):
            try:
                cand = store.get_candidate(int(h["document"]["id"]))
            except LookupError:
                continue  # index briefly ahead of a delete; SQLite is the truth
            snippet = next((hl.get("snippet") for hl in h.get("highlights", []) if hl.get("field") == "about"), None)
            hits.append(SearchHit(candidate=cand, snippet=snippet, score=h.get("text_match")))
        return SearchResult(hits=hits, total=data.get("found", len(hits)), engine="typesense", query=query,
                            filters={k: v for k, v in (("location", location), ("title", title)) if v})


class SearchService:
    """What the UI and MCP server call. Picks Typesense when it's up, FTS5 otherwise."""

    RETRY_AFTER_S = 30.0

    def __init__(self, store: Store, index: TypesenseIndex | None = None):
        self.store = store
        self.index = index
        self._down_until = 0.0
        if index is None:
            store.outbox_clear()  # nothing will ever drain it

    @property
    def engine(self) -> str:
        return "typesense" if self.index is not None and time.monotonic() >= self._down_until else "built-in"

    def search(self, query: str = "", **kw: Any) -> SearchResult:
        if any(kw.get(k) for k in ("tag", "source", "job_id", "client", "stage", "pipe", "color")):  # these filters live in SQLite
            return self.store.search_candidates(query, **kw)
        kw.pop("tag", None)
        kw.pop("source", None)
        for k in ("job_id", "client", "stage", "pipe", "days", "color"):
            kw.pop(k, None)
        if self.index is not None and time.monotonic() >= self._down_until:
            try:
                self.index.sync(self.store)
                return self.index.search(self.store, query, **kw)
            except (httpx.HTTPError, RuntimeError) as exc:
                # Don't make every keystroke wait on a dead server.
                self._down_until = time.monotonic() + self.RETRY_AFTER_S
                log.warning("Typesense unavailable, using built-in search: %s", exc)
        return self.store.search_candidates(query, **kw)
