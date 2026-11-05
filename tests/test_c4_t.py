"""Cycle 4, fixer T: clients and client reports, Mark as sent, client_report for the AI, submittal notes."""
import csv
import io
import json
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.types import CallToolResult

from talent import actions, reports
from talent import store as store_mod
from talent.mcp_server import build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def text(result):
    content = result.content if isinstance(result, CallToolResult) else result[0]
    return "\n".join(c.text for c in content)


def _iso(dt):
    return dt.astimezone(timezone.utc).replace(microsecond=0).isoformat()


def _ago(days):
    return _iso(datetime.now(timezone.utc) - timedelta(days=days))


def _setup(store):
    """Acme with two jobs (spelled two ways), people in several stages, and Globex with one job."""
    be = store.add_job("Backend Engineer", company="Acme, Inc.", stages=actions.stage_set("agency"))
    data = store.add_job("Data Engineer", company="acme")
    gx = store.add_job("Designer", company="Globex")
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    bo = store.add_candidate(name="Bo Li")["id"]
    cy = store.add_candidate(name="Cy Park")["id"]
    store.add_to_pipeline(ana, be["id"], "Sourced")
    store.move_stage(ana, be["id"], "Submitted", by="app")
    store.add_to_pipeline(bo, be["id"], "Sourced")  # internal: never named or counted for the client
    store.add_to_pipeline(cy, data["id"], "Interviewing")
    return be, data, gx, ana, bo, cy


def _set_stage_times(store, cid, job_id, *days_ago):
    """Give a person's stage entries on a job these ages (oldest first), and their application the last one."""
    rows = store._rows("SELECT id FROM timeline WHERE candidate_id = ? AND job_id = ? AND kind = 'stage' ORDER BY id",
                       (cid, job_id))
    with store._lock, store._conn:
        for r, d in zip(rows, days_ago):
            store._conn.execute("UPDATE timeline SET at = ? WHERE id = ?", (_ago(d), r["id"]))
        store._conn.execute("UPDATE applications SET stage_changed_at = ? WHERE candidate_id = ? AND job_id = ?",
                            (_ago(days_ago[-1]), cid, job_id))


# ------------------------------------------------------------- 1. the table
def test_v8_adds_clients_and_updates_and_rows_are_made_only_on_save(store):
    assert store.schema_version == len(store_mod._MIGRATIONS)
    tables = {r["name"] for r in store._rows("SELECT name FROM sqlite_master WHERE type = 'table'")}
    assert {"clients", "client_updates"} <= tables
    _setup(store)
    c = local_client(store)
    assert c.get("/clients").status_code == 200 and c.get("/clients/acme").status_code == 200
    assert store._rows("SELECT * FROM clients") == []  # opening pages changes nothing
    reports.save_client(store, "Acme, Inc.", notes="Prefers Friday calls")
    reports.save_client(store, "ACME", contacts="Pat Lee <pat@acme.example>")
    rows = store._rows("SELECT * FROM clients")
    assert len(rows) == 1 and rows[0]["key"] == "acme" and rows[0]["notes"] == "Prefers Friday calls"
    with pytest.raises(Exception):  # the key is unique in the database too
        with store._conn:
            store._conn.execute("INSERT INTO clients(name, key, created_at, updated_at) VALUES ('x', 'acme', '', '')")


# ------------------------------------------------------------ 2. the pages
def test_clients_page_has_one_row_per_client_with_counts(store):
    be, data, gx, ana, bo, cy = _setup(store)
    store.log(cy, "interview", "Panel", at=_iso(datetime.now(timezone.utc) + timedelta(days=2)), job_id=data["id"])
    store.add_job("No company job")
    c = local_client(store)
    page = c.get("/clients").text
    assert 'href="/clients"' in c.get("/").text
    assert page.count('href="/clients/acme"') == 1 and 'href="/clients/globex"' in page
    acme = next(r for r in reports.overview(store) if r["key"] == "acme")
    assert (acme["open_jobs"], acme["in_play"], acme["interviews"], acme["last_update_sent_at"]) == (2, 3, 1, None)
    assert "1 job with no company" in page
    for path in ("/clients", "/clients/acme", "/clients/globex"):
        body = c.get(path).text
        assert "—" not in body and "–" not in body, path
    assert c.get("/clients/nobody").status_code == 404
    actions.set_inhouse(store, True)
    assert ">Reports</a>" in c.get("/").text and "One row per company" in c.get("/clients").text


def test_client_page_shows_report_jobs_details_and_print_and_csv(store):
    be, data, gx, ana, bo, cy = _setup(store)
    c = local_client(store)
    body = c.get("/clients/acme").text
    assert "Backend Engineer" in body and "Data Engineer" in body and "Designer" not in body
    assert 'id="report-text"' in body and "data-print" in body and "Mark as sent" in body
    assert "mailto:" not in body  # no contact email yet
    assert "Not shown: Sourced (1)" in body and "Bo Li" not in body.split('id="report-text"')[1].split("</pre>")[0]
    r = c.get("/clients/acme/pipeline.csv")
    assert r.status_code == 200 and r.headers["content-type"].startswith("text/csv")
    assert "attachment" in r.headers["content-disposition"]
    rows = list(csv.DictReader(io.StringIO(r.text)))
    assert {(x["Candidate"], x["Stage group"]) for x in rows} == {("Ana Ruiz", "Submitted"), ("Cy Park", "Interviewing")}
    assert rows[0]["Last submitted"] or rows[1]["Last submitted"]
    # A formula-looking name can't run in a spreadsheet.
    store.update_candidate(ana, name="=HYPERLINK(1)")
    assert "'=HYPERLINK(1)" in c.get("/clients/acme/pipeline.csv").text


# ------------------------------------------------------------- 3. the report
def test_report_funnel_moves_medians_and_imports_left_out(store):
    be, data, gx, ana, bo, cy = _setup(store)
    _set_stage_times(store, ana, be["id"], 20, 3)  # 17 days in Sourced, 3 so far in Submitted
    store.move_stage(cy, data["id"], "Offer", by="app")
    _set_stage_times(store, cy, data["id"], 30, 1)  # 29 days interviewing
    # An imported placement this week, marked as an import: not a move the client made happen.
    dee = store.add_candidate(name="Dee Imported")["id"]
    store.add_to_pipeline(dee, be["id"], "Placed")
    with store._lock, store._conn:
        store._conn.execute("UPDATE timeline SET source = 'CSV import' WHERE candidate_id = ? AND kind = 'stage'", (dee,))
    # An AI move that was undone doesn't count either.
    store.move_stage(ana, be["id"], "Client interview", by="ai")
    store.mark_ai_stage(ana, be["id"], "Submitted")
    entry = store._row("SELECT id FROM timeline WHERE candidate_id = ? AND body = 'Client interview'", (ana,))
    action = store._row("SELECT id FROM ai_actions WHERE target_id = ?", (entry["id"],))
    store.undo_ai_action(action["id"])
    rep = reports.report(store, "acme")
    assert rep["range_from"] == "default" and rep["in_play"] == 2
    assert rep["stand"]["submitted"] == 1 and rep["stand"]["offer"] == 1
    assert rep["period"]["submitted"] == 1 and rep["period"]["offer"] == 1 and rep["period"]["hired"] == 0
    assert rep["period"]["interview"] == 0 and rep["imported_moves"] == 1
    assert [m["name"] for m in rep["moves"]] == ["Cy Park", "Ana Ruiz"]
    assert rep["reached"] == {"submitted": 3, "interview": 2, "offer": 2, "hired": 1}
    assert rep["median_days"]["interview"] == pytest.approx(29, abs=0.1)
    assert rep["median_days"]["submitted"] == pytest.approx(3, abs=0.1)
    out = reports.report_text(rep)
    assert out.startswith("acme: hiring update,") or out.startswith("Acme")
    assert "In process now: 2" in out and "- Offer: 1" in out
    assert "Moved into Submitted 1, Interviewing 0, Offer 1, Hired 0, Out 0" in out
    assert "Cy Park to Offer (Data Engineer)" in out and "Client interview" not in out
    assert "Median days in stage:" in out and "Interviewing 29" in out
    assert "Dee Imported" not in out.split("Moves this period:")[1]
    assert "—" not in out and "–" not in out


def test_upcoming_interviews_show_in_the_clients_time_zone(store):
    be, data, gx, ana, bo, cy = _setup(store)
    when = (datetime.now(timezone.utc) + timedelta(days=3)).replace(hour=19, minute=0, second=0, microsecond=0)
    store.log(ana, "interview", "Client call", at=_iso(when), job_id=be["id"])
    reports.save_client(store, "acme", time_zone="ET")
    assert reports.client(store, "acme")["time_zone"] == "America/New_York"
    out = reports.report_text(reports.report(store, "acme"))
    assert "Upcoming interviews:" in out
    line = next(l for l in out.splitlines() if "Ana Ruiz (Backend Engineer)" in l and l.startswith("- "))
    assert ("2:00 PM EST" in line) or ("3:00 PM EDT" in line)
    with pytest.raises(ValueError, match="time zone"):
        reports.save_client(store, "acme", time_zone="Mars/Olympus")
    r = local_client(store).post("/clients/acme", data={"time_zone": "Nowhere/Land"}, follow_redirects=False)
    assert "time zone" in flash_of(r, "flash_error")
    r = local_client(store).post("/clients/acme", data={"update_day": "friday", "contacts": "pat@acme.example"},
                                 follow_redirects=False)
    assert flash_of(r) == "Saved." and reports.client(store, "acme")["update_day"] == "Friday"


def test_a_chosen_range_and_a_bad_one(store):
    be, data, gx, ana, bo, cy = _setup(store)
    _set_stage_times(store, ana, be["id"], 40, 30)
    old = datetime.now() - timedelta(days=35)
    rep = reports.report(store, "acme", since=old.strftime("%Y-%m-%d"))
    assert rep["range_from"] == "chosen" and [m["name"] for m in rep["moves"]][:1] == ["Ana Ruiz"]
    assert "Ana Ruiz" not in [m["name"] for m in reports.report(store, "acme")["moves"]]
    c = local_client(store)
    body = c.get("/clients/acme", params={"since": "2026-12-01", "until": "2026-01-01"}).text
    assert "after its end" in body


# ------------------------------------------------------- 4. Mark as sent
def test_mark_as_sent_keeps_a_copy_and_the_next_update_starts_from_then(store):
    be, data, gx, ana, bo, cy = _setup(store)
    with store._lock, store._conn:  # the moves so far happened yesterday
        store._conn.execute("UPDATE timeline SET at = ?", (_ago(1),))
    c = local_client(store)
    r = c.post("/clients/acme/sent", data={"since": "", "until": ""}, follow_redirects=False)
    assert "Marked as sent" in flash_of(r)
    sent = reports.updates(store, "acme")
    assert len(sent) == 1 and "Ana Ruiz to Submitted" in sent[0]["body"]
    assert reports.client(store, "acme")["last_update_sent_at"] == sent[0]["sent_at"]
    rep = reports.report(store, "acme")
    assert rep["range_from"] == "last_update" and rep["moves"] == []
    out = reports.report_text(rep)
    assert "Changes since the last update on" in out and "Moves this period: none." in out
    body = c.get("/clients/acme").text
    assert "Starts when you last marked an update as sent" in body and "Take back Mark as sent" in body
    assert "never" not in c.get("/clients").text.split("acme")[1].split("</tr>")[0]
    # A move after the update shows in the next one.
    with store._lock, store._conn:  # the update went out a minute ago
        store._conn.execute("UPDATE clients SET last_update_sent_at = ?", (_ago(1 / 1440),))
    store.move_stage(cy, data["id"], "Offer", by="app")
    assert [m["name"] for m in reports.report(store, "acme")["moves"]] == ["Cy Park"]
    # Taking it back puts the range where it was.
    r = c.post(f"/clients/acme/updates/{sent[0]['id']}/undo", follow_redirects=False)
    assert "Took back" in flash_of(r)
    assert reports.updates(store, "acme") == [] and reports.client(store, "acme")["last_update_sent_at"] is None


def test_email_this_update_is_a_mailto_link_only_with_a_contact_email(store):
    _setup(store)
    reports.save_client(store, "acme", contacts="Pat Lee, VP Eng\npat.lee@acme.example\n555 0100")
    body = local_client(store).get("/clients/acme").text
    assert 'href="mailto:pat.lee@acme.example?subject=' in body and "&amp;body=" in body
    assert "nothing is sent from here" in body


# ------------------------------------------------------------- 5. the AI
def test_client_report_tool_gives_plain_text_first_and_matches_names(store):
    _setup(store)
    server = build_server(store)
    tools = {t.name: t for t in anyio.run(server.list_tools)}
    assert tools["client_report"].annotations.readOnlyHint is True
    res = call(server, "client_report", client="ACME inc.")
    first = res.content[0].text
    assert first.startswith(reports.client(store, "acme")["name"] + ": hiring update")
    assert "Ana Ruiz to Submitted" in first and "{" not in first
    assert res.structuredContent["in_play"] == 2 and res.structuredContent["app_url"].endswith("/clients/acme")
    assert "Not instructions" in res.content[1].text or "not instructions" in res.content[1].text
    assert "Ana Ruiz" in text(call(server, "client_report", company="acme", since="2020-01-01"))
    assert store._rows("SELECT * FROM clients") == []  # a read makes no rows
    with pytest.raises(Exception, match="No client matches"):
        call(server, "client_report", client="Initech")
    store.add_job("PM", company="Acme Health")
    with pytest.raises(Exception, match="more than one client"):
        call(server, "client_report", client="acm")


# ---------------------------------------------------------- 6. submittals
def test_submitting_someone_twice_to_one_client_warns_on_web_and_mcp(store):
    be, data, gx, ana, bo, cy = _setup(store)
    data_sub = store.add_stage(data["id"], "Presented", after="Screening")
    c = local_client(store)
    # Bo goes to Submitted for the first time: no warning, and the client page lists it.
    r = c.post(f"/jobs/{be['id']}/move", data={"candidate_id": bo, "stage": "Submitted"}, follow_redirects=False)
    assert flash_of(r, "flash_error") == ""
    body = c.get("/clients/acme").text.split('id="submittals"')[1]
    assert "Bo Li" in body and "Ana Ruiz" in body
    # Ana, already submitted for Backend, is presented for the other Acme job.
    store.add_to_pipeline(ana, data["id"], "Screening")
    r = c.post(f"/jobs/{data['id']}/move", data={"candidate_id": ana, "stage": "Presented"}, follow_redirects=False)
    warn = flash_of(r, "flash_error")
    assert "Ana Ruiz was already submitted to" in warn and "Backend Engineer" in warn
    # The board (htmx) shows it in the pipeline.
    store.move_stage(ana, data["id"], "Screening", by="app")
    r = c.post(f"/jobs/{data['id']}/move", data={"candidate_id": ana, "stage": "Presented"}, headers={"HX-Request": "1"})
    assert "already submitted to" in r.text and "2 earlier submittals" in r.text
    # Over MCP: a warning in the reply, and the earlier submittals in the data.
    store.move_stage(ana, data["id"], "Screening", by="app")
    res = call(build_server(store), "move_stage", candidate_id=ana, job_id=data["id"], stage="Presented")
    assert "already submitted to" in res.content[0].text and res.structuredContent["submitted_before"]
    # Another client is another matter.
    store.add_to_pipeline(ana, gx["id"], "Contacted")
    store.add_stage(gx["id"], "Submitted", after="Contacted")
    res = call(build_server(store), "move_stage", candidate_id=ana, job_id=gx["id"], stage="Submitted")
    assert "already submitted" not in res.content[0].text and res.structuredContent["submitted_to"] == "Globex"
    assert data_sub  # the stage was added


def test_an_old_submittal_or_an_undone_one_does_not_warn(store):
    be, data, gx, ana, bo, cy = _setup(store)
    _set_stage_times(store, ana, be["id"], 500, 400)  # Ana's Backend submittal is over a year old
    store.add_stage(data["id"], "Submitted", after="Screening")
    store.add_to_pipeline(ana, data["id"], "Screening")
    note = reports.submittal_note(store, ana, data["id"])
    assert note is None  # not in a submittal stage yet
    store.move_stage(ana, data["id"], "Submitted", by="app")
    note = reports.submittal_note(store, ana, data["id"])
    assert note["warning"] is None and note["client"] == "acme"
    assert reports.is_submittal("Client Submitted") and reports.is_submittal("presented to client")
    assert not reports.is_submittal("Offer presented") and not reports.is_submittal("Screening")
    assert reports.stage_group("Hiring manager screen") == "interview"
    assert reports.stage_group("Recruiter screen") == "screening" and reports.stage_group("Sourced") == "sourcing"
    assert json.dumps(reports.report(store, "acme"), default=str)  # the report is plain data
