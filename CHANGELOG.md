# Changelog

All notable changes to Talent Search. Newest first.

## [Unreleased]

## [0.1.3] - 2026-11-10

### Candidates
- Click a skill chip to light it up in its own color (first pick red, then yellow, green, blue, purple, orange; a color stays with its skill) on every row where it shows, and move people who have it to the top. People with more of your picked skills come first, and when it's a tie the skill you picked first wins. Nobody is hidden. Click it again, or press the x in the new Highlighted skills bar, to take it off. Saved searches keep the highlights.
- Skill highlights are easier to follow: keyboard focus stays on the chip you picked, a status line says what changed, the highlighted skills bar shows each pick's number, color and how many people have it, and every row shows "2 of 2" matches. The color columns are now called Label and Label color so they are not mixed up with highlight colors.
- "Use all N people in these results" now follows the order on screen for Compare, exports and Share, so Compare takes the top six you see. The bulk bar groups Compare, Export and Share under "Send out", and Compare says "Tick two to six people to compare."
- Importing keeps names and titles that start with = + - or @ as plain text, trims emoji from the end of skills, and leaves out skills with no letters (listed in the import warnings).

### Tour
- The Saved searches step lights up a real line now. With nothing saved yet it reads "Saved searches: none yet" and says how to save one, instead of an empty bar.
- A candidate's page step frames the whole summary at the top of the page (name, skills, contact line, about and details), not just the name.
- Steps whose area is taller than the window (Skills, Interviews, Today) open at the top of that area, so you see its heading and first button. The Interviews step now includes the Month and Week switch.

### Today
- The Add a to-do row wraps on narrower windows instead of pushing its button out of the panel.

### Spreadsheets
- Export xlsx next to Export CSV on the Candidates table: an Excel file with exactly the people the table shows, in the same order (your search, filters, color and highlighted skills), with email, phone, links, all skills and years of experience. Also in the bulk bar for the people you tick, in Settings for everyone, and on a client's page for the pipeline. The CSV downloads stay.
- Share as xlsx: tick people, press Share as xlsx and pick what goes in a spreadsheet for a hiring manager. Email, phone and your private notes are off unless you tick them. You can add the job (it adds each person's stage) and a one-line note at the top. The file never has colors, review marks or links to this app.
- In a chat, the People table has an Export xlsx button, and Claude can make the same shortlist file in your Downloads folder with no email, phone or notes. If it needs those, it gives you a link to the Share page instead.

### Compare people
- Tick two to six people on Candidates and press Compare to see them side by side: title, location, years of experience, skills, jobs and stages, source, last contacted, links and about. An "At a glance" list says in plain words what they share and where they differ ("Only Maya has rust and payments", "Noah has the most experience"). Skills everyone has are marked shared, skills only one person has are marked only them, and highlighted skills keep their colors. Remove anyone from the comparison, or go back to the list with the same people ticked.
- In Claude, the People table has a Compare button for two to six selected people, and Claude can compare people for you (it only reads).

### Spreadsheets, ready to forward
- Every xlsx prints on landscape pages one page wide, repeats its header row on each page, and has narrow margins. Skills and About wrap in wide columns.
- The file for a hiring manager leaves out columns nobody has anything in, keeps people in the order you picked them (or furthest along first when you pick a job), and opens with your note in bold across the top. With a job picked, people not in it have no stage, and people marked rejected stay out unless you tick "Include people marked rejected".
- Highlighted skills come with you: Export xlsx and Share add a Matched skills column after Name ("python, data engineer (2 of 2)"), filled in the color of the first skill that matches. The About sheet lists filters in words ("Location: Boston", "Highlighted skills: python (red)").
- Dates are real dates (yyyy-mm-dd), years of experience are rounded to half years, and the empty Color column is now Label color and only appears when someone has one.
- The Share page has three notes choices (No notes, Latest note only, All notes and timeline), reminds you that About and work history go out as written, and previews the file name, columns and first names. It says when the file is downloading. Files are named shortlist-<job or note>-date.xlsx.
- Exports and bulk exports that hit the 5,000 cap say "first 5,000 of N" on the About sheet and beside the Export buttons. Settings says "Export all N people as xlsx".
- Export CSV on the Candidates table now follows your filters and order, like Export xlsx. The whole-list CSV stays in Settings.
- A client's pipeline xlsx has a title line ("Acme pipeline, as of 2026-11-10"), real dates, and the next interview as a real date and time with its time zone named in the header.

### Compare and export polish
- Compare now sorts skills three ways per person: Shared (everyone), Some of them (with a count, like "python, 3 of 6") and Only them. At a glance says who has the partly shared skills ("Emily, Rafael and Priya have python"), with full names when two people share a first name. Claude's compare reply says the same.
- With five or six people, names and skill chips no longer break in the middle of a word: each column is at least 12rem wide and the table scrolls sideways, with the name row staying put. Remove is a small x button with a clear label. The same goes for the in-chat compare view.
- Rows nobody has anything for (years, links, about, found via) are left out. When only some people have work dates, the others say "No work dates saved" and a note says how to add them.
- Highlighted skills name their colors for screen readers and show their number, and each column says "Has 2 of 2 highlighted skills".
- A Hiring manager view (link on the compare page, also what Print gives) shows names, title, company, location, years, skills, stage in a job, public links and About, and leaves out your own notes on contact, source, dates, labels and app links.
- In Claude, compare takes the first six people if you give more and says who was left out, ends with a link to Open in Talent Search, and the view has Share as xlsx, Export xlsx and a Remove button for each person. The People table says "Export 12 shown" or "Export 2 ticked", reports what it saved, and has Share as xlsx beside Compare.
- The messages Claude gets back from the xlsx export are plain and clear, a Read only app is given the Share page link, and people not found are listed first.
- Candidates table: Compare, Share and both exports keep the order of the people you ticked, and Export as CSV lists the same people in the same order as the xlsx, with a "Matched skills" column when skills are highlighted.
- Candidates table: the highlight message says "1 person has it" and, after a second pick, how the order works. Removing a pick moves focus to the next pick, there is a "Skip to highlighted skills" link, and the table caption names the highlight order.
- Candidates xlsx exports leave out columns that are empty for everyone, like the Share file. Importing skips skills that start with @ = + - and lists them in the warnings.

### In-chat compare and export
- Compare in chat now reads "Skills: Shared: sql. Some of them: python (3 of 6 have it). Only them: django." and the Compare view says "python, 3 of 6 have it". Comparing one person says "Compare takes two to six people. Name or tick at least two."
- When an AI app is set to read only, the export refusal now speaks plainly to you and links the Share page. Exporting the People table makes candidates-DATE.xlsx instead of a shortlist file, and a new Load all and export button gets everyone found (up to 200) first. People table initials use letters only.

### Hiring manager view and share form
- The Hiring manager view is safer to send: it shows only stages still in play (nothing for Rejected or Withdrawn), says plainly what it hides and that About text is shown as written, and has a Hide About button that stays in its links. It also has a Side by side heading.
- The Share form warns when you include notes ("may hold pay, other interviews or private remarks"), and its preview now matches the file exactly: the people and count with rejected people left out (or added back when you tick the box) and the Stage column only when someone is in the job. The note box no longer suggests a job name when no job is picked.
- The shared file is named shortlist-<job>-date.xlsx, or shortlist-date.xlsx with no job. What you type in the note is never used in the name.
- Compare: after Remove (x), keyboard focus lands on the next Remove button; with four or more people the page uses the full window width and says "Scroll sideways to see all N people." when the table is wider than the window; At a glance says "No contact logged with anyone here yet." instead of "Nobody here has been contacted yet."
- The in-chat cards and tables now start in Claude. They were asking the chat app to begin in a shape Claude refuses, so only text showed. The Connect AI page and README now say where cards show.

### Privacy, files and web polish
- Share form: a red line says notes can hold pay talk and private remarks and go into the file as written. When notes are on, the preview shows each person's note (first 80 characters, as it will appear) with "Leave this note out" and "Leave this person out" ticks that the file honors. It says so when nobody has the highlighted skills, starts on the Candidates job filter, and names the company in the file name (shortlist-company-job-date.xlsx). The title row says "as of" the date.
- Hiring manager view: tick "Hide About" for one person at a time, see "Not in play" instead of a blank stage, read a line when someone was left out, and a proper heading, page title and landmarks. Focus starts on the heading.
- The client status text on a job leaves out Rejected, Withdrawn and other ended stages by default, like Sourced and Contacted, and a "Names that will be sent" line lists exactly who is in it.
- Export CSV uses plain dates (2026-11-10) and leaves out private notes and the timeline unless you tick "Include notes". Dates in the table spreadsheet are real date cells. Exports say "Your spreadsheet is downloading" and keep focus on the button.
- Candidates: a live "3 people ticked" count, the bulk actions stay folded until someone is ticked, and Compare, Export or Share with nobody ticked says so beside the buttons without reloading the page. The job Board lays stages out side by side again, the "I hire for my own company" switch has its own space on Jobs, and /today goes to the front page.
### Volume work
- The job table has a tick box on every row and one in the header. Tick people and press "Move N ticked to [stage]" to move them all at once, with a status line that says how many moved and one Undo (also on Changes). The table also has a Skills column with the same clickable highlight chips as Candidates, and picks sort each stage the same way.
- Candidates: choose Show 100, 250 or All. The choice stays in the address, so Use all and exports follow it. All stops at the same 5,000 limit as exports, and says so.
- Possible duplicates now shows one card per group ("Kim Haddad, 6 profiles") instead of a long list of pairs, 50 groups at a time with no 2,000 pair limit. Pick which profile to keep with Keep this one, untick anyone who is a different person, and the rest are merged into the kept one after one backup. When the name matches but city, title and phone all differ, the card says "Same name, maybe different people" and starts unticked. The pair list is still there under Show as pairs.
- Adding people to a job from Candidates now links to the job ("Open Data Lead") in the confirmation.
- Import: phone values with fewer than 4 digits (like "-555" or "'-") are left out with a warning. The import page offers a CSV download of the skipped rows and the rows saved with a blank cell, with the reason for each, kept on this computer. Counts say rows throughout, and a note says when a row runs over several lines.

### Job-title highlights, skill names and the keyboard path
- A job title is now a button-like chip in the Candidates table and under the name on a profile. Click it to highlight everyone with that current title, at any level (Sr. Data Engineer and Data Engineer II match a Data Engineer pick). It counts as one pick, like a skill, in the order, in "2 of 2", in Compare and in the Matched picks column of the spreadsheets.
- Settings has a new Skill names section. Look-alike names (react and react.js, engineer and engineering, js and javascript) are suggested with how many people have each and how many have both. You choose Treat as one (and which name to keep) or Not the same; Split undoes it. Nothing is merged for you and saved skills on profiles never change. Search, the skill filter, highlights, Compare and spreadsheets then count the names as one. When you pick a skill that has a look-alike, a line under the highlight bar offers the same choice, and a CSV import tells you when new look-alikes came in.
- Your AI app can list look-alike skill names (list_skill_lookalikes) but cannot merge them.
- Keyboard: a Skip to skill highlights link at the top of Candidates, and a Highlight a skill box where you type a skill or "title: ..." and press Enter. Clear now reads "Clear all 3 highlights" and puts focus in that box. Skill chips on a profile add to the highlights you came with. The skill list is sorted ignoring case and shows each skill once.

### In-chat views (cycle 14)
- Claude can open People and Compare already tiered: search_candidates, run_saved_search and compare_candidates take a highlight list (skills in your order, a job title as "title:Data Engineer"). Picks you make in the view are kept when you press Refresh or Back to the list.
- The in-chat table fits a chat window: Name, Title, Skills, then Job and Stage, with Company, Location, Updated, Added and Last contacted behind a Columns button. Show as table no longer jumps to full screen (Full screen is its own button), skill chips work in card view too, each row says "Top match: python, red. 2 of 2", the pick bar stays in view, and picked chips are named for screen readers with their color and number.
- In-chat Compare keeps the order you chose, names anyone left out at the top, colors your picks, fits three people without sideways scrolling, and has a Hiring manager view button.
- In-chat exports are clearer: "Export for hiring manager" (no contact details or notes) and "Export for me" (opens the Share page with contact and notes ticked). The saved file's full path is shown with a Copy path button, and export_candidates_xlsx with only a job_id exports that job's people.
- Fixes: the sticky Name cell takes the row tint, the table no longer jumps when you click a chip or export, and Today's list is no longer clipped at the bottom. The pipeline star now has a plain explanation.
- In chat, highlights now decide who loads. With a highlight list, the whole result is sorted by your picks (most picks first, then the earlier pick) before the first people are shown, so Load next carries on in that order. The pick bar says how many are in each group ("12 have both, 40 have one"). Searches can now show up to 100 people at a time and say so when you ask for more.
- The People card shows the first two lines of what the person wrote about themselves, with More, and the current title is a pick button on the card (in the person's own capitals). A title pick no longer colors skills, and labels keep acronyms ("title: QA").
- The People table: clicking a column header sorts every loaded row (the caption says highlights are set aside, with Back to highlight order), columns with nothing in them start hidden (still under Columns), Match shows only when it adds something and uses your pick colors, and the Compare button says "Compare takes 2 to 6" when the number is wrong. Export says what it holds when not everyone is loaded ("Export 10 loaded of 29"). Refresh keeps the table, ticks, sort and picks.
- In-chat Compare says how many picks each person has, reads the pick list in words, shows "Scroll sideways to see all 6", and says "saved as ..." on a skill shown under the name you kept. Possible duplicates in chat are one card per person group ("Dana Dup, 3 profiles"), counted like the app.
- Fixes: no false "looks like instructions" caution when you highlight, the job pipeline header says how many are in the pipeline and how many count in the status update, status_digest leaves out Rejected and the other ended stages like the job page, the saved search view uses its name, Export for me opens the Share page with the latest note only, and a search for react.js no longer lights js inside other words.
- In-chat views now also work in the ChatGPT desktop app. Each view has a versioned address (it changes whenever the view changes, so Claude and ChatGPT pick up new views after an update; older addresses still work), and a view that gets nothing from the chat app's usual channel draws itself from what ChatGPT hands it. The Connect AI page now says plainly where cards show (the Claude desktop app and the ChatGPT desktop app; Claude Code, the Codex command line and claude.ai in a browser show text) and has a short ChatGPT desktop section with the command to copy.

### Cycle 14, web fixes (F1)
- The hiring manager view now really leaves out anyone who is not in play: no column, nothing about them in At a glance, and a line that names who was left out and why.
- Share with no job picked now says "Sofia Example is marked rejected in Senior Backend" beside the person, with a ticked "Leave out" box so a rejected person does not go in the file unless you untick it.
- Settings, Skill names: after Treat as one, Split or Not the same, the result is read out and has focus. Three or more spellings (react, react.js, reactjs) are one row with one Treat as one, python and python3 are suggested, and once names are merged a look-alike shows as that group. "Not the same" pairs wait under Hidden suggestions with Suggest again, you can merge two names you type yourself, each pair's Name to keep says which pair it is for, and clicking a name selects it.
- Skill merges, splits and Not the same answers are on Changes with Undo (Suggest again for the last). The Matched picks column shows the name you picked or the kept name, never the old spelling, and Compare shows "Saved as ..." on a merged skill.
- Possible duplicates is safer at volume: groups start unticked unless everyone shares an email or LinkedIn address, nobody with a different first name or title is ticked, the button says "Merge 3 groups, 7 profiles", the kept person says Kept, "Keep all as separate people" is one click, and the count on Candidates counts groups.
- Importing a CSV leaves an email cell that is not an address blank and lists it with the other blank cells; the skipped and blank-cell download has one line per row and its count matches the page.
- Share: leaving a person or a note out is announced, note boxes are not in the page while No notes is chosen, and the page you reach from Export for me says it is a file for you, not one to forward, and starts on the latest note.
- Keyboard: after Show 100 / 250 / All focus goes to the result count; the Highlight box is named "Highlight a skill or title".
- Layout: the Show 100 / 250 / All caption no longer overlaps the buttons, the job page at 1400 pixels does not scroll sideways (the board scrolls in its own box), the job table's Move ticked bar stays in view, and a title in the job table is a pick chip like on Candidates.
- Selecting everything on Candidates (for example Show All, tick the header box, Export as xlsx) sends the search instead of thousands of ids, so it no longer stalls, and bulk actions accept up to 5,000 ticked people.

## [0.1.2] - 2026-11-09

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
- Four more in-chat views: Jobs (a card per job with people in each stage, last activity, next interview and a link to its pipeline), Saved searches (what each one looks for, how many are new, and Run shows the people right there), Follow-ups (grouped Overdue, Today, This week and Later, with the text to send) and Status and client updates (the update as the client sees it, a Copy update button that copies only that part, and your own notes kept apart below).
- list_jobs now answers in plain bulleted lines with a link per job instead of a block of JSON.
- Interviews in chat: a week agenda by day (Today, Tomorrow, then each weekday) with times in your time zone (and the candidate's when it differs), interviewers, a status for each session, clash warnings and each interviewer's load. Record feedback with a verdict, reschedule or cancel from the view: it sends your request to the chat, so the usual review rules apply.
- Feedback owed in chat: who owes feedback, grouped by interviewer, with days overdue, a Copy nudge button that copies a short reminder in your voice, and Log feedback or Mark chased requests.
- Duplicates in chat: each possible pair as two side-by-side cards with the matching email, LinkedIn, name or company marked. Merging and "not the same person" still happen in Talent Search, never from the view.
- add_interview_feedback with a wrong interview id now says to use list_interviews to find the right one, and takes lean yes and lean no (also written lean_yes, lean_no).
- The people table in full screen shows every column (Match, Last contacted, Added, Updated and Color when anyone has one) instead of cutting them off, and inline it wraps instead of scrolling sideways. A long Stage is shortened with the full text on hover.
- Pipeline board: each card shows the stage with days in it, how the interviews went (for example "Strong yes 1, Mixed 1"), a "Feedback owed" flag and the next interview. A "Move to..." menu asks Claude to move the person, and the header says how many are in each stage and matches the text reply.
- Finalists: a sortable comparison table (verdicts, sessions done and left, feedback owed, last interview, stage). Open a row to read each interviewer's own words. Advance and Reject ask Claude, and Reject asks you to confirm first.
- Today groups its list under headings with counts. Every person, job and interview is a real button, and Done, Snooze and the next step ask Claude.
- Shortlist: when someone matches a person you already saved, the card shows who they are, where they work, their jobs and stages, and opens their card. "Show as a table" lets you tick several people and keep or pass them. Every LinkedIn button names the person.
- Saved batch: each person shows their title, company, links and a "No contact info" badge, and when a name needs your answer the view and the text list the actual people it could be, with "Same person" and "Someone new" buttons that ask Claude.
### Other links
- Other links on a person: a list of GitHub, portfolio, Scholar, X and other web links you can add on the candidate form, see on their page, import and export in CSV (Other links column), and that your AI app can add (never remove) with save_candidate, update_candidate, save_candidates and propose_candidates. Merging two people combines their links.

### Text replies, search and import fixes
- Replies in Codex and Claude Code (which show only text) name each person and job with a link to open them in Talent Search the first time they appear, use short bulleted details instead of raw JSON, and say "you" instead of "the recruiter".
- Searching a role and a place, like "backend engineer Boston", reads both parts: people who match both come first, then people in Boston with part of the role, then the role elsewhere. Each result says which part matched, and the reply says what it understood ("Looking for backend engineer in Boston, MA: 1 exact, 3 close").
- Typing a search on the Candidates page keeps best match first, and you can click a column heading to sort the results by it instead.
- Importing a CSV with a Stage or Status column but no job column keeps it as a tag ("Stage: Offer") instead of dropping it, and columns like Years are kept as notes. The summary says so, and lists each person a row was folded into, with the reason and a link to them.
- plan_loop says which time zone its times are in (your saved one, else this computer's), and reads a start time with an offset (like 10:00 -08:00) correctly.
- A plain row in save_candidates no longer shows a false "looks like instructions" caution when the person has namesakes.
- Today: the time zone banner says which zone this browser is in and offers it in one click, there is a "Skip to today's list" link, and each Snooze and Done button names its item.
- The view test host (scripts/view_host.py) replaces the old frame each time you press Run.
- In-chat People table: Select none clears everyone (the counter says how many are hidden by the filter), new Job and Email columns, a "Has an email" toggle, Stage sorts in pipeline order, the table scrolls sideways with an edge shadow and a fixed Name column, and "Back to table" keeps your sort, filter, selection and place.
- In-chat views: when the chat app blocks copying, the view says so and shows the text selected in a box; Ask Claude with an empty box asks you to type first; picking a stage or pressing Advance asks "Move X to Y?" with Send and Cancel before anything is sent; rows say "Sent to Claude" after a button sends a message.
- In-chat views: a stopped request keeps what you were looking at, times show in your Settings time zone ("Times in PST"), Refresh on every list view, the card carousel can be focused with a hint for the arrow keys, flipping cards tells Claude who you are looking at, and controls the chat app can't support are hidden.
- In-chat Interviews shows one clash line per interviewer with the load strip on top, Finalists has Days in process and "Advance to Offer", saved people and the Shortlist show email, phone and website, the "needs your answer" card uses plain words, and Duplicates sides are labelled by person and email.
- In-chat views: screen reader and keyboard fixes (names on repeated buttons, headings read the person's name, one Tab stop in the avatar strip, focus after sending).
- Chat apps without MCP Apps no longer see the candidate_cards tool.
- Text replies match the screen: links in list_interviews, list_feedback_owed and job_pipeline, email, LinkedIn and website in search lines, a "Showing 10 of 13. Ask for the next 3." hint, and no double period after a snippet. A missing id says "Tell me which job (job_id). list_jobs shows them."
- Searching "backend engineer in Boston": singular and plural rank the same, people matching both come first, the close list is capped, and when nobody matches both it says "Nobody is a backend engineer in Boston. Closest: ..." with counts of who matches the role and the place and who matches part of it.
- Counts say "in the pipeline" everywhere; "in process" is only the client update's number, with a note when they differ. "Jobs without a client" replaces "No company".
- Connect: the Claude plugin comes first and alone for the Claude app, says what it adds and that the connection is still needed, shows the steps once with the full install command and a "Check it worked" line, and the Claude Code card no longer shows step 2 under a collapsed step 1. After Install skills, the "found" line refreshes. marketplace.json has a description. install-skills with no terminal and no --yes starts with "Nothing installed yet. Run again with --yes to install."
- Candidates: Stage and Job columns you can sort, Shift-click to tick a range, and a "N people from your chat." banner with "Show everyone" when you arrive with ?ids=.
- CSV import: "Years of experience" is named as a note in the preview; with both Stage and Status columns, Stage sets the stage and Status is kept as a tag; near stage names (Interview, Screened, Offered) land in the stages you have; Work history survives export then import.
- save_candidate and save_candidates accept linkedin, website, site and url, and the save_candidates text lists each person's email and website.
- Interview feedback: the verdict hints list all seven verdicts, list_interviews text carries interview ids and links, a double-booked interviewer gets one clash line, and plan_loop says when it skips an interviewer for working hours.
- The welcome tour: "Skip tour" ends it in one click with nothing left dimmed, it steps aside while you type in a box and never starts over one, and after it ends focus goes to the page heading.
- Time zone: while unset, /interviews and the loop planner say "Times are in UTC. Set your time zone in Settings."
- Today: verdict buttons and Reschedule name their person and interview in a labelled group, the time zone banner is not focused on first Tab, and "Skip to today's list" is the first Tab stop.


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
