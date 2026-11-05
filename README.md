# Talent Search

A candidate database for recruiters that lives on your own computer. Search it
instantly from a simple app window, track each job's pipeline, and let Claude
Desktop, Codex or a local model in LM Studio find, save and update candidates
for you in plain English.

- **Download, double-click, done.** No terminal, no accounts, no servers.
- **Fast search** across names, titles, companies, locations and each person's
  "about" summary, with highlights. Shorthand like `sf`, `nyc`, `swe`, `ml` just works.
- **Works with your AI app.** One button connects Claude Desktop, Codex or LM Studio.
  Then ask things like *"Save this person to Talent Search"* (paste a LinkedIn link)
  or *"Who do I have in Austin with Rust experience?"* In Claude, results show up
  as cards and a pipeline board right in the chat.
- **Today page.** Upcoming interviews and follow-ups first, then candidates stuck in a
  stage, jobs that need more candidates and profiles that may be out of date. Set a
  follow-up date when you log a call. Turn each kind on or off.
- **Jobs with your own stages.** Rename and add stages per job, then drag people
  across a board or work in a table grouped by stage.
- **Ready-made AI recipes.** One click copies a prompt like "Source candidates for
  this job" and opens Claude.
- **Get data in and out.** CSV import with column matching, CSV export, a database
  backup, and paste-a-resume.
- **Private.** Everything is stored in one file on your computer. Talent Search uses
  none of your data and has no telemetry. The AI can never delete anything.

## For recruiters

1. Download the Mac or Windows zip from the Releases page and unzip it.
2. Open **Talent Search**. It opens in your web browser.
   - Mac: the first time, right-click the app and choose **Open** (it isn't notarized yet).
   - Windows: if SmartScreen appears, choose **More info**, then **Run anyway** (not code-signed yet).
3. Go to **Connect AI**, press **Connect Claude**, **Connect Codex** or
   **Connect LM Studio**, then quit and reopen that app.
4. No candidates yet? Press **Try it with sample data**. Sample people and jobs are
   marked Sample, and **Remove sample data** (on Today and in Settings) deletes exactly those.

Your data is in `~/Library/Application Support/Talent Search` (Mac) or
`%LOCALAPPDATA%\Talent Search` (Windows).

## Admin and rollout

- **Where data lives.** One SQLite file, `talent.db`, plus `app.log`, in
  `~/Library/Application Support/Talent Search` (Mac) or `%LOCALAPPDATA%\Talent Search`
  (Windows). Settings shows the exact path.
- **Backups.** Settings > **Download a backup** saves a complete copy of the
  database while the app is running. Keep a recent one somewhere safe, such as a
  company drive. **Export candidates as CSV** gives a spreadsheet copy too.
- **Moving to a new computer.** Install the app there and open it once, then quit
  it. Copy your backup into the data folder above and rename it to `talent.db`,
  replacing the empty one. Open the app again and press **Connect** for each AI app.
- **Uninstalling.** Quit the app and delete it. To remove your data too, delete the
  data folder above (take a backup first if you might want it back). If you
  connected an AI app, remove the `talent-search` entry from its MCP settings:
  `claude_desktop_config.json` (Claude Desktop), `~/.codex/config.toml` (Codex) or
  `~/.lmstudio/mcp.json` (LM Studio).
- **Nothing is sent anywhere.** The app listens only on 127.0.0.1, has no accounts,
  no telemetry and no update checks. The only data that leaves the computer is
  what your AI app sends to its own model when you ask it to use Talent Search.

## For developers

Requires [uv](https://docs.astral.sh/uv/).

```bash
uv sync --all-groups
uv run talent                # start the app and open the browser (http://127.0.0.1:8765)
uv run talent serve --port 8800   # pick the port (add --no-browser for the server only)
uv run talent open           # open and unlock the running app's window in your browser
uv run talent sample-data    # load made-up candidates
uv run talent mcp            # MCP server over stdio (AI apps run this)
uv run talent connect-claude # or connect-codex / connect-lmstudio: write the AI app's MCP config
uv run talent import-csv people.csv   # or export-csv out.csv
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
  store.py            SQLite schema, migrations, candidates/jobs/stages/timeline, FTS5 search
  actions.py          Today's action items (computed on read, toggled in Settings)
  importers.py        CSV import/export and the local paste-a-resume parser
  recipes.py          ready-made AI prompts, Claude deep link, GitHub issue links
  mcp_views.py        MCP Apps in-chat views (candidate, results, board, today)
  typesense_index.py  optional Typesense mirror (outbox drain) + search fallback
  mcp_server.py       MCP tools, prompts and view resources (no delete tools)
  web.py              FastAPI app (HTMX pages + JSON API), localhost-only guard
  integrations.py     one-click Claude Desktop / Codex / LM Studio config
  cli.py              entry point: app (default), serve, mcp, sample-data, import/export, reindex
packaging/            PyInstaller spec, entry script, MCP smoke test
docs/DESIGN.md        architecture, data model, roadmap, hosting research
```
