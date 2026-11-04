# Talent Search: design

Status: v0.1 slice built (candidate store, search page, MCP server, desktop packaging).
Last updated: 2026-11-04.

## 1. What it is

A downloadable desktop app for recruiters (not engineers) that keeps their own
candidate database and makes it usable from AI apps they already have.

- **Centerpiece:** a local database of candidates: name, contact info, an indexed
  "about" summary, current title and company, location, last updated.
- **AI-first input:** recruiters use Claude Desktop or Codex to research people
  and say "save this candidate". The app's local MCP server does the writing.
- **Recruiter workflow (next):** jobs, per-job pipelines with stages, and a
  dashboard of suggested next actions.
- **Later:** optional scraping helpers, and a hosted mode with sign-in for
  teams and multi-device.

Design rules:
1. **Zero setup.** Double-click opens it. No terminal, Docker, Python or
   separate search server. Connecting an AI app is one button.
2. **Local-first.** SQLite on the user's machine is the source of truth.
   Hosted mode, when it comes, syncs from it rather than replacing it.
3. **Plain language everywhere.** UI copy, errors and MCP tool descriptions are
   written for a recruiter and for the model acting for one.

## 2. Architecture (desktop, v0.1)

```
 Claude Desktop / Codex / LM Studio            Browser tab (the app window)
            │ launches over stdio                        │ http://127.0.0.1:8765
            ▼                                            ▼
   "Talent Search mcp"  ──────────┐        ┌──────  "Talent Search" (double-click)
    (MCP server, FastMCP)         │        │         FastAPI + Jinja + HTMX
                                  ▼        ▼
                         talent.db (SQLite, WAL)
                         ├─ candidates, jobs, applications, activity
                         ├─ candidates_fts (FTS5, trigger-maintained)
                         └─ search_outbox ──► optional Typesense mirror
```

- **One executable, two modes.** No arguments: start the local web server on
  127.0.0.1 and open the browser (a second double-click just reopens the tab).
  `mcp` argument: speak MCP over stdio. Both open the same SQLite file in WAL
  mode, so the AI app and the window can work at the same time.
- **Windows detail:** a windowed .exe has no usable stdio, so the build ships a
  second console exe, `talent-search-mcp.exe`, in the same folder for AI apps.
- **Why a browser tab, not Electron/Tauri:** same pattern as the Search app,
  ~37 MB build, no Chromium to bundle, HTMX keeps the UI snappy with no JS
  build step. A native window (pywebview) is an easy later swap if users want a
  "real app" feel.
- **Local server safety:** binds to 127.0.0.1 only; middleware rejects foreign
  `Host` headers (DNS rebinding) and cross-site `Origin` on writes. Candidate
  text is always HTML-escaped; search highlights use control-character markers
  that are swapped for `<mark>` only after escaping.

## 3. Data model

All timestamps are UTC ISO 8601. Schema version in `PRAGMA user_version`;
migrations are append-only (`store._MIGRATIONS`).

**candidates**
| field | notes |
|---|---|
| id | integer |
| name | required |
| email | lowercased; unique when present (dedupe key) |
| phone, website | |
| linkedin_url | normalized (`https://linkedin.com/in/x`); unique when present (dedupe key) |
| current_title, current_company, location | filterable |
| about | 2-4 sentence summary; the main search text |
| tags | comma-separated for now |
| source | where they were found ("LinkedIn", "referral from Sam") |
| created_at, updated_at | `updated_at` is "last updated" in the UI |

**jobs**: id, title, company, location, description, status (`open`, `on_hold`, `closed`), timestamps.

**applications** (a candidate in a job's pipeline): candidate_id, job_id (unique
pair), stage, notes, created_at, stage_changed_at. Stages: `sourced, contacted,
screening, interviewing, offer, hired, rejected, withdrawn`.

**activity**: application_id, kind (`stage`, `note`; later `email`, `call`,
`interview`), detail, at. Feeds the dashboard and history.

**action items** (planned): computed by rules, not stored (see section 6), with
a small `dismissed_actions` table so a recruiter can snooze one.

Dedupe: `save_candidate` merges into an existing row when email or LinkedIn
matches, and only non-empty incoming fields overwrite. An AI re-saving a sparse
profile can't blank out what the recruiter filled in.

## 4. Search

- **Default: SQLite FTS5**, external-content table kept in sync by triggers, so
  every write path is indexed. bm25 with column weights (name 10, title 5,
  company 3, tags 3, location 2, about 1). Every word must match; the last word
  is a prefix so results update as you type. Shorthand expands (`sf` to San
  Francisco, `swe` to software engineer, etc.). User input is tokenized, never
  passed as FTS syntax, so quotes or operators can't break it.
- **UI:** HTMX fires on input with a 120 ms debounce; the results fragment
  comes back from the same `/` route. On a laptop this is well under a frame of
  work for tens of thousands of rows.
- **Optional Typesense** (`TYPESENSE_URL` + `TYPESENSE_API_KEY`): triggers also
  write `search_outbox`; the sync drains it (collapsing to the last op per
  candidate, upserting via bulk import). Search prefers Typesense and falls back
  to FTS5 for 30 s when it's unreachable. `talent reindex` rebuilds it. This is
  for hosted mode or very large databases, not something desktop users install.
- **Later:** semantic search ("people like this one") with a small local
  embedding model or sqlite-vec, blended with bm25.

## 5. MCP server

Transport: stdio, launched by the AI app. Tools (all return trimmed JSON):

| tool | purpose |
|---|---|
| `search_candidates(query, location?, title?, limit)` | ranked search with highlighted match |
| `get_candidate(id)` | full record plus jobs and stages |
| `save_candidate(name, ...)` | add or merge by email/LinkedIn |
| `update_candidate(id, ...)` | edit fields; empty string clears |
| `delete_candidate(id)` | description tells the model to confirm first |
| `create_job`, `list_jobs` | job postings with pipeline counts |
| `add_to_pipeline`, `move_stage`, `job_pipeline` | per-job pipeline |

Server instructions tell the model to search before adding, write a useful
`about`, and fill title/company/location.

**Connecting AI apps (one button each):**
- **Claude Desktop:** we merge our entry into `claude_desktop_config.json`
  (backup first, never touch other entries, refuse to edit a broken file).
- **Codex:** we write `[mcp_servers.talent-search]` into `~/.codex/config.toml`
  the same careful way. OpenAI's docs say the ChatGPT desktop app, Codex CLI and
  IDE extension share this config ([Codex MCP docs](https://developers.openai.com/codex/mcp)).
- **Anything else** (Cursor, LM Studio for local models): copy-paste JSON on the
  Connect page.
- **Next:** ship a `.mcpb` bundle so Claude Desktop installs it by double-click
  ([MCPB docs](https://claude.com/docs/connectors/building/mcpb)). The docs
  recommend Node servers and don't say whether a prebuilt binary works, so the
  likely shape is a tiny Node launcher that execs our bundled binary. Needs a test.
- **ChatGPT web / remote clients** need a remote MCP endpoint, which arrives
  with hosted mode (streamable HTTP + OAuth).

## 6. Recruiter dashboard (next milestone)

- **Jobs board:** each open job with counts per stage; click into a kanban-style
  pipeline (HTMX drag or simple stage buttons).
- **Candidate page:** history timeline from `activity`.
- **Action items**, computed on read from simple rules (thresholds editable later):
  - contacted, no change in 3+ days: "Follow up with X"
  - screening/interviewing, no change in 5+ days: "Nudge the hiring team about X"
  - offer, 3+ days: "Check in on X's offer"
  - open job with fewer than 5 active candidates: "Source more for Job"
  - candidate not updated in 6+ months: "Refresh X's profile" (good AI task)
- **MCP:** `list_action_items` so a recruiter can ask Claude "what should I do today?"

## 7. Scraping (optional, later)

Main path is the recruiter using Claude Desktop (with its own browsing or Claude
in Chrome) and saving through our MCP tools. For users who want it built in,
reuse the Search project's approach: `browser-use` driving the user's own Chrome,
streaming steps to the UI, with results going through `save_candidate`.
Behind a feature flag and off by default, and it has to respect site terms
(LinkedIn in particular forbids automated scraping).

## 8. Distribution

- PyInstaller, one spec (`packaging/talent-search.spec`): macOS `.app`, Windows
  folder app with the extra console MCP exe. Linux build verified locally (37 MB).
- GitHub Actions: tests on Linux/Windows/macOS, builds on every push, MCP smoke
  test inside each build, zips attached to a GitHub Release on `v*` tags.
- **Trust warnings are the biggest UX gap for non-engineers.** Unsigned apps get
  Gatekeeper and SmartScreen warnings. Fix before a real launch: Apple Developer
  ID signing plus notarization, and a Windows code-signing certificate. Both
  cost money and need accounts, so they're Roger's call.
- Also next: app icon, an installer (DMG with Applications link; Inno Setup or
  MSIX on Windows), auto-update check against GitHub Releases, backups/export
  (CSV and a "Back up my database" button).

## 9. Hosted mode (later, short research)

Prices from the vendors' pricing pages, read 2026-11-04.

| option | cost for a solo project | fit with a SQLite-first app |
|---|---|---|
| DigitalOcean droplet, SQLite on disk + Litestream backups | 1 GB $6/mo, 2 GB $12/mo ([DO](https://www.digitalocean.com/pricing/droplets)) | Best: same code and schema as desktop |
| DO Managed Postgres | from $15.15/mo ([DO](https://www.digitalocean.com/pricing/managed-databases)) | Needs a second schema/dialect |
| Supabase | Free: 500 MB, pauses after 1 week idle; Pro from $25/mo ([Supabase](https://supabase.com/pricing)) | Postgres, plus built-in auth |
| Neon | Free: 1 GB per project, no idle pause; pay-as-you-go after ([Neon](https://neon.com/pricing)) | Postgres |
| Turso (libSQL) | Free: 5 GB; Developer $4.99/mo ([Turso](https://turso.tech/pricing)) | SQLite-native with local sync (`pyturso` push/pull) ([docs](https://docs.turso.tech/sdk/python/reference)); its pricing page also says Turso is joining Supabase, so watch for changes |
| PocketBase | free, self-host on the droplet | SQLite-based, built-in OAuth2 (Google, GitHub) ([PocketBase](https://pocketbase.io)); but a Go app with its own data API |
| Typesense | self-host needs 2 vCPUs and roughly 2-3x the indexed text in RAM ([docs](https://typesense.org/docs/guide/system-requirements.html)); Cloud starts at 0.5 GB clusters | Only needed at scale; FTS5 covers launch |

**Recommendation:** one DigitalOcean droplet running the same FastAPI app with
SQLite (one database file per workspace), Litestream streaming backups to DO
Spaces, and sign-in with Google and GitHub via Authlib in FastAPI. Start on FTS5;
add self-hosted Typesense on the droplet (bump to a 2 vCPU size) only when search
needs typo tolerance or facets. Desktop-to-hosted sync: push changes from the
local outbox to an authenticated sync endpoint, last-writer-wins on
`updated_at` per row. Turso is the fallback if hand-rolled sync gets painful.
Hosted mode also gives ChatGPT and other remote clients an MCP endpoint
(streamable HTTP with OAuth).

## 10. Roadmap

1. **v0.2 desktop polish:** jobs board and pipeline UI, action items, CSV
   import/export, backup button, icon, installers, `.mcpb` for Claude.
2. **v0.3 trust:** code signing and notarization, auto-update check.
3. **v0.4 smarter search:** semantic similarity, saved searches, "refresh stale
   profiles" AI workflow.
4. **v1 hosted:** droplet deploy, OAuth, workspace sync, remote MCP.

## 11. Open questions for Roger

- Repo name and whether the app is called "Talent Search".
- Pay for Apple Developer ID and Windows signing before sharing with recruiters?
- Candidate-side version later (job seekers tracking applications) shares this
  core; keep one codebase with two modes, or fork?
- Upgrade to MCP Python SDK 2.x (just released, renamed FastMCP) now or after v0.2?
  v0.1 pins `mcp<2`.
