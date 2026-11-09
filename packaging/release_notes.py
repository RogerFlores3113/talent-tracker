"""Print the CHANGELOG.md section for a version, for use as GitHub Release notes.

Usage:  python packaging/release_notes.py v0.1.0
Exits 1 (and prints nothing to stdout) if the version has no section.
"""

from __future__ import annotations

import re
import sys
from pathlib import Path

CHANGELOG = Path(__file__).resolve().parent.parent / "CHANGELOG.md"


def section(version: str, text: str) -> str | None:
    """The body of `## [version] ...` up to the next `## ` heading, or None."""
    version = version.strip().removeprefix("v")
    lines = text.splitlines()
    start = None
    for i, line in enumerate(lines):
        if re.match(rf"^##\s*\[{re.escape(version)}\]", line):
            start = i
            break
    if start is None:
        return None
    end = len(lines)
    for j in range(start + 1, len(lines)):
        if lines[j].startswith("## ") or re.match(r"^\[[^\]]+\]:\s", lines[j]):
            end = j
            break
    body = "\n".join(lines[start:end]).strip()
    return body or None


def main(argv: list[str]) -> int:
    if len(argv) != 2:
        print("usage: release_notes.py <tag>", file=sys.stderr)
        return 2
    try:
        text = CHANGELOG.read_text(encoding="utf-8")
    except OSError as e:
        print(f"cannot read CHANGELOG.md: {e}", file=sys.stderr)
        return 1
    body = section(argv[1], text)
    if body is None:
        print(f"CHANGELOG.md has no section for {argv[1]}", file=sys.stderr)
        return 1
    print(body)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
