---
description: What follow-ups are due in Talent Search (candidates, interviewer feedback, client updates), with drafts ready to send.
argument-hint: "[job id or title; empty for all jobs]"
---

Show me the follow-ups due in Talent Search for: $ARGUMENTS (all jobs if nothing is given)

1. Candidate follow-ups: `list_followups` (pass the job id if one was named, and `days_ahead` 2 to include the next two days).
2. Interviewer feedback still owed: `list_feedback_owed`.
3. Client updates due: `clients_due`.

Group them by who I need to write to, most overdue first, each with the text to send. Do not send anything. After I tell you a message went out, log it on the person's timeline with `log_activity`.
