import importlib.util
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "packaging" / "release_notes.py"

spec = importlib.util.spec_from_file_location("release_notes", SCRIPT)
release_notes = importlib.util.module_from_spec(spec)
spec.loader.exec_module(release_notes)

SAMPLE = """# Changelog

## [0.2.0] - 2026-12-01
- newer thing

## [0.1.0] - 2026-11-09
Intro.

- one
- two

[0.1.0]: https://example.com
"""


def test_section_picks_one_version():
    body = release_notes.section("v0.1.0", SAMPLE)
    assert body.startswith("## [0.1.0]")
    assert "- two" in body and "newer thing" not in body and "example.com" not in body


def test_section_missing_is_none():
    assert release_notes.section("v9.9.9", SAMPLE) is None


def test_current_version_has_a_changelog_section():
    from talent import __version__
    text = (ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
    assert release_notes.section(__version__, text)


def test_cli_exit_codes():
    ok = subprocess.run([sys.executable, str(SCRIPT), "v0.1.0"], capture_output=True, text=True)
    assert ok.returncode == 0 and "0.1.0" in ok.stdout
    bad = subprocess.run([sys.executable, str(SCRIPT), "v9.9.9"], capture_output=True, text=True)
    assert bad.returncode != 0 and bad.stdout == ""
