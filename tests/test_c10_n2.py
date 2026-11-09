"""Cycle 10, fixer N (part 2): skill value typo hints, closest-match label, new people marked, cheap counts."""
import re

from test_c10_n import _save


def test_skill_value_typo_hint(store):
    store.add_candidate(name="Pia", tags=["Python"])
    res = store.search_candidates("skill:pyhton")
    assert res.total == 0 and res.suggestion == "skill:Python"
    assert store.search_candidates("skill:python").suggestion is None


def test_closest_matches_are_labelled_on_the_table(store, client):
    store.add_candidate(name="Ann", about="python developer")
    page = client.get("/candidates", params={"q": "python rust"}).text
    assert "No one has every word" in page
    assert "No one has every word" not in client.get("/candidates", params={"q": "python"}).text


def test_opening_a_saved_search_marks_the_new_people(store, client):
    store.add_candidate(name="Old Py", about="python")
    row = store.save_search("Python", {"q": "python"})
    store.add_candidate(name="Fresh Py", about="python")
    r = client.get(f"/saved-searches/{row['id']}", follow_redirects=False)
    assert "new_after=" in r.headers["location"]
    page = client.get(r.headers["location"]).text
    assert re.search(r"Fresh Py</a>\s*<span class=\"badge new\">New", page)
    assert not re.search(r"Old Py</a>\s*<span class=\"badge new\">New", page)
    assert store.saved_search_counts(store.get_saved_search(row["id"]))[1] == 0  # the chip count did reset


def test_saved_search_without_words_opens_newest_first(store, client):
    store.add_candidate(name="A", location="Berlin")
    row = store.save_search("Berlin", {"location": "Berlin"})
    loc = client.get(f"/saved-searches/{row['id']}", follow_redirects=False).headers["location"]
    assert "sort=added" in loc and "dir=desc" in loc


def test_saved_counts_cached_until_a_write(store):
    store.add_candidate(name="A", about="python")
    row = store.save_search("Python", {"q": "python"})
    calls = []
    real = store._saved_search_counts
    store._saved_search_counts = lambda r: (calls.append(1), real(r))[1]
    r = store.get_saved_search(row["id"])
    assert store.saved_search_counts(r) == store.saved_search_counts(r) == (1, 0)
    assert len(calls) == 1
    store.add_candidate(name="B", about="python")
    assert store.saved_search_counts(r) == (2, 1) and len(calls) == 2


def test_count_first_leaves_the_rest_uncounted(store):
    _save(store, 4)
    rows = store.list_saved_searches(counts=True, count_first=2)
    assert [r["count"] is None for r in rows] == [False, False, True, True]
