"""Cycle 11 fixer X: server text parity, search ranking, Connect page, Candidates table, CSV mapping, feedback text,
and the keyboard and screen reader fixes on Today and the tour."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, importers, loops, reports
from talent.cli import main
from talent.mcp_server import build_server
from talent.store import set_shown_zone

from conftest import local_client

REPO = Path(__file__).resolve().parents[1]
STATIC = REPO / "src" / "talent" / "static"
FIX = Path(__file__).parent / "fixtures"
INHOUSE = list(actions.STAGE_SETS["inhouse"][1])


@pytest.fixture(autouse=True)
def _env(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(h))
    dl = tmp_path / "Downloads"
    dl.mkdir()
    monkeypatch.setenv("TALENT_DOWNLOADS_DIR", str(dl))
    yield h
    set_shown_zone(None)


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res) -> str:
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def _ago(hours: float) -> str:
    return (datetime.now(timezone.utc) - timedelta(hours=hours)).replace(microsecond=0).isoformat()


def _job(store, title="Senior Data Engineer", company="Lumen Health"):
    return store.add_job(title, company=company, stages=INHOUSE)["id"]


def _person(store, jid, name, stage="Interviews"):
    cid = store.add_candidate(name=name)["id"]
    store.add_to_pipeline(cid, jid, stage)
    return cid


# ------------------------------------------------------------------ 1. candidate_cards hidden without MCP Apps
def test_candidate_cards_is_hidden_from_clients_without_apps(store, monkeypatch):
    from talent import mcp_server
    names = {t.name for t in anyio.run(build_server(store).list_tools)}
    assert "candidate_cards" not in names and "keep_prospect" not in names
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)
    names = {t.name for t in anyio.run(build_server(store).list_tools)}
    assert "candidate_cards" in names and "keep_prospect" in names
    assert "candidate_cards" in mcp_server.APP_ONLY_READ and "candidate_cards" not in mcp_server.CARD_TOOLS


# ------------------------------------------------------------------ 2. text parity
def test_interviews_feedback_and_pipeline_text_carry_links(store):
    jid = _job(store)
    a = _person(store, jid, "Dana Cho")
    iv = store.log(a, "interview", "Panel (30 min)", at=_ago(-20), job_id=jid, interviewer="Priya Shah")
    store.log(a, "interview", "Pairing", at=_ago(80), job_id=jid, interviewer="Priya Shah")
    srv = build_server(store)
    week = (datetime.now(timezone.utc) + timedelta(hours=20)).date().isoformat()
    out = text(call(srv, "list_interviews", week=week))
    assert f"/candidates/{a}" in out and f"/jobs/{jid}" in out and str(iv["id"]) in out
    owed = text(call(srv, "list_feedback_owed"))
    assert f"/candidates/{a}" in owed and f"/jobs/{jid}" in owed
    pipe = text(call(srv, "job_pipeline", job_id=jid))
    assert f"/jobs/{jid}" in pipe


def test_search_lines_show_contact_details_and_the_cap_hint(store):
    store.add_candidate(name="Ana Link", email="ana@x.com", linkedin_url="https://linkedin.com/in/ana-link",
                        website="https://ana.dev", current_title="Welder", current_company="Acme")
    for i in range(12):
        store.add_candidate(name=f"Welder {i:02d}", current_title="Welder", current_company="Acme")
    out = text(call(build_server(store), "search_candidates", query="ana link"))
    assert "ana@x.com" in out and "linkedin.com/in/ana-link" in out and "ana.dev" in out
    out = text(call(build_server(store), "search_candidates", query="welder", limit=10))
    assert re.search(r"Showing 10 of 13\. Ask for the next 3", out), out


def test_a_snippet_is_not_followed_by_two_periods(store):
    store.add_candidate(name="Ana Link", about="I weld things. Ends with a period.", current_title="Welder")
    out = text(call(build_server(store), "search_candidates", query="welder"))
    assert ".." not in out.replace("...", "")


# ------------------------------------------------------------------ 3. a missing id says what to do
def test_missing_job_id_says_how_to_find_it(store):
    with pytest.raises(Exception) as e:
        call(build_server(store), "job_pipeline")
    assert "Tell me which job (job_id). list_jobs shows them." in str(e.value)


# ------------------------------------------------------------------ 4. role-and-place ranking
def _people(store):
    store.add_candidate(name="Ben Boston", current_title="Backend Engineer", current_company="Acme", location="Boston, MA")
    store.add_candidate(name="Sara Soft", current_title="Software Engineer", current_company="Acme", location="Boston, MA")
    store.add_candidate(name="Cal Cambridge", current_title="Senior Backend Engineer", current_company="X",
                        location="Cambridge, MA")
    store.add_candidate(name="Aus Tin", current_title="Backend Engineer", current_company="Y", location="Austin, TX")
    store.add_candidate(name="Dee Denver", current_title="Designer", current_company="Y", location="Denver, CO")


def _names(res):
    return [h.candidate["name"] for h in res.hits]


def test_singular_and_plural_rank_the_same(store):
    _people(store)
    one = _names(store.search_candidates("backend engineer in Boston"))
    many = _names(store.search_candidates("backend engineers in Boston"))
    assert one == many and one[0] == "Ben Boston"


def test_both_first_then_role_then_place_and_close_is_capped(store):
    _people(store)
    for i in range(25):
        store.add_candidate(name=f"Rolly {i:02d}", current_title="Backend Engineer", location="Austin, TX")
    res = store.search_candidates("backend engineer Boston", limit=100)
    names = _names(res)
    assert names[0] == "Ben Boston"
    assert res.total <= 1 + 10 + 5 or len(names) <= 16  # exact first, then a short list of the close
    assert "Dee Denver" not in names


def test_nobody_matches_both_is_said_plainly(store):
    _people(store)
    res = store.search_candidates("backend engineer in Denver")
    assert "Nobody is a backend engineer in Denver" in (res.summary or "")
    assert "Closest:" in (res.summary or "")
    out = text(call(build_server(store), "search_candidates", query="backend engineer in Denver"))
    assert re.search(r"Nobody is a backend engineer in Denver[^.]*\. Closest:", out)
    d = data(call(build_server(store), "search_candidates", query="backend engineer in Denver"))
    assert "Nobody is a backend engineer in Denver" in (d.get("message") or "")


def test_counts_say_match_the_role_and_the_place_and_part_of_it(store):
    _people(store)
    out = text(call(build_server(store), "search_candidates", query="backend engineer in Boston"))
    assert re.search(r"\d+ match(?:es)? the role and the place", out), out
    assert "close" in out


# ------------------------------------------------------------------ 5. counts
def test_in_the_pipeline_wording_and_jobs_without_a_client(store):
    jid = store.add_job("Welder", stages=INHOUSE)["id"]
    store.add_job("Painter", company="Acme", stages=INHOUSE)
    cid = store.add_candidate(name="Ana Link")["id"]
    store.add_to_pipeline(cid, jid, "Interviews")
    srv = build_server(store)
    jobs = text(call(srv, "list_jobs"))
    assert "in the pipeline" in jobs and "in play" not in jobs
    assert "No company:" not in jobs
    c = local_client(store)
    page = c.get("/jobs").text
    assert "Jobs without a client" in page and "No company" not in page
    digest = actions.status_digest(store)
    assert "Jobs without a client: 1 open job, 1 in process" in digest and "No company:" not in digest


# ------------------------------------------------------------------ 6. Connect page
def test_connect_plugin_steps_show_once_and_connect_comes_before_skills(store):
    page = local_client(store).get("/connect").text
    assert page.count('id="plugin-steps"') == 1 and page.count('id="plugin-check"') == 1
    assert page.index('id="claude-panel"') < page.index('id="skills-panel"') < page.index('id="try-asking"')
    assert "/plugin install talent-search@talent-tracker" in page
    assert "Check it worked" in page
    assert "Which one should I pick?" not in page and 'id="skills-other-ways"' in page
    assert "still needs the connection from Step 1" in page
    assert "works in every app" not in page and "every AI app" not in page


def test_connect_claude_code_steps_are_in_one_fold(store):
    page = local_client(store).get("/connect").text
    block = page.split('<details id="claude-code-steps">', 1)[1].split("</details>", 1)[0]
    assert "Step 1" in block or "1." in block
    assert "Step 2" in block or "2." in block


def test_saving_the_plugin_does_not_repeat_the_steps(store, monkeypatch):
    from talent import skills
    monkeypatch.setattr(skills, "reveal", lambda path, platform=None: True)
    r = local_client(store).post("/connect/skills/plugin/save")
    assert r.status_code == 200 and "talent-search.plugin" in r.text
    assert 'id="plugin-steps"' not in r.text and "<ol" not in r.text


def test_install_refreshes_the_found_line(store, _env):
    (_env / ".claude").mkdir()
    c = local_client(store)
    assert "No skills yet" in c.get("/connect").text
    r = c.post("/connect/skills/all/install", data={"targets": "claude-code"})
    assert 'hx-swap-oob' in r.text and "No skills yet" not in r.text


def test_marketplace_json_has_a_description():
    m = json.loads((REPO / ".claude-plugin" / "marketplace.json").read_text())
    assert m["metadata"]["description"].startswith("Skills that teach")


# ------------------------------------------------------------------ 7. install-skills with nobody to ask
def test_install_skills_without_yes_says_nothing_was_installed_first(_env, capsys):
    (_env / ".claude").mkdir()
    with pytest.raises(SystemExit):
        main(["install-skills"])
    out = capsys.readouterr().out
    assert out.splitlines()[0] == "Nothing installed yet. Run again with --yes to install."
    assert not (_env / ".claude" / "skills").exists()


# ------------------------------------------------------------------ 8. Candidates table
def _table_world(store):
    j1 = store.add_job("Welder", company="Acme", stages=INHOUSE)["id"]
    j2 = store.add_job("Painter", company="Zed", stages=INHOUSE)["id"]
    ids = []
    for i, (n, j, s) in enumerate([("Ann", j1, "Interviews"), ("Bob", j2, "Applied"), ("Cy", None, None),
                                   ("Di", j1, "Applied")]):
        c = store.add_candidate(name=n)["id"]
        if j:
            store.add_to_pipeline(c, j, s)
        ids.append(c)
    return ids


def test_candidates_table_has_job_and_stage_columns_that_sort(store):
    ids = _table_world(store)
    c = local_client(store)
    html = c.get("/candidates").text
    assert ">Job" in html and ">Stage" in html
    by_job = c.get("/candidates?sort=job&dir=asc").text
    pos = [by_job.index(f"/candidates/{i}\"") for i in (ids[1], ids[0], ids[2])]
    assert pos == sorted(pos)  # Painter, then Welder, and a person with no job sorts last
    by_stage = c.get("/candidates?sort=stage&dir=asc").text
    assert by_stage.index(f"/candidates/{ids[3]}\"") < by_stage.index(f"/candidates/{ids[2]}\"") and "aria-sort" in by_stage


def test_ids_banner_and_show_everyone(store):
    ids = _table_world(store)
    c = local_client(store)
    html = c.get(f"/candidates?ids={ids[0]},{ids[1]}").text
    assert "2 people from your chat." in html and "Show everyone" in html
    assert f"/candidates/{ids[0]}\"" in html and f"/candidates/{ids[2]}\"" not in html
    assert "from your chat" not in c.get("/candidates").text


def test_shift_click_range_select_is_in_the_script():
    js = (STATIC / "app.js").read_text()
    assert "e.shiftKey" in js and "candidate_ids" in js


# ------------------------------------------------------------------ 9, 10. CSV mapping
def _stage_of(store, cid):
    return store.candidate_pipelines(cid)[0]["stage"]


def _import(store, name):
    header, rows = importers.read_csv((FIX / name).read_text())
    mapping = importers.guess_mapping(header, rows)
    return header, rows, mapping


def test_years_column_is_named_as_a_note_in_the_preview(store):
    store.add_job("Data Analyst", company="Globex")
    header, rows, mapping = _import(store, "c11_x_ats3.csv")
    lines = importers.preview_import(store, header, rows, mapping)["column_lines"]
    assert any("Years of experience (kept as a note)" in ln for ln in lines)


def test_stage_and_status_both_present_status_is_a_tag(store):
    store.add_job("Data Analyst", company="Globex")
    header, rows, mapping = _import(store, "c11_x_ats2.csv")
    assert mapping[header.index("Stage")] == "stage" and mapping[header.index("Status")] == "tag_column"
    prev = importers.preview_import(store, header, rows, mapping)
    assert any("Status was kept as a tag" in ln and "Stage column" in ln for ln in prev["column_lines"])
    importers.import_rows(store, header, rows, mapping, backup=False)
    marc = store.search_candidates("marc").hits[0].candidate
    assert "Status: Passive" in marc["tags"]
    assert _stage_of(store, marc["id"]) == "Interviewing"  # the Stage column set the stage, not Status


def test_near_stage_names_map_to_existing_stages(store):
    jid = store.add_job("Data Analyst", company="Globex")["id"]
    stages = [s["name"] if isinstance(s, dict) else s for s in store.job_stages(jid)]
    header, rows, mapping = _import(store, "c11_x_ats3.csv")
    res = importers.import_rows(store, header, rows, mapping, backup=False)
    moves = res["stage_moves"]
    assert any("Interview to Interviewing" in k for k in moves), moves
    assert any("Screened to Screening" in k for k in moves), moves
    assert "Interviewing" in stages and not res["stages_added"]


# ------------------------------------------------------------------ 11. work history round trip
def test_export_then_import_keeps_work_history(store, tmp_path):
    store.add_candidate(name="Ana Link", email="ana@x.com", work_history=[
        {"company": "Acme", "title": "Welder", "start": "2019-01", "end": "2022-06"},
        {"company": "Zed", "title": "Lead Welder", "start": "2022-07"}])
    csv_text = importers.export_csv(store)
    assert "Work history" in csv_text
    from talent.store import Store
    other = Store(tmp_path / "other.db")
    try:
        header, rows = importers.read_csv(csv_text)
        mapping = importers.guess_mapping(header, rows)
        assert mapping[header.index("Work history")] == "work_history"
        importers.import_rows(other, header, rows, mapping, backup=False)
        ana = other.search_candidates("ana").hits[0].candidate
        hist = other.get_candidate(ana["id"]).get("work_history") or other.work_history(ana["id"])
        assert [h["company"] for h in hist] == ["Acme", "Zed"]
    finally:
        other.close()


# ------------------------------------------------------------------ 12. linkedin and website aliases
def test_save_candidate_accepts_linkedin_and_website_aliases(store):
    srv = build_server(store)
    out = text(call(srv, "save_candidate", name="Ana Link", linkedin="https://linkedin.com/in/ana-link",
                    site="https://ana.dev"))
    assert "Saved" in out
    c = store.search_candidates("Ana Link").hits[0].candidate
    assert "ana-link" in (c.get("linkedin_url") or "") and c.get("website") == "https://ana.dev"
    out = text(call(srv, "save_candidates", people=[
        {"name": "Bo Web", "email": "bo@x.com", "url": "https://bo.dev"},
        {"name": "Cy Web", "email": "cy@x.com", "linkedin": "https://linkedin.com/in/cy-web"}]))
    assert "bo@x.com" in out and "bo.dev" in out and "cy@x.com" in out


# ------------------------------------------------------------------ 13. interview feedback text
def test_verdict_error_lists_every_verdict(store):
    jid = _job(store)
    a = _person(store, jid, "Dana Cho")
    iv = store.log(a, "interview", "Panel", at=_ago(30), job_id=jid, interviewer="Priya Shah")
    with pytest.raises(Exception) as e:
        call(build_server(store), "add_interview_feedback", interview_id=iv["id"], feedback="x", verdict="great")
    msg = str(e.value)
    for v in ("strong yes", "yes", "lean yes", "mixed", "lean no", "no", "strong no"):
        assert v in msg, v


def test_one_clash_line_per_interviewer(store):
    jid = _job(store)
    a = _person(store, jid, "Dana Cho")
    b = _person(store, jid, "Omar Ali")
    store.log(a, "interview", "Panel (30 min)", at=_ago(-20), job_id=jid, interviewer="Priya Shah")
    store.log(a, "interview", "Culture chat (30 min)", at=_ago(-20.1), job_id=jid, interviewer="Priya Shah")
    store.log(b, "interview", "Pairing (30 min)", at=_ago(-20.05), job_id=jid, interviewer="Priya Shah")
    week = (datetime.now(timezone.utc) + timedelta(hours=20)).date().isoformat()
    out = text(call(build_server(store), "list_interviews", week=week))
    assert len([ln for ln in out.splitlines() if ln.startswith("Clash:") and "Priya Shah" in ln]) == 1, out


def test_plan_loop_says_when_it_skips_an_interviewer_for_working_hours(store):
    srv = build_server(store)
    job = store.add_job(title="Staff Engineer", company="Acme")
    cid = store.add_candidate(name="Ana Ray")["id"]
    call(srv, "set_loop_template", job_id=job["id"],
         sessions=[{"label": "Screen", "minutes": 30, "interviewers": ["Priya Nair", "Sam Oko"]}],
         hours="9-17")
    loops.save_person_hours(store, "Priya Nair", "1-2")
    out = text(call(srv, "plan_loop", job_id=job["id"], candidate_id=cid, start="2026-11-10"))
    assert "outside their working hours" in out and "Priya Nair" in out


# ------------------------------------------------------------------ 14, 18. the tour
def test_skip_tour_ends_in_one_click_and_never_covers_a_field():
    js = (STATIC / "app.js").read_text()
    assert 'else postForm("/tour/skip", {}); }' in js and "function skip()" in js
    assert "clearInert(); root.remove();" in js and "ts-tour-ended" in js
    assert "never start over a box that has focus" in js and "focusin" in js and "focusout" in js
    assert 'main h1' in js  # after the tour ends, focus goes to the page's h1


# ------------------------------------------------------------------ 15, 17. time zone banner and Today
BANNER = "Times are in UTC. Set your time zone in Settings."


def test_zone_banner_shows_while_unset_on_interviews_and_the_loop_page(store):
    jid = store.add_job("Welder", stages=INHOUSE)["id"]
    c = local_client(store)
    for path in ("/interviews", "/interviews?view=month", f"/jobs/{jid}/loop"):
        r = c.get(path)
        if r.status_code == 200:
            assert BANNER in re.sub(r"<[^>]+>", "", r.text), path
    reports.save_user_zone(store, "America/Los_Angeles")
    assert BANNER not in re.sub(r"<[^>]+>", "", c.get("/interviews").text)


def test_today_skip_link_is_first_and_the_banner_is_not_auto_focused(store):
    store.add_candidate(name="Ana Link")
    c = local_client(store)
    html = c.get("/").text
    first = re.search(r'<a [^>]*href="#[^"]*"[^>]*>[^<]*</a>', html[html.index("<body"):]).group(0)
    assert "Skip to today" in first
    js = (STATIC / "app.js").read_text()
    assert ".flash:not(.tz-banner)" in js


# ------------------------------------------------------------------ 16. Today buttons name their person
def test_today_verdict_buttons_are_a_labelled_group_naming_the_person(store):
    jid = _job(store)
    a = _person(store, jid, "Maya Example")
    store.log(a, "interview", "Panel", at=_ago(3), job_id=jid, interviewer="Priya Shah")
    html = local_client(store).get("/").text
    assert 'role="group" aria-label="How did Maya Example' in html
    assert re.search(r'aria-label="[^"]*Maya Example[^"]*"', html.split('role="group" aria-label="How did', 1)[1])
