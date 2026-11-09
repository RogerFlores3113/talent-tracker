# Changelog

All notable changes to Talent Search. Newest first.

## [Unreleased]

### Search
- NEAR finds words close together: `python NEAR django` (within 5 words) or `python NEAR/3 django`.
- "Did you mean" for a mistyped prefix (`skil:`, `tittle:`), a mistyped skill (`skill:pyhton`) and misspelled words in OR and quoted searches.
- Phrase, OR and NEAR searches look in notes too, so a wider search never finds fewer people.
- `c#`, `c++`, `f#` and `.net` are words of their own, so `c#` no longer finds C++ people.
- A prefix typed twice means either (`company:google company:meta`), and a filter plus a typed prefix both count. The read-back line says so.
- A match count, search tips on Candidates, and a type-ahead skill filter with every skill.

### Saved searches
- Today lists every saved search, with pinning. Each count says what it counts.
- Opening a saved search marks the people who are new since you last looked, and the table has an Added column. Someone added in the same second now counts.

### Interviews and time zones
- Interviews can carry the candidate's and each interviewer's time zone. Invites and calendar files show every person their own clock first, with yours in brackets.
- A time that doesn't exist on a daylight saving day is refused with the nearest real times; a time that happens twice takes the first and says so.
- A date with no time is an all-day interview, not midnight. The log form needs a time for an interview.
- Calendar files give each session its own title, and a week can be downloaded. Decide's finalists are people in late stages or with a finished interview. Today merges feedback cards per candidate, interviewer emails are saved, and the loop planner skips US public holidays.

### Importing and duplicates
- A re-import that brings someone back with a new email offers to update them instead of adding a copy.
- Merge many duplicate pairs at once, with one backup, and pick which email stays main.
- Today and Jobs load fast with thousands of people.
- CSV exports open correctly in Excel (names like Søren and Łukasz) and keep stage dates on a round trip.
- Tick people on the Candidates table (or all the results) to set a color, add them to a job at a stage, or export just them as CSV. There is no bulk delete.
- A re-import matches a changed email by name plus company, or name plus phone, and updates by default. When several people fit, the one with the same phone or job is picked, or you choose. People who still came in as new show on the result page with a Merge button for each.
- Possible duplicates can be filtered by reason, one tick covers every pair of a filter, bulk merge pages start unticked, the newer email is the default main email, and same-named people are told apart by email or company.
- Import tidies names: spaces trimmed, ALL CAPS or all lowercase turned into Title Case (McDonald and de la Cruz are left alone). "Other emails" are kept in their own field, not repeated in Notes.

### Paste and connect
- Pasting a resume reads every role with its dates, and a note reads name, title and company. The check page says what wasn't found.
- The Connect AI page asks which app you use, says when an app isn't installed, keeps technical details in a fold, and gives Claude Code its one-line connect command.

### Working with AI apps
- Saved searches made by an AI app are on Changes and can be undone.
- Interview replies say which time zone was used and name any clash. An interview needs a start time.
- Held-for-review replies say exactly what went through and what waits.
- `talent install-skills` asks first (or takes `--yes`), and reinstalls say which files are new, updated or unchanged.

### Keyboard and screen readers
- A skip link, current-page marks, named calendar days, table captions and sort state.
- Stage menus on the board wait for Enter, keep focus on the person you moved and announce the move.
- Better contrast on Rejected and Withdrawn columns.

## [0.2.0] - 2026-11-09

### Search
- Search shorthand: `skill:`, `title:`, `company:` and other prefixes, `-word` to leave things out, quoted phrases, `OR`, and a line that says how the app read your search.
- Saved searches, shown on Today and on the Candidates table. Saved searches can include a color tag.

### Candidates
- Color tag bubbles. Click a bubble to cycle red, orange, yellow, green, blue, purple and off. Tagged people sort to the top.
- A duplicate finder with bulk actions. Every email a person has now counts for duplicates, imports, merges and search.
- Pasting a profile or an email signature fills in more fields, including "Title, Company" lines.

### Interviews
- A month calendar, with the week view kept as an alternate. Download interviews as a calendar (.ics) file for your own calendar app.

### First run
- A guided tour that can load sample data and clear it again. Skip it at any time, or replay a short version from the footer or Settings.

### Working with AI apps
- Ready-made skills for Claude Code, Codex and Claude Desktop, installed from the app or by following INSTALL-SKILLS.md.
- Prompt-injection guards: text that tries to give the AI orders is flagged, held for your review, and shown with a warning. The AI still can never delete anything, and it never adds an email to a person who is already saved.
- Scraping is no longer ruled out. A "Stricter site guards" switch in Settings (off by default) asks the AI app to stay off sites whose terms forbid scraping and lowers its hourly cap. Some sites' terms forbid scraping, so check before you point an AI app at them.

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
