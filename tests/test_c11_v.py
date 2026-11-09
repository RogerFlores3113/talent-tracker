"""c11 V: the view kit (files under static/mcp_views), the People view, candidate_cards, paging and ?ids=."""
import re

import anyio
import pytest
from mcp.types import CallToolResult

from conftest import local_client
from talent.mcp_server import build_server
from talent.mcp_views import RESOURCE_META, URIS, tool_meta, view_html


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def tools(server):
    return {t.name: t for t in anyio.run(server.list_tools)}


def test_views_have_no_external_urls_or_unsafe_dom_calls():
    for name in URIS:
        html = view_html(name)
        assert not re.findall(r"""(?:src|href)\s*=\s*['"]?https?://""", html), name
        assert "innerHTML" not in html and "outerHTML" not in html and "document.write" not in html, name
        assert "fetch(" not in html and "XMLHttpRequest" not in html and "importScripts" not in html, name
        assert "—" not in html and "–" not in html, name
        assert "ui/initialize" in html and "availableDisplayModes" in html, name


def test_the_kit_is_in_files_and_documented():
    from talent.mcp_views import _DIR
    for f in ("kit.css", "kit.js", "people.js", "people.css", "pipeline.js", "today.js", "shortlist.js", "saved.js",
              "decide.js"):
        assert (_DIR / f).is_file(), f
    head = (_DIR / "kit.js").read_text().split("*/")[0]
    for word in ("header(", "personCard(", "peopleTable(", "linksRow(", "jobLink(", "openLink(", "askClaude(",
                 "tellClaude(", "emptyState"):
        assert word in head, word


def test_aliases_serve_the_people_view():
    assert view_html("candidate") == view_html("people") == view_html("results")
    assert tool_meta("people")["ui"]["resourceUri"] == URIS["people"]
    assert tool_meta("people")["ui/resourceUri"] == URIS["people"]  # the older flat key
    assert RESOURCE_META["ui"]["prefersBorder"] is True


def test_every_view_tool_points_at_its_view(store):
    t = tools(build_server(store))
    for name, uri in (("search_candidates", "people"), ("run_saved_search", "people"), ("get_candidate", "people"),
                      ("save_candidate", "people"), ("job_pipeline", "pipeline"), ("list_action_items", "today"),
                      ("save_candidates", "saved"), ("propose_candidates", "shortlist")):
        assert t[name].meta["ui"]["resourceUri"] == URIS[uri], name


def test_resources_ask_for_a_border(store):
    server = build_server(store)
    for r in anyio.run(server.list_resources):
        assert (r.meta or {}).get("ui", {}).get("prefersBorder") is True, r.uri


def test_candidate_cards_is_app_only_and_lean(store):
    server = build_server(store)
    job = store.add_job("Staff Backend", "Acme")
    a = store.add_candidate(name="Ana Ruiz", email="ana@x.com", current_title="Engineer", current_company="Stripe",
                            website="https://ana.dev", about="x" * 900, source="Referral", tags=["python"])
    store.add_to_pipeline(a["id"], job["id"])
    for i in range(4):
        store.log(a["id"], "note", f"note {i}")
    t = tools(server)["candidate_cards"]
    assert t.meta["ui"]["visibility"] == ["app"] and t.annotations.readOnlyHint
    out = data(call(server, "candidate_cards", candidate_ids=[a["id"], 9999]))
    assert out["missing"] == [9999]
    c = out["cards"][0]
    assert c["name"] == "Ana Ruiz" and c["website"] == "https://ana.dev" and c["email"] == "ana@x.com"
    assert len(c["about"]) < 410 and len(c["activity"]) == 3
    assert c["jobs"][0]["app_url"].endswith(f"/jobs/{job['id']}") and c["app_url"].endswith(f"/candidates/{a['id']}")


def test_candidate_cards_caps_at_twenty_and_reads_links(store):
    server = build_server(store)
    ids = [store.add_candidate(name=f"Person {i}")["id"] for i in range(25)]
    assert len(data(call(server, "candidate_cards", candidate_ids=ids))["cards"]) == 20
    from talent.mcp_server import _card_links
    assert _card_links({"links": ["https://github.com/a", "javascript:alert(1)", "https://github.com/a"]}) == [
        "https://github.com/a"]
    assert _card_links({"links": '["https://x.dev"]'}) == ["https://x.dev"]


def test_search_is_lean_and_pages_with_offset(store):
    server = build_server(store)
    for i in range(7):
        store.add_candidate(name=f"Rust Dev {i}", current_title="Rust engineer", email=f"r{i}@x.com")
    first = data(call(server, "search_candidates", query="rust", limit=3))
    assert len(first["results"]) == 3 and first["total"] == 7 and first["offset"] == 0
    row = first["results"][0]
    assert row["has_email"] is True and "created_at" in row and "about" not in row and "work_history" not in row
    second = data(call(server, "search_candidates", query="rust", limit=3, offset=3))
    assert second["offset"] == 3
    assert not {c["id"] for c in first["results"]} & {c["id"] for c in second["results"]}
    # The link to the app carries the query and the filters.
    filt = data(call(server, "search_candidates", query="rust", location="Austin"))
    assert "/candidates?" in filt["app_url"] and "q=rust" in filt["app_url"] and "location=Austin" in filt["app_url"]


def test_candidates_page_shows_exactly_these_ids_ticked(store):
    a = store.add_candidate(name="Aa One")["id"]
    b = store.add_candidate(name="Bb Two")["id"]
    store.add_candidate(name="Cc Three")
    page = local_client(store).get(f"/candidates?ids={a},{b}").text
    assert "Aa One" in page and "Bb Two" in page and "Cc Three" not in page
    assert page.count(" checked ") + page.count(" checked\n") >= 2 or page.count("checked") >= 2


def test_people_view_reads_both_shapes_and_keeps_the_old_states():
    js = view_html("people")
    for bit in ("Array.isArray(c.results)", "c.candidate", "notSaved", "Did you mean ", "Showing results for ",
                "No one named", "saved_status", "possible_duplicates", "candidate_cards", "Load next ",
                "aria-roledescription", "Show as table", "Show as cards", "ui/update-model-context"):
        assert bit in js or bit.replace("ui/update-model-context", "tellClaude") in js, bit
    kit = view_html("people")
    for bit in ("ui/open-link", "ui/message", "ui/request-display-mode", "tool-cancelled", "tool-input-partial",
                "styles", "size-changed", "aria-sort", "Select all"):
        assert bit in kit, bit
