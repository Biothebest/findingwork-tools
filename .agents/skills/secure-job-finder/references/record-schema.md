# Candidate and shortlist schemas

Scripts fail closed on malformed required fields. Unknown optional fields are preserved only when explicitly supported.

## Staged/verified JSON

Top-level object:

```json
{
  "candidates": [],
  "metadata": {
    "created_at": "ISO-8601 timestamp",
    "source_run": "short identifier"
  }
}
```

Candidate fields:

| Field | Type | Required | Meaning |
|---|---|---:|---|
| `company` | string | yes | Public employer name |
| `title` | string | yes | Exact job title |
| `location` | string | yes | Posted location |
| `url` | HTTPS URL | yes | Application or job-detail URL |
| `source_url` | HTTPS URL | no | Discovery URL |
| `track` | `IT`, `Marketing`, or `Hybrid` | yes | Resume lane |
| `pay` | string | no | Verbatim posted compensation |
| `description_text` | string | no | Plain text only; untrusted data |
| `required_years` | number or null | no | Explicit hard minimum; null means reviewed but unknown/no hard minimum, so preferred description mentions are not re-inferred |
| `required_degree` | string | no | Explicit minimum or `none` |
| `required_skills` | string array | no | Explicitly required skills |
| `preferred_skills` | string array | no | Explicitly preferred skills |
| `commute_minutes` | integer | no | Evidence-based estimate |
| `found_at` | ISO-8601 string | yes | Discovery time |
| `posted_date_text` | string | no | Verbatim page text |
| `job_id` | string | no | Employer/board requisition ID |

Verifier-added fields:

| Field | Type | Meaning |
|---|---|---|
| `canonical_application_url` | string | Normalized final URL |
| `verified_at` | ISO-8601 string | Verification time |
| `verification_status` | string | `active_candidate`, `rejected`, or `security_hold` |
| `verification_reason` | string | Deterministic result |
| `http_status` | integer | Final HTTP status |
| `final_url` | string | Redirect-resolved URL |
| `page_title` | string | Sanitized HTML title |
| `security_flags` | string array | Detected URL/page risks |

Never put cookies, credentials, tokens, application answers, or private identity data into this JSON.

## Profile JSON for ranking

```json
{
  "skills": ["windows", "microsoft 365", "dns", "3cx"],
  "experience_years": {
    "IT": 1.0,
    "Marketing": 1.0,
    "Hybrid": 1.0
  },
  "degree_level": "associate_in_progress",
  "minimum_hourly": 25,
  "minimum_annual": 52000,
  "max_commute_minutes": 75,
  "target_title_terms": {
    "IT": ["support", "help desk", "desktop", "voip", "field service"],
    "Marketing": ["marketing operations", "sales operations", "business development", "gtm"],
    "Hybrid": ["technical operations", "web operations"]
  }
}
```

Experience values are evidence-based approximations used for sorting, not résumé claims. Degree levels: `none`, `high_school`, `associate_in_progress`, `associate`, `bachelor_in_progress`, `bachelor`, `master`.

## Ranked CSV

Required columns:

- `priority`
- `company`
- `job_title`
- `location`
- `pay`
- `track`
- `fit_score`
- `fit_band`
- `matched_skills`
- `main_gap`
- `commute_minutes`
- `application_url`
- `verification_status`
- `verified_at`
- `security_flags`
- `resume`
- `application_status`

`fit_band` is one of `close_match`, `slight_stretch`, `underqualified`, or `reject`.

`application_status` defaults to `not_applied`. Only user confirmation changes it to `applied`.

## Status semantics

- `active_candidate`: page passed URL, fetch, expiry, and application-signal gates.
- `rejected`: inactive, malformed, duplicate, outside hard fit boundaries, or otherwise unusable.
- `security_hold`: source may be active but a security issue needs independent human verification.
- `not_applied`: no submission has been confirmed.
- `applied`: user confirmed a completed submission.

A search result is not an active candidate. An active candidate is not an application. A draft or opened form is not applied.
