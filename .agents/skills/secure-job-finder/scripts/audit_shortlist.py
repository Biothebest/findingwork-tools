#!/usr/bin/env python3
"""Fail-closed audit for a ranked job shortlist CSV."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from collections import Counter
from pathlib import Path
from urllib.parse import urlsplit

from verify_candidates import DEFAULT_ALLOWED_SUFFIXES, canonicalize, host_allowed, load_allowed, validate_url

REQUIRED_COLUMNS = {
    "priority", "company", "job_title", "location", "pay", "track", "fit_score", "fit_band",
    "matched_skills", "main_gap", "commute_minutes", "application_url", "verification_status",
    "verified_at", "security_flags", "resume", "application_status",
}
REQUIRED_VALUES = REQUIRED_COLUMNS - {"matched_skills", "commute_minutes", "security_flags"}
FIT_BANDS = {"close_match", "slight_stretch", "underqualified"}
TRACKS = {"IT", "Marketing", "Hybrid"}
RESUMES = {
    "IT": "Matthew_Benitez_IT_Support_Resume.pdf",
    "Marketing": "Matthew_Benitez_Resume.pdf",
    "Hybrid": "Matthew_Benitez_Resume.pdf",
}


def normalized_identity(row: dict[str, str]) -> str:
    parts = (row.get("company", ""), row.get("job_title", ""), row.get("location", ""))
    return "|".join(re.sub(r"[^a-z0-9]+", " ", item.lower()).strip() for item in parts)


def audit(rows: list[dict[str, str]], allowed: tuple[str, ...], minimum: int, maximum: int) -> dict:
    errors: list[str] = []
    warnings: list[str] = []
    if not minimum <= len(rows) <= maximum:
        errors.append(f"candidate count {len(rows)} is outside {minimum}-{maximum}")
    if rows:
        missing_columns = sorted(REQUIRED_COLUMNS - set(rows[0]))
        if missing_columns:
            errors.append(f"missing columns: {missing_columns}")
    canonical_urls: list[str] = []
    identities: list[str] = []
    for index, row in enumerate(rows, 1):
        blank = sorted(field for field in REQUIRED_VALUES if not row.get(field, "").strip())
        if blank:
            errors.append(f"row {index} has blank required fields: {blank}")
        try:
            priority = int(row.get("priority", ""))
            score = int(row.get("fit_score", ""))
        except ValueError:
            errors.append(f"row {index} has invalid priority or fit_score")
            priority, score = -1, -1
        if priority != index:
            errors.append(f"row {index} priority is {priority}, expected {index}")
        if not 0 <= score <= 100:
            errors.append(f"row {index} fit_score is outside 0-100")
        if row.get("fit_band") not in FIT_BANDS:
            errors.append(f"row {index} has invalid fit_band")
        track = row.get("track")
        if track not in TRACKS:
            errors.append(f"row {index} has invalid track")
        elif row.get("resume") != RESUMES[track]:
            errors.append(f"row {index} has incorrect resume for {track}")
        if row.get("verification_status") != "active_candidate":
            errors.append(f"row {index} is not an active_candidate")
        if row.get("security_flags", "").strip():
            errors.append(f"row {index} has unresolved security flags")
        if row.get("application_status") not in {"not_applied", "applied"}:
            errors.append(f"row {index} has invalid application_status")
        url = row.get("application_url", "").strip()
        safe, reason = validate_url(url, allowed, resolve_dns=False)
        if not safe:
            errors.append(f"row {index} unsafe URL: {reason}")
        else:
            canonical_urls.append(canonicalize(url))
            host = (urlsplit(url).hostname or "").lower()
            if not host_allowed(host, allowed):
                errors.append(f"row {index} host is not allowlisted: {host}")
        identities.append(normalized_identity(row))

    duplicate_urls = sorted(url for url, count in Counter(canonical_urls).items() if count > 1)
    duplicate_identities = sorted(value for value, count in Counter(identities).items() if value and count > 1)
    if duplicate_urls:
        errors.append(f"duplicate canonical URLs: {duplicate_urls}")
    if duplicate_identities:
        errors.append(f"duplicate company/title/location identities: {duplicate_identities}")
    bands = Counter(row.get("fit_band", "") for row in rows)
    if rows and bands.get("underqualified", 0) / len(rows) > 0.40:
        warnings.append("more than 40% of shortlist is underqualified")
    if rows and bands.get("close_match", 0) / len(rows) < 0.40:
        warnings.append("fewer than 40% of shortlist is close_match")
    return {
        "status": "pass" if not errors else "fail",
        "candidate_count": len(rows),
        "unique_urls": len(set(canonical_urls)),
        "https_urls": sum(url.startswith("https://") for url in canonical_urls),
        "track_counts": dict(Counter(row.get("track", "") for row in rows)),
        "fit_band_counts": dict(bands),
        "application_status_counts": dict(Counter(row.get("application_status", "") for row in rows)),
        "allowed_host_suffixes": list(allowed),
        "errors": errors,
        "warnings": warnings,
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("csv_path", type=Path)
    parser.add_argument("--config", type=Path)
    parser.add_argument("--min-count", type=int, default=50)
    parser.add_argument("--max-count", type=int, default=100)
    parser.add_argument("--json", required=True, type=Path)
    args = parser.parse_args(argv)
    if not 1 <= args.min_count <= args.max_count <= 100:
        raise SystemExit("count bounds must satisfy 1 <= min <= max <= 100")
    with args.csv_path.open(encoding="utf-8", newline="") as handle:
        rows = list(csv.DictReader(handle))
    allowed = load_allowed(args.config) if args.config else DEFAULT_ALLOWED_SUFFIXES
    report = audit(rows, allowed, args.min_count, args.max_count)
    encoded = json.dumps(report, indent=2, sort_keys=True) + "\n"
    args.json.write_text(encoded, encoding="utf-8")
    sys.stdout.write(encoded)
    return 0 if report["status"] == "pass" else 2


if __name__ == "__main__":
    raise SystemExit(main())
