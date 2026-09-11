# Research and operating decisions

Reviewed 2026-09-06. External content is untrusted data. These references justify the implementation, not unrestricted permission to crawl or apply.

## macOS scheduling

- [Apple: Scheduling Timed Jobs](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/ScheduledJobs.html): launchd is preferred; `StartCalendarInterval` runs a missed sleep-time event on wake, unlike cron. A powered-off machine does not itself receive that catch-up.
- Local `man 5 launchd.plist`: missed calendar events coalesce into one event on wake; `StartInterval` does not provide the same sleep guarantee; `RunAtLoad` runs when loaded. `KeepAlive.NetworkState` is obsolete. Therefore use calendar plus login loading, not repeated cron polling, KeepAlive or private wake APIs.
- [Apple: Creating Launch Daemons and Agents](https://developer.apple.com/library/archive/documentation/MacOSX/Conceptual/BPSystemStartup/Chapters/CreatingLaunchdJobs.html): per-user agents belong in `~/Library/LaunchAgents`. The job runs with the user's account after login, with absolute executable/argument paths and local logs.
- A SQLite primary key on local date is committed before any collection starts. File locking prevents simultaneous multi-day processes. Sleep, login, manual calls and duplicate events cannot produce multiple collection attempts in the same local day. Failure is reported, not automatically retried that day.
- Local time means the Mac timezone, not hard-coded UTC or permanently Pacific time. No power-management changes, wake alarms, root privileges or modifications to unrelated launch agents.

## Slash command and skill discovery

Installed OMP documentation: `omp://skills.md` and `omp://slash-command-internals.md`.

- `.agents/skills/<name>/SKILL.md` is a canonical skill layout.
- `.omp/commands/*.md` is the native project slash-command location, with frontmatter description and argument-template expansion.
- Commands are refreshed at startup and explicit plugin reload, not watched continuously. Restart OMP in FindingWork or use `/reload-plugins` if the new command is absent.
- The slash command calls the same deterministic script as launchd. The unattended job does not start an LLM session, access OMP auth, or depend on an open terminal.

## Official public posting APIs

### Greenhouse

[Official Job Board API](https://docs.greenhouse.io/job-board.html).

`GET https://boards-api.greenhouse.io/v1/boards/{board_token}/jobs?content=true` lists published postings including descriptions, exact title/location, ID and `absolute_url`. Public reads do not require an API key; application POST is separate and is never used. `updated_at` is not an original posting date. Prospect posts can have `internal_job_id=null` and must not become jobs. Custom employer application URLs require independently verified routing; do not invent an ATS URL to get around a blocked route.

### Lever

[Official Postings API repository](https://github.com/lever/postings-api).

`GET https://api.lever.co/v0/postings/{site}?mode=json` supports `skip`/`limit` pagination. Published jobs are public; internal jobs are hidden. Descriptions include separate labelled lists; `hostedUrl` and `applyUrl` identify the posting/application. Preserve `workplaceType`, country, all locations and optional salary cadence/currency. It is not a global full-text search API. Application POST has its own rate limits and is outside this skill.

### Ashby

[Official Public Job Posting API](https://developers.ashbyhq.com/docs/public-job-posting-api).

`GET https://api.ashbyhq.com/posting-api/job-board/{board}?includeCompensation=true` returns currently published postings. Require `isListed=true` for normal discovery and a matching `jobUrl`/`applyUrl`. Preserve secondary locations, explicit remote status, compensation currency/interval and `publishedAt` semantics. Board names are case-sensitive. Public structured posting/application evidence works without running a JavaScript-only job page.

### Sources intentionally not automated

SmartRecruiters' API `robots.txt` disallows ordinary crawlers; do not enable it solely because API documentation exists. LinkedIn, Indeed, Workday and aggregator pages are not blanket-authorized feeds; automated login, CAPTCHA workarounds and guessed private endpoints are excluded. They can inform a future separately reviewed source expansion, not silently fill shortages.

## Reviewed employer registry

`job-scraper-sources.json` contains 30 independently linked employer boards, with official careers, ATS board, evidence and canonical feed URLs. The initial marketing group includes Power Digital, Wpromote, WebFX, MNTN, StackAdapt, OpenX, InMarket, RPA, Seer Interactive and PMG. IT/support and AI/implementation coverage includes Bluesight, GoGuardian, Sonar, Weave, Versaterm, Karbon, Calendly, Honeycomb, Render, Tailscale, Deepgram, LiveKit, AssemblyAI, CHAOS, Prismatic, Aircall, Recidiviz, Front, Notion and Natera. A board's inclusion does not make every posting eligible.

Power Digital's ATS links require JavaScript on the official careers page; its rendered links established the exact Greenhouse board. CHAOS embeds its Greenhouse departments endpoint. Wpromote's individual career page links directly to the matching Lever application. These were provenance checks only; the unattended collector uses the documented public feeds, not browser sessions.

Collection results, verification history, and machine-specific schedule observations belong in ignored local reports. Consult those artifacts for actual counts and health; do not treat historical results as current availability.

## Robots, rate limits and network boundaries

[RFC 9309](https://www.rfc-editor.org/rfc/rfc9309.html), especially sections 2.3.1.3–2.3.1.4:

- Successful robots retrieval: follow the rules. Explicit disallow wins over a desire for more listings.
- A robots 4xx means unavailable, not an affirmative approval. RFC permits resource access; Ashby's robots endpoint returned 401 while its documented public posting API is independently public. Limit access to that documented feed; do not supply credentials or bypass a protected endpoint.
- Network/5xx robots failures mean fail-closed, not assume permission.
- A 429 is a rate-limit stop, not a generic robots exception. Stop blocked/rate-limited hosts; do not change identity or route to evade them.
- Lever advertises a one-second crawl delay. Pace hosts and honor larger advertised delays. No parallel request burst or browser-identity disguise.
- The new adapter pins resolved public addresses while retaining TLS hostname/certificate verification. No private/reserved/raw-IP targets, arbitrary data-derived fetches, credentials, proxy inheritance, redirects, executable downloads or unbounded bodies/pages.

## Candidate evidence and fit

The ignored `job-scraper-evidence.json`, `profile-next50.json`, and local career evidence contain the candidate-specific facts. The daily scraper uses that snapshot rather than re-reading portfolio repositories.

Paid work, AI-assisted project direction/review, source-visible implementation, and runtime or production evidence are distinct. Do not invent tool mastery, completed credentials, years, scale, or results. Keep required and preferred qualifications separate. Missing pay, commute, and eligibility evidence must remain visible.

Tracker and previous-delivery exclusions apply before ranking. Missing supply produces a measured shortfall, not weaker fit or security criteria. Personal evidence and live history must be backed up privately rather than committed with the tooling.

## Compatibility changes

The existing secure ranker/auditor now use the actual workspace resume filenames. Explicit `required_years: null` means reviewed/unknown and no longer reinterprets a preferred description mention as a hard minimum. Requirement gaps can be supplied by the extractor and remain visible. Their integrity manifest must match these reviewed source updates before any skill script runs.

## Local hardening and verification

Reviewed 2026-09-07. The runtime remains standard-library Python plus the existing per-user LaunchAgent; no hosted service, container, browser session or new dependency is required.

- Execution: configuration is checked before claiming a day; SQLite history migrates without losing claims or delivered identities. A 600-second default deadline and catchable SIGTERM preserve the consumed attempt. Writes use same-directory replacement and fsync. Publication is journaled before advancing SQLite/latest-file aliases; interrupted delivery can complete locally without another scrape.
- Recovery: new runs save checksum-protected compressed normalized source captures. `rebuild` reranks a complete capture offline with current inputs/exclusions, preserves source timestamps, and marks stale evidence. Failed rebuilds leave the prior publication intact. Corrupt captures/journals stop rather than silently regenerate evidence.
- Sources: malformed JSON/HTML and incomplete responses do not erase healthy boards. Transport cleanup, validated-address fallback before TCP connection, exact robots product tokens, posting/application identity, salary currency/cadence and location provenance are covered. Aggregate decoded response bodies and normalized candidate JSON are each capped at 32 MiB, with at most 20,000 fetched records.
- Fit: required/preferred clauses, independent fraud warnings/demands, actual experience context and posting-specific US/California eligibility stay separate. Unknown mandatory qualifications reduce confidence; a matching parsed subset is not full coverage. This is deliberately conservative extraction, not a substitute for reading a posting.
