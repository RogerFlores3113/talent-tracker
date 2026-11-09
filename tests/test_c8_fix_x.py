"""Fixer X: search syntax (prefix punctuation, negated prefixes, exact exclusions, lowercase or, OR in prefixes)
and saved searches (color, counts, lookups, messages)."""
from datetime import datetime, timedelta, timezone

import pytest
from mcp.server.fastmcp.exceptions import ToolError

from talent import trust
from talent.mcp_server import build_server
from talent.searchquery import clean_params, describe, parse_query, search_kwargs

from conftest import local_client
from test_mcp import call, structured, text


def names(res):
    return sorted(h.candidate["name"] for h in res.hits)


@pytest.fixture
def people(store):
    a = store.add_candidate(name="Ana Ruiz", current_title="Backend Engineer", current_company="Acme",
                            about="Systems in C++ and Python.", tags=["c++", "python", "node.js"],
                            source="Referral from Alex", location="Remote")["id"]
    b = store.add_candidate(name="Ben Cole", current_title="Recruiter", current_company="Hirely",
                            about="Hires Python people.", tags=["python"], source="LinkedIn")["id"]
    c = store.add_candidate(name="Cy Dee", current_title="Recruiting Coordinator", current_company="Bolt",
                            about="Schedules interviews. Cares about design systems.", tags=["ci/cd"],
                            source="Sample data")["id"]
    d = store.add_candidate(name="Di Fox", current_title="Designer", about="Product design", tags=["figma", "go"],
                            source="Referral")["id"]
    e = store.add_candidate(name="Cam Lee", current_title="Engineering Manager", about="Leads engineers, writes Go.",
                            tags=["go", ".net"])["id"]
    return dict(ana=a, ben=b, cy=c, di=d, cam=e)


# ---------------------------------------------------------------- parser
def test_prefix_values_keep_punctuation():
    for typed, want in (("skill:c++", "c++"), ("skill:node.js", "node.js"), ("skill:.net", ".net"),
                        ("tag:ci/cd", "ci/cd"), ('skill:"c#"', "c#")):
        assert parse_query(typed).fields["skill"] == [want]


def test_skill_prefix_finds_punctuated_tags(store, people):
    for q, who in (("skill:c++", ["Ana Ruiz"]), ("skill:node.js", ["Ana Ruiz"]), ("skill:ci/cd", ["Cy Dee"]),
                   ("skill:.net", ["Cam Lee"])):
        assert names(store.search_candidates(q)) == who, q


def test_lowercase_or_between_words_is_or():
    pq = parse_query("python or go")
    assert pq.operator_mode and [i.text for i in pq.groups[0]] == ["python", "go"]
    assert parse_query("or").groups and not parse_query("or").operator_mode
    assert not parse_query("python or").operator_mode


def test_lowercase_or_searches_either(store, people):
    assert names(store.search_candidates("figma or rust")) == ["Di Fox"]
    got = names(store.search_candidates("figma or recruiter"))
    assert "Ben Cole" in got and "Di Fox" in got and "Ana Ruiz" not in got


def test_or_inside_a_prefix_joins_the_prefix(store, people):
    assert names(store.search_candidates("skill:figma OR go")) == ["Cam Lee", "Di Fox"]
    assert names(store.search_candidates("title:designer OR manager")) == ["Cam Lee", "Di Fox"]
    pq = parse_query("skill:python OR go")
    assert pq.fields["skill"] == ["python", "go"] and pq.skill_any and not pq.groups


def test_or_between_a_word_and_a_prefix_says_so():
    pq = parse_query("go OR skill:python")
    assert pq.notes and "skill" in pq.notes[0]


def test_negated_prefix_filters_that_field(store, people):
    assert "Di Fox" not in names(store.search_candidates("design -source:referral"))
    got = names(store.search_candidates("-source:referral"))
    assert "Ana Ruiz" not in got and "Di Fox" not in got and "Ben Cole" in got
    # the word "engineer" in the title only, not anywhere in the profile
    got = names(store.search_candidates("-title:designer"))
    assert "Di Fox" not in got and "Cy Dee" in got  # Cy only writes about design systems
    assert "Ben Cole" in names(store.search_candidates("NOT skill:go python"))
    assert names(store.search_candidates("python NOT skill:c++")) == ["Ben Cole"]


def test_exclusion_is_the_exact_word_not_the_stem(store, people):
    got = names(store.search_candidates("-recruiter"))
    assert "Ben Cole" not in got and "Cy Dee" in got
    assert "Cy Dee" in names(store.search_candidates("schedules -recruiter"))
    assert "Cy Dee" in names(store.search_candidates("interviews -designer"))  # "design systems" is not "designer"


def test_short_or_punctuated_last_words_are_not_prefixes(store):
    store.add_candidate(name="Cora Cain", about="Loves cooking and cats")
    store.add_candidate(name="Max Sys", about="C++ developer")
    assert names(store.search_candidates("c++")) == ["Max Sys"]
    assert names(store.search_candidates("c")) == []  # one letter is a word, not "starts with c"; and C++ is its own word now (cycle 10, Q)
    assert names(store.search_candidates("cook")) == ["Cora Cain"]  # three letters still complete as you type


def test_source_prefix_matches_the_found_via_label(store, people):
    assert names(store.search_candidates("source:sample")) == ["Cy Dee"]
    assert names(store.search_candidates("source:referral")) == ["Ana Ruiz", "Di Fox"]
    assert names(store.search_candidates("source:alex")) == ["Ana Ruiz"]
    # the dropdown filter stays exact
    assert names(store.search_candidates("", source="Referral")) == ["Di Fox"]


def test_explain_reads_the_query_back():
    pq = parse_query('title:engineer "machine learning" python OR go -recruiter')
    line = pq.explain()
    assert "(python OR go)" in line and "NOT recruiter" in line and "title: engineer" in line
    assert '"machine learning"' in line
    assert parse_query("python go").explain() == ""
    assert "NOT found via: referral" in parse_query("-source:referral").explain()


# ---------------------------------------------------------------- saved searches
def test_saved_search_keeps_color_everywhere(store, people):
    store.set_candidate_color(people["ana"], "red") if hasattr(store, "set_candidate_color") else None
    with store._lock, store._conn:
        store._conn.execute("UPDATE candidates SET color = 'red' WHERE id = ?", (people["ana"],))
    row = store.save_search("Red python", {"q": "python", "color": "red"})
    assert row["params"]["color"] == "red" and "red color" in describe(row["params"])
    assert search_kwargs(row["params"])["color"] == "red"
    assert store.saved_search_counts(row)[0] == 1
    _, res = store.run_saved_search(row["id"])
    assert names(res) == ["Ana Ruiz"]
    s = build_server(store)
    got = structured(call(s, "run_saved_search", name_or_id="Red python"))
    assert got["total"] == 1
    assert clean_params({"color": "pink"}) == {}


def test_saved_search_count_has_the_typo_help_opening_has(store, people):
    row = store.save_search("Typo", {"q": "pyhton"})
    assert store.saved_search_counts(row)[0] >= 1


def test_run_saved_search_prefers_the_exact_name_then_the_id(store, people):
    first = store.save_search("Python folks", {"q": "python"})
    second = store.save_search("2", {"q": "figma"})
    s = build_server(store)
    assert structured(call(s, "run_saved_search", name_or_id="2"))["total"] == 1  # the one named "2" (figma)
    assert "Di Fox" in text(call(s, "run_saved_search", name_or_id="2"))
    assert "Ana Ruiz" in text(call(s, "run_saved_search", name_or_id=first["id"]))
    assert second["id"] == 2


def test_added_days_zero_means_not_set_on_both_sides(store, people):
    assert "added_days" not in clean_params({"added_days": "0", "q": "x"})
    s = build_server(store)
    assert structured(call(s, "search_candidates", query="python", added_days=0))["total"] == 2


def test_negative_contacted_days_is_refused_on_the_ai_side(store, people):
    s = build_server(store)
    with pytest.raises(ToolError):
        call(s, "search_candidates", query="python", contacted="older", contacted_days=-3)


def test_save_search_says_what_it_left_out(store, people):
    s = build_server(store)
    with pytest.raises(ToolError, match="up to 60"):  # a silly number of years is refused outright now
        call(s, "save_search", name="Silly", query="python", min_years=9999999)
    out = call(s, "save_search", name="Silly", query="python", added_days=9999999)
    assert "added_days" in text(out) and "Not saved" in text(out)
    ok = call(s, "save_search", name="Fine", query="python", min_years=3)
    assert "Not saved" not in text(ok)


def test_collision_and_cap_messages_are_for_the_ai(store, people):
    s = build_server(store)
    call(s, "save_search", name="Mine", query="python")
    with pytest.raises(ToolError) as e:
        call(s, "save_search", name="mine", query="go")
    assert "Pick another name." in str(e.value) and "rename" not in str(e.value)
    for i in range(19):
        store.save_search(f"Extra {i}", {"q": f"w{i}"}, by="ai")
    with pytest.raises(ToolError) as e:
        call(s, "save_search", name="One more", query="go")
    assert "can't delete" in str(e.value) and "Delete one" not in str(e.value)


def test_suggest_mode_save_search_checks_name_and_cap_before_holding(store, people):
    store.save_search("Taken", {"q": "python"})
    s = build_server(store)
    row = trust.touch(store, None)
    trust.set_trust(store, row["id"], "suggest")
    with pytest.raises(ToolError) as e:
        call(s, "save_search", name="taken", query="go")
    assert "Pick another name." in str(e.value)
    assert not trust_pending(store)
    res = call(s, "save_search", name="Free", query="go")
    assert text(res).startswith("Waiting for the recruiter's review")


def trust_pending(store):
    from talent import review
    return review.pending(store)


def test_exp_min_over_exp_max_explains_itself_on_the_web(store, people):
    c = local_client(store)
    r = c.get("/candidates", params={"q": "python", "exp_min": "9", "exp_max": "2"})
    assert "both be true" in r.text


def test_candidates_page_shows_how_we_read_your_search(store, people):
    c = local_client(store)
    r = c.get("/candidates", params={"q": "python OR go -recruiter"})
    assert "How we read your search" in r.text and "NOT recruiter" in r.text
    assert "How we read your search" not in c.get("/candidates", params={"q": "python"}).text
    r = c.get("/candidates", params={"q": "skill:python OR go"})
    assert "skill: python OR go" in r.text


def test_search_tips_copy_mentions_lowercase_or(store, people):
    r = local_client(store).get("/")
    assert "small letters" in r.text and "Write OR in capitals" not in r.text


# ------------------------------------------------------------------ re-review: c# tags and huge years
def test_tags_keep_a_trailing_hash_and_lose_a_leading_one(store):
    from talent.store import split_tags
    assert split_tags("c#, f#, #python") == ["c#", "f#", "python"]
    store.add_candidate(name="Cee Sharp", tags="c#, .net")
    store.add_candidate(name="Plain C", tags="c")
    res = call(build_server(store), "search_candidates", query="skill:c#")
    assert "Cee Sharp" in text(res) and "Plain C" not in text(res)


def test_mcp_refuses_years_of_experience_over_sixty(store):
    for tool, kw in (("search_candidates", {"query": "python"}), ("save_search", {"name": "Old hands", "query": "python"})):
        with pytest.raises(ToolError, match="up to 60"):
            call(build_server(store), tool, min_years=500, **kw)
