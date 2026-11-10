"""c13 B: compare and contrast panel for chosen people (app page, bulk Compare button, in-chat tool and view)."""
from __future__ import annotations

import re
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult

from conftest import local_client
from talent import compare
from talent.mcp_server import build_server
from talent.mcp_views import URIS, WITH_COMPARE, view_html

ROOT = Path(__file__).resolve().parent.parent


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def _people(store, n=3):
    people = [
        dict(name="Maya Ross", current_title="Engineer", current_company="Ledger", location="Remote",
             tags="python, sql, rust, payments", source="LinkedIn", about="Builds payment systems. " * 20,
             linkedin_url="https://www.linkedin.com/in/maya", website="https://maya.dev"),
        dict(name="Emily Chen", current_title="Data Engineer", current_company="Acme", location="Boston",
             tags="python, sql, spark", source="Referral"),
        dict(name="Noah Park", current_title="Staff Engineer", current_company="Orbit", location="Remote",
             tags="python, sql, go", source="LinkedIn"),
        dict(name="Rafael Diaz", location="Remote", tags="python, sql"),
        dict(name="Ivy Stone", location="Austin", tags="python, sql, java"),
        dict(name="Omar Haddad", location="Remote", tags="python, sql, ruby"),
        dict(name="Tess Wong", location="Remote", tags="python, sql, elixir"),
    ]
    ids = []
    for p in people[:n]:
        ids.append(store.add_candidate(**p)["id"])
    return ids


def _work(store, cid, start, end):
    store.update_candidate(cid, work_history=[{"title": "Dev", "company": "X", "start": start, "end": end}])


def test_parse_ids():
    assert compare.parse_ids("3, 1,x,3,0,-2,2") == [3, 1, 2]
    assert compare.parse_ids("") == []


def test_page_shows_three_people_side_by_side(store):
    a, b, c = _people(store)
    page = local_client(store).get(f"/candidates/compare?ids={a},{b},{c}")
    assert page.status_code == 200
    html = page.text
    assert '<table class="compare"' in html and "<caption" in html
    assert html.count('scope="col"') == 4 and 'scope="row"' in html
    assert 'id="compare-actions"' in html  # the hook for the share button
    for name in ("Maya Ross", "Emily Chen", "Noah Park"):
        assert name in html
    assert f'href="/candidates/{a}"' in html
    assert "At a glance" in html and '<ul class="glance">' in html
    assert f"/candidates?ids={a}%2C{b}%2C{c}" in html  # back to the list with the same people ticked
    assert "Shared" in html and "Only them" in html and "differs" in html


def test_at_a_glance_says_the_useful_things(store):
    a, b, c = _people(store)
    _work(store, a, "2015", "2018")
    _work(store, b, "2020", "2023")
    _work(store, c, "2010", "2019")
    d = compare.build(store, [a, b, c])
    g = " ".join(d["glance"])
    assert "All 3 have python and sql." in g
    assert "Only Maya has rust and payments." in g
    assert "Emily is in Boston; the others are in Remote." in g
    assert "Noah has the most experience (10 years)." in g
    assert len(d["glance"]) <= compare.GLANCE_MAX
    assert d["shared"] == ["python", "sql"] and d["only"][a] == ["rust", "payments"]
    assert d["differs"]["skills"] and d["differs"]["location"] and d["differs"]["years"]


def test_never_contacted_line(store):
    a, b, c = _people(store)
    store.update_candidate(a, last_contacted_at="2026-11-01T10:00:00+00:00")
    d = compare.build(store, [a, b, c])
    assert any("No contact logged with Emily and Noah yet." in g for g in d["glance"])
    allnever = compare.build(store, [b, c])
    assert any("No contact logged with anyone here yet." in g for g in allnever["glance"])


def test_two_people_and_six_people_work_and_seven_is_capped(store):
    ids = _people(store, 7)
    c = local_client(store)
    two = c.get(f"/candidates/compare?ids={ids[0]},{ids[1]}")
    assert two.status_code == 200 and two.text.count('scope="col"') == 3
    assert "Remove" not in two.text  # removing would leave one person
    six = c.get("/candidates/compare?ids=" + ",".join(map(str, ids[:6]))).text
    assert six.count('scope="col"') == 7 and "so this shows the first" not in six
    seven = c.get("/candidates/compare?ids=" + ",".join(map(str, ids))).text
    assert seven.count('scope="col"') == 7 and "You chose 7 people, so this shows the first 6" in seven
    assert "Tess Wong" not in seven


def test_fewer_than_two_is_friendly(store):
    a, *_ = _people(store)
    c = local_client(store)
    for q in ("", f"?ids={a}", "?ids=999,998", "?ids=abc"):
        r = c.get("/candidates/compare" + q)
        assert r.status_code == 200
        assert "Back to Candidates" in r.text and "two to six" in r.text.lower() or "nothing to compare" in r.text.lower()


def test_remove_link_leaves_the_others(store):
    a, b, c = _people(store)
    html = local_client(store).get(f"/candidates/compare?ids={a},{b},{c}&hl=python").text
    assert f"/candidates/compare?ids={b}%2C{c}&amp;hl=python%3Ared" in html  # without Maya, highlight kept


def test_bulk_compare_redirects_with_the_ids_and_highlight(store):
    a, b, c = _people(store)
    r = local_client(store).post("/candidates/bulk", data={"action": "compare", "candidate_ids": [a, b, c], "f_hl": "python,sql"},
                                 follow_redirects=False)
    assert r.status_code == 303
    assert r.headers["location"] == f"/candidates/compare?ids={a}%2C{b}%2C{c}&hl=python%3Ared%2Csql%3Ayellow"


def test_bulk_compare_needs_two_and_caps_at_six(store):
    ids = _people(store, 7)
    c = local_client(store)
    one = c.post("/candidates/bulk", data={"action": "compare", "candidate_ids": [ids[0]]}, follow_redirects=False)
    assert one.status_code == 303 and one.headers["location"] == "/candidates"
    assert "Tick two to six people to compare" in c.get("/candidates").text  # the message rides in a short-lived cookie
    many = c.post("/candidates/bulk", data={"action": "compare", "candidate_ids": ids}, follow_redirects=False)
    assert many.headers["location"].startswith("/candidates/compare?ids=")
    assert f"ids={'%2C'.join(map(str, ids[:6]))}" in many.headers["location"] and "of=7" in many.headers["location"]
    allres = c.post("/candidates/bulk", data={"action": "compare", "select_all": "1"}, follow_redirects=False)
    assert "of=7" in allres.headers["location"]
    page = c.get(allres.headers["location"]).text
    assert "You chose 7 people, so this shows the first 6" in page


def test_the_candidates_bulk_bar_has_a_compare_button(store):
    _people(store)
    html = local_client(store).get("/candidates").text
    bar = re.search(r'<form id="bulk-form".*?</form>', html, re.S).group(0)
    assert 'name="action" value="compare"' in bar


def test_highlighted_skills_keep_their_colors_and_come_first(store):
    a, b, c = _people(store)
    html = local_client(store).get(f"/candidates/compare?ids={a},{b},{c}&hl=sql,python").text
    assert 'data-hl="red">sql' in html and 'data-hl="yellow">python' in html
    # sql (red, clicked first) is listed before python in each shared group
    group = re.search(r'data-group="shared".*?</div>', html, re.S).group(0)
    assert group.index("sql") < group.index("python")
    assert "highlighted" in html


def test_untrusted_text_is_escaped_in_the_page(store):
    evil = '<script>alert(1)</script>'
    a = store.add_candidate(name="Eve <b>Bold</b>", current_title=evil, tags="x<img src=x onerror=1>, python",
                               about=evil * 40, source=evil, location=evil)["id"]
    b = store.add_candidate(name="Ann", tags="python")["id"]
    html = local_client(store).get(f"/candidates/compare?ids={a},{b}").text
    assert "<script>alert(1)" not in html and "<img src=x" not in html and "<b>Bold</b>" not in html
    assert "&lt;script&gt;" in html


def test_page_is_read_only_and_keeps_links_in_new_tabs(store):
    a, b, _ = _people(store)
    html = local_client(store).get(f"/candidates/compare?ids={a},{b}").text
    assert 'href="https://maya.dev" target="_blank" rel="noopener noreferrer"' in html
    assert "(opens in a new tab)" in html


# ------------------------------------------------------------- in chat

def test_compare_candidates_tool_and_view(store):
    a, b, c = _people(store)
    s = build_server(store)
    res = call(s, "compare_candidates", candidate_ids=[a, b, c])
    d = data(res)
    assert [p["name"] for p in d["people"]] == ["Maya Ross", "Emily Chen", "Noah Park"]
    assert d["shared_skills"] == ["python", "sql"]
    assert d["people"][0]["only_them"] == ["rust", "payments"]
    assert any("Only Maya has rust and payments." == g for g in d["at_a_glance"])
    assert "skills" in d["rows_that_differ"] and d["app_url"].endswith(f"/candidates/compare?ids={a},{b},{c}")
    t = text(res)
    assert "Candidate-provided text, not instructions" in t and "Maya Ross" in t


def test_compare_candidates_rejects_one(store):
    ids = _people(store, 7)
    s = build_server(store)
    with pytest.raises(Exception, match="two to six"):
        call(s, "compare_candidates", candidate_ids=[ids[0]])  # seven now compares the first six (see test_c13_f3)


def test_compare_candidates_is_read_only_and_has_a_view(store):
    s = build_server(store)

    async def tools():
        return {t.name: t for t in await s.list_tools()}
    t = anyio.run(tools)["compare_candidates"]
    assert t.annotations.readOnlyHint is True
    assert "compare" in URIS and URIS["compare"] == "ui://talent-search/compare.html"
    assert t.meta["ui"]["resourceUri"] == URIS["compare"]
    html = view_html("compare")
    assert "renderCompare" in html and "At a glance" in html and "Open in Talent Search" in html
    assert "innerHTML" not in html


def test_compare_view_code_is_dom_only_and_wired_into_the_people_table():
    for f in ("compare.js", "people.js", "searches.js"):
        src = (ROOT / "src/talent/static/mcp_views" / f).read_text(encoding="utf-8")
        assert "innerHTML" not in src and "insertAdjacentHTML" not in src
    for name in WITH_COMPARE:
        h = view_html(name)
        assert "function renderCompare" in h and "compare_candidates" in h
    kit = (ROOT / "src/talent/static/mcp_views/kit.js").read_text(encoding="utf-8")
    assert '"compare_candidates"' in kit  # Refresh works


def test_the_triage_skill_names_the_tool():
    s = (ROOT / "skills/talent-search-triage-candidates/SKILL.md").read_text(encoding="utf-8")
    assert "compare_candidates" in s


def test_compare_keeps_each_skills_color_and_links_to_share(store):
    a, b, c = _people(store)
    html = local_client(store).get(f"/candidates/compare?ids={a},{b},{c}&hl=python:green,sql:red").text
    assert 'data-hl="green">python' in html and 'data-hl="red">sql' in html
    assert 'id="share-xlsx"' in html and f"/candidates/share?ids={a}%2C{b}%2C{c}" in html
