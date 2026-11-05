"""Cycle 5, fixer E: sourcing fixes (last contacted, name dedupe, fuller Shortlist rows, intro emails) and Keep and
Pass on the in-chat Shortlist card."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import importers, shortlist
from talent.mcp_server import APP_ONLY, NOT_FROM_VIEW, VIEW_KEY, build_server
from talent.mcp_views import view_html
from talent.store import looks_like_name

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(res):
    return "\n".join(c.text for c in res.content)


def iso(dt):
    return dt.astimezone(timezone.utc).isoformat(timespec="seconds")


def key_of(res):
    return res.meta[VIEW_KEY]["key"]


# ------------------------------------------------------------ 1. last contacted
def test_moving_to_contacted_keeps_the_logged_outreach_date(store):
    job = store.add_job("Backend", company="Ledgerly")
    rafael = store.add_candidate(name="Rafael Gomez")
    store.add_to_pipeline(rafael["id"], job["id"])
    yesterday = iso(datetime.now(timezone.utc) - timedelta(days=1))
    server = build_server(store)
    call(server, "log_activity", candidate_id=rafael["id"], kind="inmail", summary="InMail about Ledgerly",
         when=yesterday, job_id=job["id"])
    call(server, "move_stage", candidate_id=rafael["id"], job_id=job["id"], stage="Contacted")
    assert store.get_candidate(rafael["id"])["last_contacted_at"] == yesterday


def test_adding_straight_into_contacted_stamps_last_contacted_once(store):
    job = store.add_job("Backend")
    ayesha = store.add_candidate(name="Ayesha Khan")
    store.add_to_pipeline(ayesha["id"], job["id"], "Contacted")
    stamped = store.get_candidate(ayesha["id"])["last_contacted_at"]
    assert stamped
    # The real outreach, logged after the move with an earlier time, replaces the move's stamp.
    earlier = iso(datetime.now(timezone.utc) - timedelta(days=2))
    store.log(ayesha["id"], "email", "Sent the role", at=earlier)
    assert store.get_candidate(ayesha["id"])["last_contacted_at"] == earlier
    # Moving out and back in again doesn't stamp over logged outreach.
    store.move_stage(ayesha["id"], job["id"], "Sourced")
    store.move_stage(ayesha["id"], job["id"], "Contacted")
    assert store.get_candidate(ayesha["id"])["last_contacted_at"] == earlier


def test_an_old_note_does_not_replace_a_stamp_and_real_outreach_still_wins(store):
    job = store.add_job("Backend")
    bo = store.add_candidate(name="Bo Li")
    store.add_to_pipeline(bo["id"], job["id"])
    store.move_stage(bo["id"], job["id"], "Contacted")
    stamped = store.get_candidate(bo["id"])["last_contacted_at"]
    store.log(bo["id"], "note", "Looks strong", at=iso(datetime.now(timezone.utc) - timedelta(days=3)))
    assert store.get_candidate(bo["id"])["last_contacted_at"] == stamped


# ------------------------------------------------------------- 2. name dedupe
def test_a_name_search_is_never_spell_corrected(store):
    store.add_candidate(name="Maya Okafor", current_title="Data Engineer", about="Data pipelines and data tools.")
    res = store.search_candidates("Dana Okafor")
    assert not res.corrected and res.suggestion is None and res.exact_total == 0
    low = store.search_candidates("dana okafor")
    assert not low.corrected and low.exact_total == 0
    # Ordinary typos are still fixed.
    assert store.search_candidates("pipelnes").corrected
    assert looks_like_name("Dana Okafor") and looks_like_name("Mary-Kate O'Neil")
    assert not looks_like_name("python") and not looks_like_name("Senior SRE Python Golang Kubernetes")


def test_the_dedupe_reply_says_no_one_by_that_name_is_saved(store):
    store.add_candidate(name="Maya Okafor", current_title="Data Engineer", about="Dana Point surfer.")
    server = build_server(store)
    res = call(server, "search_candidates", query="Dana Okafor")
    t = text(res)
    assert t.startswith('No one named "Dana Okafor" is saved.') and "1 match" not in t and "data Okafor" not in t
    assert res.structuredContent["no_one_named"] == "Dana Okafor"
    assert res.structuredContent["results"][0]["other_name"] is True
    store.add_candidate(name="Dana Okafor")
    t = text(call(server, "search_candidates", query="Dana Okafor"))
    assert "No one named" not in t and "- Dana Okafor" in t and "Maya Okafor" in t and "(a different name)" in t
    assert text(call(server, "search_candidates", query="Zed Quorn")) == 'No one named "Zed Quorn" is saved.'
    assert "Did you mean" not in text(call(server, "search_candidates", query="Dana Okafor"))


# ---------------------------------------------- 3. fuller Shortlist rows and Keep
def test_propose_carries_about_skills_and_history_and_keep_saves_them(store):
    job = store.add_job("Backend", company="Ledgerly")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=[{
        "name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana-ruiz", "title": "Staff Engineer",
        "company": "Plaid", "fit": "Go payments", "about": "Built Go payment rails at Plaid.",
        "skills": ["Go", "Payments"], "work_history": [{"title": "Staff Engineer", "company": "Plaid", "start": "2022"}]}])
    row = res.structuredContent["added"][0]
    assert row["tags"] == ["Go", "Payments"] and row["about"].startswith("Built Go")
    assert "Skills: Go, Payments" in text(res)
    listed = call(server, "list_prospects", job_id=job["id"]).structuredContent["prospects"][0]
    assert listed["work_history"][0]["company"] == "Plaid"
    done = shortlist.keep(store, job["id"], row["id"])
    c = store.get_candidate(done["candidate"]["id"])
    assert c["about"] == "Built Go payment rails at Plaid." and "Payments" in c["tags"]
    assert store.work_history(c["id"])[0]["company"] == "Plaid"
    assert [h.candidate["name"] for h in store.search_candidates("payments").hits] == ["Ana Ruiz"]


def test_keep_linking_someone_saved_fills_their_blank_fields_and_undo_puts_them_back(store):
    job = store.add_job("Backend", company="Ledgerly")
    other = store.add_job("Platform", company="Lumen")
    ayesha = store.add_candidate(name="Ayesha Khan", linkedin_url="https://linkedin.com/in/ayesha-khan",
                                 current_title="SRE")
    store.add_to_pipeline(ayesha["id"], other["id"], "Screening")
    res = shortlist.propose(store, job["id"], [{"name": "Ayesha Khan", "linkedin_url": ayesha["linkedin_url"],
                                                "title": "Staff SRE", "location": "Oakland", "tags": ["Kubernetes"]}])
    pid = res["added"][0]["id"]
    shortlist.keep(store, job["id"], pid)
    c = store.get_candidate(ayesha["id"])
    assert c["location"] == "Oakland" and c["current_title"] == "SRE" and "Kubernetes" in c["tags"]
    batch = shortlist.keep_batches(store)[0]
    shortlist.undo_keep(store, batch["id"])
    c = store.get_candidate(ayesha["id"])
    assert not c["location"] and not c["tags"]


def test_same_name_lines_name_that_persons_jobs(store):
    job = store.add_job("Backend", company="Ledgerly")
    acme = store.add_job("Payments", company="Acme")
    lee = store.add_candidate(name="Lee Chen", current_title="SWE")
    store.add_to_pipeline(lee["id"], acme["id"], "Screening")
    res = shortlist.propose(store, job["id"], [{"name": "Lee Chen", "linkedin_url": "https://linkedin.com/in/lee-c"}])
    lines = shortlist.check_lines(res["added"][0]["checks"])
    assert lines == [f"Same name as saved Lee Chen (id {lee['id']}), SWE. In Payments, Acme: Screening"]


def test_kept_list_shows_stage_and_refresh_for_thin_records(store):
    job = store.add_job("Backend")
    res = shortlist.propose(store, job["id"], [{"name": "Kim Ode", "linkedin_url": "https://linkedin.com/in/kim-ode"}])
    shortlist.keep(store, job["id"], res["added"][0]["id"])
    page = local_client(store).get(f"/jobs/{job['id']}/shortlist").text
    assert "· Sourced" in page and "Refresh with Claude" in page


# ------------------------------------------------------------- 4. intro emails
INTRO = """From: Jen Park <jen@parkventures.vc>
To: Sam Rivera <sam@loomwork.io>
Subject: Intro: Priya Natarajan <> Sam (Loomwork)

Hi Sam,

I'd love for you to meet Priya Natarajan (priya.n@gmail.com). She led payments at Stripe.

Priya, meet Sam, cofounder of Loomwork.

Jen
parkventures.vc
"""


def test_paste_reads_an_intro_email(store):
    got = importers.quick_parse(INTRO)
    assert got["name"] == "Priya Natarajan" and got["email"] == "priya.n@gmail.com"
    assert got["source"] == "Referral from Jen Park" and "website" not in got
    fwd = ("---------- Forwarded message ---------\nFrom: Jen Park <jen@parkventures.vc>\nSubject: Intro to Tomas\n"
           "To: Sam Rivera <sam@loomwork.io>\nCc: Tomas Reyes <tomas@proton.me>\n\n"
           "Sam, wanted to introduce you to Tomas, a great backend engineer.\n")
    got = importers.quick_parse(fwd)
    assert got["name"] == "Tomas Reyes" and got["email"] == "tomas@proton.me"
    page = local_client(store).post("/paste", data={"text": INTRO}).text
    assert "Priya Natarajan" in page and "priya.n@gmail.com" in page and "Referral from Jen Park" in page
    assert 'value="jen@parkventures.vc"' not in page
    # A plain email that isn't an intro reads as before.
    assert importers.quick_parse("From: Ana Ruiz <ana@x.com>\nSubject: Application\n\nHi, my resume.")["email"] \
        == "ana@x.com"


# --------------------------------------------- 5. Keep and Pass on the chat card
def test_keep_and_pass_tools_are_app_only_and_refuse_the_model(store):
    job = store.add_job("Backend", company="Ledgerly")
    server = build_server(store)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    for name in ("keep_prospect", "pass_prospect"):
        assert tools[name].meta == APP_ONLY and "Calls from the model" in tools[name].description
    res = call(server, "propose_candidates", job_id=job["id"], people=[
        {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana-ruiz"}])
    pid = res.structuredContent["added"][0]["id"]
    assert res.meta[VIEW_KEY]["job_id"] == job["id"] and key_of(res) not in text(res)
    assert key_of(res) not in str(res.structuredContent)
    # Refusals come back as plain error results (cycle 5 re-review: no "Error executing tool" prefix on the card).
    for args, says in (({}, "Only the recruiter keeps or passes"), ({"view_key": "guess"}, "earlier Claude session"),
                       ({"view_key": key_of(res) + "x"}, "earlier Claude session")):
        for tool in ("keep_prospect", "pass_prospect"):
            out = call(server, tool, job_id=job["id"], prospect_id=pid, **args)
            assert out.isError and says in text(out) and not text(out).startswith("Error")
    assert shortlist.counts(store, job["id"])["proposed"] == 1 and store.count_candidates() == 0
    assert "Nothing changed" in NOT_FROM_VIEW
    # A key is per job. Since the cycle 5 re-review it survives a restart (a new server takes it).
    other = store.add_job("Platform")
    other_key = key_of(call(server, "list_prospects", job_id=other["id"]))
    assert call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=other_key).isError
    assert not call(build_server(store), "keep_prospect", job_id=job["id"], prospect_id=pid,
                    view_key=key_of(res)).isError


def test_a_keep_from_the_card_is_logged_in_claude_and_undoable(store):
    job = store.add_job("Backend", company="Ledgerly")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=[
        {"name": "Ana Ruiz", "linkedin_url": "https://linkedin.com/in/ana-ruiz", "skills": ["Go"]}])
    pid = res.structuredContent["added"][0]["id"]
    out = call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(res))
    d = out.structuredContent
    assert d["prospect"]["status"] == "kept" and d["counts"]["kept"] == 1
    assert d["message"].startswith('Saved "Ana Ruiz" and added them to Sourced on Backend (Ledgerly).')
    batch = shortlist.keep_batches(store)[0]
    assert batch["text"].endswith(", in Claude") and batch["kept"][0]["via"] == "claude"
    c = local_client(store)
    assert "in Claude" in c.get("/changes").text
    r = c.post(f"/changes/shortlist-keep/{batch['id']}/undo", follow_redirects=False)
    assert r.status_code == 303 and "Undid the keep" in flash_of(r)
    assert shortlist.counts(store, job["id"])["proposed"] == 1
    call(server, "keep_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(res))
    again = call(server, "pass_prospect", job_id=job["id"], prospect_id=pid, view_key=key_of(res))
    assert again.isError and "already kept" in text(again)


def test_card_keep_asks_for_a_choice_then_keeps_as_the_chosen_person_or_passes(store):
    job = store.add_job("Backend")
    lee = store.add_candidate(name="Lee Chen", current_title="SWE")
    server = build_server(store)
    res = call(server, "propose_candidates", job_id=job["id"], people=[
        {"name": "Lee Chen", "linkedin_url": "https://linkedin.com/in/lee-c", "location": "Austin"},
        {"name": "Bo Li", "linkedin_url": "https://linkedin.com/in/bo-li"}])
    key = key_of(res)
    lee_pid, bo_pid = (a["id"] for a in res.structuredContent["added"])
    ask = call(server, "keep_prospect", job_id=job["id"], prospect_id=lee_pid, view_key=key).structuredContent
    assert ask["needs_choice"] and ask["matches"][0]["id"] == lee["id"] and ask["prospect"]["status"] == "proposed"
    done = call(server, "keep_prospect", job_id=job["id"], prospect_id=lee_pid, view_key=key,
                candidate_id=lee["id"]).structuredContent
    assert done["candidate_id"] == lee["id"] and store.count_candidates() == 1
    assert store.get_candidate(lee["id"])["location"] == "Austin"
    passed = call(server, "pass_prospect", job_id=job["id"], prospect_id=bo_pid, view_key=key, reason="Too junior")
    assert passed.structuredContent["prospect"]["pass_reason"] == "Too junior"
    assert text(passed).startswith('Passed on "Bo Li".')
    again = call(server, "pass_prospect", job_id=job["id"], prospect_id=bo_pid, view_key=key)
    assert again.isError and text(again) == "Bo Li was already passed."


def test_the_shortlist_card_has_buttons_only_when_the_host_can_call_tools():
    html = view_html("shortlist")
    for bit in ("keep_prospect", "pass_prospect", "Someone new", "Same as ", "Reason (optional)",  # c6 labels
                "serverTools", "talent-search/shortlist", "Review in Talent Search"):
        assert bit in html, bit
    assert "innerHTML" not in html and "—" not in html and "–" not in html
    assert "No one named" in view_html("results")


# ------------------------------------------------- 6. aliases and plain replies
def test_log_activity_aliases_and_plain_sentences(store):
    job = store.add_job("Backend", company="Ledgerly")
    ana = store.add_candidate(name="Ana Ruiz")
    server = build_server(store)
    for kind in ("inmail", "LinkedIn", "DM", "linkedin_message"):
        out = call(server, "log_activity", candidate_id=ana["id"], kind=kind, summary=f"Sent a {kind}")
        assert out.structuredContent["logged"]["kind"] == "message"
    with pytest.raises(Exception, match="InMail, LinkedIn message or DM is a message"):
        store.log(ana["id"], "fax", "x")
    made = call(server, "create_job", title="Platform", company="Lumen")
    assert text(made).startswith("Created Platform (Lumen)") and "{" not in text(made)
    assert made.structuredContent["id"]
    call(server, "add_to_pipeline", candidate_id=ana["id"], job_id=job["id"])
    moved = call(server, "move_stage", candidate_id=ana["id"], job_id=job["id"], stage="Screening")
    assert text(moved) == 'Moved "Ana Ruiz" to Screening on Backend (Ledgerly) (was Sourced).'
    assert moved.structuredContent["stage"] == "Screening"
    todo = call(server, "add_suggestion", text="Follow up with Ana", candidate_id=ana["id"], job_id=job["id"])
    assert text(todo) == ('Added a to-do to the recruiter\'s Today list: "Follow up with Ana", for "Ana Ruiz", on '
                          'Backend (Ledgerly). Written by your AI app.')
    assert todo.structuredContent["candidate_id"] == ana["id"]
