#!/usr/bin/env python3
"""Reconcile sanitized Gmail confirmation metadata with the local job tracker."""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import tempfile
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

CONFIRMATION_PATTERNS = (
    re.compile(r"\bthank you for applying\b", re.I),
    re.compile(r"\bapplication (?:has been )?(?:received|submitted)\b", re.I),
    re.compile(r"\bwe (?:have )?received your application\b", re.I),
    re.compile(r"\byour application (?:to|for)\b", re.I),
    re.compile(r"\bapplication was sent\b", re.I),
    re.compile(r"\bsuccessfully applied\b", re.I),
)
NEGATIVE_PATTERNS = (
    re.compile(r"\bnot selected\b", re.I),
    re.compile(r"\bnot moving forward\b", re.I),
    re.compile(r"\bother candidates\b", re.I),
    re.compile(r"\bposition (?:has been )?filled\b", re.I),
    re.compile(r"\bapplication (?:is )?(?:incomplete|withdrawn)\b", re.I),
)
TRACKING_KEYS = {"trk", "source", "sourceid", "ref", "referrer", "gh_src"}
REQUIRED_MESSAGE_FIELDS = {"message_id", "date", "from", "subject", "snippet", "links"}
KNOWN_COMPANY_ALIASES = {
    "sony interactive entertainment": {"playstation global"},
}



def normalize_text(value: str) -> str:
    value = re.sub(r"\b(?:inc|llc|ltd|corp|corporation|company|co)\b", " ", value.lower())
    return re.sub(r"[^a-z0-9]+", " ", value).strip()

def company_aliases(value: str) -> set[str]:
    normalized = normalize_text(value)
    aliases = {normalized} if len(normalized) >= 4 else set()
    aliases.update(KNOWN_COMPANY_ALIASES.get(normalized, set()))
    first_original = re.split(r"[^A-Za-z0-9]+", value.strip())[0]
    if len(first_original) >= 3 and first_original.isupper():
        aliases.add(normalize_text(first_original))
    return {alias for alias in aliases if alias}


def canonicalize_url(url: str) -> str:
    parts = urlsplit(url.strip())
    if parts.scheme.lower() != "https" or not parts.hostname:
        return ""
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_KEYS:
            continue
        query.append((key, value))
    path = parts.path if parts.path == "/" else parts.path.rstrip("/")
    return urlunsplit(("https", parts.netloc.lower(), path, urlencode(query, doseq=True), ""))


def job_identifier(url: str) -> str:
    canonical = canonicalize_url(url)
    if not canonical:
        return ""
    patterns = (
        r"/jobs/view/(\d+)",
        r"[?&]jk=([a-z0-9]+)",
        r"/jobs/(\d+)",
        r"/postings/([a-z0-9-]+)",
        r"/job-([a-z0-9-]+)",
    )
    for pattern in patterns:
        match = re.search(pattern, canonical, re.I)
        if match:
            return match.group(1).lower()
    return ""


def parse_date(value: str) -> str:
    text = value.strip()
    if not text:
        return ""
    normalized = text.replace("Z", "+00:00")
    try:
        return datetime.fromisoformat(normalized).date().isoformat()
    except ValueError:
        pass
    if re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        return text
    return ""


def load_messages(path: Path) -> list[dict]:
    payload = json.loads(path.read_text(encoding="utf-8"))
    messages = payload.get("messages") if isinstance(payload, dict) else None
    if not isinstance(messages, list):
        raise ValueError("messages input must be an object with a messages array")
    result: list[dict] = []
    seen_ids: set[str] = set()
    for index, message in enumerate(messages, 1):
        if not isinstance(message, dict):
            raise ValueError(f"message {index} must be an object")
        missing = REQUIRED_MESSAGE_FIELDS - set(message)
        if missing:
            raise ValueError(f"message {index} is missing fields: {sorted(missing)}")
        message_id = str(message["message_id"]).strip()
        if not message_id or message_id in seen_ids:
            raise ValueError(f"message {index} has a blank or duplicate message_id")
        links = message["links"]
        if not isinstance(links, list) or not all(isinstance(link, str) for link in links):
            raise ValueError(f"message {index} links must be a string array")
        seen_ids.add(message_id)
        result.append({key: message[key] for key in REQUIRED_MESSAGE_FIELDS})
    return result


def read_tracker(path: Path) -> tuple[list[str], list[dict[str, str]]]:
    with path.open(encoding="utf-8", newline="") as handle:
        reader = csv.DictReader(handle)
        if not reader.fieldnames:
            raise ValueError("tracker has no header")
        rows = list(reader)
    required = {"campaign_id", "company", "job_title", "job_url", "date_applied", "application_status", "message_status", "notes"}
    missing = required - set(reader.fieldnames)
    if missing:
        raise ValueError(f"tracker is missing columns: {sorted(missing)}")
    return list(reader.fieldnames), rows


def is_confirmation(message: dict) -> tuple[bool, str]:
    corpus = " ".join(str(message[field]) for field in ("subject", "snippet"))
    if any(pattern.search(corpus) for pattern in NEGATIVE_PATTERNS):
        return False, "negative or non-confirmation language"
    for pattern in CONFIRMATION_PATTERNS:
        if pattern.search(corpus):
            return True, pattern.pattern
    return False, "no strong confirmation phrase"


def match_message(message: dict, rows: list[dict[str, str]]) -> dict:
    confirmed, phrase = is_confirmation(message)
    base = {
        "message_id": str(message["message_id"]),
        "date": str(message["date"]),
        "from": str(message["from"]),
        "subject": str(message["subject"]),
        "status": "ignored",
        "campaign_id": "",
        "reason": phrase,
    }
    if not confirmed:
        return base

    links = {canonicalize_url(link) for link in message["links"]}
    links.discard("")
    link_ids = {job_identifier(link) for link in links}
    link_ids.discard("")
    exact: list[dict[str, str]] = []
    for row in rows:
        row_url = canonicalize_url(row.get("job_url", ""))
        row_id = job_identifier(row_url)
        if (row_url and row_url in links) or (row_id and row_id in link_ids):
            exact.append(row)
    if len(exact) == 1:
        base.update(status="matched", campaign_id=exact[0]["campaign_id"], reason="exact canonical URL or job ID plus confirmation phrase")
        return base
    if len(exact) > 1:
        base.update(status="ambiguous", reason="confirmation links match multiple tracker rows")
        return base

    corpus = normalize_text(" ".join(str(message[field]) for field in ("from", "subject", "snippet")))
    company_matches: list[dict[str, str]] = []
    for row in rows:
        aliases = company_aliases(row.get("company", ""))
        if any(re.search(r"(?:^| )" + re.escape(alias) + r"(?: |$)", corpus) for alias in aliases):
            company_matches.append(row)
    if len(company_matches) == 1:
        base.update(status="matched", campaign_id=company_matches[0]["campaign_id"], reason="unique normalized company plus confirmation phrase")
    elif len(company_matches) > 1:
        base.update(status="ambiguous", reason="company has multiple tracker rows and no exact job link")
    else:
        base.update(status="unmatched", reason="confirmation found but no tracker identity matched")
    return base


def append_note(existing: str, note: str) -> str:
    existing = existing.strip()
    if note in existing:
        return existing
    return f"{existing}; {note}" if existing else note


def atomic_write_tracker(path: Path, fieldnames: list[str], rows: list[dict[str, str]]) -> None:
    descriptor, temporary = tempfile.mkstemp(prefix=path.name + ".", suffix=".tmp", dir=path.parent)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=fieldnames, lineterminator="\n")
            writer.writeheader()
            writer.writerows(rows)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    except Exception:
        try:
            os.unlink(temporary)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--messages", required=True, type=Path)
    parser.add_argument("--tracker", required=True, type=Path)
    parser.add_argument("--proposals", required=True, type=Path)
    parser.add_argument("--apply", action="store_true")
    args = parser.parse_args()

    messages = load_messages(args.messages)
    fieldnames, rows = read_tracker(args.tracker)
    proposals = [match_message(message, rows) for message in messages]
    matched = [proposal for proposal in proposals if proposal["status"] == "matched"]
    applied_updates: list[str] = []

    if args.apply:
        by_campaign = {row["campaign_id"]: row for row in rows}
        for proposal in matched:
            row = by_campaign[proposal["campaign_id"]]
            message_date = parse_date(proposal["date"])
            row["application_status"] = "applied"
            if message_date and not row["date_applied"].strip():
                row["date_applied"] = message_date
            row["message_status"] = "application_confirmation_received"
            note = f"Gmail confirmation {proposal['message_id']}: {proposal['reason']}"
            row["notes"] = append_note(row["notes"], note)
            applied_updates.append(row["campaign_id"])
        if applied_updates:
            atomic_write_tracker(args.tracker, fieldnames, rows)

    report = {
        "message_count": len(messages),
        "status_counts": {status: sum(1 for item in proposals if item["status"] == status) for status in ("matched", "ambiguous", "unmatched", "ignored")},
        "applied_updates": sorted(set(applied_updates)),
        "proposals": proposals,
    }
    args.proposals.write_text(json.dumps(report, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(json.dumps({key: report[key] for key in ("message_count", "status_counts", "applied_updates")}, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
