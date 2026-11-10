"""Cycle 14, D1: job-title highlight picks, user-made skill aliases, and the keyboard path to highlights."""
from __future__ import annotations

import html as htmllib
import io
import re
import zipfile
from urllib.parse import parse_qs, urlsplit

import anyio
import pytest
from conftest import local_client
from mcp.types import CallToolResult
from test_c13_c import read_xlsx

from talent import compare, picks, searchquery, xlsx_export
from talent.mcp_server import build_server


def names_of(html):
    return re.findall(r'<a href="/candidates/\d+">([^<]+)</a>', html)


def seed(store, rows):
    for name, title, tags in rows:
        store.add_candidate(name=name, current_title=title, tags=tags)


PEOPLE = [
    ("Ann Both", "Data Engineer", ["python", "sql"]),
    ("Bo Senior", "Sr. Data Engineer", ["go"]),
    ("Cy Level", "Data Engineer II", ["rust"]),
    ("Di Python", "Analyst", ["Python"]),
    ("Ed Nothing", "Designer", ["figma"]),
    ("Flo History", "Manager", ["python", "go"]),
]


# ------------------------------------------------------------------ 1. title folds and picks
def test_fold_title_drops_levels_and_punctuation():
    assert picks.fold_title("Sr. Data Engineer") == "data engineer"
    assert picks.fold_title("Data Engineer II") == "data engineer"
    assert picks.fold_title("Staff, Principal Data-Engineer 3") == "data engineer"
    assert picks.fold_title("Lead Junior IV Data Engineer (Contract)") == "data engineer contract"
    assert picks.fold_title("Senior") == ""
    assert picks.make_title_pick("Senior") == ""
    assert picks.label("title:data engineer") == "title: Data Engineer"


def test_parse_hl_keeps_old_links_and_title_picks():
    assert searchquery.parse_hl("python,go") == [("python", "red"), ("go", "yellow")]
    assert searchquery.parse_hl("python:red") == [("python", "red")]
    got = searchquery.parse_hl("python:red,title:Sr. Data Engineer:yellow,title:data engineer")
    assert got == [("python", "red"), ("title:data engineer", "yellow")]
    assert searchquery.join_hl(got) == "python:red,title:data engineer:yellow"
    assert searchquery.describe({"hl": "python:red,title:data engineer:yellow"}) == "highlighting python, title: Data Engineer"


def test_title_matches_current_title_only_not_history(store):
    c = store.add_candidate(name="Hist Hal", current_title="Manager", tags=["x"],
                            work_history=[{"title": "Data Engineer", "company": "Acme"}])
    assert picks.person_hits(c["tags"], c["current_title"], ["title:data engineer"]) == [False]
    html = local_client(store).get("/candidates?hl=title:data engineer:red").text
    assert 'class="hlrow"' not in html


def test_tier_order_python_plus_title(store):
    seed(store, PEOPLE)
    html = local_client(store).get("/candidates?hl=python:red,title:data engineer:yellow&sort=name&dir=asc").text
    names = names_of(html)
    assert len(names) == 6
    assert names[0] == "Ann Both"  # both picks
    # one pick each: python holders (earlier pick) come before the title-only people
    assert names[1:3] == ["Di Python", "Flo History"]
    assert set(names[3:5]) == {"Bo Senior", "Cy Level"}
    assert set(names[5:]) == {"Ed Nothing"}
    assert ">2 of 2<" in html and ">1 of 2<" in html and ">0 of 2<" in html
    assert "Top match: title: Data Engineer, yellow." in html


def test_bar_chip_reads_title_with_count_and_variants(store):
    seed(store, PEOPLE)
    html = htmllib.unescape(local_client(store).get("/candidates?hl=title:data engineer:yellow").text)
    bar = html[html.index('aria-label="Highlighted skills"'):]
    bar = bar[:bar.index("</div>")]
    assert 'aria-label="Remove highlight: title: Data Engineer (yellow)"' in bar
    assert "(3, 3 variants)" in bar
    assert "Clear all 1 highlight<" in bar


def test_bar_counts_with_a_search_too(store):
    seed(store, PEOPLE)
    html = htmllib.unescape(local_client(store).get("/candidates?q=engineer&hl=title:data engineer").text)
    assert "(3, 3 variants)" in html


def test_title_cell_is_a_button_like_chip_that_toggles(store):
    seed(store, PEOPLE)
    c = local_client(store)
    html = c.get("/candidates?sort=name&dir=asc").text
    row = html[html.index(">Bo Senior</a>"):]
    row = row[:row.index("</tr>")]
    m = re.search(r'<a class="chip title-chip"[^>]*href="([^"]+)" aria-label="Highlight title: Sr. Data Engineer"[^>]*>Sr. Data Engineer</a>', row)
    assert m, row
    q = parse_qs(urlsplit(htmllib.unescape(m.group(1))).query)
    assert q["hl"] == ["title:data engineer:red"]
    on = c.get("/candidates?hl=title:data engineer:red&sort=name&dir=asc").text
    row = on[on.index(">Bo Senior</a>"):]
    row = row[:row.index("</tr>")]
    m = re.search(r'<a class="chip title-chip hl"[^>]*data-hl="red"[^>]*href="([^"]+)"', row)
    assert m and "hl=" not in htmllib.unescape(m.group(1))  # clicking again removes it


def test_profile_title_is_a_chip_and_skill_chips_keep_picks(store):
    seed(store, PEOPLE)
    cid = store.search_candidates("Ann Both").hits[0].candidate["id"]
    c = local_client(store)
    html = c.get(f"/candidates/{cid}?hl=title:data engineer:yellow").text
    assert 'aria-label="Highlighted yellow, title: Data Engineer (remove)"' not in html  # add only on a profile
    assert 'aria-label="Highlighted yellow, title: Data Engineer"' in html
    chip = re.search(r'href="([^"]+)" aria-label="Highlight python"', html).group(1)
    assert parse_qs(urlsplit(htmllib.unescape(chip)).query)["hl"] == ["title:data engineer:yellow,python"]
    plain = c.get(f"/candidates/{cid}").text
    assert 'aria-label="Highlight title: Data Engineer"' in plain


def test_profile_picks_come_from_the_table_referer(store):
    seed(store, PEOPLE)
    cid = store.search_candidates("Ann Both").hits[0].candidate["id"]
    c = local_client(store)
    html = c.get(f"/candidates/{cid}", headers={"referer": "http://127.0.0.1:8000/candidates?hl=go:blue",
                                                  "host": "127.0.0.1:8000"}).text
    assert "Highlight python" in html


# ------------------------------------------------------------------ xlsx and csv
def test_xlsx_matched_picks_with_titles(store):
    seed(store, PEOPLE)
    ids = [r["id"] for r in store._rows("SELECT id FROM candidates ORDER BY id")]
    rows = [store.get_candidate(i) for i in ids]
    data = xlsx_export.table_workbook(store, rows, len(rows), hl=[("python", "red"), ("title:data engineer", "yellow")])
    book = read_xlsx(data)
    head = book["Candidates"]["rows"][0]
    assert head[:2] == ["Name", "Matched picks"]
    out = {r[0]: r[1] for r in book["Candidates"]["rows"][1:]}
    assert out["Ann Both"] == "python, title: Data Engineer (2 of 2)"
    assert out["Bo Senior"] == "title: Data Engineer (1 of 2)"
    assert out["Ed Nothing"] in ("", None)
    about = dict(book["About this export"]["rows"][1:])
    assert about["Highlighted picks"] == "python (red), title: Data Engineer (yellow)"


def test_csv_in_order_has_matched_picks(store):
    seed(store, PEOPLE)
    ids = [r["id"] for r in store._rows("SELECT id FROM candidates ORDER BY id")]
    raw = xlsx_export.csv_in_order(store, ids, [("title:data engineer", "red")]).decode("utf-8-sig")
    lines = raw.splitlines()
    assert lines[0].split(",")[1] == "Matched picks"
    assert "title: Data Engineer (1 of 1)" in raw


# ------------------------------------------------------------------ 2. aliases
def alias_seed(store):
    seed(store, [
        ("R1", "Dev", ["react"]), ("R2", "Dev", ["react"]), ("R3", "Dev", ["react.js"]),
        ("R4", "Dev", ["React.JS", "react"]), ("P1", "Dev", ["python"]),
        ("C1", "Dev", ["C#"]), ("C2", "Dev", ["C++"]),
    ])


def test_alias_store_roundtrip_and_chains(store):
    alias_seed(store)
    assert store.skill_alias_map() == {}
    store.add_skill_alias("react.js", "react")
    assert store.skill_alias_map() == {"react.js": "react"}
    assert sorted(store.skill_group_names("react.js")) == ["react", "react.js"]
    with pytest.raises(ValueError):
        store.add_skill_alias("react", "REACT")
    store.add_skill_alias("react", "reactjs")  # the kept name itself folds into another: its group follows
    assert store.skill_alias_map() == {"react": "reactjs", "react.js": "reactjs"}
    assert store.split_skill_alias("reactjs") == 2
    assert store.skill_alias_map() == {}


def test_alias_does_not_rewrite_stored_tags(store):
    alias_seed(store)
    store.add_skill_alias("react.js", "react")
    r3 = store.search_candidates("R3").hits[0].candidate
    assert r3["tags"] == ["react.js"]


def test_alias_in_filters_search_and_highlight_counts(store):
    alias_seed(store)
    store.add_skill_alias("react.js", "react")
    c = local_client(store)
    html = c.get("/candidates?tag=react").text
    assert set(names_of(html)) == {"R1", "R2", "R3", "R4"}
    html = c.get("/candidates?skills=react.js").text
    assert set(names_of(html)) == {"R1", "R2", "R3", "R4"}
    both = c.get("/candidates?skills=react,python&skill_mode=all").text
    assert names_of(both) == []
    hit = store.search_candidates("skill:react").hits
    assert {h.candidate["name"] for h in hit} == {"R1", "R2", "R3", "R4"}
    html = htmllib.unescape(c.get("/candidates?hl=react.js").text)  # picking the alias counts the group once
    bar = html[html.index('aria-label="Highlighted skills"'):]
    assert 'data-skill="react"' in bar and "(4)" in bar
    # the row chip of the alias name is the same pick
    row = html[html.index(">R3</a>"):]
    row = row[:row.index("</tr>")]
    assert 'class="chip hl"' in row and 'data-skill="react"' in row


def test_alias_pick_toggles_from_either_name(store):
    alias_seed(store)
    store.add_skill_alias("react.js", "react")
    c = local_client(store)
    html = c.get("/candidates?hl=react:red&sort=name&dir=asc").text
    row = html[html.index(">R3</a>"):]
    row = row[:row.index("</tr>")]
    href = re.search(r'<a class="chip hl"[^>]*href="([^"]+)"', row).group(1)
    assert "hl=" not in htmllib.unescape(href)  # clicking react.js removes the react pick


def test_alias_in_compare_groups_and_counts(store):
    alias_seed(store)
    ids = [store.search_candidates(n).hits[0].candidate["id"] for n in ("R1", "R3")]
    d = compare.build(store, ids)
    assert d["shared"] == []  # react vs react.js are two skills until merged
    store.add_skill_alias("react.js", "react")
    d = compare.build(store, ids)
    assert d["shared"] == ["react"] and d["counts"]["react"] == 2
    assert compare.hl_counts(d["people"], ["react", "title:dev"]) == {ids[0]: 2, ids[1]: 2}
    html = local_client(store).get(f"/candidates/compare?ids={ids[0]},{ids[1]}&hl=title:dev:blue,react:red").text
    assert "Highlighted picks:" in html and "title: Dev" in html and "Has 2 of 2 highlighted picks" in html


def test_alias_in_xlsx_matched_picks(store):
    alias_seed(store)
    store.add_skill_alias("react.js", "react")
    r3 = store.search_candidates("R3").hits[0].candidate
    data = xlsx_export.table_workbook(store, [r3], 1, hl=[("react", "red")])
    rows = read_xlsx(data)["Candidates"]["rows"]
    assert rows[1][1] == "react.js (1 of 1)"  # written as the person has it


# ------------------------------------------------------------------ suggestions
def test_suggestions_pairs_counts_and_overlap(store):
    alias_seed(store)
    seed(store, [("E1", "x", ["engineer"]), ("E2", "x", ["engineering"]), ("J1", "x", ["js"]), ("J2", "x", ["JavaScript"]),
                 ("K1", "x", ["k8s", "kubernetes"]), ("PL", "x", ["tests", "test"])])
    pairs = {frozenset((p["a"].lower(), p["b"].lower())): p for p in store.skill_lookalikes(100)}
    p = pairs[frozenset(("react", "react.js"))]
    assert p["keep"] == "react" and p["keep_count"] == 3 and p["other_count"] == 2 and p["both"] == 1
    assert frozenset(("engineer", "engineering")) in pairs
    assert frozenset(("js", "javascript")) in pairs
    assert frozenset(("k8s", "kubernetes")) in pairs and pairs[frozenset(("k8s", "kubernetes"))]["both"] == 1
    assert frozenset(("tests", "test")) in pairs


def test_suggestions_never_pair_c_sharp_with_c_plus_plus(store):
    alias_seed(store)
    seed(store, [("C3", "x", ["c"]), ("F1", "x", ["f#"]), ("F2", "x", ["f"])])
    for p in store.skill_lookalikes(100):
        assert not ({p["a"].lower(), p["b"].lower()} & {"c#", "c++", "f#"})


def test_short_list_pairs(store):
    seed(store, [("A", "x", ["golang"]), ("B", "x", ["go"]), ("C", "x", ["postgres"]), ("D", "x", ["postgresql"]),
                 ("E", "x", ["ml"]), ("F", "x", ["machine learning"]), ("G", "x", ["node"]), ("H", "x", ["node.js"]),
                 ("I", "x", ["ts"]), ("J", "x", ["typescript"])])
    got = {frozenset((p["a"].lower(), p["b"].lower())) for p in store.skill_lookalikes(100)}
    for pair in (("golang", "go"), ("postgres", "postgresql"), ("ml", "machine learning"), ("node", "node.js"), ("ts", "typescript")):
        assert frozenset(pair) in got


def test_not_the_same_is_remembered_and_merged_pairs_vanish(store):
    alias_seed(store)
    assert any({"react", "react.js"} == {p["a"].lower(), p["b"].lower()} for p in store.skill_lookalikes())
    store.reject_skill_pair("react.js", "React")
    assert not any({"react", "react.js"} == {p["a"].lower(), p["b"].lower()} for p in store.skill_lookalikes())
    store2 = store
    store2.reject_skill_pair("react", "react.js")  # same pair either way round: still one row
    assert store._row("SELECT count(*) AS n FROM skill_alias_rejects")["n"] == 1


def test_merge_removes_the_pair_and_split_brings_it_back(store):
    alias_seed(store)
    store.add_skill_alias("react.js", "react")
    assert not store.skill_lookalikes()
    store.split_skill_alias("react.js")
    assert store.skill_lookalikes()


def test_skill_names_sorted_case_insensitive_and_each_group_once(store):
    seed(store, [("A", "x", ["Zed", "apple"]), ("B", "x", ["banana", "react.js"]), ("C", "x", ["react"])])
    store.add_skill_alias("react.js", "react")
    got = store.skill_names_sorted()
    assert [g["tag"] for g in got] == ["apple", "banana", "react", "Zed"]
    assert [g["count"] for g in got if g["tag"] == "react"] == [2]


# ------------------------------------------------------------------ settings and routes
def test_settings_lists_pairs_and_groups(store):
    alias_seed(store)
    c = local_client(store)
    html = c.get("/settings").text
    assert 'id="skill-names"' in html and "react / react.js" in html
    assert "3 + 2 people, 1 has both" in html or "3 + 2 people, 1 have both" in html
    r = c.post("/settings/skill-names/merge", data={"a": "react", "b": "react.js", "kept": "react.js"}, follow_redirects=True)
    assert r.status_code == 200 and "Now react counts as react.js" in r.text
    assert store.skill_alias_map() == {"react": "react.js"}
    assert "Split" in r.text and "also counts" in r.text
    r = c.post("/settings/skill-names/split", data={"name": "react.js"}, follow_redirects=True)
    assert "Split react.js" in r.text and store.skill_alias_map() == {}
    r = c.post("/settings/skill-names/reject", data={"a": "react", "b": "react.js"}, follow_redirects=True)
    assert "won't suggest" in htmllib.unescape(r.text)
    assert "react / react.js" not in c.get("/settings").text


def test_highlight_bar_hint_offers_treat_as_one(store):
    alias_seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=react.js").text
    assert "Also 2 people with react. Treat as the same skill?" in html
    assert "Treat as one" in html and "Not the same" in html
    r = c.post("/settings/skill-names/merge", data={"alias": "react.js", "kept": "react", "next": "/candidates?hl=react.js"},
               follow_redirects=False)
    assert r.status_code == 303 and r.headers["location"].endswith("#hlmerged")
    assert "Treat as the same skill?" not in c.get("/candidates?hl=react").text
    r = c.post("/settings/skill-names/reject", data={"alias": "react.js", "kept": "react", "next": "/candidates?hl=react"})
    assert "Treat as the same skill?" not in c.get("/candidates?hl=react").text


def test_next_is_only_ever_a_page_of_this_server(store):
    alias_seed(store)
    r = local_client(store).post("/settings/skill-names/merge", data={"alias": "react.js", "kept": "react", "next": "//evil.example/x"},
                                 follow_redirects=False)
    assert r.headers["location"].startswith("/settings")


def test_import_result_says_new_skill_pairs(store):
    seed(store, [("Old", "x", ["react"])])
    c = local_client(store)
    csv_bytes = b"Name,Skills\nNew Nell,react.js\n"
    up = c.post("/import", files={"file": ("p.csv", csv_bytes, "text/csv")})
    token = re.search(r'name="token" value="(\w+)"', up.text).group(1)
    r = c.post("/import/run", data={"token": token, "source": "t", "col0": "name", "col1": "tags", "filename": "p.csv"})
    assert "1 skill pair looks the same" in r.text and 'href="/settings#skill-names"' in r.text


# ------------------------------------------------------------------ 3. keyboard path
def test_skip_link_and_box_without_picks(store):
    seed(store, PEOPLE)
    html = local_client(store).get("/candidates").text
    skips = re.findall(r'class="skip-link" href="([^"]+)">([^<]+)<', html)
    assert ("#hl-add", "Skip to skill highlights") in skips
    assert 'id="hl-add"' in html and "<label for=\"hl-add\">Highlight a skill</label>" in html
    with_picks = local_client(store).get("/candidates?hl=python").text
    assert ("#hl-bar", "Skip to skill highlights") in re.findall(r'class="skip-link" href="([^"]+)">([^<]+)<', with_picks)
    assert 'id="hl-add"' in with_picks


def test_highlight_box_adds_next_pick_with_status_fragment(store):
    seed(store, PEOPLE)
    c = local_client(store)
    r = c.get("/candidates?sort=name&dir=asc&hl_add=python", follow_redirects=False)
    assert r.status_code == 303
    loc = r.headers["location"]
    assert "hl=python%3Ared" in loc and "sort=name" in loc and re.search(r"#hlbar-python-[0-9a-f]{4}$", loc)
    r = c.get("/candidates?hl=python:red&hl_add=title: Data Engineer II", follow_redirects=False)
    assert "hl=python%3Ared%2Ctitle%3Adata+engineer%3Ayellow" in r.headers["location"]
    r = c.get("/candidates?hl=python:red&hl_add=Python", follow_redirects=False)  # already picked: nothing new
    assert r.headers["location"].count("python") >= 1 and "yellow" not in r.headers["location"]
    r = c.get("/candidates?hl_add=title:", follow_redirects=False)
    assert r.headers["location"].endswith("#hlempty")
    full = ",".join(f"s{i}:{col}" for i, col in enumerate(searchquery.HL_PALETTE))
    r = c.get(f"/candidates?hl={full}&hl_add=extra", follow_redirects=False)
    assert r.headers["location"].endswith("#hlfull")
    follow = c.get("/candidates?hl_add=python", follow_redirects=True)
    assert follow.status_code == 200 and 'data-skill="python"' in follow.text


def test_highlight_box_resolves_aliases(store):
    alias_seed(store)
    store.add_skill_alias("react.js", "react")
    r = local_client(store).get("/candidates?hl_add=React.JS", follow_redirects=False)
    assert "hl=react%3Ared" in r.headers["location"]


def test_clear_label_counts_and_focus_target_exist(store):
    seed(store, PEOPLE)
    html = local_client(store).get("/candidates?hl=python,go,title:data engineer").text
    assert "Clear all 3 highlights</a>" in html
    js = open("src/talent/static/app.js", encoding="utf-8").read()
    assert 'hash === "hlcleared"' in js and 'target = box || status' in js


def test_skill_filter_datalist_sorted_ignoring_case(store):
    seed(store, [("A", "x", ["Zed", "apple", "Banana"])])
    html = local_client(store).get("/candidates").text
    block = html[html.index('<datalist id="skill-list">'):]
    block = block[:block.index("</datalist>")]
    assert re.findall(r'value="([^"]+)"', block) == ["apple", "Banana", "Zed"]


# ------------------------------------------------------------------ MCP
def call(server, tool, **args):
    return anyio.run(lambda: server.call_tool(tool, args))


def text(res):
    content = res.content if isinstance(res, CallToolResult) else res[0]
    return "\n".join(c.text for c in content)


def test_mcp_lists_lookalikes_but_cannot_merge(store):
    alias_seed(store)
    srv = build_server(store)
    names = {t.name for t in anyio.run(srv.list_tools)}
    assert "list_skill_lookalikes" in names
    assert not any("alias" in n or "merge_skill" in n for n in names)
    out = text(call(srv, "list_skill_lookalikes"))
    assert "react" in out and "react.js" in out and "/settings#skill-names" in out
    store.add_skill_alias("react.js", "react")
    assert "Already treated as one" in text(call(srv, "list_skill_lookalikes"))
    assert store.skill_alias_map() == {"react.js": "react"}  # reading changed nothing
