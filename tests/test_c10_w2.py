"""Cycle 10, fixer W2: every saved-search chip counted and explained, hints for common query slips, a useful
zero-results page, color counters that follow the search, and a tour finish that leaves no dead chips."""
import html
import re

from conftest import flash_of, local_client
from talent import searchquery as sq
from talent.sample_data import load_sample_data


def _many(store, n=9):
    store.add_candidate(name="Pia Python", about="python developer", tags=["Python"])
    for i in range(n):
        store.save_search(f"Search {i}", {"q": "python"})


# ---------------------------------------------------------------- 1. chips
def test_every_chip_has_count_and_tooltip(store, client):
    _many(store)
    for url in ("/", "/candidates"):
        page = client.get(url).text
        chips = re.findall(r'<a class="chip saved-link"[^>]*>', page)
        assert len(chips) == 9
        assert all('title="python"' in c and "1 person" in c for c in chips), url
    assert all(r["count"] == 1 for r in store.list_saved_searches(counts=True))


def test_new_marker_shows_past_the_sixth_chip(store, client):
    _many(store)
    store.add_candidate(name="Newbie", about="python")
    page = client.get("/").text
    assert page.count("+1 new") == 9


def test_tooltip_shows_filters_too(store, client):
    store.add_candidate(name="A", about="python", location="Berlin")
    store.save_search("Berlin py", {"q": "python OR go", "location": "Berlin"})
    page = client.get("/candidates").text
    assert 'title="python OR go, in Berlin"' in page


# ---------------------------------------------------------------- 2. hints
def test_prefix_with_two_words_suggests_quotes(store):
    store.add_candidate(name="A", tags=["Machine Learning"])
    store.add_candidate(name="B", about="nothing")
    res = store.search_candidates("skill:zebra crossing")
    assert res.suggestion == 'skill:"zebra crossing"'
    assert any('skill:"zebra crossing"' in n for n in res.notes)


def test_prefix_hint_is_quiet_when_something_matches(store):
    store.add_candidate(name="A", about="berlin python", location="Berlin")
    res = store.search_candidates("location:berlin python")
    assert res.total == 1 and res.suggestion is None and not res.notes


def test_proximity_from_other_tools(store):
    store.add_candidate(name="A", about="python and django together")
    res = store.search_candidates('"python django"~3')
    assert res.suggestion == "python NEAR/3 django"
    assert any("NEAR/3" in n for n in res.notes)


def test_lowercase_not_and_near_and_bare_minus():
    pq = sq.parse_query("python not manager")
    assert "NOT manager" in pq.notes[0] and pq.apply_advice("python not manager") == "python NOT manager"
    assert sq.parse_query("python - manager").apply_advice("python - manager") == "python -manager"
    assert any("minus on its own" in n for n in sq.parse_query("-").notes)
    near = sq.parse_query("python near django")
    assert not near.notes and near.apply_advice("python near django") == "python NEAR django"
    assert not sq.parse_query("lives near boston").notes  # plain English stays quiet


def test_lowercase_near_hint_only_when_nobody_matches(store):
    store.add_candidate(name="A", about="python developer")
    res = store.search_candidates("python near django")
    assert res.suggestion == "python NEAR django" and any("lowercase near" in n for n in res.notes)
    assert store.search_candidates("python near").notes == []


def test_hints_reach_the_read_back_line(store, client):
    store.add_candidate(name="A", about="python")
    page = client.get("/candidates", params={"q": "python not manager"}).text
    assert "How we read your search" in page and "NOT in capitals" in page
    today = client.get("/", params={"q": "python - x"}).text
    assert "minus on its own" in today


# ---------------------------------------------------------------- 3. zero results
def test_zero_results_page_has_did_you_mean_and_a_way_to_widen(store, client):
    store.add_candidate(name="Pia", tags=["Python"])
    page = client.get("/candidates", params={"q": "skill:pyhton"}).text
    row = re.search(r'<td colspan="9" class="none">(.*?)</td>', page, re.S).group(1)
    assert "No candidates match that" in row and "Did you mean" in row and "skill:Python" in html.unescape(row)
    assert "To widen it" in row
    today = client.get("/", params={"q": "skill:zebra crossing"}).text
    assert 'No one matches that yet.' in today and "Did you mean" in today and "To widen it" in today
    filt = client.get("/candidates", params={"q": "zzzz", "location": "Mars"}).text
    assert "take off a filter chip" in filt


# ---------------------------------------------------------------- 4. color counters
def test_color_counters_follow_the_query_and_filters(store, client):
    a = store.add_candidate(name="A", about="python", location="Berlin")
    b = store.add_candidate(name="B", about="python", location="Paris")
    c = store.add_candidate(name="C", about="rust", location="Berlin")
    store.set_color(a["id"], "red")
    store.set_color(c["id"], "red")
    assert store.color_counts()["red"] == 2

    def counts(**params):
        page = client.get("/candidates", params=params).text
        return {k: int(v) for k, v in re.findall(r'data-color-count="(\w+)">(\d+)<', page)}

    assert counts() == {**{k: 0 for k in ("orange", "yellow", "green", "blue", "purple")}, "red": 2, "none": 1}
    q = counts(q="python")
    assert q["red"] == 1 and q["none"] == 1
    assert counts(q="zzzz")["none"] == 0 and counts(q="zzzz")["red"] == 0
    loc = counts(location="Berlin")
    assert loc["red"] == 2 and loc["none"] == 0
    # a chosen color does not hide the other colors' numbers
    assert counts(q="python", color="red")["none"] == 1
    assert store.color_counts(location="Paris")["none"] == 1 and b


# ---------------------------------------------------------------- 5. tour finish and sample data
def test_removing_sample_data_removes_only_the_searches_it_made(store):
    store.add_candidate(name="Real Rustacean", about="rust")
    store.save_search("Real rust", {"q": "rust"})  # saved before the sample people came
    load_sample_data(store)
    store.save_search("Go on samples", {"q": "go"})  # matches sample people only
    store.save_search("Rust both", {"q": "rust"})  # matches a real person too
    store.save_search("Nobody", {"q": "zzzqqq"})  # was always empty: the recruiter's own
    store.save_search("AI made", {"q": "go"}, by="ai")
    from talent.sample_data import remove_sample_data
    out = remove_sample_data(store)
    names = {r["name"] for r in store.list_saved_searches()}
    assert out["deleted_searches"] == ["Go on samples"]
    assert names == {"Real rust", "Rust both", "Nobody", "AI made"}


def test_tour_finish_is_one_line_and_leaves_no_dead_chip(store):
    c = local_client(store)
    c.post("/tour/start", data={"mode": "full"})
    c.post("/tour/load", follow_redirects=False)
    for i, w in enumerate(("rust", "python", "kafka")):
        store.save_search(f"Demo {i}", {"q": w})
    msg = flash_of(c.post("/tour/finish", follow_redirects=False))
    assert "Also removed 3 saved searches that only matched sample people" in msg
    assert store.list_saved_searches() == []
    js = (__import__("pathlib").Path(__file__).resolve().parents[1] / "src/talent/static/app.js").read_text()
    assert "Also remove my saved search" not in js
