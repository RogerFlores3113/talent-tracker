"""Cycle 2, fixer E: search finds word forms, typos, pipelines and notes; filters take shorthand; paging."""
import shutil
from pathlib import Path

import pytest

import talent.store as store_mod
from talent import recipes
from talent.mcp_server import build_server
from talent.mcp_views import view_html
from talent.store import Store

from conftest import local_client
from test_mcp import call, structured, text

FIXTURES = Path(__file__).parent / "fixtures"


def ids(res):
    return [h.candidate["id"] for h in res.hits]


def exact(res):
    return [h.candidate["id"] for h in res.hits if not h.closest]


@pytest.fixture
def austin(store):
    people = [
        store.add_candidate(name="Ada", current_title="Rust Engineer", location="Austin, TX"),
        store.add_candidate(name="Bo", current_title="Senior Engineer", location="Austin, Texas", tags="Rust"),
        store.add_candidate(name="Cy", current_title="Engineering Lead", location="austin", about="Writes Rust daily"),
        store.add_candidate(name="Di", current_title="Staff engineer", location="Austin, TX",
                            work_history=[{"title": "Rust engineer", "company": "Ferrous"}]),
    ]
    store.add_candidate(name="Ed", current_title="Rust Engineer", location="Denver, CO")
    store.add_candidate(name="Flo", current_title="Designer", location="Austin, TX")
    return [p["id"] for p in people]


# 1. plurals and stems ---------------------------------------------------------
def test_porter_index_finds_word_forms(store, austin):
    sql = store._row("SELECT sql FROM sqlite_master WHERE name = 'candidates_fts'")["sql"]
    assert "porter unicode61 remove_diacritics 2" in sql
    assert set(ids(store.search_candidates("rust engineers in austin"))[:4]) == set(austin)
    res = store.search_candidates("rust engineers in austin")
    assert res.exact_total == 4 and not any(h.closest for h in res.hits)
    assert len(ids(store.search_candidates("engineers"))) == 5  # not the designer
    # exact words and the last-word prefix still work
    assert ids(store.search_candidates("desig")) == ids(store.search_candidates("Designer"))
    assert store.search_candidates("rust engineers in austin", location="Denver").total == 1


def test_v3_database_upgrades_to_the_stemmed_index(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    shutil.copy(FIXTURES / "v1.db", path)
    all_steps = store_mod._MIGRATIONS
    monkeypatch.setattr(store_mod, "_MIGRATIONS", all_steps[:3])
    s = Store(path)
    cid = s.add_candidate(name="Old Timer", current_title="Payments Engineer")["id"]
    s.close()
    monkeypatch.setattr(store_mod, "_MIGRATIONS", all_steps)
    s = Store(path)
    assert s.schema_version == len(all_steps)
    assert cid in ids(s.search_candidates("engineers"))
    assert s._row("SELECT count(*) AS n FROM candidates_words_vocab WHERE term = 'payments'")["n"] == 1
    s.update_candidate(cid, current_title="Kubernetes Engineer")
    assert ids(s.search_candidates("kuberentes")) == [cid]  # vocabulary follows edits
    s.delete_candidate(cid)
    assert s.search_candidates("engineers").total == 0
    assert s._row("PRAGMA integrity_check")["integrity_check"] == "ok"
    s.close()


# 2. fallbacks and typos ------------------------------------------------------
def test_closest_matches_follow_fewer_than_three_exact_hits(store):
    a = store.add_candidate(name="Ana", about="React and Rust")["id"]
    b = store.add_candidate(name="Ben", about="React only")["id"]
    c = store.add_candidate(name="Cam", about="Rust only")["id"]
    res = store.search_candidates("react rust")
    assert ids(res)[0] == a and set(ids(res)[1:]) == {b, c}
    assert [h.closest for h in res.hits] == [False, True, True]
    assert res.exact_total == 1 and res.total == 3 and not res.closest
    out = call(build_server(store), "search_candidates", query="react rust")
    assert "1 match, plus 2 closest matches with some of the words:" in text(out)
    assert text(out).count("(closest match)") == 2
    assert [r.get("closest", False) for r in structured(out)["results"]] == [False, True, True]
    for name in ("Dee", "Eve"):
        store.add_candidate(name=name, about="React and Rust")
    assert not any(h.closest for h in store.search_candidates("react rust").hits)


def test_typos_get_fixed_or_suggested(store):
    k = store.add_candidate(name="Tom", current_title="ML Engineer", location="Seattle", about="Kubernetes and PyTorch")
    res = store.search_candidates("kuberentes")
    assert ids(res) == [k["id"]] and res.corrected and res.suggestion == "kubernetes"
    assert store.search_candidates("pytorhc engineer").suggestion == "pytorch engineer"
    # short or unknown words aren't "fixed"
    assert store.search_candidates("zzzz").suggestion is None and store.search_candidates("tom").total == 1
    # the fix finds nobody with these filters: keep the query, offer the fix
    res = store.search_candidates("kuberentes", location="Austin")
    assert res.total == 0 and res.suggestion == "kubernetes" and not res.corrected

    server = build_server(store)
    out = call(server, "search_candidates", query="kuberentes")
    assert 'Nothing matched "kuberentes". Showing results for kubernetes.' in text(out)
    assert structured(out)["corrected"] and structured(out)["suggestion"] == "kubernetes"
    out = call(server, "search_candidates", query="kuberentes", location="Austin")
    assert text(out).endswith("Did you mean kubernetes?")

    client = local_client(store)
    page = client.get("/?q=kuberentes").text
    assert "Showing results for <strong>kubernetes</strong>" in page and "Tom" in page
    page = client.get("/search?q=kuberentes&location=Austin").text
    assert "Did you mean" in page and "kubernetes</a>?" in page
    assert "Did you mean" in client.get("/candidates?q=kuberentes&location=Austin").text
    html = view_html("results")
    assert "Did you mean " in html and "Showing results for " in html and "Closest match" in html


# 3. pipelines and notes -----------------------------------------------------
def test_search_finds_people_by_pipeline_job_and_notes(store):
    priya = store.add_candidate(name="Priya", current_title="Backend Engineer")["id"]
    marcus = store.add_candidate(name="Marcus", current_title="Engineer")["id"]
    lee = store.add_candidate(name="Lee", about="Worked at Acme Fintech for years")["id"]
    store.add_candidate(name="Nobody")
    acme = store.add_job("Senior Backend Engineer", company="Acme Fintech")
    nw = store.add_job("Data Lead", company="Northwind")
    store.add_to_pipeline(priya, acme["id"])
    store.add_to_pipeline(marcus, acme["id"])
    store.add_to_pipeline(marcus, nw["id"])
    store.log(priya, "interview", "Panel with Dana and Sam", job_id=acme["id"])
    store.log(marcus, "call", "Wants 140k base, two weeks notice")
    store._conn.execute("INSERT INTO timeline (candidate_id, kind, body, at, created_at)"
                        " VALUES (?, 'stage', 'Moved to Dana', '2026-01-01', '2026-01-01')", (lee,))  # not searched

    res = store.search_candidates("Acme Fintech")
    assert ids(res)[0] == lee and set(ids(res)) == {lee, priya, marcus}
    labels = {h.candidate["id"]: h.reason for h in res.hits}
    assert labels[priya] == labels[marcus] == "In pipeline: Senior Backend Engineer (Acme Fintech)"
    assert labels[lee] is None
    assert ids(store.search_candidates("northwind")) == [marcus]
    assert ids(store.search_candidates("dana")) == [priya]
    hit = store.search_candidates("140k").hits[0]
    assert hit.candidate["id"] == marcus and hit.reason == "Matched a call" and "\x02140k\x03" in hit.snippet
    # a word in the profile plus a word only in the pipeline still counts as every word
    assert exact(store.search_candidates("backend northwind")) == [marcus]  # one word per pipeline
    assert exact(store.search_candidates("engineer northwind")) == [marcus]
    assert ids(store.search_candidates("acme", title="backend")) == [priya]

    out = text(call(build_server(store), "search_candidates", query="Acme Fintech"))
    assert "In pipeline: Senior Backend Engineer (Acme Fintech)" in out
    assert "Matched a call: Wants **140k** base" in text(call(build_server(store), "search_candidates", query="140k"))
    page = local_client(store).get("/?q=140k").text
    assert "Matched a call" in page and "<mark>140k</mark>" in page


# 4. filters and aliases -----------------------------------------------------
def test_filters_take_shorthand_and_ignore_case(store):
    sf = store.add_candidate(name="Sam", current_title="Software Engineer", location="San Francisco, CA",
                             tags="React", about="Golang and Postgres")["id"]
    ny = store.add_candidate(name="Nia", current_title="product manager", location="NYC")["id"]
    la = store.add_candidate(name="Lou", current_title="Designer", location="Los Angeles")["id"]
    atl = store.add_candidate(name="Ari", current_title="Engineer", location="Atlanta, GA")["id"]
    oak = store.add_candidate(name="Oki", current_title="Engineer", location="Oakland, CA", about="nodejs, reactjs")["id"]
    malmo = store.add_candidate(name="Mo", location="Malmö")["id"]
    loc = lambda value, q="": set(ids(store.search_candidates(q, location=value)))  # noqa: E731
    assert loc("sf") == loc("SF") == loc("san francisco") == {sf}
    assert loc("nyc") == loc("New York") | {ny} == {ny}
    assert loc("la") == {la}  # not Atlanta
    assert loc("Bay Area") == loc("bay area") == {sf, oak}
    assert loc("malmo") == {malmo} and loc("ATLANTA") == {atl}
    res = store.search_candidates("engineers in the bay area")
    assert {h.candidate["id"] for h in res.hits if not h.closest} == {sf, oak} and res.exact_total == 2
    assert set(ids(store.search_candidates("", title="SWE"))) == {sf}
    assert set(ids(store.search_candidates("", title="PM"))) == {ny}
    assert set(ids(store.search_candidates("", title="engineers"))) == {sf, atl, oak}
    assert set(ids(store.search_candidates("", tag="REACT"))) == {sf}
    # synonyms work both ways
    assert ids(store.search_candidates("go")) == [sf]
    assert exact(store.search_candidates("golang engineer")) == [sf]
    assert ids(store.search_candidates("postgresql")) == [sf]
    assert ids(store.search_candidates("node")) == [oak] and ids(store.search_candidates("nodejs")) == [oak]
    assert set(ids(store.search_candidates("react"))) == {sf, oak}
    assert set(ids(store.search_candidates("reactjs"))) == {sf, oak}

    out = structured(call(build_server(store), "search_candidates", query="engineer", location="sf"))
    assert [r["id"] for r in out["results"]] == [sf]


# 5. app paging and table filters ---------------------------------------------
def test_show_more_on_results_and_table(store):
    for i in range(130):
        store.add_candidate(name=f"Person {i}", about="python", location="Austin" if i % 2 else "Boston",
                            source="LinkedIn" if i % 3 else "Referral")
    client = local_client(store)
    page = client.get("/?q=python").text
    assert page.count('<a class="card') == 50 and "Show more" in page and "50 of 130" in page
    more = client.get("/search?q=python&offset=50").text
    assert more.count('<a class="card') == 50 and "100 of 130" in more and "<ul" not in more
    last = client.get("/search?q=python&offset=100").text
    assert last.count('<a class="card') == 30 and "Show more" not in last

    table = client.get("/candidates").text
    assert table.count("<tr>") == 100 + 1 and "Show more" in table and "Showing 100 of 130" in table
    rows = client.get("/candidates?page_no=2&more=1", headers={"HX-Request": "true"}).text
    assert rows.count("<tr>") == 30 and "<table" not in rows and "Show more" not in rows
    assert "Any source" in table and "LinkedIn (86)" in table and "Referral (44)" in table
    page = client.get("/candidates?location=austin&source=referral").text
    assert "Person 3<" in page and "Person 2<" not in page and "Person 1<" not in page
    assert '<span class="muted">22</span>' in page
    assert '<span class="muted">22</span>' in client.get("/candidates?q=python&location=AUSTIN&source=Referral").text


# 6. in-chat views ------------------------------------------------------------
def test_candidate_view_shows_a_saved_badge():
    html = view_html("candidate")
    assert "saved_status" in html and '"Saved"' in html and '"Updated: "' in html
    assert "innerHTML" not in html
    results = view_html("results")
    assert "c.reason" in results and "c.closest" in results


# 7. refresh prompt ----------------------------------------------------------
def test_refresh_prompt_uses_update_candidate(store):
    c = store.add_candidate(name="Rae", linkedin_url="https://linkedin.com/in/rae")
    body = recipes.refresh_candidate(c)
    assert "update_candidate" in body and f"candidate {c['id']}" in body and "Don't use save_candidate" in body
    assert recipes.GUARD in body and "—" not in body and "–" not in body
    page = local_client(store).get(f"/candidates/{c['id']}").text
    assert "update_candidate" in page and "Save this person" not in page
