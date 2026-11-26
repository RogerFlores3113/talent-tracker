"""Cycle 21: Settings has a section index on the left and one save bar instead of a Save button per form."""
from __future__ import annotations

import re
from pathlib import Path

from conftest import local_client

ROOT = Path(__file__).resolve().parents[1]
JS = (ROOT / "src" / "talent" / "static" / "app.js").read_text(encoding="utf-8")


def test_the_index_lists_every_section_in_page_order(store):
    page = local_client(store).get("/settings").text
    nav = page.split('aria-label="Settings sections"', 1)[1].split("</nav>", 1)[0]
    ids = re.findall(r'href="#([a-z-]+)"', nav)
    assert len(ids) == 15 and ids[0] == "look" and ids[-1] == "demo-data"
    body = page.split("</nav>", 1)[1]
    at = [body.index(f'id="{i}"') for i in ids]
    assert at == sorted(at)  # the list reads in the same order as the page


def test_settings_forms_have_no_save_buttons_of_their_own(store):
    page = local_client(store).get("/settings").text
    for form in re.findall(r"<form[^>]*data-settings-form.*?</form>", page, re.S):
        assert 'type="submit"' not in form
    assert page.count("data-settings-form") == 5  # look/today/..., time zone, cautious, AI apps, updates
    assert 'id="save-bar"' in page and "data-save-all" in page and "data-save-discard" in page
    assert ">Save settings<" not in page
    assert 'action="/update/check"' in page and ">Check now<" in page  # actions keep their own buttons


def test_the_bar_and_the_index_are_wired_in_the_script():
    assert "form[data-settings-form]" in JS and "bar.hidden = changed().length === 0" in JS
    assert 'a.setAttribute("aria-current", "location")' in JS
    assert '(a.id === "settings-main") - (b.id === "settings-main")' in JS  # the main form is sent last


def test_each_form_still_saves_on_its_own(store):
    c = local_client(store)
    c.post("/settings/updates", data={}, follow_redirects=False)
    assert store.setting("update_check") == "0"
    c.post("/settings/cautious", data={"ai_cautious": "1"}, follow_redirects=False)
    assert store.setting("ai_cautious") == "1"
