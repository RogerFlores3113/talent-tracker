"""install.sh: installs a release asset after checking its checksum; docs mention the one-liners."""
import hashlib
import io
import os
import stat
import subprocess
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
INSTALL_SH = ROOT / "install.sh"

posix_only = pytest.mark.skipif(sys.platform == "win32", reason="install.sh is for macOS and Linux")

SCRIPT = b"#!/bin/sh\necho talent-search fake\n"


def _make_release(folder: Path) -> str:
    """Write a fake release asset for this OS plus SHA256SUMS.txt. Returns the asset name."""
    folder.mkdir(parents=True, exist_ok=True)
    if sys.platform == "darwin":
        name = "TalentSearch-mac.zip"
        with zipfile.ZipFile(folder / name, "w") as z:
            info = zipfile.ZipInfo("Talent Search.app/Contents/MacOS/Talent Search")
            info.create_system = 3
            info.external_attr = (stat.S_IFREG | 0o755) << 16
            z.writestr(info, SCRIPT)
    else:
        name = "TalentSearch-linux.tar.gz"
        with tarfile.open(folder / name, "w:gz") as t:
            ti = tarfile.TarInfo("Talent Search/talent-search")
            ti.size = len(SCRIPT)
            ti.mode = 0o755
            t.addfile(ti, io.BytesIO(SCRIPT))
    digest = hashlib.sha256((folder / name).read_bytes()).hexdigest()
    (folder / "SHA256SUMS.txt").write_text(f"{digest}  {name}\n")
    return name


def _installed_exe(target: Path) -> Path:
    if sys.platform == "darwin":
        return target / "Talent Search.app" / "Contents" / "MacOS" / "Talent Search"
    return target / "Talent Search" / "talent-search"


def _run(release: Path, target: Path, tmp_path: Path) -> subprocess.CompletedProcess:
    env = {
        **os.environ,
        "TALENT_INSTALL_FROM": str(release),
        "TALENT_INSTALL_DIR": str(target),
        "HOME": str(tmp_path / "home"),
    }
    return subprocess.run(
        ["sh", str(INSTALL_SH), "--no-open"], env=env, capture_output=True, text=True, timeout=120
    )


def _skip_if_unsupported():
    if sys.platform == "darwin":
        import platform

        if platform.machine() != "arm64":
            pytest.skip("install.sh refuses Intel Macs")
    elif sys.platform.startswith("linux"):
        import platform

        if platform.machine() not in ("x86_64", "AMD64"):
            pytest.skip("install.sh only supports Linux x86_64")


@posix_only
def test_install_sh_installs(tmp_path):
    _skip_if_unsupported()
    release, target = tmp_path / "rel", tmp_path / "inst"
    _make_release(release)
    r = _run(release, target, tmp_path)
    assert r.returncode == 0, r.stdout + r.stderr
    exe = _installed_exe(target)
    assert exe.is_file() and os.access(exe, os.X_OK)
    assert subprocess.run([str(exe)], capture_output=True, text=True).stdout.strip() == "talent-search fake"
    assert "Connect Claude" in r.stdout and "Add skills" in r.stdout


@posix_only
def test_install_sh_replaces_existing_copy(tmp_path):
    _skip_if_unsupported()
    release, target = tmp_path / "rel", tmp_path / "inst"
    _make_release(release)
    assert _run(release, target, tmp_path).returncode == 0
    stale = _installed_exe(target).parent / "stale.txt"
    stale.write_text("old")
    assert _run(release, target, tmp_path).returncode == 0
    assert not stale.exists()
    assert _installed_exe(target).is_file()


@posix_only
def test_install_sh_bad_checksum_installs_nothing(tmp_path):
    _skip_if_unsupported()
    release, target = tmp_path / "rel", tmp_path / "inst"
    name = _make_release(release)
    (release / "SHA256SUMS.txt").write_text(f"{'0' * 64}  {name}\n")
    r = _run(release, target, tmp_path)
    assert r.returncode != 0
    assert "checksum" in (r.stdout + r.stderr)
    assert not _installed_exe(target).exists()
    assert not target.exists() or not any(target.iterdir())


@posix_only
def test_install_sh_missing_checksum_entry_fails(tmp_path):
    _skip_if_unsupported()
    release, target = tmp_path / "rel", tmp_path / "inst"
    _make_release(release)
    (release / "SHA256SUMS.txt").write_text(f"{'1' * 64}  something-else.zip\n")
    r = _run(release, target, tmp_path)
    assert r.returncode != 0
    assert not _installed_exe(target).exists()


@posix_only
def test_install_sh_syntax():
    r = subprocess.run(["sh", "-n", str(INSTALL_SH)], capture_output=True, text=True)
    assert r.returncode == 0, r.stderr


ONE_LINERS = [
    "curl -fsSL https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.sh | sh",
    "irm https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.ps1 | iex",
]


@pytest.mark.parametrize("doc", ["README.md", "INSTALL.md"])
def test_docs_have_one_liners_and_no_em_dash(doc):
    text = (ROOT / doc).read_text(encoding="utf-8")
    for line in ONE_LINERS:
        assert line in text, f"{doc} is missing: {line}"
    assert "—" not in text


@pytest.mark.parametrize("name", ["install.sh", "install.ps1", "INSTALL.md"])
def test_install_files_have_no_em_dash(name):
    assert "—" not in (ROOT / name).read_text(encoding="utf-8")


def test_install_md_forbids_security_changes():
    text = (ROOT / "INSTALL.md").read_text(encoding="utf-8")
    assert "Gatekeeper" in text and "SmartScreen" in text
    assert "Connect Claude" in text and "Add skills" in text
