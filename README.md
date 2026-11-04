# Talent Search

A candidate database for recruiters that lives on your own computer. Search it
instantly from a simple app window, and let Claude Desktop or Codex find, save
and update candidates for you in plain English.

- **Download, double-click, done.** No terminal, no accounts, no servers.
- **Fast search** across names, titles, companies, locations and each person's
  "about" summary, with highlights. Shorthand like `sf`, `nyc`, `swe`, `ml` just works.
- **Works with your AI app.** One button connects Claude Desktop or Codex.
  Then ask things like *"Save this person as a candidate"* (paste a profile)
  or *"Who do I have in Austin with Rust experience?"*
- **Private.** Everything is stored in one file on your computer.

## For recruiters

1. Download the Mac or Windows zip from the Releases page and unzip it.
2. Open **Talent Search**. It opens in your web browser.
   - Mac: the first time, right-click the app and choose **Open** (it isn't notarized yet).
   - Windows: if SmartScreen appears, choose **More info**, then **Run anyway** (not code-signed yet).
3. Go to **Connect AI**, press **Connect to Claude** or **Connect to Codex**,
   then quit and reopen that app.
4. No candidates yet? Press **load sample candidates** to try it out.

Your data is in `~/Library/Application Support/Talent Search` (Mac) or
`%LOCALAPPDATA%\Talent Search` (Windows).

## For developers

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
uv run talent                # start the app and open the browser (http://127.0.0.1:8765)
uv run talent serve          # server only
uv run talent sample-data    # load made-up candidates
uv run talent mcp            # MCP server over stdio (AI apps run this)
uv run talent connect-claude # or connect-codex: write the AI app's MCP config
uv run pytest -q
```

Dev data lives in `./data/` (override with `TALENT_DATA_DIR`).

Build the desktop app (on the target OS):

```bash
uv run pyinstaller packaging/talent-search.spec --noconfirm
```

CI (`.github/workflows/ci.yml`) runs tests on Linux, Windows and macOS, builds
the Mac `.app` and Windows app, smoke-tests the MCP server inside each build, and
attaches zips to a GitHub Release when you push a `v*` tag.

### Optional: Typesense

Built-in search (SQLite FTS5) needs nothing extra. To mirror into Typesense for
typo tolerance and facets, set `TYPESENSE_URL` and `TYPESENSE_API_KEY`, then run
`uv run talent reindex`. If Typesense goes down, search falls back to built-in.

### Layout

```
src/talent/
  store.py            SQLite schema, migrations, candidates/jobs/pipeline, FTS5 search
  typesense_index.py  optional Typesense mirror (outbox drain) + search fallback
  mcp_server.py       MCP tools for Claude Desktop / Codex / local models
  web.py              FastAPI app (HTMX pages + JSON API), localhost-only guard
  integrations.py     one-click Claude Desktop / Codex config
  cli.py              entry point: app (default), serve, mcp, sample-data, reindex
packaging/            PyInstaller spec, entry script, MCP smoke test
docs/DESIGN.md        architecture, data model, roadmap, hosting research
```
