---
description: A short status update on one job, one client or every open job in Talent Search, ready to paste.
argument-hint: "[job id, job title or client name; empty for all open jobs]"
---

Give me a pipeline review in Talent Search for: $ARGUMENTS

Follow the Talent Search playbook pipeline-review (call `get_playbook` with `pipeline-review` if you have not read it).

- One job named: use `job_pipeline` for it.
- A client or company named: use `client_report` for that client.
- Nothing given above: use `status_digest` for every open job.

Lead with the short answer (what is moving, what is stuck, what needs me), then the paste-ready update. Keep candidate ids out of anything meant for a client or hiring manager. Do not change anything.
