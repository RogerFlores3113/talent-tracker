"""Cycle 15 T: the in-chat views wear the Talent Search theme in a strict host, whatever colors the host sends.

Skipped without Playwright. Screenshots go to /home/user/scratch/c15/shots (or $C15_SHOTS) for looking at.
"""
import json
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from talent.mcp_views import theme_blocks

ROOT = Path(__file__).resolve().parent.parent
pw = pytest.importorskip("playwright.sync_api")
SHOTS = Path(os.environ.get("C15_SHOTS", "/home/user/scratch/c15/shots"))
BLOCKS = theme_blocks()

VIEWS = {  # name: (tool, args, what to do before looking)
    "people-card": ("search_candidates", {"query": "", "limit": 6}, None),
    "people-table": ("search_candidates", {"query": "", "limit": 6}, "table"),
    "compare": ("compare_candidates", {"candidate_ids": [1, 2, 3]}, None),
    "pipeline": ("job_pipeline", {"job_id": 1}, None),
    "duplicates": ("find_duplicates", {}, None),
    "digest": ("status_digest", {}, None),
}
# (theme, dark host, host sends white colors)
CASES = [("sunset", False, False), ("sunset", True, False), ("forest-night", True, False), ("ocean", True, False),
         ("midnight", True, False), ("classic", True, False), ("sunset", False, True), ("classic", False, False)]
CARDS = ".pcard, .card, .lrow, article.pair, section.who, .col, li.session, table.ctable thead th, table.ptable thead th, table.ptable td, .preview"


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rgb(hexv: str) -> str:
    n = int(hexv.strip().lstrip("#"), 16)
    return f"rgb({(n >> 16) & 255}, {(n >> 8) & 255}, {n & 255})"


@pytest.fixture(scope="module")
def hosts(tmp_path_factory):
    procs, ports = [], {}
    for theme, white in {(t, w) for t, _, w in CASES}:
        port = _free_port()
        dd = tmp_path_factory.mktemp(f"c15-{theme}-{int(white)}")
        env = {**os.environ, "TALENT_DATA_DIR": str(dd)}
        subprocess.run([sys.executable, "-m", "talent", "sample-data"], env=env, check=True, stdout=subprocess.DEVNULL)
        subprocess.run([sys.executable, "-c", "from talent.paths import db_path; from talent.store import Store; s = Store(db_path())\n"
                        "for i in range(2): s.add_candidate(name='Dana Dup', email=f'd{i}@x.com', current_title='Engineer', current_company='Zed')"],
                       env=env, check=True)  # a pair for Find duplicates to show
        args = [sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port), "--data", str(dd), "--theme", theme]
        if white:
            args.append("--styles-white")
        procs.append(subprocess.Popen(args, env=env, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL))
        ports[(theme, white)] = port
    for port in ports.values():
        for _ in range(120):
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tools", timeout=1)
                break
            except Exception:
                time.sleep(0.5)
        else:
            [p.terminate() for p in procs]
            pytest.skip("a view host did not start")
    yield ports
    for p in procs:
        p.terminate()
        p.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Chromium is not available")
        yield b
        b.close()


def _open(browser, port, view, dark):
    tool, args, step = VIEWS[view]
    page = browser.new_page(viewport={"width": 1500, "height": 1100})
    page.goto(f"http://127.0.0.1:{port}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    if dark:
        page.check("#dark")
    page.select_option("#tool", tool)
    page.fill("#args", json.dumps(args))
    page.click("#run")
    fr = page.frame_locator("iframe").last
    fr.locator("main > *:not(.empty)").first.wait_for(timeout=20000)
    if step == "table":
        fr.get_by_role("button", name="Show as table").click()
        fr.locator("table.ptable").wait_for(timeout=10000)
    page.wait_for_timeout(400)
    return page, fr


@pytest.mark.parametrize("view", list(VIEWS))
@pytest.mark.parametrize("theme,dark,white", CASES)
def test_view_wears_the_theme(browser, hosts, view, theme, dark, white):
    page, fr = _open(browser, hosts[(theme, white)], view, dark)
    try:
        # the server's word arrives with the result: dataset.tt is the theme
        assert fr.locator("html").get_attribute("data-tt") == theme
        t = BLOCKS[theme if not (dark and theme in ("sunset", "classic")) else theme + "-dark"]
        got = fr.locator("body").evaluate("""(b) => {
            return {page: getComputedStyle(b).backgroundColor, text: getComputedStyle(b).color}; }""")
        assert got["page"] == rgb(t["--bg"]) and got["text"] == rgb(t["--text"]), (got, t["--bg"])
        card = fr.locator("body").evaluate("""(b, sel) => { for (const e of b.querySelectorAll(sel)) { const c = getComputedStyle(e).backgroundColor; if (c !== 'rgba(0, 0, 0, 0)') return c; } return 'none'; }""", CARDS)
        assert card in (rgb(t["--surface"]), rgb(t["--surface-2"]), "rgb(255, 255, 255)"), (card, t["--surface"])  # the client update is white paper
        # a primary button, real or a probe, wears the app's button gradient
        probe = fr.locator("body").evaluate("""(b) => { const x = document.createElement('button'); x.className = 'btn primary'; x.textContent = 'probe';
            b.append(x); const s = getComputedStyle(x); const r = {img: s.backgroundImage, color: s.color}; x.remove(); return r; }""")
        assert probe["img"].startswith("linear-gradient") and rgb(t["--btn-text"]) == probe["color"], probe
        assert page.evaluate("window.HOSTREJECTS") == []
        SHOTS.mkdir(parents=True, exist_ok=True)
        page.screenshot(path=str(SHOTS / f"{view}-{theme}-{'dark' if dark else 'light'}{'-white' if white else ''}.png"))
    finally:
        page.close()
