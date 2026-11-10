"""c11 J: views for jobs, saved searches, follow-ups, status updates and client reports."""
import re
from datetime import datetime, timedelta, timezone

import anyio
from mcp.types import CallToolResult

from talent.mcp_server import HIDDEN_LEAD, build_server
from talent.mcp_views import URIS, tool_meta, versioned_uri, view_html


def call(server, tool, **args):
    return anyio.run(lambda: server.call_tool(tool, args))


def sc(res):
    return res.structuredContent if isinstance(res, CallToolResult) else res[1].get("result", res[1])


def txt(res):
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def tools(server):
    return {t.name: t for t in anyio.run(server.list_tools)}


def test_each_tool_points_at_its_view(store):
    t = tools(build_server(store))
    for name, view in (("list_jobs", "jobs"), ("list_saved_searches", "searches"), ("list_followups", "followups"),
                       ("status_digest", "digest"), ("client_report", "digest")):
        assert t[name].meta["ui"]["resourceUri"] == versioned_uri(view), name
        assert tool_meta(view)["ui/resourceUri"] == versioned_uri(view)


def test_the_new_views_are_clean_pages():
    for name in ("jobs", "searches", "followups", "digest"):
        html = view_html(name)
        assert not re.findall(r"""(?:src|href)\s*=\s*['"]?https?://""", html), name
        assert "innerHTML" not in html and "fetch(" not in html and "—" not in html and "–" not in html, name
        assert "ui/initialize" in html
    assert "renderPeople" in view_html("searches") and "personCard" in view_html("searches")
    assert "copyText" in view_html("digest") and "askClaude" in view_html("followups")


def _job(store):
    job = store.add_job("Staff Backend", "Acme", None, "d")
    c = store.add_candidate(name="Dana Okafor")["id"]
    store.add_to_pipeline(c, job["id"], "Screening")
    return job, c


def test_list_jobs_text_is_headed_plain_lines_with_links(store):
    job, _ = _job(store)
    res = call(build_server(store), "list_jobs")
    lines = [l for l in txt(res).splitlines() if not l.startswith("Note:")]
    assert lines[0] == "1 open job:"
    assert lines[1].startswith("- Staff Backend (Acme) (job id")
    assert f"/jobs/{job['id']}" in txt(res) and not any(l.startswith("[{") for l in lines)
    j = sc(res)["jobs"][0]
    assert j["app_url"].endswith(f"/jobs/{job['id']}") and j["stage_counts"]["Screening"] == 1 and j["active_count"] == 1


def test_list_jobs_has_next_interview(store):
    job, c = _job(store)
    at = datetime.now(timezone.utc) + timedelta(days=2)
    store.log(c, "interview", "Phone screen", at=at, job_id=job["id"])
    res = call(build_server(store), "list_jobs")
    assert sc(res)["jobs"][0]["next_interview"] and "Next interview:" in txt(res)


def test_saved_searches_carry_link_color_and_last_opened(store):
    store.add_candidate(name="Ana Python", current_title="Python dev")
    row = store.save_search("Pythonistas", {"q": "python", "color": "blue"}, by="app")
    res = call(build_server(store), "list_saved_searches")
    s = sc(res)["saved_searches"][0]
    assert s["color"] == "blue" and s["app_url"].endswith(f"/saved-searches/{row['id']}") and "last_opened" in s
    assert "Pythonistas" in txt(res) and s["app_url"] in txt(res)


def test_followups_carry_links(store):
    job = store.add_job("Designer", "Studio", None, "d")
    server = build_server(store)
    c = store.add_candidate(name="Rae Lin")["id"]
    store.add_to_pipeline(c, job["id"], "Contacted")
    call(server, "log_activity", candidate_id=c, kind="message", summary="Hello", job_id=job["id"],
         when=(datetime.now(timezone.utc) - timedelta(days=6)).isoformat())
    call(server, "set_followup_sequence", job_id=job["id"], enroll_existing=True, enroll_from="first_message", steps=[
        {"label": "First message", "day": 0, "text": "Hi {first_name}"}, {"label": "Nudge", "day": 2, "text": "Hi again"}])
    res = call(server, "list_followups")
    f = sc(res)["followups"][0]
    assert f["candidate_url"].endswith(f"/candidates/{c}") and f["app_url"].endswith(f"/jobs/{job['id']}/outreach")
    assert "Rae Lin" in txt(res) and f["draft"]


def test_status_digest_gives_the_view_the_client_safe_text_apart_from_notes(store):
    job, c = _job(store)
    store.add_to_pipeline(store.add_candidate(name="Sam Sourced")["id"], job["id"], "Sourced")
    res = call(build_server(store), "status_digest")
    d = sc(res)
    assert d["update_text"] and "Staff Backend" in d["update_text"]
    assert HIDDEN_LEAD not in d["update_text"] and not any(n in d["update_text"] for n in d["recruiter_notes"])
    assert d["recruiter_notes"] and "Sourced" in " ".join(d["recruiter_notes"])
    assert d["job_links"][0]["app_url"].endswith(f"/jobs/{job['id']}")
    assert d["update_text"] in txt(res)  # text parity: the chat text still holds the whole update


def test_client_report_gives_the_view_updates_and_notes(store):
    job, c = _job(store)
    store.add_to_pipeline(store.add_candidate(name="Sam Sourced")["id"], job["id"], "Sourced")
    res = call(build_server(store), "client_report", client="acme")
    d = sc(res)
    assert d["updates"] and "Acme" in d["updates"][0]["text"] and HIDDEN_LEAD not in d["updates"][0]["text"]
    assert isinstance(d["recruiter_notes"], list) and d["job_links"][0]["app_url"].endswith(f"/jobs/{job['id']}")
    assert d["updates"][0]["text"] in txt(res)


def test_digest_view_copies_only_the_update_text():
    js = view_html("digest")
    assert "copyText(u.text" in js and "recruiter_notes" in js and "For you, not in the update" in js
    assert js.index("copyText(u.text") < js.index("recruiter_notes")
