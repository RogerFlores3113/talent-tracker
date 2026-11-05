"""Entry point.

Double-clicking the app runs with no arguments: start the local server and open
the browser. AI apps launch it with `mcp` to talk over stdio.
"""
from __future__ import annotations

import argparse
import hmac
import logging
import os
import secrets
import socket
import sys
import threading
import webbrowser
from pathlib import Path

import httpx

from .paths import DEFAULT_PORT, app_key, data_dir, db_path, health_proof, is_frozen, launch_url, port_file


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


def _probe(port: int) -> str:
    """Who answers on this port: "ours" (this install's app, proven with an HMAC of a fresh nonce under the
    app key), "other" (something else is listening there) or "free"."""
    nonce = secrets.token_urlsafe(24)
    try:
        r = httpx.get(f"http://127.0.0.1:{port}/api/health", params={"nonce": nonce}, timeout=0.5)
        body = r.json()
    except (httpx.HTTPError, ValueError):
        return "free" if _port_free(port) else "other"
    proof = body.get("proof") if isinstance(body, dict) else None
    if r.status_code == 200 and isinstance(proof, str) and hmac.compare_digest(proof, health_proof(app_key(), nonce)):
        return "ours"
    return "other"


def _already_running(port: int) -> bool:
    """This install's app answers on the port. Only then does a launcher send it a launch code."""
    return _probe(port) == "ours"


def _port_free(port: int) -> bool:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _pick_port(preferred: int) -> int:
    if _port_free(preferred):
        return preferred
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def _recorded_port() -> int | None:
    try:
        return int(port_file().read_text().strip())
    except (OSError, ValueError):
        return None


def open_window() -> bool:
    """Open (and unlock) the app window of the running app. False when it isn't running."""
    for port in dict.fromkeys(p for p in (_recorded_port(), DEFAULT_PORT) if p):
        if _already_running(port):
            webbrowser.open(launch_url(port))  # a one-time code, never the key itself
            return True
    return False


def run_app(port: int = DEFAULT_PORT, open_browser: bool = True) -> None:
    import uvicorn

    from .store import Store
    from .web import create_app

    say = logging.getLogger(__name__)
    if _already_running(port):
        # Second double-click: just bring the window back.
        if open_browser:
            webbrowser.open(launch_url(port))
        say.info("Talent Search is already running at http://127.0.0.1:%s/, so this copy didn't start another. %s",
                 port, "Opened its window." if open_browser else "Run talent open to open its window.")
        return
    wanted = port
    port = _pick_port(port)
    if port != wanted:
        say.warning("Port %s is in use by another program that isn't this Talent Search, so no launch code was sent "
                    "to it. Starting on port %s instead.", wanted, port)
    app = create_app(Store(db_path()))
    _write_port_file(port)
    url = f"http://127.0.0.1:{port}/"
    if open_browser:
        threading.Timer(1.2, lambda: webbrowser.open(launch_url(port))).start()
    logging.getLogger(__name__).info("Talent Search running at %s (data: %s)", url, data_dir())
    logging.getLogger(__name__).info("If the window says it's locked, open Talent Search from its app icon "
                                     "or run: talent open")
    # log_config=None: the frozen windowed build has no console for uvicorn's default handlers.
    uvicorn.run(app, host="127.0.0.1", port=port, log_config=None, access_log=False)


def _sync(args: argparse.Namespace) -> None:
    """talent sync FILE: read the export the way it was read last time, show what will change, then apply it
    as one import (one row on Changes, undoable while nothing else changes)."""
    from . import backups, importers
    from .store import Store

    path = Path(args.file)
    try:
        header, rows = importers.read_csv(importers.decode(path.read_bytes()))
    except (OSError, ValueError) as exc:
        sys.exit(f"Couldn't read {path.name}: {exc}")
    store = Store(db_path())
    mapping, prof = importers.mapping_for(store, header, rows)
    prof = prof or {}
    try:
        mapping = importers.apply_map(header, mapping, args.map)
    except ValueError as exc:
        sys.exit(str(exc))
    if not any(m in ("name", "first_name", "last_name") for m in mapping):
        sys.exit("Couldn't find a name column. Use --map, or the app's Import page to match columns by hand.")
    source = args.source or prof.get("source") or importers.default_source(header)
    client = args.client if args.client is not None else prof.get("client") or ""
    opts = {"source": source, "add_stages": bool(prof.get("add_stages", True)), "move_stages": not args.no_move,
            "profile": {**prof, "client": client}}
    print(("Using the saved matches for this export" + (f" ({prof['file']})" if prof.get("file") else "") + ".")
          if prof else "No saved profile for these columns yet, so the columns were guessed.")
    print("Columns: " + ", ".join(f"{h} -> {importers.TARGET_LABELS.get(m, m)}" for h, m in zip(header, mapping) if m))
    preview = importers.preview_import(store, header, rows, mapping, **opts)
    for line in importers.diff_lines(preview):
        print(line)
    if args.dry_run:
        print("Nothing was changed (dry run).")
        return
    if not args.yes:
        try:
            answer = input("Apply these changes? [y/N] ")
        except EOFError:
            answer = ""
        if answer.strip().lower() not in ("y", "yes"):
            print("Nothing was changed.")
            return
    r = importers.import_rows(store, header, rows, mapping, file_name=path.name, **opts)
    importers.save_sync_profile(store, header, mapping, r, file_name=path.name, source=source,
                                add_stages=opts["add_stages"], move_stages=opts["move_stages"], client=client)
    backups.record_import(store, r, path.name, len(rows))
    print(f"Added {r['added']}, updated {r['updated']}, moved {len(r['moved'])}, unchanged {r['unchanged']}, "
          f"skipped {r['skipped_rows']}.")
    if r["backup"]:
        print("Saved as one row on Changes. Undo it there while nothing else has changed.")
    else:
        print("Nothing changed, so no backup was needed.")
    asks = [i for i in r.get("new_job_ids") or [] if i in importers.openings_to_ask(store)]
    if asks:
        print(f"{len(asks)} new job(s) made. Set their openings in the app, under Import, or on Today.")


def main(argv: list[str] | None = None) -> None:
    parser = argparse.ArgumentParser(prog="talent", description="Talent Search")
    sub = parser.add_subparsers(dest="cmd")
    sub.add_parser("app", help="start the app and open it in your browser (default)")
    serve = sub.add_parser("serve", help="start the web server and open the app window")
    serve.add_argument("--port", type=int, default=DEFAULT_PORT)
    serve.add_argument("--no-browser", action="store_true", help="don't open the window (run talent open later)")
    sub.add_parser("open", help="open the window of the running app (unlocks it in this browser)")
    sub.add_parser("mcp", help="run the MCP server over stdio (AI apps start this)")
    sub.add_parser("sample-data", help="load made-up candidates to try the app")
    sub.add_parser("reindex", help="rebuild the optional Typesense index from SQLite")
    sub.add_parser("connect-claude", help="add Talent Search to Claude Desktop")
    sub.add_parser("connect-codex", help="add Talent Search to Codex")
    sub.add_parser("connect-lmstudio", help="add Talent Search to LM Studio")
    imp = sub.add_parser("import-csv", help="import candidates from a CSV file (columns guessed from the header)")
    imp.add_argument("file")
    imp.add_argument("--dry-run", action="store_true", help="show what would change, and change nothing")
    imp.add_argument("--source", default=None, help='where these people came from (default "CSV import")')
    imp.add_argument("--map", default="", metavar="SPEC",
                     help='match columns by hand, like "Stage Entered=stage_changed_at, Recruiter=tag_column, Owner=skip"')
    syn = sub.add_parser("sync", help="re-import this week's ATS export: shows what will change, then applies it")
    syn.add_argument("file")
    syn.add_argument("--yes", action="store_true", help="apply without asking")
    syn.add_argument("--dry-run", action="store_true", help="show what would change, and change nothing")
    syn.add_argument("--map", default="", metavar="SPEC", help="match columns by hand (see import-csv --map)")
    syn.add_argument("--client", default=None, help="the client for jobs in a file with no client column")
    syn.add_argument("--source", default=None, help="where these people came from")
    syn.add_argument("--no-move", action="store_true", help="don't move people already in a job")
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
        run_app(port=args.port, open_browser=not args.no_browser)
    elif cmd == "open":
        if not open_window():
            sys.exit("Talent Search isn't running. Start it from its app icon or with: talent serve")
    elif cmd == "sample-data":
        from .sample_data import load_sample_data
        from .store import Store

        print(f"Added {load_sample_data(Store(db_path()))} sample candidates.")
    elif cmd == "reindex":
        from .store import Store
        from .typesense_index import TypesenseIndex

        from .typesense_index import env_status

        status = env_status()
        if status and status["refused"]:
            sys.exit(f"TYPESENSE_URL {status['url']} must use https unless it's on this computer.")
        index = TypesenseIndex.from_env()
        if index is None:
            sys.exit("Set TYPESENSE_URL and TYPESENSE_API_KEY first. Built-in search needs no index.")
        print(f"Indexed {index.rebuild(Store(db_path()))} candidates.")
    elif cmd == "sync":
        _sync(args)
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
            mapping, _ = importers.mapping_for(store, header, rows)
            try:
                mapping = importers.apply_map(header, mapping, args.map)
            except ValueError as exc:
                sys.exit(str(exc))
            if not any(m in ("name", "first_name", "last_name") for m in mapping):
                sys.exit("Couldn't find a name column. Use the app's Import page to match columns by hand.")
            source = args.source or importers.default_source(header)
            print("Columns: " + ", ".join(f"{h} -> {importers.TARGET_LABELS.get(m, m)}" for h, m in zip(header, mapping)
                                          if m))
            if args.dry_run:
                r = importers.preview_import(store, header, rows, mapping, source=source)
                for line in importers.preview_lines(r):
                    print(line)
                details = r["changes"] + r["skipped"] + r["possible_duplicates"] + r["warnings"]
                for line in details[:20]:
                    print("  " + line)
                print("Nothing was changed (dry run).")
            else:
                from . import backups

                r = importers.import_rows(store, header, rows, mapping, source=source, file_name=Path(args.file).name)
                importers.save_sync_profile(store, header, mapping, r, file_name=Path(args.file).name, source=source)
                backups.record_import(store, r, Path(args.file).name, len(rows))
                print(f"Added {r['added']}, updated {r['updated']}, unchanged {r['unchanged']}, "
                      f"skipped {r['skipped_rows']}, {r['notes']} note{'' if r['notes'] == 1 else 's'} added.")
                if r["pipelines_added"] or r["jobs_created"]:
                    print(f"Put {r['pipelines_added']} in job pipelines"
                          + (f", made {len(r['jobs_created'])} new job(s): " + ", ".join(r["jobs_created"])
                             if r["jobs_created"] else "") + ".")
                for p in importers.result_panels(r):
                    if p["key"] == "changes":
                        continue
                    print(f"{p['title']} ({p['count']}):")
                    for line in (p["groups"] or p["lines"])[:importers.PANEL_LINES]:
                        print("  " + line)
                if r["not_imported"]:
                    print("Not imported: " + ", ".join(r["not_imported"]))
                if r["backup"]:
                    print(f"Backup from before this import: {r['backup']}")
                else:
                    print("Nothing changed, so no backup was needed.")
    elif cmd in ("connect-claude", "connect-codex", "connect-lmstudio"):
        from . import integrations

        fn = {"connect-claude": integrations.connect_claude, "connect-codex": integrations.connect_codex,
              "connect-lmstudio": integrations.connect_lmstudio}[cmd]
        result = fn()
        if not result.get("installed", True):
            sys.exit(result["message"])
        print(f"Updated {result['path']}. {result.get('next_step') or 'Restart the app to load Talent Search.'}")
