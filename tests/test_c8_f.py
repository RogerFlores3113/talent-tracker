"""First-run guided tour: when it starts, what it loads and clears, the short replay, and its routes."""
import html
import json
import re

from conftest import LOCAL_URL, flash_of, local_client
from talent import tour
from talent.sample_data import load_sample_data, sample_ids


def tour_data(response):
    """The tour's steps as the page renders them, or None when the page has no tour."""
    raw = tour_block(response)
    return json.loads(raw) if raw else None


def tour_block(response):
    """The raw, still escaped attribute text the steps travel in (None when the page has no tour)."""
    m = re.search(r'<div id="tour-data" hidden data-tour="([^"]*)"></div>', response.text)
    return html.unescape(m.group(1)) if m else None


def names(store):
    return sorted(c["name"] for c in store.all_candidates())


def start_full(c):
    r = c.post("/tour/start", data={"mode": "full"}, follow_redirects=False)
    assert r.status_code == 303
    return r


def run_load(c):
    r = c.post("/tour/load", follow_redirects=False)
    assert r.status_code == 303
    return r


# ---------------------------------------------------------------- when it starts
def test_fresh_database_starts_the_full_tour_on_any_page(store):
    c = local_client(store)
    data = tour_data(c.get("/"))
    assert data and data["mode"] == "full" and data["step"] == 0 and data["demo"] is False
    assert [s["id"] for s in data["steps"]][:2] == ["welcome", "claude"]
    assert store.settings()["tour_active"] == "1"
    assert tour_data(c.get("/jobs"))["step"] == 0  # the same tour on another page, not a new one


def test_real_candidates_or_jobs_mean_no_auto_start(store):
    store.add_candidate(name="Jordan Pierce")
    c = local_client(store)
    assert tour_data(c.get("/")) is None and tour.state(store)["active"] is False


def test_a_job_of_their_own_means_no_auto_start(store):
    store.add_job("Staff Engineer")
    assert tour_data(local_client(store).get("/")) is None


def test_demo_people_do_not_count_as_real_data(store):
    load_sample_data(store)
    assert tour.should_autostart(store)
    assert tour_data(local_client(store).get("/candidates")) is not None


def test_never_starts_again_after_finish_or_skip_even_if_data_is_emptied(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/skip", follow_redirects=False)
    assert tour_data(c.get("/")) is None and store.count_candidates() == 0
    # The data is empty and the tour was skipped: still no tour.
    cand = store.add_candidate(name="Temp Person")
    store.delete_candidate(cand["id"])
    assert tour_data(c.get("/connect")) is None and not tour.should_autostart(store)


def test_finish_also_closes_it_for_good(store):
    c = local_client(store)
    start_full(c)
    c.post("/tour/finish", follow_redirects=False)
    assert tour_data(c.get("/")) is None and tour.state(store)["seen"]


def test_reset_lets_a_fresh_first_run_start_it_again(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/skip", follow_redirects=False)
    tour.reset(store)
    assert tour_data(c.get("/")) is not None


# ---------------------------------------------------------------- the full tour
def test_full_tour_loads_the_demo_people_and_moves_on(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/step", data={"step": "4"})  # the load step
    steps = tour_data(c.get("/"))["steps"]
    assert steps[4]["id"] == "load" and steps[4]["action"] == "load"
    r = run_load(c)
    assert store.count_candidates() == 16 and sample_ids(store)[1]
    assert tour.state(store)["loaded"] is True and tour.state(store)["step"] == 5
    assert r.headers["location"] == "/candidates"  # the next step lives on the candidates page
    after = tour_data(c.get("/candidates"))
    assert after["demo"] is True and after["step"] == 5
    ids = {s["id"]: s["path"] for s in after["steps"]}
    assert re.fullmatch(r"/candidates/\d+", ids["person"]) and re.fullmatch(r"/jobs/\d+", ids["pipeline"])
    assert ids["person"] == f"/candidates/{_maya(store)}"


def _maya(store):
    return next(c["id"] for c in store.all_candidates() if c["name"] == "Maya Example")


def test_loading_twice_does_not_duplicate(store):
    c = local_client(store)
    start_full(c)
    run_load(c)
    run_load(c)
    assert store.count_candidates() == 16


def test_finish_removes_only_demo_data_and_keeps_what_the_user_worked_with(store):
    real = store.add_candidate(name="Jordan Pierce")  # added mid-tour, so the user's own
    c = local_client(store)
    start_full(c)
    run_load(c)
    maya = _maya(store)
    store.log(maya, "call", "Real call about the Austin role", source="App")  # the user worked with her
    r = c.post("/tour/finish", follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # the message travels in the flash cookie
    msg = flash_of(r)
    assert "Removed the demo data: 15 candidates and 1 job." in msg and "Maya Example" in msg and "Kept" in msg
    assert "backup" in msg
    assert names(store) == ["Jordan Pierce", "Maya Example"]
    assert store.get_candidate(real["id"])["name"] == "Jordan Pierce"
    assert sample_ids(store) == (set(), set()) and not tour.state(store)["active"]


def test_finish_says_nothing_was_kept_when_nothing_was(store):
    c = local_client(store)
    start_full(c)
    run_load(c)
    msg = flash_of(c.post("/tour/finish", follow_redirects=False))
    assert "Removed the demo data: 16 candidates and 2 jobs." in msg and "Kept" not in msg
    assert store.count_candidates() == 0


def test_skip_mid_tour_clears_what_the_tour_loaded(store):
    c = local_client(store)
    start_full(c)
    run_load(c)
    c.post("/tour/step", data={"step": "8"})
    assert tour_data(c.get("/"))["demo"] is True  # the page knows to ask "End the tour?" first
    c.post("/tour/skip", follow_redirects=False)
    assert store.count_candidates() == 0 and not store.list_jobs()
    assert tour.state(store) == {"active": False, "mode": "full", "step": 0, "loaded": False, "seen": True}


def test_skip_before_loading_removes_nothing_and_asks_nothing(store):
    store.add_candidate(name="Jordan Pierce")
    c = local_client(store)
    start_full(c)
    assert tour_data(c.get("/"))["demo"] is False
    c.post("/tour/skip", follow_redirects=False)
    assert names(store) == ["Jordan Pierce"]


def test_skip_never_removes_sample_data_the_user_loaded_themselves(store):
    local_client(store).post("/sample-data")
    c = local_client(store)
    assert tour_data(c.get("/"))["demo"] is False  # theirs, not the tour's
    run_load(c)  # the tour has nothing to add and takes no credit for what is there
    assert tour.state(store)["loaded"] is False
    step = {s["id"]: s for s in tour_data(c.get("/"))["steps"]}["load"]
    assert step["action"] == "" and "loaded them yourself" in step["body"]
    c.post("/tour/skip", follow_redirects=False)
    assert store.count_candidates() == 16 and sample_ids(store)[1]
    # and finishing the tour doesn't clear them either
    tour.reset(store)
    start_full(c)
    c.post("/tour/finish", follow_redirects=False)
    assert store.count_candidates() == 16


def test_finish_step_names_what_it_will_do(store):
    c = local_client(store)
    start_full(c)
    last = tour_data(c.get("/"))["steps"][-1]
    assert last["action"] == "finish" and last["cta"] == "Finish the tour"
    run_load(c)
    last = tour_data(c.get("/"))["steps"][-1]
    assert last["cta"] == "Clear the demo data and start" and "backup" in last["body"]


# ---------------------------------------------------------------- the short replay
def test_short_replay_never_changes_candidates_with_real_data(store):
    store.add_candidate(name="Jordan Pierce")
    store.add_job("Staff Engineer")
    c = local_client(store)
    r = c.get("/?tour=short")
    data = tour_data(r)
    assert data["mode"] == "short"
    ids = [s["id"] for s in data["steps"]]
    assert "load" not in ids and "welcome" not in ids and ids[-1] == "finish" and "person" in ids
    assert c.post("/tour/load", follow_redirects=False).status_code == 303  # refused: nothing is added
    assert names(store) == ["Jordan Pierce"]
    assert tour_data(c.get("/candidates"))["step"] == 0
    c.post("/tour/finish", follow_redirects=False)
    assert names(store) == ["Jordan Pierce"] and len(store.list_jobs()) == 1
    assert tour_data(c.get("/")) is None


def test_short_replay_uses_their_own_people_and_leaves_demo_data_alone(store):
    mine = store.add_candidate(name="Jordan Pierce")
    load_sample_data(store)  # demo data they loaded themselves
    c = local_client(store)
    c.post("/tour/start", data={"mode": "short"})
    steps = {s["id"]: s for s in tour_data(c.get("/"))["steps"]}
    assert steps["person"]["path"] == f"/candidates/{mine['id']}"
    before = store.count_candidates()
    c.post("/tour/skip", follow_redirects=False)
    assert store.count_candidates() == before and sample_ids(store)[0]


def test_short_replay_with_no_data_skips_the_steps_that_need_it(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/skip", follow_redirects=False)  # the full tour is done
    data = tour_data(c.get("/?tour=short"))
    ids = [s["id"] for s in data["steps"]]
    assert "person" not in ids and "pipeline" not in ids and "claude" in ids and "interviews" in ids
    assert store.count_candidates() == 0


def test_short_replay_does_not_clear_a_demo_the_full_tour_never_loaded(store):
    c = local_client(store)
    c.get("/")
    c.post("/tour/skip", follow_redirects=False)
    c.post("/sample-data")
    c.get("/?tour=short")
    c.post("/tour/skip", follow_redirects=False)
    assert store.count_candidates() == 16


def test_take_the_tour_entries_exist(store):
    store.add_candidate(name="Jordan Pierce")
    c = local_client(store)
    assert 'href="/?tour=short"' in c.get("/connect").text
    settings = c.get("/settings").text
    assert 'action="/tour/start"' in settings and 'value="short"' in settings
    r = c.post("/tour/start", data={"mode": "short"}, follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"] == "/"  # short starts on Today
    assert tour_data(c.get("/"))["mode"] == "short"


def test_a_tour_already_running_is_not_restarted(store):
    c = local_client(store)
    start_full(c)
    run_load(c)
    c.post("/tour/start", data={"mode": "short"})  # e.g. a click on Take the tour mid-way
    assert tour.state(store)["mode"] == "full" and tour.state(store)["loaded"] is True


# ---------------------------------------------------------------- state and routes
def test_step_state_persists_across_requests(store):
    c = local_client(store)
    c.get("/")
    assert c.post("/tour/step", data={"step": "3"}).json() == {"ok": True, "step": 3}
    other = local_client(store)  # a new browser session: the state is in the database, not the page
    assert tour_data(other.get("/interviews"))["step"] == 3
    assert c.post("/tour/step", data={"step": "999"}).json()["step"] == len(tour_data(c.get("/"))["steps"]) - 1
    assert c.post("/tour/step", data={"step": "-5"}).json()["step"] == 0


def test_step_without_a_tour_or_with_junk(store):
    store.add_candidate(name="Jordan Pierce")
    c = local_client(store)
    assert c.post("/tour/step", data={"step": "2"}).status_code == 409
    c.post("/tour/start", data={"mode": "short"})
    assert c.post("/tour/step", data={"step": "two"}).status_code == 422
    assert c.post("/tour/start", data={"mode": "weird"}).status_code == 422


def test_routes_reject_cross_origin_posts(store):
    store.add_candidate(name="Jordan Pierce")
    c = local_client(store, origin=False)
    c.post("/tour/start", data={"mode": "short"}, headers={"Origin": LOCAL_URL})
    for path, data in (("/tour/start", {"mode": "full"}), ("/tour/step", {"step": "2"}), ("/tour/load", {}),
                       ("/tour/skip", {}), ("/tour/finish", {})):
        for origin in ("http://evil.example", "null"):
            r = c.post(path, data=data, headers={"Origin": origin}, follow_redirects=False)
            assert r.status_code == 403, (path, origin)
    st = tour.state(store)
    assert st["active"] and st["step"] == 0 and not st["seen"]  # nothing moved


def test_routes_need_the_app_window_key(store):
    c = local_client(store, unlocked=False)
    for path in ("/tour/start", "/tour/step", "/tour/load", "/tour/skip", "/tour/finish"):
        assert c.post(path, data={"mode": "short", "step": "1"}, follow_redirects=False).status_code == 401


def test_tour_settings_are_registered_keys(store):
    for key in ("tour_active", "tour_mode", "tour_step", "tour_loaded", "tour_seen"):
        assert key in store.settings()
    store.set_settings(tour_seen="1")
    assert store.setting("tour_seen") == "1"


def test_rendered_step_data_is_valid_json_and_cannot_break_out_of_the_page(store):
    c = local_client(store)
    r = c.get("/")
    data = tour_data(r)
    assert len(data["steps"]) == 13 and all({"id", "title", "body", "path", "target"} <= set(s) for s in data["steps"])
    raw = re.search(r'data-tour="([^"]*)"', r.text).group(1)  # what is really in the page
    assert "<" not in raw and ">" not in raw and '"' not in raw  # quotes and brackets are escaped
    assert r.text.count('id="tour-data"') == 1 and "<script>" not in r.text
    assert "script-src 'self'" in r.headers["content-security-policy"]  # no inline script anywhere


def test_a_hostile_candidate_name_never_reaches_the_tour_data(store):
    evil = store.add_candidate(name="</script><script>alert(1)</script>")
    c = local_client(store)
    c.post("/tour/start", data={"mode": "short"})
    r = c.get("/")
    block = tour_block(r)
    assert "alert" not in block and "script" not in block
    assert tour_data(r)["steps"][[s["id"] for s in tour_data(r)["steps"]].index("person")]["path"] == f"/candidates/{evil['id']}"
    assert "<script>alert(1)</script>" not in c.get(f"/candidates/{evil['id']}").text


def test_tour_targets_exist_on_the_pages_they_point_at(store):
    c = local_client(store)
    c.get("/")
    connect = c.get("/connect").text
    for hook in ('id="claude-panel"', 'id="codex-panel"', 'id="by-hand"'):
        assert hook in connect
    assert 'name="q"' in c.get("/candidates").text and 'class="tablebar"' in c.get("/candidates").text


def test_tour_script_is_not_inline_and_the_data_only_shows_while_running(store):
    c = local_client(store)
    r = c.get("/")
    assert '<script src="/static/app.js"' in r.text
    assert "startTour" in c.get("/static/app.js").text and "tour-pop" in c.get("/static/style.css").text
    c.post("/tour/skip", follow_redirects=False)
    assert 'id="tour-data"' not in c.get("/").text
