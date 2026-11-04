# PyInstaller spec for Talent Search: macOS .app, Windows folder app, Linux folder.
#
# Windows gets two executables in one folder:
#   "Talent Search.exe"      windowed, what the user double-clicks
#   "talent-search-mcp.exe"  console, what Claude Desktop / Codex launch for MCP over stdio
# A windowed Windows exe has no usable stdin/stdout, so the MCP side needs its own console build.
# macOS and Linux use one binary for both (`... mcp`).
#
# Run from the repo root:  uv run pyinstaller packaging/talent-search.spec --noconfirm

import os
import sys
from PyInstaller.utils.hooks import collect_submodules

HERE = SPECPATH                       # .../packaging
SRC = os.path.join(HERE, "..", "src")

hidden = (
    collect_submodules("uvicorn")
    + collect_submodules("mcp", filter=lambda name: not name.startswith("mcp.cli"))
    + ["talent.web", "talent.mcp_server", "talent.sample_data", "talent.typesense_index"]
)

a = Analysis(
    [os.path.join(HERE, "entry.py")],
    pathex=[SRC],
    datas=[
        (os.path.join(SRC, "talent", "templates"), os.path.join("talent", "templates")),
        (os.path.join(SRC, "talent", "static"), os.path.join("talent", "static")),
    ],
    hiddenimports=hidden,
    excludes=["tkinter", "pytest", "mcp.cli", "typer"],
    noarchive=False,
)
pyz = PYZ(a.pure)

app_exe = EXE(
    pyz, a.scripts, [],
    exclude_binaries=True,
    name="Talent Search" if sys.platform != "linux" else "talent-search",
    console=sys.platform == "linux",
    upx=False,          # UPX breaks macOS codesign and trips Windows antivirus
    icon=None,          # TODO: app icon (icns / ico)
)

executables = [app_exe]
if sys.platform == "win32":
    mcp_exe = EXE(
        pyz, a.scripts, [],
        exclude_binaries=True,
        name="talent-search-mcp",
        console=True,
        upx=False,
    )
    executables.append(mcp_exe)

coll = COLLECT(*executables, a.binaries, a.datas, upx=False, name="Talent Search")

if sys.platform == "darwin":
    app = BUNDLE(
        coll,
        name="Talent Search.app",
        bundle_identifier="dev.rogerflores.talentsearch",
        version="0.0.1",
        info_plist={"NSHighResolutionCapable": True, "LSUIElement": False},
    )
