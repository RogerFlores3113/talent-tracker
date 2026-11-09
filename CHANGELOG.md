# Changelog

All notable changes to Talent Search. Newest first.

## [Unreleased]

### Search
- NEAR finds words close together: `python NEAR django` (within 5 words) or `python NEAR/3 django`.
- "Did you mean" for a mistyped prefix (`skil:`, `tittle:`), a mistyped skill (`skill:pyhton`) and misspelled words in OR and quoted searches.
- Phrase, OR and NEAR searches look in notes too, so a wider search never finds fewer people.
- `c#`, `c++`, `f#` and `.net` are words of their own, so `c#` no longer finds C++ people.
- Every saved search shows its count and "+N new" (not just the first six), and hovering a chip shows what it searches for.
- Hints for common slips: `skill:machine learning` (use quotes), `"a b"~3` (use NEAR/3), lowercase `not` and `near`, and a lone `-`.
- The zero-results page offers "Did you mean" and a way to widen the search.
- The color counters follow your search and filters.
- Clearing sample data or finishing the tour removes the saved searches you made on the sample people, so no empty chips are left.
- A prefix typed twice means either (`company:google company:meta`), and a filter plus a typed prefix both count. The read-back line says so.
- A match count, search tips on Candidates, and a type-ahead skill filter with every skill.

### Saved searches
- Today lists every saved search, with pinning. Each count says what it counts.
- Opening a saved search marks the people who are new since you last looked, and the table has an Added column. Someone added in the same second now counts.

### Importing and duplicates
- A re-import that brings someone back with a new email offers to update them instead of adding a copy.
- Merge many duplicate pairs at once, with one backup, and pick which email stays main.
- Today and Jobs load fast with thousands of people.
- CSV exports open correctly in Excel (names like Søren and Łukasz) and keep stage dates on a round trip.
- Tick people on the Candidates table (or all the results) to set a color, add them to a job at a stage, or export just them as CSV. There is no bulk delete.
- A re-import matches a changed email by name plus company, or name plus phone, and updates by default. When several people fit, the one with the same phone or job is picked, or you choose. People who still came in as new show on the result page with a Merge button for each.
- Possible duplicates can be filtered by reason, one tick covers every pair of a filter, bulk merge pages start unticked, the newer email is the default main email, and same-named people are told apart by email or company.
- Import tidies names: spaces trimmed, ALL CAPS or all lowercase turned into Title Case (McDonald and de la Cruz are left alone). "Other emails" are kept in their own field, not repeated in Notes.

### Interviews and time zones
- Interviews can carry the candidate's and each interviewer's time zone. Invites and calendar files show every person their own clock first, with yours in brackets.
- A time that doesn't exist on a daylight saving day is refused with the nearest real times; a time that happens twice takes the first and says so.
- A date with no time is an all-day interview, not midnight. The log form needs a time for an interview.
- Calendar files give each session its own title, and a week can be downloaded. Decide's finalists are people in late stages or with a finished interview. Today merges feedback cards per candidate, interviewer emails are saved, and the loop planner skips US public holidays.

### Paste and connect
- Pasting a resume reads every role with its dates, and a note reads name, title and company. The check page says what wasn't found.
- The Connect AI page asks which app you use, says when an app isn't installed, keeps technical details in a fold, and gives Claude Code its one-line connect command.

### Keyboard, screen reader and first-time use
- The month calendar links only days that have interviews, so Tab skips the empty days.
- Choosing Rejected or Withdrawn on the board and tabbing away now says "Not moved yet", shows a dashed menu, and puts the keyboard in the reason box.
- After a save that reloads the page (Pin, Check again on Import, Settings), the keyboard lands on the notice so it is read out.
- Time zone boxes take any zone (type a city or zone name) and show its offset. Interview forms name their session. "All day" interviews can be logged on the web form, and the interview note has a 500 character limit.
- Input borders reach 3:1 in every theme, and forced colors and high contrast settings are handled. Esc closes a menu without closing the tour. No autofocus on Today or the add forms.
- The time zone bar shows only when it matters. Welcome, tour and Settings wording is shorter and plainer, Settings has a Jump to list, and the tour finish says in one line what happens to its saved searches.
- A bad email on the add or edit form is refused next to the field. Pasting a note that names someone fills the name.
- A skip link, current-page marks, named calendar days, table captions and sort state.
- Stage menus on the board wait for Enter, keep focus on the person you moved and announce the move.
- Better contrast on Rejected and Withdrawn columns.

### Working with AI apps
- Saved searches made by an AI app are on Changes and can be undone.
- Interview replies say which time zone was used and name any clash. An interview needs a start time.
- Held-for-review replies say exactly what went through and what waits.
- `talent install-skills` asks first (or takes `--yes`), and reinstalls say which files are new, updated or unchanged.
- Talent Search's own notes in a reply (time zone, clash, who isn't listed) are no longer mistaken for text that gives an AI orders, so logging an interview without a zone no longer holds later changes for review.
- An all-day interview says "all day" in the reply, not a made-up time.
- Interviews can carry a length (`duration` in minutes), used by the clash check and calendar files. Common other names for a setting are accepted (`note` for `summary`, a list of people for `add_to_pipeline`), and a wrong name gets "Did you mean".
- The loop planner keeps to the candidate's and each interviewer's daytime hours when their zones are known.
- Feedback from someone not listed on that session is saved, and the reply says they weren't listed.
- A move to Hired and a job close wait on Review even for Trusted apps. A Settings switch lets trusted apps do them.
- The Review page intro is a short list, held lines read as proper sentences, a saved search shows as "Saved search" on Changes, and saving a search is held like other changes in a session that read suspicious text.
- A calendar file for an unchanged interview is identical each time you download it, so calendar apps don't see a change.

### Leftovers from cycle 10
- On search results, the person's name is the link (the whole card still works as a click target), and LinkedIn and Website are their own links.
- Your AI app can't save an interview note, outcome note or reason longer than 500 characters (the same as the app). It is told the limit and how long its text was.
- Feedback from someone not listed on an interview is kept and shown as "from someone not on this interview", but their verdict no longer counts toward the session's result.
- The loop planner keeps to each interviewer's own working hours (9 to 5 on their clock unless you set others in Interview details), and a start like 2026-11-12T13:00 means not before 1 PM that day.
- On the Candidates table, the bulk bar has Add tag and Move stage (people not in the chosen job are skipped).

### Skills
- One **Install skills** button on the Connect AI page adds the skills to every AI app found on your computer, after one preview of the files. Apps not found say so and offer "Install anyway".
- **Get the Claude plugin** saves one `talent-search.plugin` file in your Downloads folder and shows it to you; in the Claude app it is one upload under Customize > Plugins > Add > Upload plugin. No more six zips. An Open Claude button starts the app.
- The repository is also a plugin marketplace: add `RogerFlores3113/talent-tracker` in the Claude app, or run `claude plugin marketplace add RogerFlores3113/talent-tracker` in Claude Code, to get updates.
- `talent install-skills` with no options installs into every app it finds (after listing the files); `--plugin [PATH]` writes the plugin file, and replaces `--zip`.
- Connect page: long commands wrap and the three app cards stack on mid-size screens, the first-run tour no longer opens over the install buttons, and each app card lists connect and skills as steps 1 and 2.

### In-chat views
- One People view for searches, saved searches, one candidate and saved candidates: a summary card with links (profile in the app, LinkedIn, website and every saved link), skills, jobs with stage and days, recent activity and an "Ask Claude" button.
- Flip through several people with the arrows (or Left, Right, Home and End), or jump from a strip of initials. "Show as table" opens a sortable table with filter, select all, shift-click ranges, "Ask Claude about these", "Open in Talent Search" and "Copy emails". Full screen where your chat app offers it.
- "Load next 50" pages through long searches, and "Open in Talent Search" carries the search and filters. The Candidates page also opens a list of exact people with `?ids=1,2,3`, ticked.
- Clicking a person in the pipeline, finalists and saved views opens their card in place. If your chat app won't open a link, the address is shown with a Copy button.
- Better keyboard and screen reader support, stronger contrast in dark mode, and a Refresh button on read-only views.
- Interviews in chat: a week agenda by day (Today, Tomorrow, then each weekday) with times in your time zone (and the candidate's when it differs), interviewers, a status for each session, clash warnings and each interviewer's load. Record feedback with a verdict, reschedule or cancel from the view: it sends your request to the chat, so the usual review rules apply.
- Feedback owed in chat: who owes feedback, grouped by interviewer, with days overdue, a Copy nudge button that copies a short reminder in your voice, and Log feedback or Mark chased requests.
- Duplicates in chat: each possible pair as two side-by-side cards with the matching email, LinkedIn, name or company marked. Merging and "not the same person" still happen in Talent Search, never from the view.
- add_interview_feedback with a wrong interview id now says to use list_interviews to find the right one, and takes lean yes and lean no (also written lean_yes, lean_no).
### Other links
- Other links on a person: a list of GitHub, portfolio, Scholar, X and other web links you can add on the candidate form, see on their page, import and export in CSV (Other links column), and that your AI app can add (never remove) with save_candidate, update_candidate, save_candidates and propose_candidates. Merging two people combines their links.


## [0.1.1] - 2026-11-09

### Search and candidates
- Search syntax: OR groups, exclusions and field prefixes, with a note on how your search was read. More filters, and saved searches you can color.
- Color tags on the Candidates table, each person's page, CSV exports and the AI app.
- A duplicate finder with bulk actions, better paste-a-profile parsing, and CSV export aliases.
- Imports never skip an email column, re-imports only move forward, and every email a person has counts. Tags like `c#` and `f#` stay whole.

### Interviews
- A month calendar, with the week view still available, and calendar (`.ics`) downloads.

### Your AI app
- Talent Search skills for Claude Code, Codex and Claude Desktop, installed from inside the app.
- Guards against instructions hidden in profiles and pages: warnings in the app, and the AI app can never delete anything.
- A Settings switch that tells the AI app to stay off sites whose terms forbid scraping, with a lower hourly cap.
- The AI app never adds an email to a person already saved, and says which ones it left out. It refuses more than 60 years of experience.

### Getting started
- A first-run guided tour with demo data that you can skip or replay.

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
