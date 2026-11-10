"""Cycle 14 R: the in-chat views start in a strict MCP Apps host (the shapes the spec gives each message)."""
import os
import re
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

from talent.mcp_views import URIS, tool_meta, view_html

ROOT = Path(__file__).resolve().parent.parent


@pytest.mark.parametrize("name", list(URIS))
def test_every_view_sends_the_spec_handshake(name):
    html = view_html(name)
    assert "appInfo:{name:" in html and "appCapabilities" in html and 'protocolVersion:"2026-01-26"' in html
    assert 'content:[{type:"text",text}]' in html  # ui/message content is an array of blocks
    assert "ui/notifications/initialized" in html
    assert html.count("ui/initialize") >= 1


def test_tool_meta_has_the_chatgpt_alias():
    for name, uri in URIS.items():
        m = tool_meta(name)
        assert m["ui"]["resourceUri"] == uri and m["openai/outputTemplate"] == uri


def test_connect_page_and_readme_say_where_cards_show():
    page = (ROOT / "src/talent/templates/connect.html").read_text(encoding="utf-8")
    assert "Always allow" in page and "quit and reopen Claude" in page and "Claude Code" in page  # no jargon on the page
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    assert "Always allow" in readme and "Reload MCP Configuration" in readme and "Claude Code" in readme


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _start(tmp_path, *extra):
    port = _free_port()
    env = {**os.environ, "TALENT_DATA_DIR": str(tmp_path)}
    proc = subprocess.Popen([sys.executable, str(ROOT / "scripts" / "view_host.py"), "--port", str(port),
                             "--data", str(tmp_path), "--sample", *extra], env=env,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    for _ in range(80):
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/tools", timeout=1)
            return proc, port
        except Exception:
            time.sleep(0.5)
    proc.terminate()
    pytest.skip("the view host did not start")


@pytest.fixture(scope="module")
def pw_browser():
    sync = pytest.importorskip("playwright.sync_api")
    with sync.sync_playwright() as p:
        try:
            b = p.chromium.launch()
        except Exception:
            pytest.skip("Chromium is not available")
        yield b
        b.close()


@pytest.fixture(scope="module")
def strict_host(tmp_path_factory):
    proc, port = _start(tmp_path_factory.mktemp("r_strict"))
    yield port
    proc.terminate()
    proc.wait(timeout=10)


CALLS = {"people": ("search_candidates", {"query": "", "limit": 5}), "candidate": ("search_candidates", {"query": "", "limit": 5}),
         "results": ("search_candidates", {"query": "", "limit": 5}), "searches": ("list_saved_searches", {}),
         "today": ("list_action_items", {}), "jobs": ("list_jobs", {}), "followups": ("list_followups", {}),
         "digest": ("status_digest", {}), "interviews": ("list_interviews", {}), "owed": ("list_feedback_owed", {}),
         "duplicates": ("find_duplicates", {})}
NEEDS_JOB = {"pipeline": "job_pipeline", "shortlist": "list_prospects", "decide": "compare_finalists"}

JS = """async ([uri, tool, args, needsJob]) => {
  const call = async (n, a) => (await fetch('/api/call', {method: 'POST', headers: {'content-type': 'application/json'},
      body: JSON.stringify({name: n, arguments: a})})).json();
  if (needsJob) {
    const jobs = await call('list_jobs', {});
    const id = ((jobs.structuredContent || {}).jobs || [])[0];
    args = Object.assign({job_id: id ? id.id : 1}, args);
  }
  let res = await call(tool, args);
  if (!res.structuredContent) res = {content: [{type: 'text', text: 'Nothing'}], structuredContent: {}};
  const html = await (await fetch('/api/resource?uri=' + encodeURIComponent(uri))).text();
  mount(html, args, res, tool);
}"""


@pytest.mark.parametrize("name", list(URIS))
def test_view_starts_under_the_strict_host(strict_host, pw_browser, name):
    tool, args = CALLS.get(name) or (NEEDS_JOB.get(name), {})
    if name == "compare":
        tool, args = "compare_candidates", {"candidate_ids": [1, 2]}
    if name == "saved":
        tool, args = "list_jobs", {}
    page = pw_browser.new_page(viewport={"width": 1000, "height": 800})
    try:
        page.goto(f"http://127.0.0.1:{strict_host}")
        page.wait_for_function("document.querySelector('#tool').options.length > 3")
        page.evaluate(JS, [URIS[name], tool, args, name in NEEDS_JOB])
        fr = page.frame_locator("iframe").last
        fr.locator("#main").wait_for()
        page.wait_for_function("window.HOSTLOG.some(m => m.method === 'ui/notifications/initialized')", timeout=15000)
        deadline = time.time() + 15
        text = ""
        while time.time() < deadline:
            text = fr.locator("#main").inner_text()
            if text.strip() and "Loading..." not in text and "Working on it" not in text:
                break
            time.sleep(0.3)
        assert text.strip(), name
        assert "Loading..." not in text, name
        assert "couldn't start the view" not in text, name
        rejects = page.evaluate("window.HOSTREJECTS")
        assert rejects == [], rejects
        assert page.evaluate("window.HOSTLOG.some(m => m.method === 'ui/notifications/size-changed')")
    finally:
        page.close()


def test_strict_host_rejects_the_old_handshake(strict_host, pw_browser):
    page = pw_browser.new_page()
    try:
        page.goto(f"http://127.0.0.1:{strict_host}")
        page.wait_for_function("document.querySelector('#tool').options.length > 3")
        page.evaluate("""() => { const f = document.createElement('iframe'); f.srcdoc = '<script>' +
          'window.addEventListener("message", e => parent.postMessage({jsonrpc:"2.0",method:"x/reply",params:e.data},"*"));' +
          'parent.postMessage({jsonrpc:"2.0",id:1,method:"ui/initialize",params:{protocolVersion:"2026-01-26",clientInfo:{name:"a",version:"1"},appCapabilities:{}}},"*");' +
          'parent.postMessage({jsonrpc:"2.0",id:2,method:"ui/message",params:{role:"user",content:{type:"text",text:"hi"}}},"*");<\\/script>';
          f.setAttribute('sandbox', 'allow-scripts'); document.body.append(f);
          mount('<p>x</p>', {}, {content: [], structuredContent: {}}, 'search_candidates'); }""")
        # the stub frame above is not the mounted one, so drive the mounted view's check() directly
        reasons = page.evaluate("""() => [
          check({method:'ui/initialize',id:1,params:{protocolVersion:'2026-01-26',clientInfo:{name:'a',version:'1'},appCapabilities:{}}}),
          check({method:'ui/message',id:2,params:{role:'user',content:{type:'text',text:'hi'}}}),
          check({method:'ui/open-link',id:3,params:{url:5}}),
          check({method:'ui/request-display-mode',id:4,params:{mode:'huge'}}),
          check({method:'ui/initialize',id:5,params:{protocolVersion:'2026-01-26',appInfo:{name:'a',version:'1'},appCapabilities:{}}})]""")
        assert all(reasons[:4]) and reasons[4] == ""
    finally:
        page.close()
