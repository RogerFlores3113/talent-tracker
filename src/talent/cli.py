"""Entry point.

Double-clicking the app runs with no arguments: start the local server and open
the browser. AI apps launch it with `mcp` to talk over stdio.
"""
from __future__ import annotations

import argparse
import logging
import os
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import httpx

from .paths import DEFAULT_PORT, data_dir, db_path, is_frozen, port_file


def _private(path: Path) -> Path:
    """Create the file readable by this user only (0600), and tighten an existing one. Not on Windows."""
    if os.name != "nt":
        try:
            os.close(os.open(path, os.O_CREAT | os.O_APPEND | os.O_WRONLY, 0o600))
            os.chmod(path, 0o600)
        except OSError:
            pass
    return path


def _write_port_file(port: int) -> None:
    try:  # so the AI app's "Open in Talent Search" links point at the right port
        _private(port_file()).write_text(str(port))
    except OSError:
        pass


def _setup_logging(to_stderr: bool) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(_private(data_dir() / "app.log"), encoding="utf-8")]
    if to_stderr and sys.stderr is not None:
        handlers.append(logging.StreamHandler(sys.stderr))
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s: %(message)s",
                        handlers=handlers)


def _already_running(port: int) -> bool:
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", timeout=0.5)
        return r.status_code == 200 and "version" in r.json()
    except (httpx.HTTPError, ValueError):
        return False


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _pick_port(preferred: int) -> int:
    if _port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def run_app(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    import uvicorn

    from .store import Store
    from .web import create_app

    if _already_running(port):
        # Second double-click: just bring the window back.
        if open_browser:
            webbrowser.open(f"http://127.0.0.1:{port}/")
        return
    port = _pick_port(port)
    app = create_app(Store(db_path()))
    _write_port_file(port)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.2, webbrowser.open, args=[url]).start()
    logging.getLogger(__name__).info("Talent Search running at %s (data: %s)", url, data_dir())
    # log_config=None: the frozen windowed build has no console for uvicorn's default handlers.
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="talent", description="Talent Search")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("app", help="start the app and open it in your browser (default)")
    serve = sub.add_parser("serve", help="start the web server without opening a browser")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    sub.add_parser("mcp", help="run the MCP server over stdio (AI apps start this)")
    sub.add_parser("sample-data", help="load made-up candidates to try the app")
    sub.add_parser("reindex", help="rebuild the optional Typesense index from SQLite")
    sub.add_parser("connect-claude", help="add Talent Search to Claude Desktop")
    sub.add_parser("connect-codex", help="add Talent Search to Codex")
    sub.add_parser("connect-lmstudio", help="add Talent Search to LM Studio")
    imp = sub.add_parser("import-csv", help="import candidates from a CSV file (columns guessed from the header)")
    imp.add_argument("file")
    exp = sub.add_parser("export-csv", help="write every candidate to a CSV file")
    exp.add_argument("file")
    args = parser.parse_args(argv)
    cmd = args.cmd or "app"

    # stdout belongs to the MCP protocol; never log there.
    _setup_logging(to_stderr=cmd != "app" or not is_frozen())

    if cmd == "mcp":
        from .mcp_server import run_stdio
        from .store import Store

        run_stdio(Store(db_path()))
    elif cmd == "app":
        run_app()
    elif cmd == "serve":
        run_app(port=args.port, open_browser=False)
    elif cmd == "sample-data":
        from .sample_data import load_sample_data
        from .store import Store

        print(f"Added {load_sample_data(Store(db_path()))} sample candidates.")
    elif cmd == "reindex":
        from .store import Store
        from .typesense_index import TypesenseIndex

        index = TypesenseIndex.from_env()
        if index is None:
            sys.exit("Set TYPESENSE_URL and TYPESENSE_API_KEY first. Built-in search needs no index.")
        print(f"Indexed {index.rebuild(Store(db_path()))} candidates.")
    elif cmd in ("import-csv", "export-csv"):
        from pathlib import Path

        from . import importers
        from .store import Store

        store = Store(db_path())
        if cmd == "export-csv":
            Path(args.file).write_text(importers.export_csv(store), encoding="utf-8", newline="")
            print(f"Wrote {store.count_candidates()} candidates to {args.file}.")
        else:
            header, rows = importers.read_csv(importers.decode(Path(args.file).read_bytes()))
            mapping = importers.guess_mapping(header)
            if not any(m in ("name", "first_name", "last_name") for m in mapping):
                sys.exit("Couldn't find a name column. Use the app's Import page to match columns by hand.")
            r = importers.import_rows(store, header, rows, mapping)
            print(f"Added {r['added']}, updated {r['updated']}, unchanged {r['unchanged']}, "
                  f"skipped {r['skipped_rows']}.")
            for line in (r["skipped"] + r["warnings"])[:20]:
                print("  " + line)
    elif cmd in ("connect-claude", "connect-codex", "connect-lmstudio"):
        from . import integrations

        fn = {"connect-claude": integrations.connect_claude, "connect-codex": integrations.connect_codex,
              "connect-lmstudio": integrations.connect_lmstudio}[cmd]
        result = fn()
        if not result.get("installed", True):
            sys.exit(result["message"])
        print(f"Updated {result['path']}. {result.get('next_step') or 'Restart the app to load Talent Search.'}")
