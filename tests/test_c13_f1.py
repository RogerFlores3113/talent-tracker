"""Cycle 13, fixer F1: skill highlight polish, "Use all" ordering, import clean-up."""
import re

from conftest import local_client
from talent import importers


def _run(store, text, **kw):
    header, rows = importers.read_csv(text)
    return importers.import_rows(store, header, rows, importers.guess_mapping(header, rows), backup=False, **kw)


# ------------------------------------------------------------------ import
def test_names_and_titles_that_start_with_a_symbol_import_as_text(store):
    text = ("Name,Email,Title,Skills\n"
            "=Ana Cruz,ana@x.com,=cmd(),python\n"
            "+Bo Lee,bo@x.com,-Lead,go\n"
            "-Cy Dunn,cy@x.com,@home,rust\n"
            "@Di Ray,di@x.com,Dev,sql\n")
    r = _run(store, text)
    assert r["added"] == 4 and r["skipped_rows"] == 0
    ana = store.find_duplicate("ana@x.com")
    assert ana["name"] == "=Ana Cruz" and ana["current_title"] == "=cmd()"
    assert store.find_duplicate("bo@x.com")["name"] == "+Bo Lee"
    assert store.find_duplicate("cy@x.com")["current_title"] == "@home"
    assert store.find_duplicate("di@x.com")["name"] == "@Di Ray"


def test_skills_lose_trailing_emoji_and_symbol_only_skills_are_dropped(store):
    text = ("Name,Email,Skills\n"
            'Ana,ana@x.com,"python 🚀, c++, -2+3, +1+1, 🚀, @cmd, go ✨, c#"\n')
    r = _run(store, text)
    assert r["added"] == 1
    tags = store.find_duplicate("ana@x.com")["tags"]
    assert tags == ["python", "c++", "go", "c#"]
    assert any("-2+3" in w and "letters" in w for w in r["warnings"])
    assert len([w for w in r["warnings"] if "a skill needs some letters" in w]) == 3
    assert any("@cmd" in w and "can't start with" in w for w in r["warnings"])


def test_a_row_with_no_name_is_still_skipped_with_a_reason(store):
    r = _run(store, "Name,Email\n,x@x.com\nAna,ana@x.com\n")
    assert r["skipped_rows"] == 1 and any("no name" in s for s in r["skipped"])


# ------------------------------------------------------------------ highlights
def _seed(store):
    ids = {}
    for name, tags in {"Zed Both": ["python", "data engineer"], "Amy Py": ["python"], "Bob DE": ["data engineer"],
                       "Cat None": ["rust"]}.items():
        ids[name] = store.add_candidate(name=name, tags=tags)["id"]
    return ids


def test_bar_chips_say_color_number_count_and_have_ids(store):
    _seed(store)
    html = local_client(store).get("/candidates?hl=python,data%20engineer").text
    bar = html[html.index('aria-label="Highlighted skills"'):]
    bar = bar[:bar.index("</div>")]
    assert 'aria-label="Remove highlight: python (red)"' in bar
    assert 'aria-label="Remove highlight: data engineer (yellow)"' in bar
    assert re.search(r'id="hlbar-python-[0-9a-f]{4}"', bar) and 'data-count="2"' in bar
    assert '<span class="hl-n" aria-hidden="true">1</span> python <span class="hl-count" aria-hidden="true">(2)</span>' in bar
    assert 'id="hl-bar" tabindex="-1"' in html
    assert 'id="hl-status"' in html and 'aria-live="polite"' in html


def test_row_chips_have_ids_and_links_carry_a_fragment(store):
    ids = _seed(store)
    html = local_client(store).get("/candidates?hl=python").text
    row = html[html.index(">Zed Both</a>"):]
    row = row[:row.index("</tr>")]
    slug = re.search(r'id="chip-%d-(python-[0-9a-f]{4})"' % ids["Zed Both"], row).group(1)
    assert f'#chip-{ids["Zed Both"]}-{slug}"' in row  # remove python: back to this chip
    assert re.search(r'href="[^"]*hl=python[^"]*data\+engineer[^"]*#chip-%s-data-engineer-[0-9a-f]{4}"' % ids["Zed Both"], row)
    bar_remove = re.search(r'id="hlbar-python[^"]*"[^>]*href="([^"]+)"', html).group(1)
    assert bar_remove == "/candidates?sort=color&amp;dir=asc#removed-python"
    clear = re.search(r'href="([^"]+)">Clear all \d+ highlights?</a>', html[html.index('id="hl-bar"'):]).group(1)
    assert clear.endswith("#hlcleared")


def test_rows_say_top_match_and_how_many_they_match(store):
    _seed(store)
    html = local_client(store).get("/candidates?hl=python,data%20engineer").text
    zed = html[html.index(">Zed Both</a>"):]
    zed = zed[:zed.index("</tr>")]
    assert "Top match: python, red." in zed and ">2 of 2<" in zed
    amy = html[html.index(">Amy Py</a>"):]
    assert ">1 of 2<" in amy[:amy.index("</tr>")]
    bob = html[html.index(">Bob DE</a>"):]
    assert "Top match: data engineer, yellow." in bob[:bob.index("</tr>")]
    cat = html[html.index(">Cat None</a>"):]
    assert "Top match" not in cat[:cat.index("</tr>")] and ">0 of 2<" in cat[:cat.index("</tr>")]
    assert "of 2" not in local_client(store).get("/candidates").text


def test_counts_follow_the_current_filters(store):
    _seed(store)
    c = local_client(store)
    assert 'data-count="2"' in c.get("/candidates?hl=python").text
    assert 'data-count="1"' in c.get("/candidates?hl=python&tag=data%20engineer").text
    assert 'data-count="2"' in c.get("/candidates?hl=python&q=zed OR amy OR bob").text.replace("python,", "python")  # q path
    q = c.get("/candidates?hl=python&q=python").text
    assert 'data-count="2"' in q


def test_label_wording_and_no_color_arrow_while_highlighting(store):
    _seed(store)
    c = local_client(store)
    on = c.get("/candidates?hl=python").text
    assert "Highlighted skills first, then by label color." in on
    assert "Sorted by color: red" not in on
    head = on[on.index('class="colorcell"'):]
    head = head[:head.index("</th>")]
    assert "Label color" in head and "▲" not in head and "▼" not in head and "aria-sort" not in head
    off = c.get("/candidates").text
    assert "Sorted by color: red" in off and "Label color" in off
    assert '<span class="fine">Label:</span>' in off and 'aria-label="Label colors"' in off


# ------------------------------------------------------------------ bulk bar
def test_bulk_bar_groups_send_out_actions(store):
    _seed(store)
    html = local_client(store).get("/candidates").text
    bar = re.search(r'<form id="bulk-form".*?</form>', html, re.S).group(0)
    grp = re.search(r'<div class="bulk-actions bulk-send" data-needs-ticks role="group" aria-label="Send out">.*?</div>', bar, re.S).group(0)
    for v in ("compare", "export", "export_xlsx", "share"):
        assert f'name="action" value="{v}"' in grp
    assert 'id="share-xlsx"' in grp and 'value="color"' not in grp


def test_use_all_follows_the_screen_order(store):
    people = {}
    for name in ["Zara", "Yan", "Xi", "Wes", "Val", "Uma", "Tom", "Sue"]:  # ids run opposite to the name order
        people[name] = store.add_candidate(name=name, tags=["x"])["id"]
    c = local_client(store)
    page = c.get("/candidates?sort=name&dir=asc").text
    assert 'name="f_sort" value="name"' in page and 'name="f_dir" value="asc"' in page
    form = {"action": "compare", "select_all": "1", "f_sort": "name", "f_dir": "asc"}
    r = c.post("/candidates/bulk", data=form, follow_redirects=False)
    want = [people[n] for n in ["Sue", "Tom", "Uma", "Val", "Wes", "Xi"]]
    assert f"ids={'%2C'.join(map(str, want))}" in r.headers["location"] and "of=8" in r.headers["location"]
    csv = c.post("/candidates/bulk", data={**form, "action": "export"}).content.decode("utf-8-sig")
    # export_csv keeps its own order for the file; the ids it was handed are all eight
    assert all(n in csv for n in people)


def test_use_all_follows_highlight_order_too(store):
    ids = _seed(store)
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "compare", "select_all": "1", "f_hl": "python,data engineer",
                                         "f_sort": "name", "f_dir": "asc"}, follow_redirects=False)
    assert r.headers["location"].startswith(f"/candidates/compare?ids={ids['Zed Both']}%2C{ids['Amy Py']}%2C{ids['Bob DE']}%2C{ids['Cat None']}")


def test_compare_message_says_two_to_six(store):
    _seed(store)
    c = local_client(store)
    r = c.post("/candidates/bulk", data={"action": "compare"}, follow_redirects=False)
    assert r.status_code == 303
    assert "Tick two to six people to compare." in c.get("/candidates").text
