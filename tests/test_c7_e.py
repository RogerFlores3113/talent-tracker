"""Cycle 7, fixer E: ops at volume. Candidate filters, stage/limit on the AI tools, the chase at volume, the
same-name save, and the Ops review."""
from datetime import datetime, timedelta, timezone

import anyio
import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import actions, chase, mcp_server, opsreview
from talent.mcp_server import build_server

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def data(res):
    return res.structuredContent


def text(res):
    return "\n".join(c.text for c in res.content)


def _at(days: float) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).replace(microsecond=0).isoformat()


def _age(store, cid, jid, days, created=None):
    with store._lock, store._conn:
        store._conn.execute("UPDATE applications SET stage_changed_at = ?, created_at = coalesce(?, created_at)"
                            " WHERE candidate_id = ? AND job_id = ?", (_at(-days), created, cid, jid))


def _world(store):
    sre = store.add_job("SRE", company="Globex", stages=actions.stage_set("agency"))
    be = store.add_job("Backend", company="Acme, Inc.", stages=actions.stage_set("agency"))
    ids = {}
    for name, job, stage in [("Ana", sre, "Submitted"), ("Bo", sre, "Submitted"), ("Cy", sre, "Screened"),
                             ("Di", be, "Submitted")]:
        ids[name] = store.add_candidate(name=f"{name} Person")["id"]
        store.add_to_pipeline(ids[name], job["id"], stage)
    return sre, be, ids


@pytest.fixture(autouse=True)
def _apps_host(monkeypatch):
    monkeypatch.setattr(mcp_server, "_client_apps", lambda mcp: True)


# ----------------------------------------------------------------- 1. filters
def test_candidates_page_filters_by_job_client_and_stage(store):
    sre, be, ids = _world(store)
    c = local_client(store)
    names = lambda r: sorted(n for n in ("Ana", "Bo", "Cy", "Di") if f"{n} Person" in r.text)
    assert names(c.get("/candidates")) == ["Ana", "Bo", "Cy", "Di"]
    assert names(c.get(f"/candidates?job={sre['id']}")) == ["Ana", "Bo", "Cy"]
    assert names(c.get(f"/candidates?job={sre['id']}&stage=Submitted")) == ["Ana", "Bo"]
    assert names(c.get("/candidates?client=acme")) == ["Di"]  # "Acme, Inc." and "acme" are one client
    assert names(c.get("/candidates?client=globex&stage=screened")) == ["Cy"]
    page = c.get(f"/candidates?job={sre['id']}&stage=Submitted").text
    assert 'name="job"' in page and 'name="client"' in page and 'name="stage"' in page
    assert "job=" in page  # paging and sort links keep the filter
    assert "—" not in page


def test_search_and_job_pipeline_take_stage_job_and_limit(store):
    sre, be, ids = _world(store)
    _age(store, ids["Ana"], sre["id"], 9)
    server = build_server(store)
    res = call(server, "search_candidates", job_id=sre["id"], stage="Submitted")
    assert sorted(r["name"] for r in data(res)["results"]) == ["Ana Person", "Bo Person"]
    res = call(server, "search_candidates", client="Acme")
    assert [r["name"] for r in data(res)["results"]] == ["Di Person"]
    res = call(server, "search_candidates", query="Person", job_id=sre["id"], stage="Screened")
    assert [r["name"] for r in data(res)["results"]] == ["Cy Person"]
    with pytest.raises(ToolError):
        call(server, "search_candidates", job_id=sre["id"], stage="Nonsense")
    res = call(server, "job_pipeline", job_id=sre["id"], stage="Submitted")
    d = data(res)
    assert d["total"] == 2 and d["more"] == 0
    assert [p["name"] for p in d["stages"][0]["candidates"]] == ["Ana Person", "Bo Person"]  # longest wait first
    assert d["stages"][0]["candidates"][0]["days_in_stage"] == 9
    res = call(server, "job_pipeline", job_id=sre["id"], stage="Submitted", limit=1)
    assert data(res)["more"] == 1 and "1 more" in text(res)
    res = call(server, "job_pipeline", job_id=sre["id"], limit=1)
    assert all(len(s["candidates"]) <= 1 for s in data(res)["stages"])


# --------------------------------------------------------------------- 2. chase
def _crowd(store, n=25):
    sre = store.add_job("SRE", company="Globex", stages=actions.stage_set("agency"), hiring_manager="Pat Lee",
                        hiring_manager_email="pat@globex.com")
    be = store.add_job("Backend", company="Globex", stages=actions.stage_set("agency"), hiring_manager="Pat Lee",
                       hiring_manager_email="pat@globex.com")
    for i in range(n):
        job = sre if i % 2 else be
        cid = store.add_candidate(name=f"Waiter {i:02}")["id"]
        store.add_to_pipeline(cid, job["id"], "Submitted")
        _age(store, cid, job["id"], 6 + i)
    return sre, be


def test_nudge_names_ten_oldest_then_a_count_table(store):
    _crowd(store)
    g = chase.chase_list(store)[0]
    lines = g["text"].splitlines()
    named = [x for x in lines if x.startswith("- Waiter")]
    assert len(named) == 10 and "Waiter 24" in named[0] and "Waiter 15" in named[-1]  # oldest first
    assert "- and 15 more, by job and stage:" in lines
    assert any(x.strip() == "SRE, Submitted: 7" for x in lines) and any(x.strip() == "Backend, Submitted: 8" for x in lines)
    assert sorted((r["job"], r["count"]) for r in g["by_job_stage"]) == [("Backend", 13), ("SRE", 12)]
    page = local_client(store).get("/clients/chase").text
    assert "Longest wait" in page and "Mark this client chased" in page and "—" not in page


def test_mark_client_chased_snoozes_for_n_days(store):
    _crowd(store)
    assert [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"]
    item = [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"][0]
    assert item["detail"].count("days)") == 5 and "and 20 more" in item["detail"]  # the worst 5, then a count
    c = local_client(store)
    r = c.post("/clients/globex/chased-all", data={"snooze": "30"}, follow_redirects=False)
    assert "Marked Globex as chased" in flash_of(r) and "30 days" in flash_of(r)
    assert not [i for i in actions.action_items(store)["items"] if i["kind"] == "client_chase"]
    later = datetime.now(timezone.utc) + timedelta(days=20)  # still quiet at 20 days, back after 30
    assert chase.snoozed(store, "globex", "pat lee", later)
    assert not chase.snoozed(store, "globex", "pat lee", datetime.now(timezone.utc) + timedelta(days=31))
    r = c.post("/clients/globex/chased-all", data={"snooze": "abc"}, follow_redirects=False)
    assert "number of days" in flash_of(r, "flash_error")


# ------------------------------------------------------------- 3. same name save
def test_save_candidate_with_a_different_linkedin_is_saved_as_new(store):
    store.add_candidate(name="Wen Khan", linkedin_url="https://www.linkedin.com/in/wen-khan-1")
    store.add_candidate(name="Wen Khan", linkedin_url="https://www.linkedin.com/in/wen-khan-2")
    server = build_server(store)
    res = call(server, "save_candidate", name="Wen Khan", linkedin_url="https://www.linkedin.com/in/wen-k-3")
    d = data(res)
    assert d["created"] is True and "Same name, different LinkedIn: saved as someone new" in text(res)
    assert len(store.find_by_name("Wen Khan")) == 3
    # a name alone, or a person with no link saved, still asks
    store.add_candidate(name="Ira Moss")
    res = call(server, "save_candidate", name="Ira Moss", linkedin_url="https://www.linkedin.com/in/ira-moss")
    assert data(res)["needs_confirmation"] is True
    res = call(server, "save_candidate", name="Wen Khan")
    assert data(res)["needs_confirmation"] is True


def test_one_matching_saved_person_still_holds_among_different_ones(store):
    store.add_candidate(name="Wen Khan", linkedin_url="https://www.linkedin.com/in/wen-khan-1")
    store.add_candidate(name="Wen Khan")  # no link saved: could be the same person
    res = call(build_server(store), "save_candidate", name="Wen Khan", linkedin_url="https://www.linkedin.com/in/new")
    d = data(res)
    assert d["needs_confirmation"] is True and len(d["possible_duplicates"]) == 1


# ----------------------------------------------------------------- 4. ops review
def test_ops_review_counts_and_links_agree_with_the_filtered_lists(store):
    sre, be, ids = _world(store)
    _age(store, ids["Ana"], sre["id"], 20, created=_at(-30))    # stuck, not new
    _age(store, ids["Bo"], sre["id"], 2, created=_at(-30))      # moved this week
    store.log(ids["Cy"], "interview", "Loop", at=_at(3), job_id=sre["id"])
    from talent import offers
    offers.save(store, ids["Bo"], sre["id"], status="out")
    data_ = opsreview.review(store, 14)
    row = next(r for r in data_["rows"] if r["job_id"] == sre["id"])
    assert row["counts"] == {"process": 3, "new": 1, "moved": 1, "stuck": 1, "interviews": 1, "offers": 1}
    assert data_["totals"]["process"] == 4 and row["days_since_update"] is None
    c = local_client(store)
    for pipe, n in row["counts"].items():
        r = c.get(row["links"][pipe])
        assert r.status_code == 200
        assert f"<h1>Candidates <span class=\"muted\">{n}</span>" in r.text, pipe
    page = c.get("/clients/review").text
    assert "Copy as table" in page and "ops-table" in page and "—" not in page and "SRE" in page
    assert "Backend" in c.get("/clients/review?client=acme").text and "SRE" not in c.get("/clients/review?client=acme").text
    assert c.get("/clients/globex").status_code in (200, 404) and c.get("/clients").text.count("/clients/review") >= 1


def test_ops_review_tool_is_read_only_and_takes_company_too(store):
    sre, be, ids = _world(store)
    server = build_server(store)
    before = store.count_candidates()
    res = call(server, "ops_review")
    d = data(res)
    assert d["open_jobs"] == 2 and d["totals"]["process"] == 4 and "SRE (Globex)" in text(res)
    assert d["rows"][0]["links"]["stuck"].startswith("http") and "pipe=stuck" in d["rows"][0]["links"]["stuck"]
    assert [r["title"] for r in data(call(server, "ops_review", company="globex"))["rows"]] == ["SRE"]
    assert [r["title"] for r in data(call(server, "ops_review", client="acme"))["rows"]] == ["Backend"]
    with pytest.raises(ToolError):
        call(server, "ops_review", client="Nobody Inc")
    assert store.count_candidates() == before


# --------------------------------------------------------------------- 5. S bugs
def test_add_to_pipeline_on_a_missing_job_says_so(store):
    cid = store.add_candidate(name="Ana Person")["id"]
    with pytest.raises(Exception, match="No job with id 99"):
        call(build_server(store), "add_to_pipeline", candidate_id=cid, job_id=99)


def test_digest_and_client_tools_take_company_and_client(store):
    sre, be, ids = _world(store)
    server = build_server(store)
    a = text(call(server, "status_digest", company="globex"))
    b = text(call(server, "status_digest", client="globex"))
    assert a == b and "SRE" in a
    assert data(call(server, "client_chase", company="globex"))["nudge_count"] == data(
        call(server, "client_chase", client="globex"))["nudge_count"]


def test_job_table_view_uses_one_move_form(store):
    sre, be, ids = _world(store)
    page = local_client(store).get(f"/jobs/{sre['id']}?view=table").text
    assert page.count("<form") < 8 and page.count("data-table-stage") == 3 and 'id="tbl-move"' in page
    r = local_client(store).post(f"/jobs/{sre['id']}/move", data={"candidate_id": ids["Cy"], "stage": "Submitted"},
                                 follow_redirects=False)
    assert store.candidate_pipelines(ids["Cy"])[0]["stage"] == "Submitted"
