"""Cycle 4 re-review, fixer Y: import speed (an indexed LinkedIn key), resubmit warnings on add, import busy notes."""
import random
import sqlite3
import time

import anyio
import pytest

from talent import actions, importers, reports
from talent import store as store_mod
from talent.mcp_server import build_server
from talent.store import Store

from conftest import flash_of, local_client


def call(server, tool, **args):
    async def go():
        return await server.call_tool(tool, args)
    return anyio.run(go)


def _plans(store, fn):
    """Run fn and return the query plan of every SELECT on candidates it ran."""
    seen: list[str] = []
    store._conn.set_trace_callback(seen.append)
    try:
        fn()
    finally:
        store._conn.set_trace_callback(None)
    out = []
    for sql in seen:
        if sql.lstrip().upper().startswith("SELECT") and "FROM candidates" in sql:
            out.append((sql, [r[3] for r in store._conn.execute("EXPLAIN QUERY PLAN " + sql).fetchall()]))
    return out


# ------------------------------------------------------------ 1. the index
def test_linkedin_and_email_lookups_use_an_index(store):
    ana = store.add_candidate(name="Ana Ruiz", email="ana@example.com", linkedin_url="https://www.linkedin.com/in/ana-r/")
    store.add_candidate(name="Bo Li", linkedin_url="linkedin.com/in/bo-li")
    plans = _plans(store, lambda: (
        store.find_duplicate(linkedin_url="https://uk.linkedin.com/in/ANA-R/details/experience"),
        store.find_duplicate(email="ANA@example.com"),
        store.lookup_exact("https://linkedin.com/in/bo-li"),
        store.update_candidate(ana["id"], linkedin_url="https://www.linkedin.com/in/ana-ruiz"),
    ))
    assert plans
    for sql, plan in plans:
        assert not any(p.startswith("SCAN candidates") and "INDEX" not in p for p in plan), (sql, plan)
    used = " ".join(p for _, plan in plans for p in plan)
    assert "candidates_linkedin_key_idx" in used and "candidates_email_uq" in used
    # The key is kept on update: the new link finds her, the old one doesn't, and it never shows on her record.
    assert store.find_duplicate(linkedin_url="linkedin.com/in/ana-ruiz")["id"] == ana["id"]
    assert store.find_duplicate(linkedin_url="linkedin.com/in/ana-r") is None
    assert "linkedin_key" not in store.get_candidate(ana["id"])
    with pytest.raises(Exception):  # someone else can't take her profile
        store.add_candidate(name="Other", linkedin_url="https://www.linkedin.com/in/Ana-Ruiz/")


def test_key_follows_undo_and_merge(store):
    ana = store.add_candidate(name="Ana Ruiz", linkedin_url="https://linkedin.com/in/ana-r")
    store.update_candidate(ana["id"], linkedin_url="https://linkedin.com/in/ana-new")
    change = [t for t in store.timeline(ana["id"]) if t["kind"] == "change"][0]
    store.undo_change(change["id"], candidate_id=ana["id"])
    assert store.find_duplicate(linkedin_url="linkedin.com/in/ana-r")["id"] == ana["id"]
    assert store.find_duplicate(linkedin_url="linkedin.com/in/ana-new") is None
    bo = store.add_candidate(name="Bo Li")
    dup = store.add_candidate(name="Bo Li", linkedin_url="https://linkedin.com/in/bo-li")
    store.merge_candidates(bo["id"], dup["id"])
    assert store.find_duplicate(linkedin_url="https://www.linkedin.com/in/bo-li/")["id"] == bo["id"]


def test_v9_fills_keys_for_older_rows_and_a_new_rule_works_them_out_again(tmp_path, monkeypatch):
    path = tmp_path / "v8.db"
    all_steps = store_mod._MIGRATIONS
    monkeypatch.setattr(store_mod, "_MIGRATIONS", all_steps[:8])
    s = Store(path)
    with s._lock, s._conn:  # saved before links were cut to /in/<slug>
        s._conn.execute("INSERT INTO candidates(id, name, linkedin_url, created_at, updated_at) VALUES"
                        " (1, 'Wei Zhang', 'https://www.linkedin.com/in/Wei-Zhang/details/experience', 'x', 'x')")
    s.close()
    monkeypatch.setattr(store_mod, "_MIGRATIONS", all_steps)
    s = Store(path)
    assert s.schema_version == len(all_steps) >= 9  # later cycles add steps after v9
    assert s._row("SELECT linkedin_key FROM candidates WHERE id = 1")["linkedin_key"] == "https://linkedin.com/in/wei-zhang"
    assert s.find_duplicate(linkedin_url="linkedin.com/in/wei-zhang")["id"] == 1
    # A row written by another tool has no key: it is still found, and the next open fills it.
    with s._lock, s._conn:
        s._conn.execute("INSERT INTO candidates(id, name, linkedin_url, created_at, updated_at) VALUES"
                        " (2, 'Bo Li', 'https://linkedin.com/in/bo-li/', 'x', 'x')")
    assert s.find_duplicate(linkedin_url="https://www.linkedin.com/in/bo-li")["id"] == 2
    # A changed matching rule (a new normalize_linkedin) works every key out again on open.
    with s._lock, s._conn:
        s._conn.execute("UPDATE candidates SET linkedin_key = 'stale' WHERE id = 1")
        s._conn.execute("UPDATE settings SET value = 'older' WHERE key = ?", (store_mod.LINKEDIN_RULE_KEY,))
    s.close()
    s = Store(path)
    keys = dict(s._conn.execute("SELECT id, linkedin_key FROM candidates").fetchall())
    assert keys == {1: "https://linkedin.com/in/wei-zhang", 2: "https://linkedin.com/in/bo-li"}
    assert s._row("PRAGMA integrity_check")["integrity_check"] == "ok"
    s.close()


def _csv_rows(n, start=0, seed=7):
    rnd = random.Random(seed)
    header = ["Candidate Name", "Email", "LinkedIn", "Current Title", "Current Company", "Skills", "Requisition",
              "Client", "Candidate Status"]
    first = ["Ana", "Ben", "Chen", "Dana", "Eli", "Gus", "Hana", "Ivan", "Kofi", "Lena", "Omar", "Priya"]
    last = ["Lopez", "Smith", "Wang", "Novak", "Kim", "Patel", "Rossi", "Silva", "Cohen", "Reyes"]
    reqs = [("Backend Engineer", "Acme"), ("Designer", "Globex"), ("Sales Engineer", "Initech")]
    rows = []
    for i in range(start, start + n):
        f, l = rnd.choice(first), rnd.choice(last)
        t, c = rnd.choice(reqs)
        rows.append([f"{f} {l}", f"{f.lower()}.{l.lower()}{i}@example.com" if rnd.random() > 0.1 else "",
                     f"https://www.linkedin.com/in/{f.lower()}-{l.lower()}-{i}", "Engineer", "Stripe",
                     ", ".join(rnd.sample(["python", "go", "react", "sql", "aws"], 2)), t, c,
                     rnd.choice(["New", "Phone Screen", "Onsite", "Offer", "Hired", "Rejected"])])
    return header, rows


def test_a_big_import_stays_quick_with_many_people_saved(store):
    """Was O(rows x people): each row's LinkedIn check normalised every saved link. 1,500 rows on top of 1,500
    saved took about 40s before; now a few seconds. The bound is loose so a slow machine still passes."""
    header, rows = _csv_rows(1500)
    mapping = importers.guess_mapping(header, rows)
    importers.import_rows(store, header, rows, mapping, backup=False, add_stages=True)
    header, more = _csv_rows(1500, start=1500, seed=8)
    calls = 0
    real = store_mod.normalize_linkedin

    def counted(url):
        nonlocal calls
        calls += 1
        return real(url)
    store._conn.create_function("li_key", 1, counted, deterministic=True)
    t = time.perf_counter()
    preview = importers.preview_import(store, header, more, mapping, add_stages=True)
    done = importers.import_rows(store, header, more, mapping, backup=False, add_stages=True)
    took = time.perf_counter() - t
    assert preview["added"] == done["added"] == 1500 and store.count_candidates() == 3000
    assert calls == 0  # no saved link is normalised again inside a query
    assert took < 25, f"preview and import of 1,500 rows took {took:.1f}s"


# --------------------------------------------------- 2. resubmit warning on add
def _acme(store):
    be = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    data = store.add_job("Data Engineer", company="Acme", stages=["Client Submitted", "Client interview", "Hired"])
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    store.add_to_pipeline(ana, be["id"], "Sourced")
    store.move_stage(ana, be["id"], "Submitted", by="app")
    return be, data, ana


def test_mcp_add_to_pipeline_straight_into_a_submitted_stage_warns(store):
    be, data, ana = _acme(store)
    res = call(build_server(store), "add_to_pipeline", candidate_id=ana, job_id=data["id"], stage="Client Submitted")
    first_line, warning = res.content[0].text.split("\n")[:2]
    assert first_line.startswith("Added \"Ana Ruiz\" to Data Engineer")
    assert warning.startswith("Heads up: Ana Ruiz was already submitted to Acme on ") and "Backend Engineer" in warning
    assert res.structuredContent["submitted_to"] == "Acme" and res.structuredContent["submitted_before"]
    # Adding again is a no-op and doesn't warn twice; a first submittal names the client without a warning.
    res = call(build_server(store), "add_to_pipeline", candidate_id=ana, job_id=data["id"], stage="Client Submitted")
    assert "Heads up" not in res.content[0].text
    bo = store.add_candidate(name="Bo Li")["id"]
    res = call(build_server(store), "add_to_pipeline", candidate_id=bo, job_id=data["id"], stage="Client Submitted")
    assert "Heads up" not in res.content[0].text and res.structuredContent["submitted_to"] == "Acme"


def test_web_add_into_a_job_that_starts_at_submitted_warns(store):
    be, data, ana = _acme(store)
    c = local_client(store)
    r = c.post(f"/jobs/{data['id']}/add", data={"candidate_id": ana}, follow_redirects=False)
    assert "Added Ana Ruiz to this job" in flash_of(r) and "already submitted to Acme" in flash_of(r, "flash_error")
    other = store.add_job("Platform Engineer", company="Acme", stages=["Submitted", "Hired"])
    r = c.post(f"/candidates/{ana}/jobs", data={"job_id": other["id"]}, follow_redirects=False)
    assert "2 earlier submittals" in flash_of(r, "flash_error")
    bo = store.add_candidate(name="Bo Li")["id"]  # never submitted: no warning
    r = c.post(f"/jobs/{data['id']}/add", data={"candidate_id": bo}, follow_redirects=False)
    assert flash_of(r, "flash_error") == ""
    r = c.post(f"/jobs/{be['id']}/add", data={"candidate_id": ana}, follow_redirects=False)  # already there
    assert flash_of(r, "flash_error") == ""


# ------------------------------------------- 3. an imported submittal isn't dated
def test_an_imported_submittal_says_imported_not_the_import_day(store):
    header = ["Name", "Job", "Client", "Status"]
    rows = [["Chen Murphy", "Sales Engineer", "Initech", "Client Submitted"]]
    importers.import_rows(store, header, rows, importers.guess_mapping(header, rows), backup=False, add_stages=True)
    chen = store.find_by_name("Chen Murphy")[0]["id"]
    other = store.add_job("Account Executive", company="Initech", stages=["Sourced", "Client Submitted", "Hired"])
    store.add_to_pipeline(chen, other["id"], "Sourced")
    res = call(build_server(store), "move_stage", candidate_id=chen, job_id=other["id"], stage="Client Submitted")
    warning = res.structuredContent["warning"]
    assert "already submitted to Initech (imported, date unknown), for Sales Engineer" in warning
    assert " on " not in warning.split("Sales Engineer")[0]
    note = reports.submittal_note(store, chen, other["id"])
    assert note["earlier"] and "imported" in note["warning"]


# ------------------------------------------------------- 4. import busy notes
def test_check_again_says_it_saves_nothing_while_it_works(client):
    csv_text = "Name,Email\nAna Ruiz,ana@example.com\nBo Li,bo@example.com\n"
    r = client.post("/import", files={"file": ("people.csv", csv_text.encode(), "text/csv")})
    assert 'data-busy="Checking 2 rows again. Nothing is saved yet.' in r.text
    assert "data-busy=\"Working on it. 2 rows" in r.text and "\u2014" not in r.text and "\u2013" not in r.text
    js = client.get("/static/app.js").text
    assert "e.submitter && e.submitter.dataset.busy" in js


def test_mcp_add_to_pipeline_refuses_a_hired_or_out_stage(store):
    job = store.add_job("Backend Engineer", company="Acme", stages=actions.stage_set("agency"))
    ana = store.add_candidate(name="Ana Ruiz")["id"]
    server = build_server(store)
    for stage, word in (("Placed", "hired"), ("Client passed", "out")):
        with pytest.raises(Exception, match=f"Nothing saved. {stage} counts as {word} on Backend Engineer"):
            call(server, "add_to_pipeline", candidate_id=ana, job_id=job["id"], stage=stage)
    with pytest.raises(Exception, match="still in play: Sourced, Screened, Submitted, Client interview, Offer"):
        call(server, "add_to_pipeline", candidate_id=ana, job_id=job["id"], stage="Placed")
    assert store.candidate_pipelines(ana) == [] and not store._rows("SELECT id FROM ai_actions")
    res = call(server, "add_to_pipeline", candidate_id=ana, job_id=job["id"], stage="Screened")
    assert res.structuredContent["stage"] == "Screened"
    # The app's own add routes are unchanged.
    c = local_client(store)
    bo = store.add_candidate(name="Bo Li")["id"]
    c.post(f"/candidates/{bo}/jobs", data={"job_id": job["id"]}, follow_redirects=False)
    assert store.candidate_pipelines(bo)[0]["stage"] == "Sourced"
