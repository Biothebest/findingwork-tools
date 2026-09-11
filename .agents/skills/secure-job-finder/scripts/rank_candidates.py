#!/usr/bin/env python3
"""Deterministically score verified candidates against an explicit profile."""
from __future__ import annotations

import argparse
import csv
import json
import re
import sys
from pathlib import Path

MANAGEMENT_TITLE = re.compile(r"\b(manager|director|principal|architect|head|vice president|vp)\b", re.I)
LEAD_TITLE = re.compile(r"^(?:senior\s+)?lead\b(?!\s+generation\b)|\blead$", re.I)
YEARS_RE = re.compile(r"\b(\d+(?:\.\d+)?)\s*\+?\s+years?\s+(?:of\s+)?(?:directly\s+)?(?:relevant\s+)?experience\b", re.I)
MONEY_RE = re.compile(r"(?:US\$|\$|USD\s*)\s*([0-9][0-9,]*(?:\.\d+)?)\s*([kK]?)")
DEGREE_LEVELS = {
    "none": 0,
    "high_school": 1,
    "associate_in_progress": 2,
    "associate": 3,
    "bachelor_in_progress": 4,
    "bachelor": 5,
    "master": 6,
}
ALIASES = {
    "office 365": "microsoft 365",
    "m365": "microsoft 365",
    "o365": "microsoft 365",
    "azure active directory": "entra id",
    "azure ad": "entra id",
    "ms intune": "intune",
    "tcp ip": "tcp/ip",
    "helpdesk": "help desk",
    "service desk": "help desk",
    "deskside support": "desktop support",
}
FIELDNAMES = [
    "priority", "company", "job_title", "location", "pay", "track", "fit_score", "fit_band",
    "matched_skills", "main_gap", "commute_minutes", "application_url", "verification_status",
    "verified_at", "security_flags", "resume", "application_status",
]


def normalize(value: str) -> str:
    text = re.sub(r"[^a-z0-9+#/.]+", " ", value.lower()).strip()
    return re.sub(r"\s+", " ", text)


def canonical_skill(value: str) -> str:
    skill = normalize(value)
    return ALIASES.get(skill, skill)


def required_years(candidate: dict) -> float | None:
    explicit = candidate.get("required_years")
    if isinstance(explicit, (int, float)) and explicit >= 0:
        return float(explicit)
    if "required_years" in candidate:
        # Explicit null means extraction found no hard minimum. Do not promote
        # preferred experience elsewhere in the description into a requirement.
        return None
    text = str(candidate.get("description_text", ""))
    matches = [float(item) for item in YEARS_RE.findall(text)]
    return min(matches) if matches else None


def pay_bounds(pay: str) -> tuple[float, float, str] | None:
    values = [float(amount.replace(",", "")) * (1000 if suffix else 1) for amount, suffix in MONEY_RE.findall(pay)]
    if not values:
        return None
    lower, upper = min(values), max(values)
    lower_text = pay.lower()
    if "hour" in lower_text or "/hr" in lower_text or "hourly" in lower_text:
        return lower, upper, "hourly"
    if "year" in lower_text or "/yr" in lower_text or "annual" in lower_text or lower >= 10_000:
        return lower, upper, "annual"
    return None


def title_points(title: str, track: str, target_terms: dict) -> tuple[int, bool]:
    normalized = normalize(title)
    terms = [normalize(str(item)) for item in target_terms.get(track, [])]
    if any(term and term in normalized for term in terms):
        return 20, True
    adjacent = {
        "IT": ("support", "technician", "analyst", "systems", "operations"),
        "Marketing": ("marketing", "sales", "campaign", "operations", "coordinator", "gtm"),
        "Hybrid": ("technical", "web", "operations", "automation"),
    }
    if any(term in normalized for term in adjacent.get(track, ())):
        return 14, True
    return 6, False


def skill_points(candidate: dict, profile_skills: set[str]) -> tuple[int, list[str], list[str]]:
    required = [canonical_skill(str(item)) for item in candidate.get("required_skills", []) if str(item).strip()]
    if not required:
        return 8, [], []
    matched = sorted({item for item in required if item in profile_skills})
    missing = sorted({item for item in required if item not in profile_skills})
    ratio = len(matched) / len(set(required))
    points = 25 if ratio >= 0.8 else 18 if ratio >= 0.6 else 10 if ratio >= 0.4 else 4 if ratio > 0 else 0
    if candidate.get("requirements_incomplete"):
        # A parsed subset is not 80–100% coverage of the actual requirements.
        points = min(points, 8)
    return points, matched, missing


def degree_points(candidate: dict, profile_level: str) -> tuple[int, str | None]:
    required = str(candidate.get("required_degree", "none")).strip().lower().replace(" ", "_")
    if required in {"", "none", "not_required"}:
        return 5, None
    if required not in DEGREE_LEVELS:
        return 0, f"unrecognized degree requirement: {required}"
    current = DEGREE_LEVELS[profile_level]
    needed = DEGREE_LEVELS[required]
    if current >= needed:
        return 5, None
    if candidate.get("degree_equivalent_experience") and needed - current <= 2:
        return -3, f"{required.replace('_', ' ')} requested; equivalent experience may substitute"
    return -10, f"completed {required.replace('_', ' ')} required"


def compensation_points(candidate: dict, profile: dict) -> tuple[int, str | None, bool]:
    parsed = pay_bounds(str(candidate.get("pay", "")))
    if not parsed:
        return 0, None, False
    lower, upper, cadence = parsed
    floor = float(profile["minimum_hourly"] if cadence == "hourly" else profile["minimum_annual"])
    if lower >= floor:
        return 5, None, False
    if upper >= floor:
        return 0, f"compensation range starts below the {cadence} floor", False
    reason = str(candidate.get("pay_exception_reason", "")).strip()
    return -10, reason or f"compensation maximum is below the {cadence} floor", not bool(reason)


def commute_points(candidate: dict, profile: dict) -> tuple[int, str | None, bool]:
    value = candidate.get("commute_minutes")
    if value in (None, ""):
        return 0, None, False
    try:
        minutes = int(value)
    except (TypeError, ValueError):
        return 0, "invalid commute estimate", True
    maximum = int(profile["max_commute_minutes"])
    if minutes <= 45:
        return 5, None, False
    if minutes <= 60:
        return 0, None, False
    if minutes <= maximum:
        return -5, f"estimated commute is {minutes} minutes", False
    allowed = bool(candidate.get("commute_exception_approved"))
    return -10, f"estimated commute is {minutes} minutes", not allowed


def experience_points(candidate: dict, track_years: float) -> tuple[int, str | None, bool]:
    needed = required_years(candidate)
    if needed is None:
        return 5, None, False
    if needed > 5:
        return -15, f"requires {needed:g}+ years", True
    gap = needed - track_years
    if gap <= 0:
        return 15, None, False
    if gap <= 1:
        return 10, f"experience gap is about {gap:g} year", False
    if gap <= 2:
        return 5, f"experience gap is about {gap:g} years", False
    if gap <= 3:
        return -5, f"experience gap is about {gap:g} years", False
    return -15, f"experience gap is about {gap:g} years", False


def score(candidate: dict, profile: dict) -> tuple[dict | None, dict | None]:
    hard_reasons: list[str] = []
    if candidate.get("verification_status") != "active_candidate":
        hard_reasons.append(f"verification status is {candidate.get('verification_status', 'missing')}")
    if candidate.get("security_flags"):
        hard_reasons.append("security flags are unresolved")
    title = str(candidate.get("title", "")).strip()
    if MANAGEMENT_TITLE.search(title) or LEAD_TITLE.search(title):
        hard_reasons.append("senior leadership title")
    track = str(candidate.get("track", ""))
    if track not in {"IT", "Marketing", "Hybrid"}:
        hard_reasons.append("invalid or unsupported track")

    title_score, relevant = title_points(title, track, profile["target_title_terms"])
    if not relevant:
        hard_reasons.append("title is outside target lanes")
    profile_skills = {canonical_skill(str(item)) for item in profile.get("skills", [])}
    skills_score, matched, missing = skill_points(candidate, profile_skills)
    years_score, years_gap, years_hard = experience_points(candidate, float(profile["experience_years"].get(track, 0)))
    degree_score, degree_gap = degree_points(candidate, str(profile["degree_level"]))
    pay_score, pay_gap, pay_hard = compensation_points(candidate, profile)
    commute_score, commute_gap, commute_hard = commute_points(candidate, profile)
    if years_hard:
        hard_reasons.append(years_gap or "experience hard gate")
    if pay_hard:
        hard_reasons.append(pay_gap or "compensation hard gate")
    if commute_hard:
        hard_reasons.append(commute_gap or "commute hard gate")
    if degree_score == -10:
        hard_reasons.append(degree_gap or "degree hard gate")

    source = str(candidate.get("source", candidate.get("source_type", ""))).lower()
    recognized = any(item in source for item in ("official", "employer", "ats"))
    confidence = 5 if recognized and candidate.get("required_skills") and pay_bounds(str(candidate.get("pay", ""))) else 0
    if candidate.get("requirements_incomplete") or (not candidate.get("description_text") and not candidate.get("required_skills")):
        confidence = -5

    total = max(0, min(100, 35 + title_score + skills_score + years_score + degree_score + pay_score + commute_score + confidence))
    band = "close_match" if total >= 80 else "slight_stretch" if total >= 65 else "underqualified" if total >= 50 else "reject"
    if hard_reasons:
        band = "reject"
    gaps = [item for item in (years_gap, degree_gap, commute_gap, pay_gap) if item]
    if missing:
        gaps.insert(1 if gaps else 0, "missing required skills: " + ", ".join(missing[:5]))
    gaps.extend(str(value) for value in candidate.get("requirement_gaps", []) if value)
    main_gap = gaps[0] if gaps else "Structured requirements incomplete; confirm eligibility and tool requirements"
    canonical_url = str(candidate.get("canonical_application_url") or candidate.get("final_url") or candidate.get("url", ""))
    resume = {
        "IT": "Matthew_Benitez_IT_Support_Resume.pdf",
        "Marketing": "Matthew_Benitez_Resume.pdf",
        "Hybrid": "Matthew_Benitez_Resume.pdf",
    }.get(track, "")
    row = {
        "priority": 0,
        "company": candidate.get("company", ""),
        "job_title": title,
        "location": candidate.get("location", ""),
        "pay": candidate.get("pay", "Not posted") or "Not posted",
        "track": track,
        "fit_score": total,
        "fit_band": band,
        "matched_skills": "; ".join(matched),
        "main_gap": main_gap,
        "commute_minutes": candidate.get("commute_minutes", ""),
        "application_url": canonical_url,
        "verification_status": candidate.get("verification_status", ""),
        "verified_at": candidate.get("verified_at", ""),
        "security_flags": "; ".join(candidate.get("security_flags", [])),
        "resume": resume,
        "application_status": "not_applied",
    }
    rejected = dict(row)
    rejected["rejection_reasons"] = "; ".join(hard_reasons) if hard_reasons else "fit score below 50"
    return (None, rejected) if band == "reject" else (row, None)


def load_profile(path: Path) -> dict:
    profile = json.loads(path.read_text(encoding="utf-8"))
    required = ("skills", "experience_years", "degree_level", "minimum_hourly", "minimum_annual", "max_commute_minutes", "target_title_terms")
    missing = [key for key in required if key not in profile]
    if missing:
        raise ValueError(f"profile missing fields: {missing}")
    if profile["degree_level"] not in DEGREE_LEVELS:
        raise ValueError("invalid profile degree_level")
    return profile


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--profile", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--rejected", type=Path)
    parser.add_argument("--limit", type=int, default=100)
    args = parser.parse_args(argv)
    if not 1 <= args.limit <= 100:
        raise SystemExit("--limit must be between 1 and 100")

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise SystemExit("input must be an object with a candidates array")
    profile = load_profile(args.profile)
    accepted: list[dict] = []
    rejected: list[dict] = []
    for candidate in candidates:
        row, reject = score(candidate, profile)
        if row:
            accepted.append(row)
        if reject:
            rejected.append(reject)
    accepted.sort(key=lambda item: (-int(item["fit_score"]), str(item["company"]).lower(), str(item["job_title"]).lower()))
    accepted = accepted[: args.limit]
    for priority, row in enumerate(accepted, 1):
        row["priority"] = priority

    with args.output.open("w", encoding="utf-8", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=FIELDNAMES, lineterminator="\n")
        writer.writeheader()
        writer.writerows(accepted)
    if args.rejected:
        args.rejected.write_text(json.dumps({"candidates": rejected}, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps({"accepted": len(accepted), "rejected": len(rejected)}, sort_keys=True) + "\n")
    return 0 if accepted else 2


if __name__ == "__main__":
    raise SystemExit(main())
