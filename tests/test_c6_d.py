"""Cycle 6, fixer D: outreach moves people on (one call for many), the Shortlist card's Keep all / Pass all /
Keep selected, other clients' claims, whole-word closest matches, and follow-up sequences per job."""
import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, mcp_server, outreach, shortlist
from talent.mcp_server import VIEW_KEY, build_server
from talent.mcp_views import view_html
from talent.sample_data import load_sample_data

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    """These tests play a host that says it shows MCP Apps views (only those get the card tools)."""
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


def key_of(server, res):
    """The key the Shortlist card asks for with its app-only tool (never in a result the model sees)."""
    d = res.structuredContent or {}
    ids = [p["id"] for p in d.get("added") or d.get("prospects") or [] if p.get("status", "proposed") == "proposed"]
    out = call(server, "shortlist_card_key", job_id=res.meta[VIEW_KEY]["job_id"], prospect_ids=ids)
    return out.structuredContent["key"]


def text(res):
    return "\n".join(c.text for c in res.content)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def stage_of(store, cid, jid):
    return next(p["stage"] for p in store.candidate_pipelines(cid) if p["job_id"] == jid)


def _people(*names, link=True):
    return [{"name": n, "title": "ML Engineer", "company": "Acme", "fit": "Ranking work",
             **({"linkedin_url": f"https://www.linkedin.com/in/{n.lower().replace(' ', '-')}/"} if link else {})}
            for n in names]


# ------------------------------------------------------------ 1. outreach moves people on
def test_logging_outreach_moves_sourced_to_contacted_on_their_one_job(store):
    job = store.add_job("ML Engineer", company="Northwind")
    ana = store.add_candidate(name="Ana Ruiz")
    store.add_to_pipeline(ana["id"], job["id"])
    server = build_server(store)
    res = call(server, "log_activity", candidate_id=ana["id"], kind="InMail", summary="Intro")  # no job_id given
    assert stage_of(store, ana["id"], job["id"]) == "Contacted"
    assert "Moved them from Sourced to Contacted" in text(res) and "suggested_step" not in res.structuredContent
    assert res.structuredContent["logged"]["job_id"] == job["id"]
    # The move is the AI's, with its own Undo on Changes.
    move = next(a for a in store.ai_actions() if a["kind"] == "stage_move")
    store.undo_ai_action(move["id"])
    assert stage_of(store, ana["id"], job["id"]) == "Sourced"
    # A note doesn't move anyone; with the switch off, outreach doesn't either (the nudge comes back).
    call(server, "log_activity", candidate_id=ana["id"], kind="note", summary="Looks strong")
    assert stage_of(store, ana["id"], job["id"]) == "Sourced"
    store._conn.execute("INSERT INTO settings(key, value) VALUES ('outreach_moves', '0')")
    res = call(server, "log_activity", candidate_id=ana["id"], kind="email", summary="Second note", job_id=job["id"])
    assert stage_of(store, ana["id"], job["id"]) == "Sourced" and res.structuredContent["suggested_step"]


def test_one_call_logs_outreach_for_many_as_one_undoable_batch(store):
    job = store.add_job("ML Engineer", company="Northwind")
    ids = [store.add_candidate(name=n)["id"] for n in ("Ana Ruiz", "Bo Li", "Cy Park")]
    for cid in ids:
        store.add_to_pipeline(cid, job["id"])
    server = build_server(store)
    res = call(server, "log_activity", candidate_ids=ids, kind="message", summary="InMail: ML role", job_id=job["id"])
    first = text(res).splitlines()[0]
    assert first.startswith('Logged a message with 3 people ("Ana Ruiz", "Bo Li", "Cy Park")')
    assert "Moved 3 to Contacted" in first and "{" not in text(res)
    assert all(stage_of(store, cid, job["id"]) == "Contacted" for cid in ids)
    batches = [a for a in store.ai_actions() if a["kind"] == "outreach_batch"]
    assert len(batches) == 1 and len(batches[0]["children"]) == 6 and batches[0]["undoable"]
    c = local_client(store)
    page = c.get("/changes").text
    # once in the AI session ledger, once in the list: the six moves and notes never show one by one
    assert page.count("Logged a message with 3 people and moved 3 to Contacted") == 2
    r = c.post(f"/changes/ai/{batches[0]['id']}/undo", follow_redirects=False)
    assert r.status_code == 303
    assert all(stage_of(store, cid, job["id"]) == "Sourced" for cid in ids)
    assert all(t["struck"] for cid in ids for t in store.timeline(cid) if t["kind"] == "message")
    # Nothing is logged when one of them can't be.
    with pytest.raises(ToolError, match="No candidate with id 9999"):
        call(server, "log_activity", candidate_ids=[ids[0], 9999], kind="message", summary="Again")
    assert len([t for t in store.timeline(ids[0]) if t["kind"] == "message"]) == 1
    assert call(server, "log_activity", candidates=ids[:2], kind="email").structuredContent["people"]  # alias


def test_contacted_today_skips_a_duplicate_and_moves_them(store):
    job = store.add_job("ML Engineer", company="Northwind")
    tomas = store.add_candidate(name="Tomas Diaz")
    store.add_to_pipeline(tomas["id"], job["id"])
    c = local_client(store)
    r = c.post(f"/candidates/{tomas['id']}/contacted", follow_redirects=False)
    assert "Moved them from Sourced to Contacted" in flash_of(r)
    assert stage_of(store, tomas["id"], job["id"]) == "Contacted"
    r = c.post(f"/candidates/{tomas['id']}/contacted", follow_redirects=False)
    assert "already logged for today" in flash_of(r)
    assert len([t for t in store.timeline(tomas["id"]) if t["kind"] == "message"]) == 1


# ------------------------------------------------------------ 2. the Shortlist card
def test_card_keeps_all_keeps_selected_and_passes_all(store):
    job = store.add_job("ML Engineer", company="Northwind")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz", "Bo Li", "Cy Park", "Di Wu"))
    key = key_of(server, res)
    assert res.structuredContent["keep_stages"] == ["Sourced", "Contacted"]
    pids = {p["name"]: p["id"] for p in res.structuredContent["added"]}
    refused = call(server, "keep_prospects", job_id=job["id"], prospect_ids=list(pids.values()))
    assert refused.isError and not shortlist.counts(store, job["id"])["kept"]
    kept = call(server, "keep_prospects", job_id=job["id"], prospect_ids=[pids["Ana Ruiz"], pids["Bo Li"]],
                view_key=key, stage="Contacted")
    assert text(kept).startswith("Kept 2 people and added them to Contacted")
    rows = {p["name"]: p for p in kept.structuredContent["prospects"]}
    assert rows["Ana Ruiz"]["status"] == "kept" and rows["Ana Ruiz"]["candidate_url"].endswith(
        f"/candidates/{rows['Ana Ruiz']['candidate_id']}")
    assert stage_of(store, rows["Bo Li"]["candidate_id"], job["id"]) == "Contacted"
    passed = call(server, "pass_prospects", job_id=job["id"], prospect_ids=[pids["Cy Park"], pids["Di Wu"]],
                  view_key=key, reason="Not open to hybrid")
    assert text(passed).startswith("Passed on 2 people")
    assert {p["pass_reason"] for p in shortlist.listed(store, job["id"], "passed")} == {"Not open to hybrid"}
    assert len(shortlist.keep_batches(store)) == 1  # one Keep, one Undo
    call(server, "propose_candidates", job_id=job["id"], people=_people("Eve Moss"))
    page = local_client(store).get(f"/jobs/{job['id']}/shortlist").text
    assert "Keep in Contacted" in page and "Into Contacted" in page
    html = view_html("shortlist")
    for bit in ("Keep all", "Keep selected", "Pass all", "Same as ", "keep_prospects", "pass_prospects",
                "Open \"+p.name+\" in Talent Search", "About and work history", "Keep into"):
        assert bit in html, bit
    assert "innerHTML" not in html and "—" not in html and "–" not in html


def test_same_name_buttons_say_who_and_keep_returns_the_record_link(store):
    job = store.add_job("ML Engineer", company="Northwind")
    maya = store.add_candidate(name="Maya Okafor", current_company="Ledgerline")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=_people("Maya Okafor", link=False))
    assert "1 with no LinkedIn link" in text(res).splitlines()[0]
    row = res.structuredContent["added"][0]
    assert any("No LinkedIn link" in x for x in row["check_lines"])
    out = call(server, "keep_prospect", job_id=job["id"], prospect_id=row["id"], view_key=key_of(server, res),
               candidate_id=maya["id"])
    assert out.structuredContent["prospect"]["candidate_url"].endswith(f"/candidates/{maya['id']}")
    store.add_candidate(name="Lee Chan", current_company="Globex")
    call(server, "propose_candidates", job_id=job["id"], people=_people("Lee Chan"))
    page = local_client(store).get(f"/jobs/{job['id']}/shortlist").text
    assert "Same as Lee Chan, Globex" in page and "Someone new" in page and "Into Contacted" in page


def test_propose_candidates_takes_prospects_and_candidates(store):
    job = store.add_job("ML Engineer")
    server = build_server(store)
    assert not call(server, "propose_candidates", job_id=job["id"], prospects=_people("Ana Ruiz")).isError
    assert not call(server, "propose_candidates", job_id=job["id"], candidates=_people("Bo Li")).isError
    assert shortlist.counts(store, job["id"])["proposed"] == 2


def test_passed_for_another_job_comes_first(store):
    a, b = store.add_job("ML Engineer", company="Acme"), store.add_job("Data Engineer", company="Acme")
    shortlist.propose(store, a["id"], _people("Ana Ruiz"), by="app")
    shortlist.pass_on(store, a["id"], shortlist.listed(store, a["id"])[0]["id"], "Wants remote only")
    store.add_candidate(name="Ana Ruiz")
    shortlist.propose(store, b["id"], _people("Ana Ruiz"), by="app")
    lines = shortlist.check_lines(shortlist.listed(store, b["id"])[0]["checks"])
    assert lines[0] == "Passed on for ML Engineer, Acme: Wants remote only"


# ------------------------------------------------------------ 3. other clients, fuzzy search
def test_adding_someone_submitted_at_another_client_warns(store):
    hfl = store.add_job("Robotics Engineer", company="Harbor Freight Labs")
    acme = store.add_job("Backend Engineer", company="Acme Payments")
    lena = store.add_candidate(name="Lena Hoffmann")
    store.add_to_pipeline(lena["id"], hfl["id"])
    store.add_stage(hfl["id"], "Submitted to client", after="Screening")
    store.move_stage(lena["id"], hfl["id"], "Submitted to client", by="app")
    res = call(build_server(store), "add_to_pipeline", candidate_id=lena["id"], job_id=acme["id"])
    assert 'Heads up: "Lena Hoffmann" is already in Submitted to client at Harbor Freight Labs' in text(res)
    assert res.structuredContent["other_clients"][0]["client"] == "Harbor Freight Labs"
    page = local_client(store).get(f"/jobs/{acme['id']}").text
    assert "In play at 1 other client" in page
    # Same client, or someone only sourced elsewhere: no warning.
    other = store.add_job("Firmware Engineer", company="Harbor Freight Labs")
    assert outreach.other_clients(store, lena["id"], other["id"]) == []
    bo = store.add_candidate(name="Bo Li")
    store.add_to_pipeline(bo["id"], hfl["id"])
    assert outreach.other_clients(store, bo["id"], acme["id"]) == []
    # The app's Add to job says so too.
    c = local_client(store)
    third = store.add_job("SRE", company="Pinecrest")
    r = c.post(f"/candidates/{lena['id']}/jobs", data={"job_id": str(third["id"])}, follow_redirects=False)
    assert "Harbor Freight Labs" in flash_of(r, "flash_error")


def test_closest_matches_use_whole_words(store):
    load_sample_data(store)
    res = store.search_candidates("robotics C++ ROS")
    assert "Hannah Schultz" not in [h.candidate["name"] for h in res.hits]


# ------------------------------------------------------------ 4. follow-up sequences
def _with_sequence(store, job_id, days_ago=10):
    outreach.save_sequence(store, job_id, list(outreach.DEFAULT_STEPS))
    key = f"{outreach.SEQ_PREFIX}{job_id}"
    seq = outreach.sequence(store, job_id)
    seq["since"] = iso(datetime.now(timezone.utc) - timedelta(days=days_ago))
    store._conn.execute("UPDATE settings SET value = ? WHERE key = ?", (json.dumps(seq), key))


def test_a_sequence_brings_follow_ups_due_and_a_reply_ends_them(store):
    job = store.add_job("ML Engineer", company="Northwind")
    maya, bo = store.add_candidate(name="Maya Okafor"), store.add_candidate(name="Bo Li")
    for cid in (maya["id"], bo["id"]):
        store.add_to_pipeline(cid, job["id"])
    _with_sequence(store, job["id"])
    server = build_server(store)
    five = iso(datetime.now(timezone.utc) - timedelta(days=5))
    call(server, "log_activity", candidate_ids=[maya["id"], bo["id"]], kind="message", summary="InMail", when=five,
         job_id=job["id"])
    items = [i for i in actions.action_items(store)["items"] if i["kind"] == "followup"]
    assert {i["title"] for i in items} == {"Follow-up to Maya Okafor", "Follow-up to Bo Li"}
    assert "step 2 of 3" in items[0]["detail"] and "Hi Maya" in next(i for i in items if "Maya" in i["title"])["draft"]
    res = call(server, "list_followups")
    assert "2 follow-ups due" in text(res) and "Hi Bo, following up on my note about the ML Engineer role at Northwind" in text(res)
    # They reply: their follow-ups end, in the reply and on Today.
    res = call(server, "log_activity", candidate_id=maya["id"], kind="reply", summary="Interested!", job_id=job["id"])
    assert text(res).startswith('Logged a reply from "Maya Okafor"') and "follow-ups for this job stop" in text(res)
    assert [p["name"] for p in outreach.due(store)] == ["Bo Li"]
    # "I sent it" on Today logs the follow-up; the last step comes due later.
    c = local_client(store)
    page = c.get("/").text
    assert "Follow-up due" in page and "I sent it" in page and "Copy text" in page
    c.post(f"/jobs/{job['id']}/followups/{bo['id']}/sent")
    assert outreach.due(store) == []
    later = outreach.due(store, days_ahead=7)
    assert later[0]["label"] == "Last follow-up" and later[0]["step"] == 3
    # Moving past Contacted ends it too.
    store.move_stage(bo["id"], job["id"], "Screening", by="app")
    assert next(p for p in outreach.progress(store, job["id"]) if p["name"] == "Bo Li")["status"] == "moved_on"
    jp = c.get(f"/jobs/{job['id']}").text
    assert "Follow-ups" in jp and "replied" in jp and "moved on to Screening" in jp


def test_sequence_form_saves_and_checks_the_steps(store):
    job = store.add_job("ML Engineer", company="Northwind")
    c = local_client(store)
    page = c.get(f"/jobs/{job['id']}").text
    assert "Set up follow-ups" in page and "—" not in page
    form = {"label_0": "InMail", "day_0": "0", "text_0": "Hi {first_name}", "label_1": "Nudge", "day_1": "4",
            "text_1": "Following up", "label_2": "", "day_2": "", "text_2": ""}
    r = c.post(f"/jobs/{job['id']}/sequence", data=form, follow_redirects=False)
    assert "Saved 2 steps" in flash_of(r)
    assert [s["label"] for s in outreach.sequence(store, job["id"])["steps"]] == ["InMail", "Nudge"]
    r = c.post(f"/jobs/{job['id']}/sequence", data={**form, "day_1": "0"}, follow_redirects=False)
    assert "later day" in flash_of(r, "flash_error")
    r = c.post(f"/jobs/{job['id']}/sequence", data={"remove": "1"}, follow_redirects=False)
    assert "off for this job" in flash_of(r) and outreach.sequence(store, job["id"]) is None
    res = call(build_server(store), "list_followups", job_id=job["id"])
    assert "has no follow-up sequence" in text(res)


def test_people_contacted_before_the_sequence_dont_get_follow_ups(store):
    job = store.add_job("ML Engineer", company="Northwind")
    ana = store.add_candidate(name="Ana Ruiz")
    store.add_to_pipeline(ana["id"], job["id"])
    store.log(ana["id"], "message", "Old InMail", at=iso(datetime.now(timezone.utc) - timedelta(days=20)),
              job_id=job["id"])
    _with_sequence(store, job["id"], days_ago=10)
    assert outreach.progress(store, job["id"]) == []


# ------------------------------------------------------------ 5. paste
def test_paste_profile_with_link_is_linkedin_and_about_is_only_about(store):
    from talent import importers

    got = importers.quick_parse("Maya Okafor\nSenior ML Engineer at Ledgerline\nSan Francisco, CA\n"
                                "https://www.linkedin.com/in/maya-okafor-ml/?utm_source=share\nAbout\n"
                                "I build ranking systems.\nExperience\nSenior ML Engineer\nLedgerline\n2021 - Present")
    assert got["source"] == "LinkedIn" and got["about"] == "I build ranking systems."
    bare = importers.quick_parse("https://www.linkedin.com/in/dev-raman/")
    assert bare["source"] == "LinkedIn" and "about" not in bare
    dev = store.add_candidate(name="Dev Raman", linkedin_url="https://linkedin.com/in/dev-raman")
    r = local_client(store).post("/paste", data={"text": "https://www.linkedin.com/in/dev-raman/?utm_source=share"},
                                 follow_redirects=False)
    assert r.headers["location"].startswith(f"/candidates/{dev['id']}") and "Already saved: Dev Raman" in flash_of(r)


def test_card_keep_all_and_pass_all_are_ai_actions_undo_covers():
    """Merge of cycle 6 A and D: Keep all and Pass all on the card are logged as AI actions, so Undo covers them."""
    from talent.store import Store
    import tempfile, os
    store = Store(os.path.join(tempfile.mkdtemp(), "t.db"))
    job = store.add_job("ML Engineer", company="Northwind")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=_people("Ana Ruiz", "Bo Li", "Cy Park"))
    pids = {p["name"]: p["id"] for p in res.structuredContent["added"]}
    key = key_of(server, res)
    call(server, "keep_prospects", job_id=job["id"], prospect_ids=[pids["Ana Ruiz"], pids["Bo Li"]], view_key=key)
    call(server, "pass_prospects", job_id=job["id"], prospect_ids=[pids["Cy Park"]], view_key=key)
    acts = {a["kind"]: a for a in store.ai_actions() if a["kind"] in ("card_keep", "card_pass")}
    assert set(acts) == {"card_keep", "card_pass"}
    assert "Kept 2 people from the Shortlist card" in local_client(store).get("/changes").text
    store.undo_ai_action(acts["card_keep"]["id"])
    store.undo_ai_action(acts["card_pass"]["id"])
    assert shortlist.counts(store, job["id"]) == {**shortlist.counts(store, job["id"]), "kept": 0, "passed": 0}
