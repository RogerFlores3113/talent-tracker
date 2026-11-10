# Talent Search

A candidate database for recruiters that lives on your own computer. Search it
instantly in your web browser, track each job's pipeline, and let Claude
Desktop, Codex or a local model in LM Studio find, save and update candidates
for you in plain English.

- **Download, install, done.** No terminal, no accounts, no servers.
- **Fast search** across names, titles, companies, locations and each person's
  "about" summary. Shorthand like `sf`, `nyc` and `swe` just works.
- **Jobs with your own stages.** A board and a table for each job, a Today page for
  what needs you, interviews, offers, and reports for your clients.
- **Works with your AI app.** One button connects Claude Desktop, Codex or LM Studio.
  Then ask *"Who do I have in Austin with Rust experience?"* or paste a LinkedIn link
  and say *"Save this person to Talent Search"*.
- **Your data stays yours.** One file on your computer, no telemetry. The AI can never
  delete anything.

See [CHANGELOG.md](CHANGELOG.md) for everything in each version.

**Requirements:** macOS on Apple Silicon (M1 or later) for now, Windows 10 or 11 (64-bit),
or Linux x86_64.

## Install

Pick whichever is easiest. All three put the app somewhere permanent, which matters:
if you run it from Downloads, from inside a zip or from a disk image, your computer runs
a temporary copy, and AI apps such as Claude Desktop can't find it later.

### 1. Ask Claude to install it

In the Claude desktop app (the **Code** tab or **Cowork**) or in Claude Code, paste this:

> Install Talent Search from https://github.com/RogerFlores3113/talent-tracker

Claude reads [INSTALL.md](INSTALL.md), runs the installer and tells you what to press next.

### 2. One line in a terminal

Mac or Linux:

```sh
curl -fsSL https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.sh | sh
```

Windows (PowerShell):

```powershell
irm https://raw.githubusercontent.com/RogerFlores3113/talent-tracker/main/install.ps1 | iex
```

The script downloads the latest release, checks it against `SHA256SUMS.txt`, installs it
(Mac: Applications; Windows: `%LOCALAPPDATA%\Programs\Talent Search`; Linux:
`~/.local/share/talent-search-app`), and opens it. If Talent Search is already installed it
is closed and replaced, and your data is kept. Afterwards the app is in your
Applications folder (Mac), your Start menu (Windows) or your applications menu, or run
`talent-search` (Linux). Files downloaded this way are not flagged
by macOS as coming from the internet, so on a Mac you normally skip the "can't be checked
for malware" window that the manual download below shows. Add `--no-open` (`-NoOpen` on
Windows) to install without opening it. You can read the scripts first:
[install.sh](install.sh) and [install.ps1](install.ps1).

### 3. Download it yourself

Get the file for your computer from the
[latest release](https://github.com/RogerFlores3113/talent-tracker/releases/latest). Each
release also lists `SHA256SUMS.txt` so you can check your download.

| Computer | File |
|---|---|
| Mac | `TalentSearch-mac.dmg` (or `TalentSearch-mac.zip`) |
| Windows | `TalentSearch-windows.zip` |
| Linux | `TalentSearch-linux.tar.gz` |

**Mac:** open the `.dmg`, drag **Talent Search** onto **Applications**, eject the disk
image and open it from Applications. The first time, macOS says it can't be checked for
malware because the app isn't notarized yet: right-click the app, choose **Open**, then
**Open** again (on newer macOS, **System Settings > Privacy & Security > Open Anyway**).
Do this once **before** you connect Claude, so macOS trusts it.

**Windows:** right-click the zip, choose **Extract All**, move the **Talent Search** folder
somewhere permanent (such as Documents), and open **Talent Search.exe**. If SmartScreen
appears, choose **More info**, then **Run anyway**. Keep both exe files in the folder
together; `talent-search-mcp.exe` is the one AI apps start.

**Linux:**

```bash
tar -xzf TalentSearch-linux.tar.gz
cd "Talent Search"
./talent-search
```

## Connect Claude and add skills

Talent Search opens in your web browser. Two buttons there finish the setup:

1. Press **Connect Claude**. This links Talent Search to Claude Desktop. On Mac and
   Windows it quits and reopens Claude Desktop for you so the link takes effect (never by
   force, so save your work there first). Linux has no restart: reopen Claude Desktop
   yourself.
2. Press **Add skills**. This puts the Talent Search skills into Claude Code and Codex,
   whichever you have. For the Claude app, it also saves `talent-search.plugin` and shows
   you where it is: in Claude, open **Customize > Plugins > Add > Upload plugin** and pick
   that file. To get updates, add the marketplace `RogerFlores3113/talent-tracker` in
   **Customize > Plugins** instead.

More detail, Codex and LM Studio, and other ways in are under
[Connect Claude Desktop](#connect-claude-desktop) and
[Skills for your AI app](#skills-for-your-ai-app) below.

## First run

Talent Search opens in your web browser at http://127.0.0.1:8765. It only listens on
your own computer. No candidates yet? Press **Try it with sample data**. Sample people
and jobs are marked Sample, and **Remove sample data** (on Today and in Settings)
deletes exactly those. A first-time tour walks you through connecting your AI app and
the main pages, using demo people it clears at the end; skip it any time, and replay a
shorter version from **Settings** (**Take the tour**).

Your data is one file, `talent.db`, in `~/Library/Application Support/Talent Search`
(Mac), `%LOCALAPPDATA%\Talent Search` (Windows) or `~/.local/share/Talent Search`
(Linux). **Settings** shows the exact path.

## Connect Claude Desktop

1. Install [Claude Desktop](https://claude.ai/download) and open it once. (If you
   downloaded Talent Search yourself on a Mac, also open it once with right-click >
   **Open**, as in Install above.)
2. In Talent Search, open **Connect AI** and press **Connect Claude**. It writes Claude
   Desktop's settings, checks that Talent Search starts, and, if Claude Desktop is open,
   quits and reopens it for you (save anything you're typing in Claude first). It never
   forces Claude to close: if Claude doesn't quit, the page says how to quit it yourself.
3. In Claude, start a new chat and ask *"What should I do today in Talent Search?"*
   Talent Search's tools are under the **+** menu, and **Settings > Developer** lists
   **talent-search** as running.

If something looks off, press **Test connection** on the Connect AI page. **Show full
report** copies a report you can paste into a bug report.

**Codex and LM Studio:** press **Connect Codex** or **Connect LM Studio** on the same
page, then quit and reopen that app.

### Where the cards show

Cards and tables show in the Claude desktop app and the ChatGPT desktop app. Claude asks once to display them: choose Always allow. Claude Code, the Codex command line and claude.ai in a browser show the same answers as text. After you update Talent Search, quit and reopen the app so it picks up the new views (in Claude, Developer > Reload MCP Configuration also works).

**ChatGPT desktop app:** pressing **Connect Codex** on the Connect AI page also connects the ChatGPT desktop app, because
both read the same settings file. Or, in ChatGPT, go to Settings > MCP servers > Add server > STDIO and enter the command
and arguments shown on that page. The ChatGPT website and the ChatGPT phone app can't reach apps on this computer.

## Skills for your AI app

Talent Search ships six short skills (sourcing, triage, interviews, outreach, pipeline
reviews, feedback and decisions) that teach your AI app how to use it well. On the
Connect AI page, press **Add skills**. In one go it:

- adds them to Claude Code and Codex, whichever it finds on your computer (only folders
  named `talent-search-*` that the app made are ever updated, and a skill file you edited
  is kept);
- saves `talent-search.plugin` in your Downloads folder, shows it in Finder or Explorer and
  opens Claude. In Claude, open **Customize > Plugins**, press **Add**, then **Upload
  plugin**, and pick that file. Claude has no way for another app to add skills for you,
  so this last step is yours.

Want updates instead? Add the marketplace `RogerFlores3113/talent-tracker` in
**Customize > Plugins > Add marketplace** (in Claude Code:
`claude plugin marketplace add RogerFlores3113/talent-tracker`, then
`claude plugin install talent-search@talent-tracker`).

From a terminal: `talent install-skills [--claude-code] [--codex] [--plugin [PATH]]` and
`talent remove-skills [--claude-code] [--codex] [--yes]` (they list what changes and ask first). A skill file you edit yourself is never overwritten or deleted.

Ask Claude Code or Codex: "install the skills in skills/ by following
skills/INSTALL-SKILLS.md".

## Troubleshooting

**Claude Desktop doesn't show Talent Search.**
Press **Connect Claude** again, or fully quit Claude Desktop and open it again. Then press **Test
connection** on the Connect AI page. If the entry disappeared, Claude was probably open
while you connected and wrote over the file: quit Claude, press **Connect Claude
Desktop** again, then reopen Claude. For a full report, run this.

Mac (in Terminal):

```bash
"/Applications/Talent Search.app/Contents/MacOS/Talent Search" check-claude
```

Windows (in PowerShell, using the folder you extracted; the windowed `Talent Search.exe`
prints nothing, so use the other one):

```powershell
"<folder>\Talent Search\talent-search-mcp.exe" check-claude
```

It exits cleanly when everything is healthy and says what to fix when it isn't.

**Claude says Talent Search failed to start.**
Look at Claude's log for it:

- Mac: `~/Library/Logs/Claude/mcp-server-talent-search.log`
- Windows: `%APPDATA%\Claude\logs\mcp-server-talent-search.log`
- Windows, Claude from the Microsoft Store: `%LOCALAPPDATA%\Packages\Claude_...\LocalCache\Roaming\Claude\logs`

If you moved the app, press **Connect Claude** again so it points at the new
place. On a Mac, also open the app once with right-click > **Open** so macOS trusts it.

**I installed Claude Desktop from the Microsoft Store.**
Talent Search finds that settings file by itself. The Connect page shows which file it
used.

**Mac says "Talent Search can't be opened."**
Right-click the app and choose **Open**, then **Open** again, or use **Open Anyway** in
**System Settings > Privacy & Security**.

**Windows SmartScreen blocks it.**
Choose **More info**, then **Run anyway**.

**The page says "locked".**
The window is locked by a key so other websites can't read your data. Open Talent
Search again from Applications (Mac) or from the Start menu or its folder (Windows),
and it unlocks in your browser.

**The port is already in use.**
Talent Search picks a free port by itself. Use whatever address it opens in your browser.

**Where is my data, and how do I back it up?**
See First run for the folder. **Settings > Download a backup** saves a complete copy
while the app is running. **Export candidates as CSV** gives a spreadsheet copy.

**How do I update?**
Run the one-line install command again (see Install above), which closes the app and
replaces it. Or download the new version and replace the app yourself (drag the new one
onto Applications and choose Replace, or replace the folder). Your data is kept in its own folder. Press
**Connect** again if you moved the app.

**How do I move to a new computer?**
Install the app there, open it once, then quit it. Copy your backup into the data folder
and rename it to `talent.db`, replacing the empty one. Open the app and press **Connect**
for each AI app.

**How do I uninstall?**
Quit the app and delete it. To remove your data too, delete the data folder (take a
backup first if you might want it back). If you connected an AI app, remove the
`talent-search` entry from its settings: `claude_desktop_config.json` (Claude Desktop),
`~/.codex/config.toml` (Codex) or `~/.lmstudio/mcp.json` (LM Studio).

## Privacy

Everything is stored in one file on your computer. The app listens only on 127.0.0.1,
has no accounts, no telemetry and no update checks. The only data that leaves your
computer is what your AI app sends to its own model when you ask it to use Talent
Search, and the AI can never delete anything.

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
uv run talent check-claude   # full report on the Claude Desktop connection (exit 0 when healthy)
uv run talent import-csv people.csv   # or export-csv out.csv
uv run pytest -q
```

Dev data lives in `./data/` (override with `TALENT_DATA_DIR`).

Build the desktop app (on the target OS):

```bash
uv run pyinstaller packaging/talent-search.spec --noconfirm
```

On a Mac, make the disk image the same way CI does:

```bash
codesign --force --deep -s - "dist/Talent Search.app"
mkdir dmg-staging && cp -R "dist/Talent Search.app" dmg-staging/ && ln -s /Applications dmg-staging/Applications
hdiutil create -volname "Talent Search" -srcfolder dmg-staging -ov -format UDZO TalentSearch-mac.dmg
```

CI (`.github/workflows/ci.yml`) runs tests on Linux, Windows and macOS, builds the
Mac `.app` (as a `.dmg` and a zip), the Windows folder (zip) and the Linux folder
(`.tar.gz`), smoke-tests the MCP server and the one-click connect inside each build,
runs `install.sh` / `install.ps1` against each fresh build and smoke-tests the installed copy,
and publishes a GitHub Release when you push a `v*` tag.

### Making a release

1. Add a `## [X.Y.Z] - date` section to `CHANGELOG.md` (it becomes the release notes).
2. Bump the version in `pyproject.toml`, `src/talent/__init__.py` and
   `packaging/talent-search.spec`, then run `uv lock`.
3. Commit, then `git tag vX.Y.Z && git push origin main vX.Y.Z`.
4. CI builds every download and attaches them to the release.
   `python packaging/release_notes.py vX.Y.Z` previews the notes and fails if the
   changelog section is missing.

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
packaging/            PyInstaller spec, entry script, MCP smoke test, release notes script
docs/DESIGN.md        architecture, data model, roadmap, hosting research
```
