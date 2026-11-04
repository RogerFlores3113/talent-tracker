"""Entry point.

Double-clicking the app runs with no arguments: start the local server and open
the browser. AI apps launch it with `mcp` to talk over stdio.
"""
from __future__ import annotations

import argparse
import logging
import socket
import sys
import threading
import webbrowser

import httpx

from .paths import data_dir, db_path, is_frozen

DEFAULT_PORT = 8765


def _setup_logging(to_stderr: bool) -> None:
    handlers: list[logging.Handler] = [logging.FileHandler(data_dir() / "app.log", encoding="utf-8")]
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
    elif cmd in ("connect-claude", "connect-codex"):
        from . import integrations

        fn = integrations.connect_claude if cmd == "connect-claude" else integrations.connect_codex
        result = fn()
        print(f"Updated {result['path']}. Restart the app to load Talent Search.")
