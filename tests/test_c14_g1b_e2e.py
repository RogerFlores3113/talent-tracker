"""Cycle 14 G1: the window.openai fallback and the no-double-render rule, in a real browser. Skipped without Playwright."""
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
pw = pytest.importorskip("playwright.sync_api")


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path_factory, flag):
    port = _free_port()
    dd = tmp_path_factory.mktemp("g1data")
    env = {**os.environ, "TALENT_DATA_DIR": str(dd)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port),
                             "--data", str(dd), "--sample", flag], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(60):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tools", timeout=1)
            return proc, port
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    pytest.skip("the view host did not start")


@pytest.fixture(scope="module")
def host_only(tmp_path_factory):
    proc, port = _start(tmp_path_factory, "--openai-only")
    yield port
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def host_both(tmp_path_factory):
    proc, port = _start(tmp_path_factory, "--openai")
    yield port
    proc.terminate()
    proc.wait(timeout=10)


@pytest.fixture(scope="module")
def browser():
    with pw.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Chromium is not available")
        yield b
        b.close()


def _run(browser, port, tool, args):
    page = browser.new_page(viewport={"width": 1400, "height": 1600})
    page.goto(f"http://127.0.0.1:{port}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    page.select_option("#tool", tool)
    page.fill("#args", json.dumps(args))
    page.click("#run")
    return page, page.frame_locator("iframe").last


def test_people_view_draws_from_window_openai_when_the_bridge_is_silent(browser, host_only):
    page, fr = _run(browser, host_only, "search_candidates", {"query": "", "limit": 6})
    fr.locator("article").first.wait_for(timeout=15000)  # real people, drawn from window.openai.toolOutput
    assert "6 of 16 people" in fr.locator("body").inner_text()
    assert fr.get_by_text("couldn't start the view").count() == 0
    assert page.evaluate("window.HOSTREJECTS.length") == 0
    page.close()


def test_compare_view_draws_from_window_openai_when_the_bridge_is_silent(browser, host_only):
    page, fr = _run(browser, host_only, "compare_candidates", {"candidate_ids": [1, 2]})
    fr.locator("table.ctable").wait_for(timeout=15000)
    assert fr.locator("table.ctable thead th.cmp-person").count() == 2
    page.close()


def test_set_globals_redraws_the_fallback(browser, host_only):
    page, fr = _run(browser, host_only, "search_candidates", {"query": "", "limit": 6})
    fr.locator("article").first.wait_for(timeout=15000)
    page.evaluate("""() => { const f = document.querySelector('iframe'); f.contentWindow.postMessage({openaiReply: 0,
        globals: {toolOutput: {candidates: [], total: 0, count: 0}}}, '*') }""")
    page.wait_for_timeout(800)
    assert fr.locator("article").count() == 0 and "6 of 16 people" not in fr.locator("body").inner_text()
    page.close()


def test_both_bridges_render_once_with_no_rejections(browser, host_both):
    page = browser.new_page(viewport={"width": 1400, "height": 1600})
    page.goto(f"http://127.0.0.1:{host_both}")
    page.wait_for_function("document.querySelector('#tool').options.length > 3")
    page.select_option("#tool", "search_candidates")
    page.fill("#args", json.dumps({"query": "", "limit": 6}))
    # count how often the view replaces its main area
    page.click("#run")
    fr = page.frame_locator("iframe").last
    fr.locator("article").first.wait_for(timeout=15000)
    fr.locator("body").evaluate("""() => { window.__draws = 0; const m = document.getElementById('main');
        new MutationObserver(() => { window.__draws++ }).observe(m, {childList: true}); }""")
    page.wait_for_timeout(3500)  # past the fallback's 2 s timer and the host's set_globals
    assert fr.locator("body").evaluate("() => window.__draws") == 0  # nothing drew a second time
    assert fr.locator("body").evaluate("() => !!window.openai") is True  # the object really was there
    assert page.evaluate("window.HOSTREJECTS.length") == 0
    page.close()
