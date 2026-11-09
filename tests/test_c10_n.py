"""Cycle 10, fixer N: NEAR search, prefix typo hints, typo help for OR/phrase searches, all saved searches on
Today, pinned saved searches, and the same-second "new since you looked" fix."""
import re

from conftest import local_client
from test_mcp import call, structured, text

from talent import searchquery as sq
from talent.mcp_server import build_server


def _names(res):
    return sorted(h.candidate["name"] for h in res.hits)


def _people(store):
    store.add_candidate(name="Ann Close", about="Python and Django developer", current_title="Engineer")
    store.add_candidate(name="Bob Far", about="Python " + "filler " * 12 + "Django")
    store.add_candidate(name="Cy Order", about="Django then Python")
    store.add_candidate(name="Di Kubernetes", about="kubernetes operator and Go")


# ------------------------------------------------------------------ 1. NEAR
def test_near_parses_and_reads():
    pq = sq.parse_query("python NEAR django")
    assert pq.operator_mode and pq.explain() == "python within 5 words of django"
    assert sq.parse_query("python NEAR/3 django").groups[0][0].dist == 3
    assert sq.parse_query("python near/3 django").groups[0][0].dist == 3
    assert sq.parse_query('python NEAR/2 "machine learning"').explain() == 'python within 2 words of "machine learning"'
    chain = sq.parse_query("a NEAR b NEAR/9 c")
    assert chain.explain() == "a, b and c all within 9 words of each other"
    assert sq.parse_query("python NEAR/999 django").groups[0][0].dist == sq.NEAR_MAX


def test_plain_near_stays_a_word():
    for q in ("near me", "lives near boston", "python near django"):
        pq = sq.parse_query(q)
        assert not pq.operator_mode and not pq.structured and pq.explain() == ""
    assert sq.parse_query("lives near boston").bare == "lives near boston"


def test_lone_near_is_dropped_with_a_note():
    pq = sq.parse_query("NEAR python")
    assert pq.bare == "python" and any("NEAR needs" in n for n in pq.notes)


def test_near_in_the_how_we_read_line_with_filters(store, client):
    _people(store)
    page = client.get("/candidates", params={"q": 'python NEAR django -recruiter title:engineer'}).text
    assert "How we read your search" in page and "python within 5 words of django" in page


def test_near_store_search(store):
    _people(store)
    assert _names(store.search_candidates("python NEAR django")) == ["Ann Close", "Cy Order"]  # either order
    assert _names(store.search_candidates("python NEAR/20 django")) == ["Ann Close", "Bob Far", "Cy Order"]
    assert _names(store.search_candidates("python NEAR/1 django")) == ["Ann Close", "Cy Order"]
    assert _names(store.search_candidates("python NEAR/20 django -filler")) == ["Ann Close", "Cy Order"]
    assert _names(store.search_candidates("python NEAR django title:engineer")) == ["Ann Close"]
    assert _names(store.search_candidates("(python NEAR django) OR kubernetes")) == [
        "Ann Close", "Cy Order", "Di Kubernetes"]
    assert _names(store.search_candidates("-cy python NEAR django")) == ["Ann Close"]


def test_near_with_phrase_operand(store):
    store.add_candidate(name="Eve", about="machine learning with python")
    store.add_candidate(name="Fay", about="machine and then much later learning python")
    assert _names(store.search_candidates('"machine learning" NEAR python')) == ["Eve"]


def test_near_odd_input_never_breaks(store):
    _people(store)
    for q in ("NEAR", "NEAR NEAR", 'python NEAR "', "python NEAR -django", "python NEAR/0 django", "NEAR(a b, 3)"):
        store.search_candidates(q)


def test_near_help_text_everywhere(store, client):
    store.add_candidate(name="Someone")
    from pathlib import Path
    assert "NEAR" in client.get("/").text
    assert "NEAR" in sq.__doc__
    root = Path(sq.__file__).parents[2]
    assert "NEAR" in (root / "src/talent/mcp_server.py").read_text()
    assert "NEAR" in (root / "skills/talent-search-triage-candidates/SKILL.md").read_text()


def test_near_in_mcp(store):
    _people(store)
    server = build_server(store)
    out = text(call(server, "search_candidates", query="python NEAR django"))
    assert "Ann Close" in out and "Bob Far" not in out


# ------------------------------------------------------------------ 2. prefix typo hints
def test_nearest_prefix():
    assert sq.nearest_prefix("skil") == "skill"
    assert sq.nearest_prefix("tittle") == "title"
    assert sq.nearest_prefix("compnay") == "company"
    assert sq.nearest_prefix("locaton") == "location"
    assert sq.nearest_prefix("zzzz") is None and sq.nearest_prefix("title") is None


def test_prefix_hint_parser():
    pq = sq.parse_query("skil:python")
    assert pq.hints == [("skil", "skill")] and pq.bare == "python"
    assert pq.fix_prefixes("skil:python") == "skill:python"
    assert sq.parse_query("locaton:").fix_prefixes("locaton:") == "location:"
    far = sq.parse_query("foo:bar")
    assert not far.hints and far.unknown_prefix and far.bare == "foo bar" and far.notes


def test_prefix_hint_store_and_mcp(store):
    _people(store)
    res = store.search_candidates("skil:kubernetes")
    assert res.suggestion == "skill:kubernetes" and not res.corrected
    assert _names(res) == ["Di Kubernetes"]  # still searched, as a plain word
    assert any("isn't a search prefix" in n for n in res.notes)
    assert store.search_candidates("skill:kubernetes").suggestion is None
    out = call(build_server(store), "search_candidates", query="tittle:engineer")
    assert "Did you mean title:engineer?" in text(out)
    assert structured(out)["suggestion"] == "title:engineer" and structured(out)["notes"]


def test_prefix_hint_web(store, client):
    _people(store)
    page = client.get("/candidates", params={"q": "skil:kubernetes"}).text
    assert "Did you mean" in page and "skill%3Akubernetes" in page
    assert "isn&#39;t a search prefix" in page or "isn't a search prefix" in page
    home = client.get("/search", params={"q": "skil:kubernetes"}).text
    assert "Did you mean" in home


# ------------------------------------------------------------------ 3. typo help for OR / quotes / NEAR
def test_typo_help_for_or_and_phrase(store):
    _people(store)
    res = store.search_candidates("python OR kubrnetes")
    assert res.suggestion == "python OR kubernetes" and not res.corrected
    assert _names(res) == ["Ann Close", "Bob Far", "Cy Order"]  # never silently changed
    assert store.search_candidates('"python developer" OR kubrnetes').suggestion == '"python developer" OR kubernetes'
    assert store.search_candidates("pythn NEAR django").suggestion == "python NEAR django"
    assert store.search_candidates("python OR kubernetes").suggestion is None
    assert store.search_candidates("python OR kubrnetes", fix_typos=False).suggestion is None


def test_typo_help_leaves_names_and_half_typed_words(store):
    _people(store)
    assert store.search_candidates("python OR kuber").suggestion is None  # still typing: a prefix, not a typo
    assert store.search_candidates("python OR kuber ").suggestion is None
    assert store.search_candidates("Dana OR Okafor").suggestion is None


def test_typo_hint_web_link(store, client):
    _people(store)
    page = client.get("/candidates", params={"q": "python OR kubrnetes"}).text
    assert "Did you mean" in page and "kubernetes" in page


# ------------------------------------------------------------------ 4. Today shows all saved searches, pinning
def _save(store, n):
    for i in range(n):
        store.save_search(f"Search {i:02d}", {"q": f"word{i}"})


def test_today_lists_all_saved_searches_with_disclosure(store, client):
    store.add_candidate(name="Someone")
    _save(store, 9)
    page = client.get("/").text
    assert "Show all 9 saved searches" in page
    for i in range(9):
        assert f"Search {i:02d}" in page
    assert "<details class=\"saved-more\">" in page
    assert "/candidates#saved-searches" in page


def test_today_few_saved_searches_has_no_disclosure(store, client):
    store.add_candidate(name="Someone")
    _save(store, 3)
    assert "saved-more" not in client.get("/").text.replace(".saved-more", "")


def test_today_chip_says_what_the_numbers_are(store, client):
    store.add_candidate(name="Someone")
    store.save_search("Pythonistas", {"q": "python"})
    page = client.get("/").text
    assert "Saved search Pythonistas: 0 people, 0 new since you looked" in page


def test_pin_orders_and_survives(store):
    _save(store, 4)
    first = store.get_saved_search("Search 00")
    store.pin_saved_search(first["id"])
    assert [r["name"] for r in store.list_saved_searches()][:2] == ["Search 00", "Search 03"]
    store.pin_saved_search(first["id"], False)
    assert store.list_saved_searches()[0]["name"] == "Search 03"


def test_pin_routes_and_buttons(store, client):
    store.add_candidate(name="Someone")
    _save(store, 8)
    old = store.get_saved_search("Search 00")
    r = client.post(f"/saved-searches/{old['id']}/pin", follow_redirects=False)
    assert r.status_code == 303
    page = client.get("/").text
    assert page.index("Search 00") < page.index("Search 07")  # pinned first
    assert f"/saved-searches/{old['id']}/unpin" in page and "Pinned" in page
    assert f"/saved-searches/{old['id']}/unpin" in client.get("/candidates").text
    client.post(f"/saved-searches/{old['id']}/unpin")
    assert not store.get_saved_search(old["id"])["pinned"]
    assert client.post("/saved-searches/9999/pin").status_code == 404


def test_saved_searches_migration_columns(store):
    cols = {r["name"] for r in store._rows("PRAGMA table_info(saved_searches)")}
    assert {"pinned", "seen_max_id"} <= cols


# ------------------------------------------------------------------ 5. new since you looked, same second
def test_new_count_catches_someone_added_in_the_same_second(store, monkeypatch):
    from talent import store as store_mod
    monkeypatch.setattr(store_mod, "now_iso", lambda: "2030-01-01T00:00:00+00:00")  # everything in one second
    store.add_candidate(name="Old Python", about="python")
    row = store.save_search("Python", {"q": "python"})
    assert store.saved_search_counts(store.get_saved_search(row["id"])) == (1, 0)
    store.add_candidate(name="Same Second", about="python")
    assert store.saved_search_counts(store.get_saved_search(row["id"])) == (2, 1)
    store.mark_search_viewed(row["id"])
    assert store.saved_search_counts(store.get_saved_search(row["id"])) == (2, 0)
    store.add_candidate(name="Again", about="python")
    assert store.saved_search_counts(store.get_saved_search(row["id"])) == (3, 1)


def test_new_count_falls_back_to_time_for_old_rows(store):
    store.add_candidate(name="Old Python", about="python")
    row = store.save_search("Python", {"q": "python"})
    store._conn.execute("UPDATE saved_searches SET seen_max_id = NULL, last_viewed_at = '2000-01-01T00:00:00+00:00'")
    store._conn.commit()
    assert store.saved_search_counts(store.get_saved_search(row["id"]))[1] == 1
