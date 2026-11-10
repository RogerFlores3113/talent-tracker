"""Cycle 12, builder Y: clicking a skill highlights it in a color and sorts by it (never filters)."""
import html as htmllib
import re
from urllib.parse import parse_qs, urlsplit

from conftest import local_client

PEOPLE = {  # name -> skills
    "Neither Nia": ["rust"],
    "Go Gus": ["go"],
    "Py Pia": ["Python"],
    "Both Bea": ["python", "go", "sql"],
    "Zed Zoe": ["go", "python", "react", "aws", "ml"],
}


def _seed(store):
    for name, tags in PEOPLE.items():
        store.add_candidate(name=name, tags=tags)


def _names(html):
    return re.findall(r'<a href="/candidates/\d+">([^<]+)</a>', html)


def _qs(href):
    return parse_qs(urlsplit(htmllib.unescape(href)).query)


def test_hl_sorts_but_does_not_filter(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=python,go").text
    names = _names(html)
    assert len(names) == 5 and set(names) == set(PEOPLE)
    assert set(names[:2]) == {"Both Bea", "Zed Zoe"}
    assert names[2] == "Py Pia" and names[3] == "Go Gus" and names[4] == "Neither Nia"
    # the other order puts go-only before python-only
    names = _names(c.get("/candidates?hl=go,python").text)
    assert names[2] == "Go Gus" and names[3] == "Py Pia"


def test_hl_with_search_keeps_everyone_and_ranks(store):
    _seed(store)
    c = local_client(store)
    base = _names(c.get("/candidates?q=Bea OR Gus OR Pia OR Nia OR Zoe").text)
    assert len(base) == 5
    names = _names(c.get("/candidates?q=Bea OR Gus OR Pia OR Nia OR Zoe&hl=python,go").text)
    assert len(names) == 5
    assert set(names[:2]) == {"Both Bea", "Zed Zoe"} and names[2:] == ["Py Pia", "Go Gus", "Neither Nia"]
    # with a column sort and with Colored first too
    names = _names(c.get("/candidates?q=Bea OR Gus OR Pia OR Nia OR Zoe&hl=go&qsort=name&qdir=asc").text)
    assert names[:3] == ["Both Bea", "Go Gus", "Zed Zoe"] and len(names) == 5
    assert len(_names(c.get("/candidates?q=Bea OR Gus OR Pia OR Nia OR Zoe&hl=go&cf=1").text)) == 5


def test_normal_sort_inside_groups(store):
    _seed(store)
    c = local_client(store)
    names = _names(c.get("/candidates?hl=go&sort=name&dir=asc").text)
    assert names == ["Both Bea", "Go Gus", "Zed Zoe", "Neither Nia", "Py Pia"]


def test_clean_input(store):
    _seed(store)
    c = local_client(store)
    names = _names(c.get("/candidates?hl= Python ,PYTHON, go ,,").text)
    assert len(names) == 5 and set(names[:2]) == {"Both Bea", "Zed Zoe"}
    html = c.get("/candidates?hl=" + ",".join(f"s{i}" for i in range(9))).text
    assert len(re.findall(r'aria-label="Remove highlight: ', html)) == 6
    for bad in ["nope", "x'; DROP TABLE candidates;--", "%25", "a\"><script>alert(1)</script>"]:
        r = c.get("/candidates", params={"hl": bad})
        assert r.status_code == 200 and len(_names(r.text)) == 5
        assert "<script>alert(1)" not in r.text
    assert store.search_candidates("").total == 5


def test_bar_colors_and_links(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=python,go,sql&tag=python&sort=name&dir=asc").text
    assert 'role="group" aria-label="Highlighted skills"' in html
    assert "Nobody is hidden." in html
    bar = html[html.index('aria-label="Highlighted skills"'):]
    bar = bar[:bar.index("</div>")]
    assert re.findall(r'data-hl="(\w+)"[^>]*aria-label="Remove highlight: (\w+) \(\w+\)"', bar) == [
        ("red", "python"), ("yellow", "go"), ("green", "sql")]
    hrefs = re.findall(r'href="([^"]+)"[^>]*aria-label="Remove highlight: (\w+) \(\w+\)"', bar)
    q = _qs(hrefs[1][0])  # remove go
    assert q["hl"] == ["python:red,sql:green"] and q["tag"] == ["python"] and q["sort"] == ["name"]
    clear = re.search(r'href="([^"]+)">Clear</a>', bar).group(1)
    q = _qs(clear)
    assert "hl" not in q and q["tag"] == ["python"]
    assert "People with more of the highlighted skills come first" in html
    assert 'aria-label="Highlighted skills"' not in c.get("/candidates").text


def test_row_chip_toggle_and_order(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=go&tag=python&sort=name&dir=asc").text
    row = html[html.index(">Zed Zoe</a>"):]
    row = row[:row.index("</tr>")]
    chips = re.findall(r'<a class="chip( hl)?"(?: id="[^"]+")?(?: data-skill="[^"]+")?( data-hl="\w+")? href="([^"]+)" aria-label="([^"]+)" title="[^"]*">([^<]+)</a>', row)
    assert [x[4] for x in chips] == ["go", "python", "react", "aws"]  # go first, still four shown
    assert "+1" in row
    go = chips[0]
    assert go[1] == ' data-hl="red"' and go[3] == "go, highlighted red (remove)"
    q = _qs(go[2])
    assert "hl" not in q and q["tag"] == ["python"] and q["sort"] == ["name"]
    py = chips[1]
    assert py[3] == "Highlight python" and _qs(py[2])["hl"] == ["go:red,python:yellow"] and _qs(py[2])["tag"] == ["python"]


def test_candidate_page_chip_starts_highlight(store):
    _seed(store)
    cid = store.search_candidates("Zoe").hits[0].candidate["id"]
    html = local_client(store).get(f"/candidates/{cid}").text
    assert 'href="/candidates?hl=python"' in html and "?tag=" not in html.split('class="chips"')[1].split("</span>")[0]


def test_show_more_keeps_order(store):
    for i in range(130):
        store.add_candidate(name=f"Pad {i:03d}", tags=["rust"] if i < 125 else ["go"])
    c = local_client(store)
    first = _names(c.get("/candidates?hl=go").text)
    assert set(first[:5]) == {f"Pad {i}" for i in range(125, 130)}
    more = c.get("/candidates?hl=go&page_no=2&more=1", headers={"HX-Request": "true"})
    assert more.status_code == 200 and len(_names(more.text)) == 30
    assert not set(_names(more.text)) & set(first)
    assert 'hl=go' in more.text  # keep carries the highlight into Show more and chip links


def test_saved_search_keeps_highlight(store):
    _seed(store)
    c = local_client(store)
    html = c.get("/candidates?hl=python,go").text
    assert 'name="hl" value="python:red,go:yellow"' in html


def test_highlight_alone_is_not_called_a_match(store, client):
    from talent.sample_data import load_sample_data
    load_sample_data(store)
    page = client.get("/candidates", params={"hl": "python"}).text
    assert 'class="match-count"' not in page
    page = client.get("/candidates", params={"hl": "python", "location": "boston"}).text
    assert 'class="match-count"' in page
