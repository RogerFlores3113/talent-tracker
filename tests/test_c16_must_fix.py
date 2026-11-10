"""Cycle 16 must-fix bugs from the cycle 15 panel.

1. job_pipeline's paste-ready status block named rejected people and carried local app links.
2. An import preview reset the zone times show in to this computer's (UTC) while Settings still showed the
   recruiter's own zone.
"""
import anyio

from talent import reports
from talent.importers import preview_import
from talent.mcp_server import build_server
from talent.sample_data import load_sample_data
from talent.store import shown_zone


def _blocks(server, tool, **args):
    res = anyio.run(lambda: server.call_tool(tool, args))
    content = res.content if hasattr(res, "content") else res[0]
    return [c.text for c in content]


def test_job_pipeline_paste_block_matches_copy_status_update(store):
    from talent import actions
    load_sample_data(store)
    server = build_server(store)
    for job in store.list_jobs():
        blocks = _blocks(server, "job_pipeline", job_id=job["id"])
        paste = blocks[0]
        assert "127.0.0.1" not in paste and "http" not in paste
        assert "Rejected" not in paste and "Withdrawn" not in paste
        # The same text the job page's Copy status update gives when nobody chose what to leave out.
        page = actions.job_status_text(store, job["id"], omit_stages=actions.page_omit(store, job["id"]))
        assert paste == page
        # The private blocks still carry the link to open the job.
        assert any("127.0.0.1" in b for b in blocks[1:])


def test_job_pipeline_still_includes_rejected_when_asked(store):
    load_sample_data(store)
    server = build_server(store)
    job_id = store.list_jobs()[0]["id"]
    paste = _blocks(server, "job_pipeline", job_id=job_id, omit_stages=[])[0]
    assert "Rejected" in paste and "127.0.0.1" not in paste


def test_import_preview_keeps_the_saved_time_zone(store):
    reports.save_user_zone(store, "America/Los_Angeles")
    assert getattr(shown_zone(), "key", None) == "America/Los_Angeles"
    preview_import(store, ["name", "email"], [["Zed Preview", "zed@example.com"]], ["name", "email"])
    assert getattr(shown_zone(), "key", None) == "America/Los_Angeles"


def test_csv_import_through_the_app_keeps_the_saved_time_zone(store, client):
    reports.save_user_zone(store, "America/Los_Angeles")
    csv = b"name,email\nZed Import,zed@example.com\n"
    r = client.post("/import", files={"file": ("people.csv", csv, "text/csv")})
    assert r.status_code == 200
    assert getattr(shown_zone(), "key", None) == "America/Los_Angeles"
    page = client.get("/interviews").text
    assert "Times are in UTC" not in page
