"""Cycle 10, fixer Q: operator searches read notes too, c# / c++ / f# / .net are words of their own, repeated
prefixes and a prefix next to the same form filter mean either value, search tips and the read-back on every search
box, a type-ahead skill box with every skill, and a match count."""
import re

from talent import searchquery as sq
from talent import store as store_mod
from talent.store import Store


def _names(res):
    return sorted(h.candidate["name"] for h in res.hits)


def _notes_people(store):
    ann = store.add_candidate(name="Ann", about="Python developer", tags="Python", current_title="Engineer")
    di = store.add_candidate(name="Di", about="designer")
    ed = store.add_candidate(name="Ed", about="golang developer")
    fay = store.add_candidate(name="Fay", about="designer")
    store.log(di["id"], "note", "built python django services")
    store.log(fay["id"], "note", "python was used. " + "x " * 10 + "django too")
    return ann, di, ed, fay


# ------------------------------------------------------------------ 1. operators read notes
def test_or_finds_at_least_everyone_each_side_finds(store):
    _notes_people(store)
    py, go = _names(store.search_candidates("python")), _names(store.search_candidates("go"))
    both = _names(store.search_candidates("python OR go"))
    assert set(py) | set(go) <= set(both)
    assert both == ["Ann", "Di", "Ed", "Fay"]
    assert store.search_candidates("python OR go").total == 4


def test_a_phrase_finds_the_words_in_a_note(store):
    _notes_people(store)
    res = store.search_candidates('"python django"')
    assert _names(res) == ["Di"] and res.hits[0].reason == "Matched a note"
    assert "python django" in res.hits[0].snippet.lower()
    assert _names(store.search_candidates('"django python"')) == []


def test_near_reads_notes_and_counts_the_words_between(store):
    _notes_people(store)
    assert _names(store.search_candidates("python NEAR django")) == ["Di"]
    assert _names(store.search_candidates("python NEAR/20 django")) == ["Di", "Fay"]
    assert _names(store.search_candidates("django NEAR/1 python")) == ["Di"]  # either order


def test_profile_matches_still_come_first_then_notes_with_paging(store):
    _notes_people(store)
    first = store.search_candidates("python OR go", limit=2)
    assert [h.candidate["name"] for h in first.hits][0] in ("Ann", "Ed") and first.total == 4
    rest = store.search_candidates("python OR go", limit=2, offset=2)
    assert len(rest.hits) == 2
    assert sorted(h.candidate["name"] for h in [*first.hits, *rest.hits]) == ["Ann", "Di", "Ed", "Fay"]


def test_operator_search_with_filters_and_exclusions_still_applies_them(store):
    _notes_people(store)
    assert _names(store.search_candidates("python OR go -golang")) == ["Ann", "Di", "Fay"]
    assert _names(store.search_candidates("python OR go title:engineer")) == ["Ann"]


# ------------------------------------------------------------------ 2. symbol skills
def _symbol_people(store):
    store.add_candidate(name="Sharp", about="Writes C# and .NET services", tags="C#, .NET", current_title="Engineer")
    store.add_candidate(name="Plus", about="C++ systems work", tags="C++, C")
    store.add_candidate(name="Plain", about="plain c programmer", tags="C")
    store.add_candidate(name="Fsh", about="functional F# developer", tags="F#")
    store.add_candidate(name="Other", about="designer")


def test_symbol_skills_are_distinct_in_plain_search(store):
    _symbol_people(store)
    assert _names(store.search_candidates("c#")) == ["Sharp"]
    assert _names(store.search_candidates("c++")) == ["Plus"]
    assert _names(store.search_candidates("c")) == ["Plain", "Plus"]  # the skill C, not C# or C++ ("c" is in about)
    assert _names(store.search_candidates("f#")) == ["Fsh"]
    assert _names(store.search_candidates(".net")) == ["Sharp"]
    res = store.search_candidates("C# services")  # one person has both words; the rest are only closest matches
    assert res.exact_total == 1 and res.hits[0].candidate["name"] == "Sharp"


def test_symbol_skills_are_distinct_in_phrases_or_and_near(store):
    _symbol_people(store)
    assert _names(store.search_candidates('"c#"')) == ["Sharp"]
    assert _names(store.search_candidates('"c# and .net"')) == ["Sharp"]
    assert _names(store.search_candidates("c# OR f#")) == ["Fsh", "Sharp"]
    assert _names(store.search_candidates("c++ OR f#")) == ["Fsh", "Plus"]
    assert _names(store.search_candidates("c# NEAR .net")) == ["Sharp"]
    assert _names(store.search_candidates("-c# c++ OR c#")) == ["Plus"]
    assert _names(store.search_candidates("c OR c++ -c#")) == ["Plain", "Plus"]


def test_symbol_skills_in_notes(store):
    _symbol_people(store)
    other = store.search_candidates("Other").hits[0].candidate["id"]
    store.log(other, "note", "Moved from Java to C# last year")
    assert _names(store.search_candidates("c#")) == ["Other", "Sharp"]
    assert _names(store.search_candidates('"to c#"')) == ["Other"]
    assert _names(store.search_candidates("c++")) == ["Plus"]


def test_symbol_excerpt_and_read_back_show_the_symbol(store):
    _symbol_people(store)
    hit = store.search_candidates("c#").hits[0]
    assert "C#" in hit.snippet and "csharp" not in hit.snippet.lower()
    pq = sq.parse_query('"c#" OR c++')
    assert pq.explain() == '"c#" OR c++' or pq.explain() == 'c# OR c++'
    assert "c" not in pq.explain().replace("c#", "").replace("c++", "")


def test_symbol_index_follows_edits_and_deletes(store):
    _symbol_people(store)
    who = store.search_candidates("Other").hits[0].candidate["id"]
    store.update_candidate(who, about="now writes C#")
    assert _names(store.search_candidates("c#")) == ["Other", "Sharp"]
    store.update_candidate(who, about="designer")
    assert _names(store.search_candidates("c#")) == ["Sharp"]
    store.delete_candidate(store.search_candidates("Sharp").hits[0].candidate["id"])
    assert _names(store.search_candidates("c#")) == []
    store._conn.execute("INSERT INTO candidates_fts(candidates_fts) VALUES('integrity-check')")


def test_old_database_is_reindexed_by_the_new_migration(tmp_path, monkeypatch):
    path = tmp_path / "old.db"
    steps = store_mod._MIGRATIONS
    ours = next(i for i, st in enumerate(steps) if isinstance(st, tuple) and st[1] is store_mod._migrate_v10q_data)
    monkeypatch.setattr(store_mod, "_MIGRATIONS", steps[:ours])
    s = Store(path)
    a = s.add_candidate(name="Old Sharp", tags="C#")["id"]
    b = s.add_candidate(name="Old Plus", tags="C++")["id"]
    assert {h.candidate["id"] for h in s.search_candidates("c").hits} == {a, b}  # the old, merged reading
    s.close()
    monkeypatch.setattr(store_mod, "_MIGRATIONS", steps)
    s = Store(path)
    assert s.schema_version == len(steps)
    assert [h.candidate["id"] for h in s.search_candidates("c#").hits] == [a]
    assert [h.candidate["id"] for h in s.search_candidates("c++").hits] == [b]


def test_skill_prefix_still_exact(store):
    _symbol_people(store)
    assert _names(store.search_candidates("skill:c#")) == ["Sharp"]
    assert _names(store.search_candidates("skill:c++")) == ["Plus"]


# ------------------------------------------------------------------ 3. repeated prefixes and form values
def test_repeated_prefix_means_either_and_says_so(store):
    store.add_candidate(name="Gina", current_company="Google")
    store.add_candidate(name="Meta Max", current_company="Meta")
    store.add_candidate(name="Ann Acme", current_company="Acme")
    res = store.search_candidates("company:google company:meta")
    assert _names(res) == ["Gina", "Meta Max"]
    assert any("more than once" in n and "OR" in n for n in res.notes)
    pq = sq.parse_query("company:google company:meta")
    assert pq.explain() == "company: google OR meta"
    # typed with an explicit OR there is nothing to explain
    assert not sq.parse_query("company:google OR company:meta").notes
    store.add_candidate(name="Tina", current_title="Engineer")
    store.add_candidate(name="Mo", current_title="Manager")
    assert _names(store.search_candidates("title:engineer title:manager")) == ["Mo", "Tina"]


def test_form_value_and_typed_prefix_for_the_same_field_both_count(store):
    store.add_candidate(name="Tina", current_title="Engineer", location="Berlin", source="Referral")
    store.add_candidate(name="Mo", current_title="Manager", location="London", source="GitHub")
    store.add_candidate(name="Dee", current_title="Designer", location="Paris", source="Other")
    res = store.search_candidates("title:engineer", title="manager")
    assert _names(res) == ["Mo", "Tina"] and any("Title filter says manager" in n for n in res.notes)
    res = store.search_candidates("location:berlin", location="london")
    assert _names(res) == ["Mo", "Tina"] and any("Location filter" in n for n in res.notes)
    res = store.search_candidates("source:referral", source="GitHub")
    assert _names(res) == ["Mo", "Tina"] and any("Found via filter" in n for n in res.notes)
    res = store.search_candidates("title:engineer", title="engineer")  # the same value twice is not a conflict
    assert _names(res) == ["Tina"] and not res.notes


def test_skills_from_the_form_and_the_box_are_put_together_and_say_so(store):
    store.add_candidate(name="Py", tags="Python")
    store.add_candidate(name="Go", tags="Go")
    store.add_candidate(name="Both", tags="Python, Go")
    res = store.search_candidates("skill:go", skills=["python"], skill_mode="any")
    assert _names(res) == ["Both", "Go", "Py"] and any("Skills filter" in n for n in res.notes)
    res = store.search_candidates("skill:go", skills=["python"], skill_mode="all")
    assert _names(res) == ["Both"]


# ------------------------------------------------------------------ 4, 5, 6. pages
def test_candidates_page_has_tips_read_back_and_count(store, client):
    for i in range(3):
        store.add_candidate(name=f"Pat {i}", tags="Python")
    page = client.get("/candidates", params={"q": "python OR go"}).text
    assert "Search tips" in page and "How we read your search" in page and "python OR go" in page
    assert "3 people match" in page
    store.add_candidate(name="Zed Unique")
    one = client.get("/candidates", params={"q": "zed"}).text
    assert "1 person matches" in one
    assert "How we read your search" not in client.get("/candidates", params={"q": "python"}).text
    page = client.get("/candidates", params={"q": "company:google company:meta"}).text
    assert "more than once" in page


def test_today_search_shows_the_read_back_and_count(store, client):
    for i in range(3):
        store.add_candidate(name=f"Pat {i}", tags="Python")
    page = client.get("/", params={"q": '"python" OR go -recruiter'}).text
    assert "How we read your search" in page and "NOT recruiter" in page
    assert "3 people match" in page
    frag = client.get("/", params={"q": "python OR go"}, headers={"HX-Request": "true", "HX-Target": "results"}).text
    assert "How we read your search" in frag
    assert "How we read your search" not in client.get("/", params={"q": "python"}).text


def test_the_skill_box_lists_every_skill(store, client):
    for i in range(60):
        store.add_candidate(name=f"Person {i}", tags=f"skill{i:02d}")
    store.add_candidate(name="Rare", tags="Typescript")
    page = client.get("/candidates").text
    assert re.search(r'<input[^>]*name="tag"[^>]*list="skill-list"', page)
    for i in (0, 39, 40, 59):
        assert f'<option value="skill{i:02d}">' in page
    assert '<option value="Typescript">' in page
    assert "<select name=\"tag\"" not in page
    # picking a skill still filters
    assert "Rare" in client.get("/candidates", params={"tag": "typescript"}).text
