"""CI check: connect-claude then check-claude with a built executable, in a throwaway home folder.

Usage: python packaging/check_connect.py <path-to-executable>
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


def main(exe: str) -> None:
    exe = str(Path(exe).resolve())
    home = Path(tempfile.mkdtemp())
    env = {**os.environ, "HOME": str(home), "USERPROFILE": str(home), "TALENT_DATA_DIR": str(home / "data")}
    if sys.platform == "darwin":
        folder = home / "Library" / "Application Support" / "Claude"
    elif sys.platform == "win32":
        env["APPDATA"] = str(home / "Roaming")
        folder = home / "Roaming" / "Claude"
    else:
        folder = home / ".config" / "Claude"
    folder.mkdir(parents=True)
    config = folder / "claude_desktop_config.json"

    for step in ("connect-claude", "check-claude"):
        done = subprocess.run([exe, step], env=env, capture_output=True, text=True, timeout=180)
        print(f"$ {exe} {step}\n{done.stdout}{done.stderr}")
        assert done.returncode == 0, f"{step} exited with {done.returncode}"
        if step == "connect-claude":
            written = json.loads(config.read_text(encoding="utf-8"))["mcpServers"]["talent-search"]["command"]
            expected = exe
            if sys.platform == "win32":  # Windows starts the console build for stdio
                expected = str(Path(exe).with_name("talent-search-mcp.exe"))
            assert os.path.samefile(written, expected), f"wrote {written}, expected {expected}"
    print(f"Connect check passed: {exe}")


if __name__ == "__main__":
    main(sys.argv[1])
