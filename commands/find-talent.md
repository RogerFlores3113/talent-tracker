---
description: Find new people for an open job and put them on its Shortlist in Talent Search.
argument-hint: "[job id or role, e.g. senior backend engineer in Austin]"
---

Find new candidates in Talent Search for: $ARGUMENTS

Follow the Talent Search playbook source-for-a-role (call `get_playbook` with `source-for-a-role` if you have not read it).

1. Find the job with `list_jobs`. If nothing was given above, or it matches more than one job, ask which job first.
2. Check who is already saved with `search_candidates` (the role title, skills and location) and tell me who fits, so nobody is researched twice.
3. Call `list_prospects` so you skip people I already passed on.
4. Research new people from real pages only, each with a profile link and one line on why they fit.
5. Put them on the job's Shortlist with `propose_candidates`. Do not save anyone directly.

End with how many you proposed and remind me to keep or pass each one on the Shortlist in the app.
