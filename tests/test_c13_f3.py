"""c13 F3: compare page, in-chat compare and export polish."""
from __future__ import annotations

import re
from pathlib import Path

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError
from mcp.types import CallToolResult

from conftest import local_client
from talent import compare, mcp_server, review, trust
from talent.mcp_server import build_server

ROOT = Path(__file__).resolve().parent.parent
VIEWS = ROOT / "src/talent/static/mcp_views"


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def six(store):
    rows = [
        dict(name="Emily Chen", tags="python, sql, spark, observability", location="Boston", source="Referral",
             linkedin_url="https://www.linkedin.com/in/emily", website="http://127.0.0.1:8765/x"),
        dict(name="Rafael Diaz", tags="python, go, observability", location="Remote"),
        dict(name="Priya Raman", tags="python, sql, observability", location="Remote"),
        dict(name="Noah Park", tags="sql, java", location="Remote"),
        dict(name="Ivy Stone", tags="python, ruby", location="Remote"),
        dict(name="Omar Haddad", tags="elixir", location="Remote"),
        dict(name="Tess Wong", tags="python", location="Remote"),
    ]
    return [store.add_candidate(**r)["id"] for r in rows]


# ------------------------------------------------------------ 1. three skill groups
def test_skills_have_three_groups_and_a_partly_shared_glance_line(store):
    ids = six(store)[:6]
    d = compare.build(store, ids)
    assert d["shared"] == []
    assert "python" in [t.lower() for t in d["rest"][ids[0]]] and d["counts"]["python"] == 4
    assert "python" not in [t.lower() for t in d["only"][ids[0]]]
    g = " ".join(d["glance"])
    assert "Emily, Rafael, Priya and Ivy have python." in g or "Emily, Rafael, Priya and Ivy have python and" in g
    groups = compare.skill_groups(d)
    emily = {x["key"]: x for x in groups[ids[0]]}
    assert [s["name"] for s in emily["some"]["skills"]][:1] == ["python"] and emily["some"]["skills"][0]["count"] == 4
    assert emily["only"]["skills"][0]["name"] == "spark"


def test_glance_uses_full_names_when_first_names_collide(store):
    a = store.add_candidate(name="Emily Chen", tags="python, sql")["id"]
    b = store.add_candidate(name="Emily Ross", tags="python")["id"]
    c = store.add_candidate(name="Noah Park", tags="python, sql")["id"]
    d = compare.build(store, [a, b, c])
    assert any("Emily Chen and Noah Park have sql." in g for g in d["glance"])


def test_tool_text_and_data_say_some_of_them(store):
    ids = six(store)[:6]
    res = call(build_server(store), "compare_candidates", candidate_ids=ids)
    emily = data(res)["people"][0]
    assert {"skill": "python", "count": 4, "of": 6} in emily["some_of_them"]
    assert "python, 4 of 6" in text(res) and "only them: spark" in text(res)


# ------------------------------------------------------------ 2. page layout and header
def test_header_has_one_name_link_and_a_labelled_remove_button(store):
    ids = six(store)[:6]
    html = local_client(store).get("/candidates/compare?ids=" + ",".join(map(str, ids))).text
    head = re.search(r"<thead>.*?</thead>", html, re.S).group(0)
    first = head.split("</th>", 2)[1]  # Emily's column: one link, one remove button, no hidden repeats
    assert first.count(f'href="/candidates/{ids[0]}"') == 1 and "sr-only" not in first and first.count(">Emily Chen<") == 1
    assert 'aria-label="Remove Emily Chen from the comparison"' in head
    assert "color red" not in head and "Open profile" not in head
    assert 'style="--cols: 6"' in html
    css = (ROOT / "src/talent/static/style.css").read_text()
    assert "calc(9.5rem + var(--cols, 3) * 12rem)" in css and "hyphens: none" in css


# ------------------------------------------------------------ 3. empty rows and years
def test_rows_empty_for_everyone_are_left_out_and_years_says_work_dates(store):
    a = store.add_candidate(name="Ann Lee", tags="python")["id"]
    b = store.add_candidate(name="Bo Kim", tags="python, go")["id"]
    store.update_candidate(a, work_history=[{"title": "Dev", "company": "X", "start": "2015", "end": "2020"}])
    html = local_client(store).get(f"/candidates/compare?ids={a},{b}").text
    for gone in ("links", "about", "source", "contacted"):
        assert f'data-row="{gone}"' not in html
    assert 'data-row="years"' in html and "No work dates saved" in html and "No dates saved" not in html
    assert html.count("Add work history on a profile to see years of experience.") == 1
    c = store.add_candidate(name="Cy Do", tags="x")["id"]
    html2 = local_client(store).get(f"/candidates/compare?ids={b},{c}").text
    assert 'data-row="years"' not in html2 and "Add work history" not in html2


# ------------------------------------------------------------ 4. colors in words
def test_highlight_colors_are_named_and_counted(store):
    ids = six(store)[:3]
    html = local_client(store).get("/candidates/compare?ids=" + ",".join(map(str, ids)) + "&hl=python:red,sql:yellow").text
    assert "(highlighted red)" in html and "(highlighted yellow)" in html
    assert "highlighted red, number 1" in html
    assert "Has 2 of 2 highlighted skills" in html  # Emily has python and sql


# ------------------------------------------------------------ 5. hiring manager view
def test_hiring_manager_view_drops_the_recruiters_own_columns(store):
    ids = six(store)[:3]
    job = store.add_job("Data Engineer", "Acme")
    store.add_to_pipeline(ids[0], job["id"], "Interview")
    q = ",".join(map(str, ids))
    c = local_client(store)
    html = c.get(f"/candidates/compare?ids={q}&hl=python:green&view=hm").text
    for gone in ("Found via", "Last contacted", "Added", "never contacted", "Open profile", "Remove", 'class="cbub',
                 "Back to Candidates", 'class="top"', 'class="foot"', "127.0.0.1", "localhost"):
        assert gone not in html, gone
    assert "Data Engineer (Acme): Interview" in html
    assert "Emily Chen" in html and 'data-hl="green"' in html and "linkedin.com/in/emily" in html
    assert not re.search(r'href="/candidates/\d', html) and 'href="/jobs/' not in html
    full = c.get(f"/candidates/compare?ids={q}&hl=python:green").text
    assert f"view=hm" in full and 'id="hm-view"' in full
    assert f"/candidates/share?ids={ids[0]}%2C{ids[1]}%2C{ids[2]}&amp;hl=python%3Agreen" in full and 'id="share-xlsx"' in full
    assert "@media print" in (ROOT / "src/talent/static/style.css").read_text().split("Compare people (/candidates/compare)")[1]


# ------------------------------------------------------------ 6. in chat
def test_compare_takes_the_first_six_and_names_who_was_left_out(store):
    ids = six(store)
    res = call(build_server(store), "compare_candidates", candidate_ids=ids)
    d = data(res)
    assert len(d["people"]) == 6 and d["left_out"] == [{"id": ids[6], "name": "Tess Wong"}]
    t = text(res)
    assert "Tess Wong" in t and "6 at a time" in t
    assert t.rstrip().endswith("Open in Talent Search: " + d["app_url"])
    assert "/candidates/share?ids=" in d["share_url"]
    with pytest.raises(Exception, match="2 to 6"):
        call(build_server(store), "compare_candidates", candidate_ids=[ids[0]])


def test_compare_view_has_share_export_and_remove():
    js = (VIEWS / "compare.js").read_text()
    for needle in ("Share as xlsx", "Export xlsx", "Remove \"+p.name+\" from the comparison", "compare_candidates", "exportIds(",
                   "No work dates saved", "Some of them"):
        assert needle in js, needle
    assert "innerHTML" not in js
    css = (VIEWS / "compare.css").read_text()
    assert "12rem" in css and "hyphens:none" in css and "position:sticky" in css


# ------------------------------------------------------------ 7. People table
def test_people_table_export_and_share_buttons():
    js = (VIEWS / "people.js").read_text()
    assert '" ticked"' in js and '" shown"' in js and "more not loaded; open in Talent Search to export all" in (VIEWS / "kit.js").read_text()
    assert "Share as xlsx" in js and "/candidates/share?ids=" in js
    kit = (VIEWS / "kit.js").read_text()
    assert "innerHTML" not in kit.split("function exportIds")[1].split("function profileLink")[0]
    assert "Saved \"+d.file+\" with" in kit


# ------------------------------------------------------------ 8. export replies
@pytest.fixture
def downloads(tmp_path, monkeypatch):
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(tmp_path / "dl"))


def _set(store, key, value):
    with store._conn:
        store._conn.execute("INSERT INTO settings(key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value", (key, value))


def test_export_replies_are_plain_second_person(store, downloads):
    a, b = [store.add_candidate(name=n)["id"] for n in ("Ana Ruiz", "Ben Cho")]
    s = build_server(store)
    t = text(call(s, "export_candidates_xlsx", candidate_ids=[a], include_contact=True))
    assert t.startswith("No file made. Email, phone and notes are yours to include, so pick them on the Share page: http")
    t = text(call(s, "export_candidates_xlsx", candidate_ids=[a], for_hiring_manager=False))
    assert "own use" in t and "Share page" in t and "themselves" not in t
    _set(store, review.SETTING, "1")
    t = text(call(s, "export_candidates_xlsx", candidate_ids=[a]))
    assert t.startswith("Cautious mode is on, so I didn't save a file. Make it on the Share page: http")
    _set(store, review.SETTING, "0")
    t = text(call(s, "export_candidates_xlsx", candidate_ids=[a, b, 999]))
    assert t.splitlines()[0] == "Not found and left out: 999" and "Made " in t and "your computer" in t
    assert data(call(s, "export_candidates_xlsx", candidate_ids=[a, 999]))["not_found"] == [999]


def test_read_only_refusal_for_the_export_carries_the_share_link(store, downloads, monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_name", lambda mcp: "Acme Chat")
    a = store.add_candidate(name="Ana Ruiz")["id"]
    s = build_server(store)
    call(s, "search_candidates", query="x")
    trust.set_trust(store, trust.apps(store)[0]["id"], "read_only")
    with pytest.raises(ToolError) as e:
        call(s, "export_candidates_xlsx", candidate_ids=[a], include_notes=True)
    msg = str(e.value)
    assert "read only" in msg and f"/candidates/share?ids={a}" in msg and "notes=1" in msg
    with pytest.raises(ToolError) as e2:  # every other write keeps the plain refusal
        call(s, "save_candidate", name="Zed")
    assert "Share page" not in str(e2.value)
