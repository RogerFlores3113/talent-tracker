"""c11 W: in-chat view fixes. Server fields the views read (stage_rank, time_zone, recruiter_note, next_stage) and the
shape of the view code (copy fallback, confirm before send, capability checks, no pick that sends at once)."""
import re

import anyio
from mcp.types import CallToolResult

from talent import reports
from talent.mcp_server import build_server
from talent.mcp_views import _DIR, view_html


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def test_search_results_carry_stage_rank_in_pipeline_order_with_exits_last(store):
    job = store.add_job("Staff Backend", "Acme")
    stages = store.job_stages(job["id"])
    names = [s["name"] for s in stages]
    out = [s for s in stages if s["outcome"] == "lost"][0]["name"]
    a = store.add_candidate(name="Ana Ruiz", email="ana@x.com")
    b = store.add_candidate(name="Bo Lee", email="bo@x.com")
    c = store.add_candidate(name="Cy Fox", email="cy@x.com")
    store.add_to_pipeline(a["id"], job["id"], names[1])
    store.add_to_pipeline(b["id"], job["id"], names[0])
    store.add_to_pipeline(c["id"], job["id"], out)
    res = data(call(build_server(store), "search_candidates", query="", limit=10))
    rank = {p["name"]: p["stage_rank"] for p in res["results"]}
    assert rank["Bo Lee"] < rank["Ana Ruiz"] < rank["Cy Fox"]
    assert rank["Cy Fox"] >= 1000
    assert all(isinstance(j["stage_rank"], int) for p in res["results"] for j in p["jobs"])


def test_view_results_say_the_saved_time_zone(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    server = build_server(store)
    store.add_candidate(name="Ana Ruiz", email="ana@x.com")
    res = data(call(server, "search_candidates", query="", limit=5))
    assert res["time_zone"] == "America/Los_Angeles"


def test_no_saved_zone_adds_no_time_zone(store):
    server = build_server(store)
    store.add_candidate(name="Ana Ruiz", email="ana@x.com")
    assert "time_zone" not in data(call(server, "search_candidates", query="", limit=5))


def test_save_candidate_gives_the_recruiter_words_apart_from_the_ai_words(store):
    store.add_candidate(name="Ana Ruiz", email="ana@x.com")
    res = data(call(build_server(store), "save_candidate", name="Ana Ruiz"))
    assert res["needs_confirmation"]
    assert "update_candidate" in res["warning"] and "create_anyway" in res["warning"]
    note = res["recruiter_note"]
    assert "update_candidate" not in note and "create_anyway" not in note and "Ana Ruiz" in note


def test_finalists_say_the_next_stage(store):
    job = store.add_job("Staff Backend", "Acme")
    names = [s["name"] for s in store.job_stages(job["id"])]
    a = store.add_candidate(name="Ana Ruiz", email="ana@x.com")
    store.add_to_pipeline(a["id"], job["id"], names[2])
    res = data(call(build_server(store), "compare_finalists", job_id=job["id"]))
    for p in res["people"]:
        assert p["next_stage"] == names[3]


def js(name):
    return (_DIR / name).read_text(encoding="utf-8")


def test_copy_falls_back_to_a_selected_box_with_a_reason():
    kit = js("kit.js")
    assert "The chat app blocked copying" in kit and "readonly" in kit and "f.select()" in kit
    assert "copyText(text,node,status,okText)" in kit
    # callers no longer invent their own failure words
    for f in ("digest.js", "followups.js", "owed.js"):
        assert "Couldn't copy" not in js(f)


def test_a_pick_never_sends_on_change():
    pipe = js("pipeline.js")
    assert 'sel.addEventListener("change",()=>{go.disabled=!sel.value})' in pipe
    assert "confirmSend(" in pipe and "Claude makes the move when you send this." in pipe
    assert "confirmSend(" in js("decide.js") and "Yes, advance" in js("decide.js")
    assert "confirmSend(" in js("today.js")


def test_people_table_has_the_new_columns_and_selection_words():
    kit = js("kit.js")
    for word in ('k:"job"', 'k:"email"', "Has an email", "hidden by the filter", "stage_rank", "Type what you want Claude to do first."):
        assert word in kit, word
    assert "st.sel.clear()" in kit  # Select none clears the whole selection, not only the shown rows


def test_views_check_what_the_host_can_do_and_keep_the_result_when_cancelled():
    kit = js("kit.js")
    assert "function hostCan(" in kit and 'needs:"message"' in js("pipeline.js") + js("jobs.js")
    assert "What you see is from before." in kit and "if(DATA)" in kit
    assert "id='notice'" in view_html("people")


def test_carousel_is_focusable_with_a_hint_and_flips_tell_claude():
    p = js("people.js")
    assert 'tabindex:many?"0":null' in p and "Click here, then use the arrow keys" in p
    assert "candidate id" in p and "tellClaude(" in p
    assert "Back to table" in p


def test_every_refreshable_view_tool_is_listed():
    kit = js("kit.js")
    for t in ("list_jobs", "list_saved_searches", "list_interviews", "status_digest", "list_feedback_owed",
              "find_duplicates", "list_followups"):
        assert '"%s"' % t in kit
    for f, w in (("jobs.js", "refresh:false"), ("digest.js", "refresh:false"), ("owed.js", "refresh:false"),
                 ("followups.js", "refresh:false"), ("interviews.js", "refresh:false")):
        assert w not in js(f), f


def test_digest_titles_do_not_strand_the_client_name():
    assert ".preview h2 button.link" in js("kit.css") and "display:inline" in js("kit.css")


def test_no_markup_from_data_and_no_dashes_in_new_wording():
    for name in ("people", "pipeline", "decide", "interviews", "saved", "duplicates", "shortlist", "today"):
        h = view_html(name)
        assert "innerHTML" not in h and "—" not in h and "–" not in h, name
