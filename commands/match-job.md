---
description: Rank the people already saved in Talent Search against one job and offer to add the best to its pipeline.
argument-hint: "<job id or job title>"
---

Match the people I already have in Talent Search to this job: $ARGUMENTS

1. Find the job with `list_jobs`. If nothing was given above, or it is unclear which job, ask first.
2. Call `search_candidates` with the job's title, skills and location (not the job id, which only finds people already in the job) to get the best matches. Leave out anyone already in this job's pipeline (`job_pipeline` shows them).
3. Show the top 5 to 10, best first, with one line each on why they fit and what is missing. If two or more are close, show them side by side with `compare_candidates`.
4. Ask which ones to add. Only after I say which, add them with `add_to_pipeline`.

Do not save, change or move anyone before I answer.
