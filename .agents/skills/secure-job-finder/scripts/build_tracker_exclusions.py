#!/usr/bin/env python3
"""Build deterministic job-search exclusion sets from the canonical tracker."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter
from pathlib import Path

from reconcile_gmail_confirmations import canonicalize_url, job_identifier, normalize_text

ACTIVE_STATUSES = {
    "applied",
    "awaiting_manual_captcha",
    "awaiting_user_eligibility",
    "blocked_security_check",
    "queued",
    "active_candidate",
}


def identity(row: dict[str, str]) -> str:
    return "|".join(
        normalize_text(row.get(field, ""))
        for field in ("company", "job_title", "location")
    )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tracker", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    args = parser.parse_args()

    with args.tracker.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise SystemExit("tracker has no header")
        rows = list(reader)
    required = {"campaign_id", "company", "job_title", "location", "job_url", "application_status"}
    missing = required - set(reader.fieldnames)
    if missing:
        raise SystemExit(f"tracker is missing columns: {sorted(missing)}")

    all_urls: set[str] = set()
    active_urls: set[str] = set()
    active_job_ids: set[str] = set()
    active_identities: set[str] = set()
    applied_urls: set[str] = set()
    applied_job_ids: set[str] = set()
    applied_identities: set[str] = set()
    raw_active_identities: list[str] = []

    for row in rows:
        url = canonicalize_url(row.get("job_url", ""))
        job_id = job_identifier(url)
        row_identity = identity(row)
        status = row.get("application_status", "").strip()
        if url:
            all_urls.add(url)
        if status in ACTIVE_STATUSES:
            if url:
                active_urls.add(url)
            if job_id:
                active_job_ids.add(job_id)
            if row_identity:
                active_identities.add(row_identity)
                raw_active_identities.append(row_identity)
        if status == "applied":
            if url:
                applied_urls.add(url)
            if job_id:
                applied_job_ids.add(job_id)
            if row_identity:
                applied_identities.add(row_identity)

    duplicate_active_identities = sorted(
        value for value, count in Counter(raw_active_identities).items() if count > 1
    )
    report = {
        "tracker_rows": len(rows),
        "status_counts": dict(sorted(Counter(row.get("application_status", "").strip() for row in rows).items())),
        "all_urls": sorted(all_urls),
        "active_urls": sorted(active_urls),
        "active_job_ids": sorted(active_job_ids),
        "active_identities": sorted(active_identities),
        "applied_urls": sorted(applied_urls),
        "applied_job_ids": sorted(applied_job_ids),
        "applied_identities": sorted(applied_identities),
        "duplicate_active_identities": duplicate_active_identities,
    }
    args.output.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({
        "tracker_rows": report["tracker_rows"],
        "active_urls": len(active_urls),
        "applied_urls": len(applied_urls),
        "duplicate_active_identities": len(duplicate_active_identities),
    }, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
