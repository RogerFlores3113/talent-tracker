"""Cycle 13, builder A: skill highlights keep a fixed color per pick and sort in tiers (never filter)."""
import html as htmllib
import re
from urllib.parse import parse_qs, urlsplit

from conftest import local_client
from talent import searchquery

PEOPLE = {  # name -> skills
    "None Nan": ["rust"],
    "Only A": ["a"],
    "Only B": ["b"],
    "Only C": ["c"],
    "A and B": ["a", "b"],
    "A and C": ["a", "c"],
    "B and C": ["b", "c"],
    "All Three": ["c", "b", "a", "x"],
}
TIERS = ["All Three", "A and B", "A and C", "B and C", "Only A", "Only B", "Only C", "None Nan"]


def _seed(store, people=PEOPLE):
    for name, tags in people.items():
        store.add_candidate(name=name, tags=tags)


def _names(html):
    return re.findall(r'<a href="/candidates/\d+">([^<]+)</a>', html)


def _qs(href):
    return parse_qs(urlsplit(htmllib.unescape(href)).query)


def _bar(html):
    bar = html[html.index('aria-label="Highlighted skills"'):]
    return bar[:bar.index("</div>")]


def test_palette_by_click_position():
    got = searchquery.parse_hl("a,b,c,d,e,f,g")
    assert [c for _, c in got] == ["red", "yellow", "green", "blue", "purple", "orange"]
    assert len(got) == 6


def test_old_links_and_colored_links():
    assert searchquery.parse_hl("Python, go,python") == [("python", "red"), ("go", "yellow")]
    assert searchquery.parse_hl("python:blue,go") == [("python", "blue"), ("go", "red")]
    assert searchquery.parse_hl("a:red,b:red") == [("a", "red"), ("b", "yellow")]  # no two picks share a color
    assert searchquery.parse_hl("c:red") == [("c", "red")]


def test_old_hl_links_still_sort_and_color(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=a,b").text
    assert set(_names(html)[:2]) == {"A and B", "All Three"}
    assert re.findall(r'data-hl="(\w+)"[^>]*aria-label="Remove highlight: (\w+) \(\w+\)"', _bar(html)) == [
        ("red", "a"), ("yellow", "b")]


def test_color_stays_after_removing_an_earlier_pick(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=a,b,c").text
    hrefs = dict((s, h) for h, s in re.findall(r'href="([^"]+)"[^>]*aria-label="Remove highlight: (\w+) \(\w+\)"', _bar(html)))
    q = _qs(hrefs["a"])  # remove the first pick
    assert q["hl"] == ["b:yellow,c:green"]
    html2 = c.get("/candidates", params={"hl": q["hl"][0]}).text
    assert re.findall(r'data-hl="(\w+)"[^>]*aria-label="Remove highlight: (\w+) \(\w+\)"', _bar(html2)) == [
        ("yellow", "b"), ("green", "c")]
    # adding a new pick takes the free color (red), not the next one
    row = html2[html2.index(">Only A</a>"):]
    row = row[:row.index("</tr>")]
    add_a = re.search(r'href="([^"]+)"[^>]*aria-label="Highlight a"', row).group(1)
    assert _qs(add_a)["hl"] == ["b:yellow,c:green,a:red"]


def test_three_skill_tier_order(store):
    _seed(store)
    c = local_client(store)
    assert _names(c.get("/candidates?hl=a,b,c&sort=name&dir=asc").text) == TIERS
    assert _names(c.get("/candidates?hl=a:red,b:yellow,c:green&sort=name&dir=asc").text) == TIERS


def test_two_skill_tiers(store):
    _seed(store)
    c = local_client(store)
    names = _names(c.get("/candidates?hl=b,a&sort=name&dir=asc").text)
    # both; then B only (B was picked first); then A only; then neither
    assert names == ["A and B", "All Three", "B and C", "Only B", "A and C", "Only A", "None Nan", "Only C"]


def test_search_path_order(store):
    _seed(store)
    c = local_client(store)
    q = "Nan OR Only OR And OR All OR Three"
    names = _names(c.get("/candidates", params={"q": q, "hl": "a,b,c"}).text)
    assert [n for n in names if n in TIERS] == [n for n in TIERS if n in names]
    names = _names(c.get("/candidates", params={"q": q, "hl": "a,b,c", "qsort": "name", "qdir": "asc"}).text)
    assert names == [n for n in TIERS if n in names] and len(names) >= 4


def test_paging_keeps_the_tiers(store):
    for i in range(110):
        store.add_candidate(name=f"Pad {i:03d}", tags=["rust"])
    store.add_candidate(name="Late C", tags=["c"])
    store.add_candidate(name="Late AB", tags=["a", "b"])
    store.add_candidate(name="Late B", tags=["b"])
    c = local_client(store)
    first = _names(c.get("/candidates?hl=a,b,c").text)
    assert first[:3] == ["Late AB", "Late B", "Late C"]
    more = c.get("/candidates?hl=a:red,b:yellow,c:green&page_no=2&more=1", headers={"HX-Request": "true"})
    assert more.status_code == 200
    assert not set(_names(more.text)) & set(first)
    assert "hl=a%3Ared%2Cb%3Ayellow%2Cc%3Agreen" in more.text


def test_every_picked_chip_shows_even_with_many_skills(store):
    store.add_candidate(name="Many Mo", tags=["s1", "s2", "s3", "s4", "s5", "s6", "x", "y"])
    c = local_client(store)
    html = c.get("/candidates?hl=s6,s5,s4,s3,s2,s1").text
    row = html[html.index(">Many Mo</a>"):]
    row = row[:row.index("</tr>")]
    shown = re.findall(r'data-hl="(\w+)" href="[^"]*" aria-label="[^"]*" title="[^"]*">(\w+)</a>', row)
    assert shown == [("red", "s6"), ("yellow", "s5"), ("green", "s4"), ("blue", "s3"), ("purple", "s2"), ("orange", "s1")]
    assert 'class="hlrow" data-edge="red"' in html


def test_row_edge_and_wording(store):
    _seed(store)
    html = local_client(store).get("/candidates?hl=b,a").text
    assert html.count('class="hlrow"') == 6  # everyone with a picked skill
    row = html[html.index('data-edge="yellow"'):]
    assert row.count("hlrow") >= 1
    assert "People with more of these skills come first; when it's a tie, the skill you picked first wins. Nobody is hidden." in html
    assert "ainbow" not in html


def test_saved_search_keeps_colors(store):
    _seed(store)
    html = local_client(store).get("/candidates?hl=b:blue,a").text
    assert 'name="hl" value="b:blue,a:red"' in html
