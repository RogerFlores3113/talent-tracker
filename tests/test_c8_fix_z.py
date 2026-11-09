"""Fixer Z: the first-run tour, the skills install and accessibility polish."""
from __future__ import annotations

import html
import io
import json
import re
import zipfile
from pathlib import Path

import anyio
import pytest

from conftest import flash_of, local_client
from talent import skills, tour, trust
from talent.mcp_server import build_server
from talent.sample_data import load_sample_data, sample_ids

REPO = Path(__file__).resolve().parents[1]


@pytest.fixture
def home(tmp_path, monkeypatch):
    h = tmp_path / "home"
    h.mkdir()
    monkeypatch.setenv("TALENT_SKILLS_HOME", str(h))
    return h


def tour_data(response):
    m = re.search(r'<div id="tour-data" hidden data-tour="([^"]*)"></div>', response.text)
    return json.loads(html.unescape(m.group(1))) if m else None


def step_of(data, sid):
    return next(s for s in data["steps"] if s["id"] == sid)


def start_and_load(c):
    c.post("/tour/start", data={"mode": "full"})
    c.post("/tour/load", follow_redirects=False)


def maya(store):
    return next(c["id"] for c in store.all_candidates() if c["name"] == "Maya Example")


# ------------------------------------------------------------------------------------------------ skills (p5)
def test_marker_records_a_fingerprint_for_each_file(home):
    skills.install("claude-code")
    d = home / ".claude" / "skills" / "talent-search-write-outreach"
    marker = json.loads((d / skills.MARKER).read_text(encoding="utf-8"))
    assert marker["files"].keys() == {"SKILL.md"} and len(marker["files"]["SKILL.md"]) == 64


def test_reinstall_keeps_a_file_the_user_edited_and_says_so(home):
    skills.install("claude-code")
    f = home / ".claude" / "skills" / "talent-search-write-outreach" / "SKILL.md"
    f.write_text("my own version", encoding="utf-8")
    plan = skills.plan_install("claude-code")
    assert plan["edited"] == ["~/.claude/skills/talent-search-write-outreach/SKILL.md"]
    assert "~/.claude/skills/talent-search-write-outreach/SKILL.md" not in plan["files"]
    res = skills.install("claude-code")
    assert f.read_text(encoding="utf-8") == "my own version"
    assert res["edited"] and "~/.claude/skills/talent-search-write-outreach/SKILL.md" not in res["written"]
    skills.install("claude-code")  # and again: still theirs
    assert f.read_text(encoding="utf-8") == "my own version"
    assert skills.status("claude-code")["edited"] == 1


def test_a_file_the_user_deleted_is_put_back(home):
    skills.install("codex")
    f = home / ".agents" / "skills" / "talent-search-write-outreach" / "SKILL.md"
    f.unlink()
    assert skills.plan_install("codex")["edited"] == []
    skills.install("codex")
    assert f.read_text(encoding="utf-8").startswith("---")


def test_remove_keeps_an_edited_skill_and_leaves_no_silent_orphan(home):
    skills.install("claude-code")
    d = home / ".claude" / "skills" / "talent-search-write-outreach"
    (d / "SKILL.md").write_text("mine", encoding="utf-8")
    plan = skills.plan_remove("claude-code")
    assert plan["edited"] == ["~/.claude/skills/talent-search-write-outreach/SKILL.md"]
    assert plan["left"][0]["folder"] == "~/.claude/skills/talent-search-write-outreach"
    assert len(plan["folders"]) == len(skills.skill_names()) - 1
    res = skills.remove("claude-code")
    assert (d / "SKILL.md").read_text(encoding="utf-8") == "mine"
    assert res["kept"] == ["~/.claude/skills/talent-search-write-outreach"] and res["edited"]
    assert len(res["removed"]) == len(skills.skill_names()) - 1


def test_removing_a_clean_install_deletes_everything_we_wrote(home):
    skills.install("claude-code")
    res = skills.remove("claude-code")
    assert not res["kept"] and not any((home / ".claude" / "skills").iterdir())


def test_a_symlinked_skill_folder_is_skipped(home, tmp_path):
    base = home / ".claude" / "skills"
    base.mkdir(parents=True)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (base / "talent-search-write-outreach").symlink_to(elsewhere, target_is_directory=True)
    res = skills.install("claude-code")
    assert [s["folder"] for s in res["skipped"]] == ["talent-search-write-outreach"]
    assert list(elsewhere.iterdir()) == []
    assert skills.status("claude-code")["skipped"] == 1


def test_panel_leads_with_what_skills_are_and_names_the_real_folder(store, home):
    page = local_client(store).get("/connect").text
    assert "A skill is a short page of written instructions" in page
    assert "talent install-skills" in page and html.escape(str(skills.source_dir())) in page
    assert "skills/INSTALL-SKILLS.md" not in page and "marker file" not in page
    assert "Download all skills as one .zip" in page and "/connect/skills/talent-search-write-outreach.zip" in page


def test_panel_says_how_many_were_left_alone(store, home):
    mine = home / ".claude" / "skills" / "talent-search-triage-candidates"
    mine.mkdir(parents=True)
    (mine / "SKILL.md").write_text("mine", encoding="utf-8")
    c = local_client(store)
    r = c.post("/connect/skills/claude-code/install")
    assert "1 left alone because you have your own" in r.text
    assert "1 left alone because you have your own" in c.get("/connect").text


def test_preview_lists_edited_files_and_moves_focus_to_the_add_button(store, home):
    c = local_client(store)
    c.post("/connect/skills/claude-code/install")
    (home / ".claude" / "skills" / "talent-search-write-outreach" / "SKILL.md").write_text("mine", encoding="utf-8")
    r = c.post("/connect/skills/claude-code/preview-install")
    assert "edited by you, kept" in r.text and "small label file" in r.text and "marker" not in r.text
    assert re.search(r'<button class="btn primary" autofocus', r.text)
    r = c.post("/connect/skills/claude-code/preview-remove")
    assert "edited by you, kept" in r.text


def test_per_skill_zips(store):
    c = local_client(store)
    r = c.get("/connect/skills/talent-search-write-outreach.zip")
    assert r.status_code == 200
    z = zipfile.ZipFile(io.BytesIO(r.content))
    assert z.namelist() == ["talent-search-write-outreach/SKILL.md"]
    assert c.get("/connect/skills/not-a-skill.zip").status_code == 404
    assert zipfile.is_zipfile(io.BytesIO(c.get("/connect/skills.zip").content))


def test_install_doc_has_no_hardcoded_version_and_covers_symlinks_and_no_one_to_ask():
    t = (REPO / "skills" / "INSTALL-SKILLS.md").read_text(encoding="utf-8")
    assert "0.1.0" not in t and "symlink" in t and "without anyone to answer" in t and "talent install-skills" in t
    assert "from this repo" not in t


def test_cli_remove_skills_asks_and_filters(home, capsys, monkeypatch):
    from talent.cli import main
    main(["install-skills", "--claude-code", "--yes"])
    out = capsys.readouterr().out
    assert "skills in" in out and "SKILL.md" in out  # the list comes first
    main(["install-skills", "--codex", "--yes"])
    monkeypatch.setattr("sys.stdin", io.StringIO(""))  # not a terminal: it must stop, not remove
    with pytest.raises(SystemExit):
        main(["remove-skills"])
    assert any((home / ".claude" / "skills").iterdir())
    main(["remove-skills", "--codex", "--yes"])
    assert not any((home / ".agents" / "skills").iterdir()) and any((home / ".claude" / "skills").iterdir())
    main(["remove-skills", "--yes"])
    assert not any((home / ".claude" / "skills").iterdir())


def test_cli_remove_skills_answering_no_removes_nothing(home, monkeypatch):
    from talent.cli import main
    main(["install-skills", "--claude-code", "--yes"])
    monkeypatch.setattr("sys.stdin", type("T", (io.StringIO,), {"isatty": lambda self: True})(""))
    monkeypatch.setattr("builtins.input", lambda prompt="": "no")
    main(["remove-skills", "--claude-code"])
    assert any((home / ".claude" / "skills").iterdir())


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def test_a_second_color_for_one_person_replaces_the_older_pending_hold(store):
    from talent import review
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    other = store.add_candidate(name="Bo Lin")["id"]
    s = build_server(store)
    row = trust.touch(store, None)
    trust.set_trust(store, row["id"], "suggest")
    call(s, "set_candidate_color", candidate_id=cid, color="red")
    call(s, "set_candidate_color", candidate_id=other, color="blue")
    call(s, "set_candidate_color", candidate_id=cid, color="green")
    waiting = [(r["candidate_id"], r["payload"]["args"]["color"]) for r in review.pending(store)]
    assert sorted(waiting) == sorted([(cid, "green"), (other, "blue")])


# ------------------------------------------------------------------------------------------------ tour (p1)
def test_tour_shows_colors_saved_searches_and_skills(store):
    c = local_client(store)
    data = tour_data(c.get("/"))
    ids = [s["id"] for s in data["steps"]]
    assert ids.index("claude") < ids.index("skills") < ids.index("load") < ids.index("colors") < ids.index("saved")
    assert len(ids) == 13 and ids[4] == "load"
    colors = step_of(data, "colors")
    assert colors["target"] == [".colorbar"]
    assert step_of(data, "skills")["target"] == ["#skills-panel"] and step_of(data, "saved")["path"] == "/candidates"
    page = c.get("/candidates").text
    assert 'class="colorbar"' in page and 'class="savedbar"' in page and 'id="skills-panel"' in c.get("/connect").text


def test_connect_steps_say_you_can_connect_later_and_fall_back_when_the_button_is_disabled(store):
    data = tour_data(local_client(store).get("/"))
    for sid in ("claude", "codex", "skills"):
        s = step_of(data, sid)
        assert "Don't have it? Press Next, you can" in s["body"] or "Don't have one? Press Next, you can" in s["body"]
        assert ":not([disabled])" in s["pulse"]
    js = (REPO / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")
    assert "pulsed.disabled" in js


def test_the_colors_step_does_not_promise_bubbles_when_nobody_is_saved(store):
    c = local_client(store)
    assert "Add a person and try it" in step_of(tour_data(c.get("/")), "colors")["body"]
    start_and_load(c)
    colors = step_of(tour_data(c.get("/candidates")), "colors")
    assert "Try it now" in colors["body"] and colors["reach"]


def test_person_and_pipeline_steps_are_skipped_after_not_now(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/step", data={"step": "4"})  # on the load question
    assert "person" in [s["id"] for s in tour_data(c.get("/"))["steps"]]
    c.post("/tour/step", data={"step": "5"})  # "Not now": the next step, with nothing loaded
    ids = [s["id"] for s in tour_data(c.get("/candidates"))["steps"]]
    assert "person" not in ids and "pipeline" not in ids and "search" in ids and ids[-1] == "finish"
    assert tour.state(store)["step"] == 5


def test_welcome_and_today_explain_sample_data_loaded_by_hand(store):
    c = local_client(store)
    assert "never clears" not in step_of(tour_data(c.get("/")), "welcome")["body"]  # short and plain now (c10 w4)
    c.post("/tour/skip", follow_redirects=False)
    assert "never clears" not in c.get("/").text


def test_finish_names_the_demo_person_they_worked_with_and_offers_to_delete_them(store):
    c = local_client(store)
    start_and_load(c)
    store.log(maya(store), "call", "Real call", source="App")
    data = tour_data(c.get("/"))
    assert data["extras"]["people"] == [{"id": maya(store), "name": "Maya Example"}]
    finish = data["steps"][-1]
    assert "Maya Example" in finish["body"] and "only gave a color to" in finish["body"]


def test_finish_can_delete_the_person_too_and_says_so(store):
    c = local_client(store)
    start_and_load(c)
    mid = maya(store)
    store.log(mid, "call", "Real call", source="App")
    r = c.post("/tour/finish", data={"delete_people": str(mid)}, follow_redirects=False)
    msg = flash_of(r)
    assert "Also deleted Maya Example" in msg and "sample person you worked with" not in msg
    assert store.count_candidates() == 0


def test_finish_without_the_box_keeps_them(store):
    c = local_client(store)
    start_and_load(c)
    store.log(maya(store), "call", "Real call", source="App")
    msg = flash_of(c.post("/tour/finish", follow_redirects=False))
    assert "Kept 1 sample person you worked with (Maya Example)" in msg
    assert [x["name"] for x in store.all_candidates()] == ["Maya Example"]


def test_a_recolored_only_demo_person_is_removed_and_the_tour_says_so(store):
    c = local_client(store)
    start_and_load(c)
    store.set_color(maya(store), "red")
    assert tour_data(c.get("/"))["extras"]["people"] == []
    c.post("/tour/finish", follow_redirects=False)
    assert store.count_candidates() == 0


def test_only_offered_ids_can_be_deleted_by_the_tour(store):
    mine = store.add_candidate(name="Jordan Pierce")
    c = local_client(store)
    start_and_load(c)
    c.post("/tour/finish", data={"delete_people": str(mine["id"])}, follow_redirects=False)
    assert store.get_candidate(mine["id"])["name"] == "Jordan Pierce"


def test_saved_searches_made_during_the_demo_are_offered_and_removed(store):
    c = local_client(store)
    store.save_search("Before the tour", {"q": "zzz-nobody"}, by="app")  # before the demo: not offered
    c.post("/tour/start", data={"mode": "full"})
    c.post("/tour/load", follow_redirects=False)
    store.set_settings(tour_loaded_at="2999-01-01T00:00:00+00:00")  # the demo "loaded" after the first search
    store.set_settings(tour_loaded_at="1970-01-01T00:00:00+00:00")
    store._conn.execute("UPDATE saved_searches SET created_at = '1969-01-01T00:00:00+00:00' WHERE name = 'Before the tour'")
    store._conn.commit()
    demo = store.save_search("Rust folks", {"q": "rust"}, by="app")
    store.save_search("From my AI", {"q": "go"}, by="ai")
    extras = tour_data(c.get("/"))["extras"]
    assert extras["searches"] == [{"id": demo["id"], "name": "Rust folks"}]
    msg = flash_of(c.post("/tour/finish", data={"delete_searches": str(demo["id"])}, follow_redirects=False))
    assert "Also removed the saved search Rust folks" in msg  # (c10 w2: it now goes even without the tick)
    names = [x["name"] for x in store.list_saved_searches()]
    assert "Rust folks" not in names and "From my AI" in names


def test_a_search_left_behind_that_now_matches_nobody_is_mentioned(store):
    c = local_client(store)
    start_and_load(c)
    store.save_search("Rust folks", {"q": "rust"}, by="app")
    msg = flash_of(c.post("/tour/finish", follow_redirects=False))
    assert "Rust folks" not in [x["name"] for x in store.list_saved_searches()]  # it only found demo people: removed (c10 w2)
    load_sample_data(store)
    store.save_search("Go folks", {"q": "golang"}, by="app")
    msg = flash_of(c.post("/sample-data/remove", follow_redirects=False))
    assert "Go folks" in msg


def test_removal_notice_travels_in_the_cookie_not_the_address(store):
    c = local_client(store)
    start_and_load(c)
    r = c.post("/tour/finish", follow_redirects=False)
    assert r.headers["location"] == "/" and "notice" not in r.headers["location"]


def test_full_tour_can_be_replayed_from_the_footer_and_settings(store):
    store.add_candidate(name="Jordan Pierce")
    c = local_client(store)
    assert 'href="/?tour=full"' in c.get("/connect").text
    assert 'value="full"' in c.get("/settings").text
    data = tour_data(c.get("/?tour=full"))
    assert data["mode"] == "full" and data["steps"][0]["id"] == "welcome"


def test_tour_does_not_start_from_a_junk_tour_value(store):
    store.add_candidate(name="Jordan Pierce")
    assert tour_data(local_client(store).get("/?tour=bogus")) is None


def test_pipeline_step_leads_with_the_menu(store):
    body = step_of(tour_data(local_client(store).get("/")), "pipeline")["body"]
    assert body.index("menu") < body.index("drag")


def test_tour_script_makes_the_page_inert_confirms_esc_and_uses_a_neutral_note():
    js = (REPO / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")
    css = (REPO / "src" / "talent" / "static" / "style.css").read_text(encoding="utf-8")
    assert "inert" in js and "applyInert" in js
    assert 'e.key === "Escape"' in js and "else skip()" in js and "function skip() { confirming = true" in js
    assert re.search(r"\.tour-note \{[^}]*color: var\(--muted\)", css)


def test_colored_first_box_is_always_on_candidates(store):
    store.add_candidate(name="Ana Ruiz")
    assert 'name="cf"' in local_client(store).get("/candidates").text


# ------------------------------------------------------------------------------------------------ accessibility (p6)
def test_color_bubble_label_starts_with_the_name_and_mentions_shift_enter(store):
    cid = store.add_candidate(name="Ana Ruiz")["id"]
    c = local_client(store)
    page = c.get("/candidates").text
    assert 'aria-label="Ana Ruiz: color none. Click for red.' in page and 'data-who="Ana Ruiz"' in page
    body = c.post(f"/candidates/{cid}/color", data={"color": "red"}, headers={"Accept": "application/json"}).json()
    assert body["aria"].startswith("Ana Ruiz: color red.") and "Shift+Enter" in body["title"]
    js = (REPO / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")
    assert 'who + "color is now "' in js
    assert re.search(r"\.cbub \{[^}]*width: 24px; height: 24px", (REPO / "src/talent/static/style.css").read_text(encoding="utf-8"))


def test_bubble_name_is_escaped(store):
    store.add_candidate(name='"><b>x</b>')
    page = local_client(store).get("/candidates").text
    assert "<b>x</b>" not in page


def test_filter_chips_saved_chips_and_manage_buttons_carry_names(store):
    for i in range(3):
        store.add_candidate(name=f"Remote Dev {i}", location="Remote", tags=["go"])
    sv = store.save_search("Go people", {"q": "go"}, by="app")
    c = local_client(store)
    page = c.get("/candidates?location=Remote").text
    assert 'aria-label="Remove filter: Location: Remote"' in page
    assert 'aria-label="Go people: 3 people' in page
    assert f'aria-label="New name for saved search Go people"' in page
    assert 'aria-label="Rename saved search Go people"' in page and 'aria-label="Delete saved search Go people"' in page
    assert sv["id"]


def test_more_filters_labels_are_plain(store):
    page = local_client(store).get("/candidates").text
    assert "Number of days" in page and "Match skills" in page and "Days (for the choice beside)" not in page


def test_duplicates_page_buttons_carry_the_pair_and_keep_linkedin_capitalized(store):
    store.add_candidate(name="Kim Lo", current_company="Acme", email="a@x.com")
    store.add_candidate(name="Kim Lo", current_company="Acme", email="b@x.com")
    page = local_client(store).get("/candidates/duplicates").text
    assert 'aria-label="Compare and merge Kim Lo (a@x.com) and Kim Lo (b@x.com)"' in page  # same name: told apart by email
    assert 'aria-label="Not the same person: Kim Lo (a@x.com) and Kim Lo (b@x.com)"' in page
    assert "Pairs found" in page


def test_duplicates_reasons_keep_linkedin_capitalized(store):
    store.add_candidate(name="Kim Lo", linkedin_url="https://linkedin.com/in/kim-lo")
    store.add_candidate(name="Someone Else", linkedin_url="https://linkedin.com/in/other")
    with store._lock, store._conn:  # two profiles that share a LinkedIn can only come from an old import
        store._conn.execute("UPDATE candidates SET linkedin_key = (SELECT linkedin_key FROM candidates WHERE name = 'Kim Lo')")
    page = local_client(store).get("/candidates/duplicates").text
    assert "<strong>Same LinkedIn</strong>" in page


def test_duplicates_reasons_keep_their_capitals(store):
    from talent.store import normalize_linkedin, now_iso
    for name, url in (("Jo Park", "https://www.linkedin.com/in/jo-park/"), ("Joanna Park", "http://linkedin.com/in/Jo-Park")):
        with store._lock, store._conn:  # two rows with one LinkedIn, the way an old import could leave them
            store._conn.execute("INSERT INTO candidates(name, created_at, updated_at, linkedin_url, linkedin_key) "
                                "VALUES (?, ?, ?, ?, ?)", (name, now_iso(), now_iso(), url, normalize_linkedin(url)))
    page = local_client(store).get("/candidates/duplicates").text
    assert "<strong>Same LinkedIn</strong>" in page


def test_merge_shows_ids_only_when_the_names_match(store):
    a = store.add_candidate(name="Kim Lo", email="a@x.com")["id"]
    b = store.add_candidate(name="Kim Lo", email="b@x.com")["id"]
    c = local_client(store)
    same = c.get(f"/candidates/{a}/merge/{b}").text
    assert f"(id {a})" in same and f"(id {b})" in same
    d = store.add_candidate(name="Kimberly Low", email="d@x.com")["id"]
    diff = c.get(f"/candidates/{a}/merge/{d}").text
    assert "(id " not in diff and "Merge into Kim Lo" in diff


def test_paste_textarea_has_a_label(store):
    page = local_client(store).get("/paste").text
    assert re.search(r'<label for="paste-text" class="sr-only">', page)


def test_autosubmit_selects_wait_for_enter_or_blur_with_the_keyboard():
    js = (REPO / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")
    assert "el.dataset.pending" in js and 'e.key === "Enter"' in js and 'addEventListener("focusout"' in js


def test_code_in_the_skills_panel_wraps_at_narrow_widths():
    css = (REPO / "src" / "talent" / "static" / "style.css").read_text(encoding="utf-8")
    assert re.search(r"#skills-panel code[^{]*\.code-list code[^{]*\{[^}]*word-break", css) or \
        re.search(r"#skills-panel code, \.code-list code[^{]*\{[^}]*(word-break|overflow-wrap)", css)


def test_stale_implementer_comment_is_gone():
    assert "Implementer" not in (REPO / "src" / "talent" / "templates" / "table.html").read_text(encoding="utf-8")


def test_tour_is_the_same_on_a_hostile_candidate_name(store):
    store.add_candidate(name="</script><script>alert(1)</script>")
    c = local_client(store)
    c.post("/tour/start", data={"mode": "short"})
    raw = re.search(r'data-tour="([^"]*)"', c.get("/").text).group(1)
    assert "<" not in raw and "alert(1)</script>" not in raw
