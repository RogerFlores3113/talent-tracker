"""Cycle 14 G1: versioned view addresses, the old addresses still reading, and honest "where cards show" copy."""
import anyio
import pytest

from talent import mcp_views
from talent.mcp_server import build_server
from talent.mcp_views import URIS, tool_meta, versioned_uri, versioned_uris, view_html

from conftest import local_client


def test_versioned_uri_shape_and_stability():
    uri = versioned_uri("people")
    assert uri.startswith("ui://talent-search/v-") and uri.endswith("/people.html")
    assert versioned_uri("people") == uri  # same page, same address
    assert len(set(versioned_uris().values())) >= 14  # aliases of People share one page, so they may share a hash
    assert all(v.startswith("ui://") and "?" not in v for v in versioned_uris().values())


def test_address_changes_when_the_html_changes(monkeypatch):
    before = versioned_uri("compare")
    real = mcp_views._read
    monkeypatch.setattr(mcp_views, "_read", lambda n: real(n) + ("\n/* changed */" if n == "compare.js" else ""))
    assert versioned_uri("compare") != before
    assert versioned_uri("jobs") == versioned_uri("jobs")


def test_tool_meta_points_at_the_versioned_uri():
    m = tool_meta("people")
    uri = versioned_uri("people")
    assert m["ui"]["resourceUri"] == uri and m["ui/resourceUri"] == uri and m["openai/outputTemplate"] == uri


def test_server_lists_current_and_reads_old(store):
    server = build_server(store)
    listed = {str(r.uri) for r in anyio.run(server.list_resources)}
    assert listed == set(versioned_uris().values())
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert tools["search_candidates"].meta["ui"]["resourceUri"] in listed
    for old in ("ui://talent-search/people.html", "ui://talent-search/candidate.html", "ui://talent-search/compare.html"):
        body = list(anyio.run(lambda: server.read_resource(old)))[0].content
        assert "ui/initialize" in body
    new = list(anyio.run(lambda: server.read_resource(versioned_uri("compare"))))[0].content
    assert new == view_html("compare")
    with pytest.raises(Exception):
        anyio.run(lambda: server.read_resource("ui://talent-search/nope.html"))


def test_kit_has_the_openai_fallback_and_leaves_bridge_messages_alone():
    html = view_html("people")
    assert "window.openai" in html and "openai:set_globals" in html and "toolResponseMetadata" in html
    assert 'appCapabilities:{availableDisplayModes:["inline","fullscreen"]}' in html  # handshake unchanged
    assert "innerHTML" not in html and "https://" not in html


def test_connect_page_says_where_cards_show(store):
    page = local_client(store).get("/connect").text
    assert "Claude desktop app and the ChatGPT desktop app" in page
    assert "claude.ai in a browser show the same answers as text" in page
    assert "Cards and tables show in the Claude desktop app and on claude.ai" not in page
    assert "quit and reopen the app" in page
    assert 'id="chatgpt-desktop"' in page and "Settings &gt; MCP servers &gt; Add server &gt; STDIO" in page
    assert 'id="chatgpt-cmd"' in page and 'data-copy-from="chatgpt-cmd"' in page
    assert "ChatGPT website and the ChatGPT phone app can't reach apps on this computer" in page
