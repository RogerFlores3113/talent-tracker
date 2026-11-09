# Changelog

All notable changes to Talent Search. Newest first.

## [0.1.0] - 2026-11-09

The first release. Talent Search is a candidate database that lives on your own computer, that opens in your web browser for you and tools for your AI app.

### Search and candidates
- Fast search across names, titles, companies, locations and "about" text, with highlights. Shorthand like `sf`, `nyc` and `swe` works.
- Candidate pages with a timeline of calls and notes, follow-up dates, duplicate merging and paste-a-profile.
- Filters on the Candidates table, and a work history that survives re-imports.

### Jobs and pipelines
- Jobs with your own stages (rename and add per job), a drag-and-drop board and a table grouped by stage.
- A shortlist where AI-found people wait for you to Keep or Pass, a hiring plan and a loop planner.
- Outreach with follow-up sequences, batch follow-ups and reply tracking.

### Today
- One page for what needs you: upcoming interviews and follow-ups, people stuck in a stage, jobs that need more candidates and profiles that may be out of date. Each kind can be turned off.

### Interviews and decisions
- Interviews shown in your own time zone, a week view, per-interviewer feedback and a chase list for feedback still owed.
- A Decide view that compares finalists, plus an offer and close workspace.

### Clients and reports
- Clients and hiring managers, update days, a Client desk and a Friday send-out.
- Client reports, submittals with a write-up of why each person fits, and an Ops review of the whole desk.

### Working with AI apps
- Connect Claude Desktop, Codex or LM Studio from the Connect AI page. In Claude, results show as cards and a pipeline board in the chat.
- The Claude Desktop connection now checks itself: it verifies what it wrote, tests that Talent Search starts and answers, explains how to fully quit and reopen Claude, and repairs itself if the app moved. A Test connection button and `talent check-claude` give a full report.
- A review queue with a Cautious mode, per-app trust levels and hourly caps for AI changes, with Undo for AI work. The AI can never delete anything.
- Ready-made AI recipes that copy a prompt and open Claude.

### Importing and exporting
- CSV import with column matching, CSV export, paste-a-resume and a weekly ATS re-import that keeps stage history.
- A database backup you can download while the app runs, and sample data to try things out (removed with one button).

### Look and feel
- Color themes, with "Sunset to forest" as the default.
- Gentler hover and focus states so clickable things light up, and more breathing room between elements.

### Safety and privacy
- Everything is stored in one file on your computer. The app listens only on 127.0.0.1, has no accounts and no telemetry, and the window is locked with a key.

### Install
- Mac disk image: open it and drag Talent Search onto Applications. A zip is still available.
- Windows zip and a new Linux build (`.tar.gz`).
- Every build is tested before release, including that AI apps can start Talent Search from it.
- Step-by-step install and troubleshooting guide in the README.
