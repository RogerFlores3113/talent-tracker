"""Color themes: the setting, the rendered attribute, and the contrast of every theme's colors."""
import re
from pathlib import Path

import pytest

from talent.store import DEFAULT_THEME, THEMES
from conftest import local_client

CSS = (Path(__file__).resolve().parent.parent / "src" / "talent" / "static" / "style.css").read_text()


def theme_blocks() -> dict[str, dict[str, str]]:
    """{'sunset': {token: value}, 'sunset-dark': ...} read from the '/* theme: name */' blocks."""
    out = {}
    for name, body in re.findall(r"/\* theme: (\S+) \*/[^{]*\{([^}]*)\}", CSS):
        out[name] = {k: v.strip() for k, v in re.findall(r"(--[\w-]+|color-scheme):\s*([^;]+);", body)}
    return out


BLOCKS = theme_blocks()


def rgb(value: str) -> tuple[float, float, float]:
    value = value.strip()
    m = re.fullmatch(r"#([0-9a-fA-F]{6})", value)
    assert m, f"not a plain hex color: {value}"
    n = int(m.group(1), 16)
    return ((n >> 16) & 255, (n >> 8) & 255, n & 255)


def lum(value: str) -> float:
    def ch(c: float) -> float:
        c /= 255
        return c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4
    r, g, b = (ch(c) for c in rgb(value))
    return 0.2126 * r + 0.7152 * g + 0.0722 * b


def ratio(a: str, b: str) -> float:
    hi, lo = sorted((lum(a), lum(b)), reverse=True)
    return (hi + 0.05) / (lo + 0.05)


def test_every_theme_block_defines_the_same_tokens():
    assert set(BLOCKS) == {"sunset", "sunset-dark", "forest-night", "ocean", "midnight", "classic", "classic-dark"}
    want = set(BLOCKS["sunset"])
    for name, tokens in BLOCKS.items():
        assert set(tokens) == want, (name, want ^ set(tokens))
        assert tokens["color-scheme"] in ("light", "dark")
    assert {t[0] for t in THEMES} == {"sunset", "forest-night", "ocean", "midnight", "classic"}


@pytest.mark.parametrize("name", sorted(BLOCKS))
def test_theme_contrast(name):
    t = BLOCKS[name]
    checks = [
        ("text on bg", t["--text"], t["--bg"], 7), ("text on surface", t["--text"], t["--surface"], 7),
        ("text on surface-2", t["--text"], t["--surface-2"], 7),
        ("muted on surface", t["--muted"], t["--surface"], 4.5), ("muted on bg", t["--muted"], t["--bg"], 4.5),
        ("muted on surface-2", t["--muted"], t["--surface-2"], 4.5),
        ("accent on surface", t["--accent"], t["--surface"], 4.5), ("accent on bg", t["--accent"], t["--bg"], 4.5),
        ("accent-text on accent", t["--accent-text"], t["--accent"], 4.5),
        ("btn-text on btn-from", t["--btn-text"], t["--btn-from"], 4.5), ("btn-text on btn-to", t["--btn-text"], t["--btn-to"], 4.5),
        ("ok on surface", t["--ok"], t["--surface"], 4.5), ("bad on surface", t["--bad"], t["--surface"], 4.5),
        ("warn on surface", t["--warn"], t["--surface"], 4.5), ("ok on ok-bg", t["--ok"], t["--ok-bg"], 4.5),
        ("bad on bad-bg", t["--bad"], t["--bad-bg"], 4.5), ("warn on warn-bg", t["--warn"], t["--warn-bg"], 4.5),
    ]
    for key in ("--head-from", "--head-mid", "--head-to", "--head-end"):
        checks.append((f"header text on {key}", t["--header-text"], t[key], 4.5))
    bad = [(label, round(ratio(fg, bg), 2), need) for label, fg, bg, need in checks if ratio(fg, bg) < need]
    assert not bad, f"{name}: {bad}"


def test_no_stray_colors_outside_the_theme_blocks():
    """Past the theme blocks and swatch previews, the stylesheet only uses tokens (print sheet excepted)."""
    body = CSS.split("/* ==== Scales")[1]
    body = body.split("/* Preview colors")[0] + body.split("/* ==== Motion")[1]
    body = re.sub(r"@media print \{.*?\n\}\n", "", body, flags=re.S)
    assert not re.findall(r"#[0-9a-fA-F]{3,8}\b", body)


def test_default_theme_is_sunset_and_is_rendered(store):
    assert DEFAULT_THEME == "sunset"
    c = local_client(store)
    assert '<html lang="en" data-theme="sunset">' in c.get("/").text


def test_theme_is_chosen_on_settings_and_saved(store):
    c = local_client(store)
    page = c.get("/settings").text
    assert 'id="look"' in page and page.count('name="theme"') == len(THEMES)
    assert 'name="theme" value="sunset" data-theme-pick checked' in page
    r = c.post("/settings", data={"theme": "ocean", "pipeline_view": "board"}, follow_redirects=False)
    assert r.status_code == 303 and store.setting("theme") == "ocean"
    assert 'data-theme="ocean"' in c.get("/candidates").text
    assert 'name="theme" value="ocean" data-theme-pick checked' in c.get("/settings").text


def test_unknown_theme_is_ignored_and_a_bad_stored_one_falls_back(store):
    c = local_client(store)
    c.post("/settings", data={"theme": "midnight"})
    c.post("/settings", data={"theme": "<script>"})
    assert store.setting("theme") == "midnight"
    store._conn.execute("INSERT INTO settings(key, value) VALUES ('theme', 'nope') ON CONFLICT(key) DO UPDATE SET value = 'nope'")
    store._conn.commit()
    assert 'data-theme="sunset"' in c.get("/").text


def test_settings_saved_without_a_theme_keeps_it(store):
    c = local_client(store)
    c.post("/settings", data={"theme": "classic"})
    c.post("/settings", data={"stuck_days": "9"})
    assert store.setting("theme") == "classic"


def test_locked_page_uses_the_default_look(store):
    c = local_client(store, unlocked=False)
    r = c.get("/")
    assert "/static/style.css" in r.text and "data-theme" not in r.text
