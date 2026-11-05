import pytest

from talent.store import NotFound, Store, fts_query, normalize_linkedin, snippet_html
from talent.sample_data import load_sample_data


def test_add_get_update_delete(store):
    c = store.add_candidate(name="  Ana Ruiz ", email="Ana@Example.com", current_title="Engineer")
    assert c["name"] == "Ana Ruiz"
    assert c["email"] == "ana@example.com"
    assert c["created_at"] == c["updated_at"]

    u = store.update_candidate(c["id"], location="Austin, TX", current_title="")
    assert u["location"] == "Austin, TX"
    assert u["current_title"] is None  # blank clears

    store.delete_candidate(c["id"])
    with pytest.raises(NotFound):
        store.get_candidate(c["id"])
    with pytest.raises(NotFound):
        store.delete_candidate(c["id"])


def test_validation(store):
    with pytest.raises(ValueError):
        store.add_candidate(name="  ")
    with pytest.raises(ValueError):
        store.add_candidate(name="X", favourite_color="blue")
    c = store.add_candidate(name="X")
    with pytest.raises(ValueError):
        store.update_candidate(c["id"], name="")


def test_upsert_merges_by_email_and_linkedin_without_blanking(store):
    a, created = store.upsert_candidate(name="Bo Chen", email="bo@x.com", about="Python dev", phone="555")
    assert created
    b, created = store.upsert_candidate(name="Bo Chen", email="BO@x.com", current_title="Data Scientist", phone="")
    assert not created and b["id"] == a["id"]
    assert b["current_title"] == "Data Scientist"
    assert b["phone"] == "555" and b["about"] == "Python dev"

    li, _ = store.upsert_candidate(name="Cy", linkedin_url="https://www.linkedin.com/in/cy-dev/?trk=x")
    again, created = store.upsert_candidate(name="Cy", linkedin_url="linkedin.com/in/CY-dev")
    assert not created and again["id"] == li["id"]
    assert store.count_candidates() == 2


def test_linkedin_normalization():
    assert normalize_linkedin("https://uk.linkedin.com/in/jane/") == "https://linkedin.com/in/jane"
    assert normalize_linkedin("  ") is None


def test_unique_email_enforced(store):
    store.add_candidate(name="A", email="a@x.com")
    with pytest.raises(Exception):
        store.add_candidate(name="B", email="A@X.com")


def test_search_ranks_name_and_title_above_about(store):
    store.add_candidate(name="Rust Person", about="Mostly Java")
    store.add_candidate(name="Other", about="Dabbled in rust once")
    hits = store.search_candidates("rust").hits
    assert [h.candidate["name"] for h in hits] == ["Rust Person", "Other"]


def test_search_prefix_aliases_filters_and_snippet(store):
    load_sample_data(store)
    assert {h.candidate["name"] for h in store.search_candidates("pyth").hits} >= {"Priya Example", "Emily Example"}
    sf = store.search_candidates("react sf")
    assert [h.candidate["name"] for h in sf.hits] == ["Daniel Example"]
    assert "<mark>React</mark>" in snippet_html(sf.hits[0].snippet)
    ny = store.search_candidates("", location="new york")
    assert ny.total == 2 and all("New York" in h.candidate["location"] for h in ny.hits)
    assert store.search_candidates("python", title="lead").hits[0].candidate["name"] == "Priya Example"


def test_search_is_safe_against_fts_syntax(store):
    store.add_candidate(name="Liam O'Connor")
    for q in ['"', "AND OR NOT", "o'connor", "NEAR(", "*", "col:val", "(((", "naïve"]:
        store.search_candidates(q)  # must not raise
    assert store.search_candidates("o'connor").total == 1
    assert fts_query("   ") is None


def test_search_sees_updates_and_deletes(store):
    c = store.add_candidate(name="Dee", about="works at Stripe")
    store.update_candidate(c["id"], about="now at Figma")
    assert store.search_candidates("stripe").total == 0
    assert store.search_candidates("figma").total == 1
    store.delete_candidate(c["id"])
    assert store.search_candidates("figma").total == 0


def test_empty_query_lists_most_recent_first(store):
    store.add_candidate(name="First")
    second = store.add_candidate(name="Second")
    store.update_candidate(second["id"], about="touched")
    names = [h.candidate["name"] for h in store.search_candidates("").hits]
    assert names[0] == "Second"


def test_outbox_records_every_change(store):
    c = store.add_candidate(name="Q")
    store.update_candidate(c["id"], about="x")
    store.delete_candidate(c["id"])
    assert [o["op"] for o in store.outbox_batch()] == ["upsert", "upsert", "delete"]
    store.outbox_ack(store.outbox_batch()[-1]["seq"])
    assert store.outbox_batch() == []


def test_jobs_and_pipeline(store):
    c = store.add_candidate(name="Pat")
    job = store.add_job("Backend Engineer", company="Acme")
    a = store.add_to_pipeline(c["id"], job["id"])
    assert a["stage"] == "Sourced"
    assert store.add_to_pipeline(c["id"], job["id"])["id"] == a["id"]  # idempotent
    moved = store.move_stage(c["id"], job["id"], "interviewing", notes="Onsite Tuesday")
    assert moved["stage"] == "Interviewing" and moved["notes"] == "Onsite Tuesday"
    jobs = store.list_jobs()
    assert jobs[0]["pipeline_count"] == 1 and jobs[0]["active_count"] == 1
    store.move_stage(c["id"], job["id"], "hired")
    assert store.list_jobs()[0]["active_count"] == 0
    assert store.candidate_pipelines(c["id"])[0]["job_title"] == "Backend Engineer"
    with pytest.raises(ValueError):
        store.move_stage(c["id"], job["id"], "teleported")
    store.delete_candidate(c["id"])
    assert store.pipeline(job["id"]) == []  # cascade


def test_reopening_db_keeps_data_and_schema(tmp_path):
    path = tmp_path / "t.db"
    s = Store(path)
    s.add_candidate(name="Persisted", about="kept")
    s.close()
    s2 = Store(path)
    assert s2.search_candidates("kept").total == 1
    s2.close()


def test_two_connections_share_one_file(tmp_path):
    """The UI and the MCP server are separate processes on the same file."""
    path = tmp_path / "t.db"
    ui, mcp = Store(path), Store(path)
    mcp.add_candidate(name="From Claude", about="kubernetes")
    assert ui.search_candidates("kubernetes").total == 1
