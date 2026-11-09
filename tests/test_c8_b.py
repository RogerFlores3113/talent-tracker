"""Cycle 8, implementer B: color tag bubbles on the Candidates table, the person page, CSV and the AI app."""
import csv
import io
import re

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import importers, mcp_server, review, trust
from talent.mcp_server import build_server
from talent.store import COLORS, clean_color, next_color

from conftest import local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def _names(html: str) -> list[str]:
    """Candidate names in the table, in order."""
    body = html.split("<tbody>", 1)[1]
    return re.findall(r'<td><a href="/candidates/\d+">([^<]+)</a></td>', body)


def _people(store, spec):
    """spec: [(name, color)] added in this order, so later ones are newer."""
    ids = {}
    for name, color in spec:
        ids[name] = store.add_candidate(name=name)["id"]
        if color:
            store.set_color(ids[name], color)
    return ids


# ------------------------------------------------------------------ the cycle
def test_cycle_order_wraps_back_to_none():
    seen, color = [], None
    for _ in range(8):
        color = next_color(color)
        seen.append(color)
    assert seen == ["red", "orange", "yellow", "green", "blue", "purple", None, "red"]
    assert next_color(None, -1) == "purple" and next_color("red", -1) is None and next_color("orange", -1) == "red"


def test_clean_color_accepts_the_six_and_none_and_refuses_the_rest():
    assert [clean_color(c) for c in ("Red", " ORANGE ", "yellow", "green", "blue", "purple")] == list(COLORS)
    assert clean_color("") is None and clean_color(None) is None and clean_color("none") is None
    for bad in ("pink", "#ff0000", "red; drop table", "<b>red</b>"):
        with pytest.raises(ValueError, match="isn't a colour"):
            clean_color(bad)


def test_bubble_click_route_saves_and_returns_the_next_step(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/color", data={"color": "red"}, headers={"Accept": "application/json"})
    body = r.json()
    assert r.status_code == 200 and body["color"] == "red" and body["next"] == "orange" and body["prev"] == "none"
    assert body["aria"].startswith("Ana Ruiz: color red. Click for orange.") and body["letter"] == "R" and body["counts"]["red"] == 1
    assert store.get_candidate(cid)["color"] == "red"
    # all the way round, using the buttons' own data-next each time
    for want in ("orange", "yellow", "green", "blue", "purple", "none"):
        body = c.post(f"/candidates/{cid}/color", data={"color": body["next"]}, headers={"Accept": "application/json"}).json()
        assert body["name"] == want
    assert store.get_candidate(cid)["color"] is None and body["aria"].startswith("Ana Ruiz: color none. Click for red.")
    purple = c.post(f"/candidates/{cid}/color", data={"color": "purple"}, headers={"Accept": "application/json"}).json()
    assert purple["aria"].startswith("Ana Ruiz: color purple. Click to clear it.") and purple["next"] == "none"


def test_invalid_color_is_rejected_and_nothing_changes(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    store.set_color(cid, "blue")
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/color", data={"color": "pink"}, headers={"Accept": "application/json"})
    assert r.status_code == 400 and "isn't a colour" in r.json()["error"]
    assert store.get_candidate(cid)["color"] == "blue"
    with pytest.raises(ValueError):
        store.set_color(cid, "pink")
    with pytest.raises(ValueError):
        store.update_candidate(cid, color="pink")
    # the database itself refuses a stray value too
    with pytest.raises(Exception):
        with store._lock, store._conn:
            store._conn.execute("UPDATE candidates SET color = 'pink' WHERE id = ?", (cid,))
    assert c.post("/candidates/9999/color", data={"color": "red"}).status_code == 404


def test_the_click_needs_the_apps_own_origin(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    other = local_client(store, origin=False)
    r = other.post(f"/candidates/{cid}/color", data={"color": "red"}, headers={"Origin": "http://evil.example"})
    assert r.status_code >= 400 and store.get_candidate(cid)["color"] is None


def test_without_script_the_form_posts_and_goes_back(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    c = local_client(store)
    r = c.post(f"/candidates/{cid}/color", data={"color": "green"}, headers={"Referer": "http://127.0.0.1:8765/candidates?tag=x"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/candidates?tag=x"
    assert store.get_candidate(cid)["color"] == "green"


def test_a_quick_click_leaves_no_timeline_entry_and_keeps_the_updated_date(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    before = store.get_candidate(cid)
    store.set_color(cid, "red")
    after = store.candidate_detail(cid)
    assert after["updated_at"] == before["updated_at"]
    assert not [t for t in after["timeline"] if t["kind"] == "change"]


def test_color_persists_across_reopening_the_database(tmp_path):
    from talent.store import Store
    s = Store(tmp_path / "again.db")
    cid = s.add_candidate(name="Ana Ruiz")["id"]
    s.set_color(cid, "purple")
    s.close()
    s = Store(tmp_path / "again.db")
    try:
        assert s.get_candidate(cid)["color"] == "purple"
        assert s.color_counts()["purple"] == 1 and s.color_counts()["none"] == 0
    finally:
        s.close()


# ---------------------------------------------------------------------- sort
def test_default_sort_is_rainbow_then_untagged_newest_first_inside_each(store):
    _people(store, [("Una Plain", None), ("Pat Purple", "purple"), ("Rae Red", "red"), ("Bo Blue", "blue"),
                    ("Olive Orange", "orange"), ("Red Two", "red"), ("Yan Yellow", "yellow"), ("Gus Green", "green"),
                    ("Zed Plain", None)])
    html = local_client(store).get("/candidates").text
    assert _names(html) == ["Red Two", "Rae Red", "Olive Orange", "Yan Yellow", "Gus Green", "Bo Blue", "Pat Purple",
                            "Zed Plain", "Una Plain"]
    assert "Sorted by color" in html
    desc = _names(local_client(store).get("/candidates?sort=color&dir=desc").text)
    assert desc[:2] == ["Zed Plain", "Una Plain"] and desc[-1] in ("Rae Red", "Red Two")


def test_another_column_sorts_by_that_column_alone(store):
    _people(store, [("Cara", "purple"), ("Abe", None), ("Bea", "red")])
    c = local_client(store)
    assert _names(c.get("/candidates?sort=name&dir=asc").text) == ["Abe", "Bea", "Cara"]
    assert _names(c.get("/candidates?sort=name&dir=desc").text) == ["Cara", "Bea", "Abe"]
    page = c.get("/candidates?sort=name&dir=asc").text
    assert "Sorted by name only" in page and "sort=color" in page  # a way back to the color grouping
    assert _names(c.get("/candidates?sort=bogus").text) == ["Bea", "Cara", "Abe"]  # unknown sort: the default


def test_color_column_header_toggles_direction(store):
    _people(store, [("Abe", "red")])
    html = local_client(store).get("/candidates").text
    assert "sort=color&amp;dir=desc" in html


# -------------------------------------------------------------------- filter
def test_color_chips_filter_and_clear_with_counts(store):
    _people(store, [("Rae Red", "red"), ("Red Two", "red"), ("Bo Blue", "blue"), ("Una Plain", None)])
    c = local_client(store)
    html = c.get("/candidates?color=red").text
    assert _names(html) == ["Red Two", "Rae Red"]
    assert 'aria-pressed="true"' in html and re.search(r'data-color-count="red">2<', html)
    assert re.search(r'data-color-count="none">1<', html)
    assert _names(c.get("/candidates?color=none").text) == ["Una Plain"]
    # pressing the chip already chosen clears it (the chips are submit buttons named color_pick)
    assert len(_names(c.get("/candidates?color=red&color_pick=red").text)) == 4
    assert _names(c.get("/candidates?color=red&color_pick=blue").text) == ["Bo Blue"]
    assert len(_names(c.get("/candidates?color=nonsense").text)) == 4  # an unknown value is ignored
    assert 'name="color" value="red"' in html  # the form carries it for the other filters


def test_the_color_filter_keeps_other_filters_and_sort_links(store):
    a = store.add_candidate(name="Ana Ruiz", tags="python")["id"]
    b = store.add_candidate(name="Bo Li", tags="go")["id"]
    store.set_color(a, "red")
    store.set_color(b, "red")
    html = local_client(store).get("/candidates?color=red&tag=python").text
    assert _names(html) == ["Ana Ruiz"]
    assert re.search(r"/candidates\?[^\"]*color=red[^\"]*sort=name|/candidates\?[^\"]*sort=name[^\"]*color=red", html)


# ------------------------------------------------------------------- search
def test_search_sorts_by_match_and_colored_first_groups_by_color(store):
    ids = {}
    for name, color in (("Plain Python Dev", None), ("Python Python Guru", "purple"), ("Python Fan", "red"), ("Python Rookie", "orange")):
        ids[name] = store.add_candidate(name=name, about="python " * (3 if "Guru" in name else 1))["id"]
        if color:
            store.set_color(ids[name], color)
    c = local_client(store)
    plain = c.get("/candidates?q=python").text
    assert "Sorted by best match" in plain and "Colored first" in plain
    by_match = _names(plain)
    assert sorted(by_match) == sorted(ids) and "checked" not in plain.split('name="cf"')[1].split(">")[0]
    grouped = c.get("/candidates?q=python&cf=1").text
    assert _names(grouped) == ["Python Fan", "Python Rookie", "Python Python Guru", "Plain Python Dev"]
    assert "then grouped by color" in grouped and re.search(r'name="cf" value="1" checked', grouped)
    # the match order inside "no color" and the filter still work with a search
    assert _names(c.get("/candidates?q=python&color=purple").text) == ["Python Python Guru"]
    # the box is always there (without a search it says what it does)
    assert 'name="cf"' in c.get("/candidates").text


# ----------------------------------------------------------------- the pages
def test_rows_have_a_real_button_with_aria_and_a_legend(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    c = local_client(store)
    html = c.get("/candidates").text
    assert f'<form class="cbub-form" method="post" action="/candidates/{cid}/color">' in html
    assert re.search(r'<button type="submit" class="cbub" data-color-bubble data-cid="%d" data-who="Ana Ruiz" data-color="" '
                     r'data-next="red" data-prev="purple" aria-label="Ana Ruiz: color none\. Click for red\.' % cid, html)
    store.set_color(cid, "red")
    html = c.get("/candidates").text
    assert 'aria-label="Ana Ruiz: color red. Click for orange.' in html and 'data-color="red"' in html and ">R</span>" in html
    for name in COLORS:  # the legend: every color with its letter and the number of people
        assert f'value="{name}" class="cchip' in html
    page = c.get(f"/candidates/{cid}").text
    assert 'class="cbub big"' in page and 'aria-label="Ana Ruiz: color red. Click for orange.' in page


def test_html_escaping_is_unaffected(store):
    cid = store.add_candidate(name="<script>alert(1)</script> Eve", current_title="<img src=x onerror=1>")["id"]
    store.set_color(cid, "green")
    c = local_client(store)
    for url in ("/candidates", "/candidates?color=green", "/candidates?q=eve&cf=1", f"/candidates/{cid}"):
        html = c.get(url).text
        assert "<script>alert(1)</script>" not in html and "<img src=x" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt; Eve" in c.get("/candidates").text
    # a hostile value in the filter is dropped, never echoed
    html = c.get("/candidates?color=%22%3E%3Cscript%3Ex%3C/script%3E").text
    assert "<script>x" not in html


def test_the_styles_cover_every_color_and_the_script_is_documented(store):
    from pathlib import Path
    root = Path(__file__).parent.parent / "src/talent/static"
    css, js = (root / "style.css").read_text(), (root / "app.js").read_text()
    for name in COLORS:
        assert f'.cbub[data-color="{name}"]' in css
    assert "data-color-bubble" in js.split("(function")[0] and "data-color-count" in js.split("(function")[0]


def _luminance(hex_color):
    def ch(v):
        v /= 255
        return v / 12.92 if v <= 0.03928 else ((v + 0.055) / 1.055) ** 2.4
    r, g, b = (int(hex_color[i:i + 2], 16) for i in (1, 3, 5))
    return 0.2126 * ch(r) + 0.7152 * ch(g) + 0.0722 * ch(b)


def test_bubble_letters_are_readable_on_their_fill():
    from pathlib import Path
    css = (Path(__file__).parent.parent / "src/talent/static/style.css").read_text()
    for name in COLORS:
        fill = re.search(r"--tag-%s: (#\w{6});" % name, css).group(1)
        ink = re.search(r"--tag-%s-ink: (#\w{6});" % name, css).group(1)
        hi, lo = sorted((_luminance(fill), _luminance(ink)), reverse=True)
        assert (hi + 0.05) / (lo + 0.05) >= 4.5, name
        assert f'.cbub[data-color="{name}"] {{ --bub: var(--tag-{name}); --bub-ink: var(--tag-{name}-ink); }}' in css


# ----------------------------------------------------------------------- CSV
def test_csv_export_has_a_color_column_and_it_round_trips(store):
    _people(store, [("Rae Red", "red"), ("Una Plain", None), ("Pat Purple", "purple")])
    text = importers.export_csv(store)
    rows = list(csv.DictReader(io.StringIO(text)))
    assert {r["Name"]: r["Color"] for r in rows} == {"Rae Red": "red", "Una Plain": "", "Pat Purple": "purple"}


def test_csv_import_accepts_color_colour_and_flag_columns(store):
    for head in ("Color", "Colour", "flag", "colour tag"):
        assert importers.guess_mapping(["Name", "Email", head])[2] == "color", head
    assert importers.guess_mapping(["Name", "Email", "Flagship"])[2] != "color"
    header = ["Name", "Email", "Colour"]
    rows = [["Ana Ruiz", "ana@example.com", "Red"], ["Bo Li", "bo@example.com", "PURPLE"],
            ["Cy Park", "cy@example.com", "pink"], ["Di Wu", "di@example.com", ""]]
    out = importers.import_rows(store, header, rows, importers.guess_mapping(header), backup=False)
    got = {c["name"]: c["color"] for c in store.all_candidates()}
    assert got == {"Ana Ruiz": "red", "Bo Li": "purple", "Cy Park": None, "Di Wu": None}
    assert any("pink" in w for w in out["warnings"])


def test_import_round_trip_of_our_own_export(store, tmp_path):
    _people(store, [("Rae Red", "red"), ("Pat Purple", "purple"), ("Una Plain", None)])
    text = importers.export_csv(store)
    rows = list(csv.reader(io.StringIO(text)))
    header, body = rows[0], rows[1:]
    from talent.store import Store
    other = Store(tmp_path / "other.db")
    try:
        importers.import_rows(other, header, body, importers.guess_mapping(header, body), backup=False)
        assert {c["name"]: c["color"] for c in other.all_candidates()} == {"Rae Red": "red", "Pat Purple": "purple", "Una Plain": None}
    finally:
        other.close()


def test_reimporting_never_overwrites_a_color_the_recruiter_set(store):
    cid = store.add_candidate(name="Ana Ruiz", email="ana@example.com")["id"]
    store.set_color(cid, "blue")
    header = ["Name", "Email", "Color"]
    importers.import_rows(store, header, [["Ana Ruiz", "ana@example.com", "red"]], importers.guess_mapping(header), backup=False)
    assert store.get_candidate(cid)["color"] == "blue"


def test_merging_two_people_fills_a_missing_color(store):
    a = store.add_candidate(name="Ana Ruiz", email="a@example.com")["id"]
    b = store.add_candidate(name="Ana R", email="b@example.com")["id"]
    store.set_color(b, "green")
    store.merge_candidates(a, b)
    assert store.get_candidate(a)["color"] == "green"


# ----------------------------------------------------------------------- MCP
@pytest.fixture
def named(monkeypatch):
    box = {"name": "Acme Chat"}
    monkeypatch.setattr(mcp_server, "_client_name", lambda mcp: box["name"])
    return box


def test_the_tool_sets_and_clears_a_color_and_it_shows_in_get_candidate(store, named):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    server = build_server(store)
    out = call(server, "set_candidate_color", candidate_id=cid, color="Green")
    assert out.structuredContent["color"] == "green" and out.structuredContent["changed"] == ["color"]
    assert store.get_candidate(cid)["color"] == "green"
    got = call(server, "get_candidate", candidate_id=cid)
    assert got.structuredContent["color"] == "green" and "Color tag: green" in "\n".join(c.text for c in got.content)
    again = call(server, "set_candidate_color", candidate_id=cid, color="green")
    assert again.structuredContent["changed"] == [] and "already green" in again.content[0].text
    cleared = call(server, "set_candidate_color", candidate_id=cid, color="none")
    assert cleared.structuredContent["color"] == "none" and store.get_candidate(cid)["color"] is None
    with pytest.raises(ToolError, match="colour"):
        call(server, "set_candidate_color", candidate_id=cid, color="teal")
    with pytest.raises(ToolError):
        call(server, "set_candidate_color", candidate_id=9999, color="red")


def test_the_ai_color_change_is_logged_and_undoable(store, named):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    server = build_server(store)
    call(server, "set_candidate_color", candidate_id=cid, color="red")
    [action] = [a for a in store.ai_actions() if a["kind"] == "profile"]
    store.undo_ai_action(action["id"])
    assert store.get_candidate(cid)["color"] is None


def test_search_results_carry_the_color_and_can_be_filtered(store, named):
    ids = _people(store, [("Ana Ruiz", "red"), ("Ana Lee", "blue"), ("Ana Kim", None)])
    server = build_server(store)
    res = call(server, "search_candidates", query="Ana").structuredContent["results"]
    assert {r["name"]: r.get("color") for r in res} == {"Ana Ruiz": "red", "Ana Lee": "blue", "Ana Kim": None}
    only = call(server, "search_candidates", query="Ana", color="red").structuredContent["results"]
    assert [r["name"] for r in only] == ["Ana Ruiz"]
    none = call(server, "search_candidates", query="", color="none").structuredContent["results"]
    assert [r["name"] for r in none] == ["Ana Kim"]


def test_read_only_apps_cannot_set_a_color(store, named):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    server = build_server(store)
    call(server, "search_candidates", query="x")
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    with pytest.raises(ToolError, match="read only"):
        call(server, "set_candidate_color", candidate_id=cid, color="red")
    assert store.get_candidate(cid)["color"] is None
    assert call(server, "get_candidate", candidate_id=cid).isError is False


def test_suggest_only_apps_wait_on_review_and_keep_sets_it_once(store, named):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    trust.set_new_apps(store, "suggest")
    server = build_server(store)
    out = call(server, "set_candidate_color", candidate_id=cid, color="red")
    assert out.structuredContent["pending_review"]["id"] and store.get_candidate(cid)["color"] is None
    [row] = review.pending(store)
    assert row["kind"] == "tool_call" and "Set candidate color" in row["summary"]
    assert local_client(store).post(f"/review/{row['id']}/keep", follow_redirects=False).status_code == 303
    assert store.get_candidate(cid)["color"] == "red" and not review.pending(store)
    with pytest.raises(ToolError, match="isn't a colour|colour"):  # a bad color is refused before it waits
        call(server, "set_candidate_color", candidate_id=cid, color="teal")
    assert not review.pending(store)


def test_cautious_mode_holds_replacing_a_color_but_not_a_first_color(store, named):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    review.set_cautious(store, True)
    server = build_server(store)
    first = call(server, "set_candidate_color", candidate_id=cid, color="red")
    assert "pending_review" not in first.structuredContent and store.get_candidate(cid)["color"] == "red"
    held = call(server, "set_candidate_color", candidate_id=cid, color="blue")
    assert held.structuredContent["pending_review"]["id"] and store.get_candidate(cid)["color"] == "red"
    [row] = review.pending(store)
    local_client(store).post(f"/review/{row['id']}/keep", follow_redirects=False)
    assert store.get_candidate(cid)["color"] == "blue"
    assert "set_candidate_color" in {t.name for t in anyio.run(server.list_tools)}
