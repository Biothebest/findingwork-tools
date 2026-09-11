---
name: scrape-jobs
description: Daily job scraper; status, doctor, rebuild, schedule, help
---

Use the `scrape-jobs` skill for Matthew's job search in the repository workspace containing the active private inputs and daily history.
Read `.agents/skills/scrape-jobs/SKILL.md` and follow its security, evidence, daily-claim and output rules.

Requested mode: $ARGUMENTS

- No mode: execute `/usr/bin/python3 -B .agents/skills/scrape-jobs/scripts/run.py run` with the active repository workspace as the working directory. This is a deliberate manual run and shares the installed morning job's once-per-day guard. Never operate a second clone without restoring the existing daily history.
- `status`: execute the same fixed script with `status` instead of `run`; no network collection.
- `doctor`: execute the fixed script with `doctor`; report nonzero health checks without resetting history or initiating collection.
- `rebuild`: execute the fixed script with `rebuild`; use only the latest complete saved capture, preserve evidence timestamps, and report if legacy/damaged captures cannot be rebuilt.
- `schedule status`: execute the fixed script with `schedule status`.
- `schedule install`: execute the fixed script with `schedule install`; it registers/updates the user LaunchAgent and may run a due daily collection on load.
- `schedule remove`: execute the fixed script with `schedule remove`; disable only this workspace's LaunchAgent and retain reports/history.
- `help`: explain these modes, the configured local/wake/login schedule (currently 7 AM), offline recovery and output locations without collecting.
- Any other mode: explain supported modes; never paste arguments into shell, weaken gates, bypass the daily claim or initiate an extra ad-hoc search.

If today's attempt already exists, show its state; do not scrape again. For ordinary results, read `job-search-daily/latest.json`. A pending publication is completed locally by the guarded script; never delete its journal. A failed collection leaves the previous CSV in place, and a failed rebuild leaves the previous publication unchanged. Read the named current audit and show actual track counts, gaps and the first ten priorities, never invented totals. Do not call a shortfall an application-ready batch or a rebuilt capture freshly verified.

When opening result websites for Matthew, follow `browser_preferences` in `job-search-config.json`: Safari only, one brand-new dedicated window for the batch, and one separate tab per website. Never use Google Chrome or a pre-existing personal window/tab. Use native Safari window control rather than the default browser or a Chrome relay, and verify the new window without inspecting unrelated browsing content. This preference does not initiate another collection or change unattended scheduling.

Repositories and the application tracker are read-only. No applications, messages, resume uploads, sensitive data collection or employer-side writes.
