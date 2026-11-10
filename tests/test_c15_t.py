"""Cycle 15 T: in-chat views wear the Talent Search theme (generated from style.css), and the server says which."""
import re
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult

import talent.mcp_views as mv
from talent.mcp_server import build_server
from talent.mcp_views import THEME_MAP, THEME_NAMES, theme_blocks, theme_css, versioned_uri, view_html
from talent.store import DEFAULT_THEME, THEMES
from test_themes import ratio

BLOCKS = theme_blocks()
KIT_TOKENS = [mine for mine, _ in THEME_MAP]
# the rule each theme (and each dark variant) is written under in the generated CSS
RULES = {"sunset": ":root,:root[data-tt=sunset]{", "sunset-dark": ":root[data-theme=dark]:not([data-tt]),:root[data-tt=sunset][data-theme=dark]{",
         "classic": ":root[data-tt=classic]{", "classic-dark": ":root[data-tt=classic][data-theme=dark]{",
         "forest-night": ":root[data-tt=forest-night]{", "ocean": ":root[data-tt=ocean]{", "midnight": ":root[data-tt=midnight]{"}


def rule_values(key: str) -> dict[str, str]:
    css = theme_css()
    start = css.index(RULES[key]) + len(RULES[key])
    return dict(re.findall(r"(--[\w-]+|color-scheme):([^;]+);", css[start:css.index("}", start)]))


def test_theme_names_match_the_app():
    assert set(THEME_NAMES) == {t[0] for t in THEMES}
    assert set(RULES) == set(BLOCKS)


@pytest.mark.parametrize("key", sorted(RULES))
def test_generated_css_defines_every_kit_token_with_the_apps_values(key):
    got = rule_values(key)
    assert set(got) == set(KIT_TOKENS), set(got) ^ set(KIT_TOKENS)
    for mine, theirs in THEME_MAP:
        assert got[mine].strip() == BLOCKS[key][theirs], (key, mine)


def test_dark_variants_follow_the_host_or_the_computer():
    css = theme_css()
    assert "@media (prefers-color-scheme:dark){:root:not([data-theme]):not([data-tt]),:root[data-tt=sunset]:not([data-theme])" in css
    assert "@media (prefers-color-scheme:dark){:root[data-tt=classic]:not([data-theme])" in css
    assert rule_values("forest-night")["color-scheme"] == "dark" and rule_values("ocean")["color-scheme"] == "light"


@pytest.mark.parametrize("key", sorted(RULES))
def test_view_text_is_readable_on_every_theme(key):
    t = rule_values(key)
    for fg, bg in (("--text", "--surface"), ("--text", "--page"), ("--muted", "--surface"), ("--on-accent", "--accent"),
                   ("--btn-text", "--btn-from"), ("--btn-text", "--btn-to"), ("--text", "--chip")):
        assert ratio(t[fg], t[bg]) >= 4.5, (key, fg, bg, round(ratio(t[fg], t[bg]), 2))


def test_kit_carries_no_colors_of_its_own_and_every_view_gets_the_theme():
    kit = (Path(mv.__file__).parent / "static/mcp_views/kit.css").read_text()
    assert "--surface:#" not in kit and "--accent:#" not in kit and "prefers-color-scheme" not in kit
    html = view_html("people")
    assert "/* theme (generated from style.css) */" in html and html.index("--page:#fbf6ec") < html.index("body{margin:4px")
    kit_js = (Path(mv.__file__).parent / "static/mcp_views/kit.js").read_text()
    assert '"--color-background-primary"' not in kit_js  # the host's colors never reach the view


def test_view_uri_changes_when_the_css_changes(monkeypatch):
    before = versioned_uri("digest")
    monkeypatch.setattr(mv, "theme_css", lambda: "/* changed */")
    assert versioned_uri("digest") != before


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def test_view_results_carry_the_saved_theme(store):
    server = build_server(store)
    for tool, args in (("search_candidates", {"query": ""}), ("find_duplicates", {}),
                       ("status_digest", {}), ("list_jobs", {})):
        res = call(server, tool, **args)
        assert isinstance(res, CallToolResult)
        assert res.meta["talent/theme"] == DEFAULT_THEME == "sunset", tool
    store.set_settings(theme="forest-night")
    assert call(server, "search_candidates", query="").meta["talent/theme"] == "forest-night"


def test_an_unknown_saved_theme_gives_sunset(store):
    store._conn.execute("INSERT INTO settings(key, value) VALUES ('theme', 'nope') ON CONFLICT(key) DO UPDATE SET value = 'nope'")
    store._conn.commit()
    assert call(build_server(store), "list_jobs").meta["talent/theme"] == "sunset"


def test_a_tool_without_a_view_carries_no_theme(store):
    server = build_server(store)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert not ((tools["clients_due"].meta or {}).get("ui") or {}).get("resourceUri")
    res = call(server, "clients_due")
    assert "talent/theme" not in (getattr(res, "meta", None) or {})

