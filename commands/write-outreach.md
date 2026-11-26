---
description: Draft a first message or follow-up to a candidate about a job, in Talent Search.
argument-hint: "<candidate name or id> [for job id or title]"
---

Draft outreach in Talent Search for: $ARGUMENTS

Follow the Talent Search playbook write-outreach (call `get_playbook` with `write-outreach` if you have not read it).

1. Find the person with `search_candidates` (or `get_candidate` with an id) and the job with `list_jobs`. If either is unclear, ask.
2. Read the person's record and the job so the message is specific to them. Use only facts from the record.
3. Write one short message I can paste, plus a subject line if it is an email.

Do not send it. After I say I sent it, log it with `log_activity`.
