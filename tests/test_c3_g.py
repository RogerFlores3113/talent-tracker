"""Cycle 3, fixer G: every AI write is logged and undoable from Changes."""
import re
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from mcp.types import CallToolResult

from conftest import local_client
from talent import actions, store as store_mod
from talent.mcp_server import INSTRUCTIONS, build_server


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def structured(result):
    if isinstance(result, CallToolResult):
        return result.structuredContent
    content, data = result
    return data.get("result", data)


def text(result):
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return "\n".join(c.text for c in content)


try:  # notices travel in a signed cookie once that change is merged
    from conftest import flash_of
except ImportError:
    flash_of = None


def flash(r, key="notice"):
    if flash_of is not None:
        return flash_of(r, key) or ""
    return parse_qs(urlsplit(r.headers["location"]).query).get(key, [""])[0]


def hour_ago() -> str:
    return str(int((datetime.now(timezone.utc) - timedelta(hours=1)).timestamp()))


def ai_session(store):
    """What a busy AI chat does: adds a person, makes a job, pipes and moves them, edits the job and its
    stages, closes it, and adds and completes to-dos."""
    server = build_server(store)
    cid = structured(call(server, "save_candidate", name="Lee Chen", email="lee@x.com"))["candidate"]["id"]
    jid = structured(call(server, "create_job", title="Senior Backend Engineer", company="Acme"))["id"]
    call(server, "add_to_pipeline", candidate_id=cid, job_id=jid)
    call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Screening")
    call(server, "update_job", job_id=jid, openings=2, location="Remote")
    call(server, "edit_job_stages", job_id=jid, rename={"Offer": "Offer out"}, add=["Take-home"])
    sid = structured(call(server, "add_suggestion", text="Ping Lee", candidate_id=cid))["id"]
    old_todo = store.add_suggestion("Call the HM")  # the recruiter's own to-do, done by the AI
    call(server, "complete_suggestion", suggestion_id=old_todo["id"])
    call(server, "update_job", job_id=jid, status="closed")
    return server, cid, jid, sid, old_todo["id"]


# ------------------------------------------------------- 1, 2: log and Changes
def test_every_ai_write_is_listed_on_changes(store):
    ai_session(store)
    kinds = [a["kind"] for a in store.ai_actions()]
    for k in ("candidate", "job_created", "pipeline_add", "stage_move", "job_edit", "stages_edit", "todo_added",
              "todo_done"):
        assert k in kinds, k
    page = local_client(store).get("/changes?who=ai").text
    for bit in ("Added Lee Chen", "Created the job Senior Backend Engineer (Acme)",
                "Added Lee Chen to Senior Backend Engineer (Acme) in Sourced",
                "Moved Lee Chen from Sourced to Screening", "renamed Offer to Offer out, added Take-home",
                "status Open to Closed", "openings 1 to 2", "Added a to-do: Ping Lee", "Marked a to-do done: Call the HM"):
        assert bit in page, bit
    # Cycle 4 P: the add, the job and the person each wait for the newer AI change, so they show the reason
    # instead of an Undo that would fail (Undo all still puts them back).
    assert "Undo all AI changes since" in page and page.count(">Undo</button>") >= 6
    assert page.count("Can't undo: your AI app changed it again later. Undo that first.") == 3
    since = re.search(r'name="since" value="(\d+)"', page).group(1)  # epoch, so a hand-built post can't fail
    assert int(since) > 1_000_000_000
    zone = datetime.now().astimezone().tzname()
    assert f"Undo all AI changes since " in page and zone in page.split("Undo all AI changes since", 1)[1][:60]
    assert "—" not in page


def test_undo_all_reverts_every_kind_in_reverse_order(store):
    _, cid, jid, sid, todo = ai_session(store)
    c = local_client(store)
    r = c.post("/changes/undo-ai", data={"since": hour_ago()}, follow_redirects=False)
    msg = flash(r)
    assert msg.startswith("Undid 9 AI changes: Changed Senior Backend Engineer (Acme): status Open to Closed;"), msg
    assert "Left" not in msg, msg
    assert "; and 3 more." in msg  # more than 6, so the rest are counted (c5 A: then the backup note)
    assert store.count_candidates() == 0 and store.list_jobs(None) == []
    assert store._row("SELECT done_at FROM suggestions WHERE id = ?", (todo,))["done_at"] is None
    assert store._row("SELECT 1 FROM suggestions WHERE id = ?", (sid,)) is None
    assert "no AI changes" in flash(c.post("/changes/undo-ai", data={"since": hour_ago()}, follow_redirects=False))


def test_undo_all_skips_what_the_recruiter_worked_on(store):
    server = build_server(store)
    cid = structured(call(server, "save_candidate", name="Ana Diaz", email="ana@x.com"))["candidate"]["id"]
    other = structured(call(server, "save_candidate", name="Bo Li", email="bo@x.com"))["candidate"]["id"]
    jid = structured(call(server, "create_job", title="Data Lead"))["id"]
    store.log(cid, "note", "Met at the meetup")  # the recruiter's own note
    store.update_job(jid, location="Berlin")  # and their own edit to the job
    r = local_client(store).post("/changes/undo-ai", data={"since": hour_ago()}, follow_redirects=False)
    msg = flash(r)
    assert msg.startswith("Undid 1 AI change: Added Bo Li."), msg
    assert "Left 2 as they are" in msg and "Added Ana Diaz (You or an import worked on them since" in msg
    assert "Created the job Data Lead (It was edited since" in msg
    assert store.get_candidate(cid) and store.get_job(jid)
    with pytest.raises(store_mod.NotFound):
        store.get_candidate(other)


def test_one_undo_from_changes_and_a_hand_built_post(store):
    server = build_server(store)
    lee = store.add_candidate(name="Lee")
    jid = store.add_job("Backend")["id"]
    store.add_to_pipeline(lee["id"], jid)
    call(server, "move_stage", candidate_id=lee["id"], job_id=jid, stage="Rejected")
    move = store.ai_actions()[0]
    c = local_client(store)
    r = c.post(f"/changes/ai/{move['id']}/undo", follow_redirects=False)
    assert flash(r) == "Undid: Moved Lee from Sourced to Rejected in Backend."
    assert store.candidate_pipelines(lee["id"])[0]["stage"] == "Sourced"
    again = c.post(f"/changes/ai/{move['id']}/undo", follow_redirects=False)
    assert "already undone" in flash(again, "flash_error")
    assert "Undone" in c.get("/changes?who=ai").text
    # Any start time form works: epoch, Z, and +00:00 with the plus sent unescaped.
    start = datetime.now(timezone.utc) - timedelta(hours=1)
    for since in (start.strftime("%Y-%m-%dT%H:%M:%SZ"), start.strftime("%Y-%m-%dT%H:%M:%S") + " 00:00"):
        r = c.post("/changes/undo-ai", data={"since": since}, follow_redirects=False)
        assert not flash(r, "flash_error"), since


def test_undo_from_the_timeline_marks_the_log(store):
    server = build_server(store)
    lee = store.add_candidate(name="Lee")
    jid = store.add_job("Backend")["id"]
    call(server, "add_to_pipeline", candidate_id=lee["id"], job_id=jid)
    entry = next(t for t in store.timeline(lee["id"]) if t["kind"] == "stage")
    store.undo_change(entry["id"])
    assert store.ai_actions()[0]["undone_at"]
    assert "no AI changes" in flash(local_client(store).post("/changes/undo-ai", data={"since": hour_ago()},
                                                            follow_redirects=False))


def test_old_ai_entries_join_the_log_on_upgrade(store):
    ana = store.add_candidate(name="Ana", email="ana@x.com")
    store.update_candidate(ana["id"], email="evil@x.com", by="ai")
    with store._conn:
        store._conn.execute("DELETE FROM ai_actions")
        store_mod._migrate_v6_data(store._conn)
    assert [a["kind"] for a in store.ai_actions()] == ["profile"]
    assert store.schema_version == len(store_mod._MIGRATIONS)


def test_ai_stage_edit_undo_puts_names_people_and_order_back(store):
    server = build_server(store)
    jid = store.add_job("Backend")["id"]
    lee = store.add_candidate(name="Lee")
    store.add_to_pipeline(lee["id"], jid, "Offer")
    before = [(s["name"], s["outcome"]) for s in store.job_stages(jid)]
    call(server, "edit_job_stages", job_id=jid, rename={"Offer": "Screening2", "Screening": "Offer"},
         add=["Take-home"], order=["Contacted", "Sourced"])
    assert store.candidate_pipelines(lee["id"])[0]["stage"] == "Screening2"
    edit = store.ai_actions()[0]
    assert store.undo_ai_action(edit["id"]).startswith("Changed the stages of Backend: renamed")
    assert [(s["name"], s["outcome"]) for s in store.job_stages(jid)] == before
    assert store.candidate_pipelines(lee["id"])[0]["stage"] == "Offer"
    # A stage edit the recruiter changed again since is left alone.
    call(server, "edit_job_stages", job_id=jid, add=["Debrief"])
    store.rename_stage(jid, "Debrief", "Final debrief")
    with pytest.raises(ValueError, match="changed again"):
        store.undo_ai_action(store.ai_actions()[0]["id"])


def test_ai_job_edit_undo_restores_status_and_fields(store):
    server = build_server(store)
    jid = store.add_job("Backend", location="NYC")["id"]
    call(server, "update_job", job_id=jid, status="closed", location="Remote")
    store.undo_ai_action(store.ai_actions()[0]["id"])
    job = store.get_job(jid)
    assert (job["status"], job["location"]) == ("open", "NYC")
    call(server, "update_job", job_id=jid, status="on hold", location="Remote")
    store.update_job(jid, location="Paris")  # the recruiter's own edit stays
    assert "left location as you changed it" in store.undo_ai_action(store.ai_actions()[0]["id"])
    job = store.get_job(jid)
    assert (job["status"], job["location"]) == ("open", "Paris")


# ----------------------------------------------------------- 3. Today lines
def test_today_says_what_the_ai_did_to_pipelines(store):
    server = build_server(store)
    jid = store.add_job("Senior Backend Engineer")["id"]
    staff = store.add_job("Staff Backend Engineer")["id"]
    for n in ("Ana", "Bo", "Cy"):
        cid = store.add_candidate(name=n)["id"]
        store.add_to_pipeline(cid, jid, "Screening")
        call(server, "move_stage", candidate_id=cid, job_id=jid, stage="Rejected")
    call(server, "update_job", job_id=staff, status="closed")
    titles = [i["title"] for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]
    assert "Your AI app moved 3 people out of Senior Backend Engineer. Review or undo." in titles
    assert "Your AI app closed Staff Backend Engineer." in titles
    store.undo_ai_action(next(a for a in store.ai_actions() if a["kind"] == "job_edit")["id"])
    titles = [i["title"] for i in actions.action_items(store)["items"] if i["kind"] == "ai_change"]
    assert "Your AI app closed Staff Backend Engineer." not in titles


# ------------------------------------------------------ 4. edit_job_stages
def test_edit_job_stages_is_all_or_nothing(store):
    server = build_server(store)
    jid = store.add_job("Backend")["id"]
    before = store.job_stages(jid)
    with pytest.raises(Exception, match="No change made. No stage called 'Nope'"):
        call(server, "edit_job_stages", job_id=jid, rename={"Offer": "Offer2", "Nope": "X"}, add=["Take-home"])
    assert store.job_stages(jid) == before and not store.ai_actions()


def test_edit_job_stages_outcome_limits(store):
    server = build_server(store)
    jid = store.add_job("Backend")["id"]
    lee = store.add_candidate(name="Lee")
    store.add_to_pipeline(lee["id"], jid, "Interviewing")
    # Cycle 3 K: the AI can't change what any existing stage counts as, empty or not.
    with pytest.raises(Exception, match="No change made. Ask the recruiter to change what Contacted counts as"):
        call(server, "edit_job_stages", job_id=jid,
             outcomes={"Interviewing": "lost", "Screening": "lost", "Contacted": "lost"})
    with pytest.raises(Exception, match="Ask the recruiter to change what Interviewing counts as in the app"):
        call(server, "edit_job_stages", job_id=jid, outcomes={"Interviewing": "lost"})
    with pytest.raises(Exception, match="Ask the recruiter"):  # a closing name counts too
        call(server, "edit_job_stages", job_id=jid, rename={"Interviewing": {"name": "Passed", "outcome": "lost"}})
    assert {s["name"]: s["outcome"] for s in store.job_stages(jid)}["Interviewing"] == "open"
    with pytest.raises(Exception, match="change what Withdrawn counts as"):
        call(server, "edit_job_stages", job_id=jid, outcomes={"Withdrawn": "open"})
    out = structured(call(server, "edit_job_stages", job_id=jid, add=["Parked"], outcomes={"Parked": "lost"}))
    assert out["stage_outcomes"]["Parked"] == "lost" and out["stage_outcomes"]["Withdrawn"] == "lost"
    store.set_stage_outcome(jid, "Interviewing", "lost")  # the recruiter, in the app, can


def test_order_keeps_left_out_stages_in_place(store):
    server = build_server(store)
    jid = store.add_job("Backend")["id"]
    out = structured(call(server, "edit_job_stages", job_id=jid,
                          order=["Sourced", "Contacted", "Offer", "Hired", "Rejected", "Withdrawn"]))
    assert out["stages"] == list(store_mod.STAGES)
    assert out["left_out_of_order"] == ["Screening", "Interviewing"] and "kept their place" in out["note"]
    out = structured(call(server, "edit_job_stages", job_id=jid, order=["Contacted", "Sourced"]))
    assert out["stages"][:3] == ["Contacted", "Sourced", "Screening"] and out["stages"][-1] == "Withdrawn"


# ------------------------------------------------- 5. save_candidate cooldown
def test_save_candidate_keeps_identity_fields_after_a_recent_ai_change(store):
    server = build_server(store)
    kenji = store.add_candidate(name="Kenji Watanabe", email="kenji@x.com")
    call(server, "update_candidate", candidate_id=kenji["id"], email="harvest@evil.example")
    res = call(server, "save_candidate", name="Kenji Watanabe", email="harvest@evil.example", phone="555 0100",
               website="https://kenji.dev")
    out, said = structured(res), text(res)
    saved = store.get_candidate(kenji["id"])
    assert saved["website"] == "https://kenji.dev" and not saved["phone"]
    # Cycle 3 K: phone was empty, so it was not saved, but it isn't listed as a "kept" saved value.
    assert out["identity_kept"] == ["phone"] and "phone" not in out.get("kept", [])
    assert "Ask the recruiter to confirm in the app" in said and "changed this person's email" in said


def test_save_candidate_fills_at_most_one_identity_field(store):
    server = build_server(store)
    maya = store.add_candidate(name="Maya Okafor", email="maya@x.com")
    res = call(server, "save_candidate", name="Maya Okafor", email="maya@x.com", phone="555 0100",
               linkedin_url="https://linkedin.com/in/maya", website="https://maya.dev", current_title="CTO")
    saved = store.get_candidate(maya["id"])
    assert saved["website"] == "https://maya.dev" and saved["current_title"] == "CTO"
    assert not saved["phone"] and not saved["linkedin_url"]
    assert structured(res)["identity_kept"] == ["phone", "linkedin_url"]
    assert "one save can add only one of them" in text(res)
    # One at a time, with nothing recent, still goes through.
    call(server, "save_candidate", name="Maya Okafor", email="maya@x.com", phone="555 0100")
    assert store.get_candidate(maya["id"])["phone"] == "555 0100"


# ------------------------------------------------------------ 6, 7. copy
def test_instructions_and_undo_copy_match(store):
    assert "can undo it there" in INSTRUCTIONS and "jobs you create or edit" in INSTRUCTIONS
    assert "stays on the timeline" in INSTRUCTIONS
    assert "—" not in INSTRUCTIONS and "–" not in INSTRUCTIONS
    page = local_client(store).get("/changes").text
    assert "everything your AI app wrote" in page


def test_job_page_and_list_say_the_ai_closed_it(store):
    server = build_server(store)
    jid = store.add_job("Staff Backend Engineer")["id"]
    call(server, "update_job", job_id=jid, status="closed")
    day = datetime.now().astimezone()
    note = f"Closed by your AI app on {day:%b} {day.day}"
    c = local_client(store)
    assert note in c.get(f"/jobs/{jid}").text
    assert note in c.get("/jobs?status=closed").text
    store.update_job(jid, status="open")
    store.update_job(jid, status="closed")  # the recruiter closed it again themselves
    assert "by your AI app" not in c.get(f"/jobs/{jid}").text
