#!/usr/bin/env python3
"""Local job discovery: one daily network attempt, durable reports, offline rebuilds."""
from __future__ import annotations

import argparse
import csv
import fcntl
import hashlib
import io
import json
import math
import os
import signal
import sqlite3
import sys
import tempfile
from collections import Counter
from contextlib import contextmanager, redirect_stdout
from datetime import datetime, timedelta
from pathlib import Path

SKILL_ROOT = Path(__file__).resolve().parents[1]
WORKSPACE = SKILL_ROOT.parents[2]
SECURE_ROOT = SKILL_ROOT.parent / "secure-job-finder"
TRACKS = {"Marketing", "IT", "Hybrid"}


def timestamp() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def emit(payload: dict) -> None:
    print(json.dumps(payload, ensure_ascii=False, allow_nan=False, indent=2))


def preflight() -> None:
    """Review the complete source set before importing local helper modules."""
    manifest_path = SKILL_ROOT / "manifest.json"
    if manifest_path.is_symlink():
        raise ValueError("scraper manifest is a symlink")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    expected = manifest.get("files")
    if not isinstance(expected, dict) or not expected:
        raise ValueError("scraper manifest files must be a nonempty object")
    actual = set()
    for path in SKILL_ROOT.rglob("*"):
        if "__pycache__" in path.parts or path.name == ".DS_Store":
            continue
        if path.is_symlink():
            raise ValueError("scraper integrity: symlink: " + str(path))
        if path.is_file() and path != manifest_path:
            actual.add(path.relative_to(SKILL_ROOT).as_posix())
    if set(expected) != actual:
        raise ValueError("scraper integrity: unexpected or missing files; review changes before updating manifest")
    for relative, digest in expected.items():
        path = SKILL_ROOT / relative
        if SKILL_ROOT not in path.resolve().parents:
            raise ValueError("scraper integrity: unsafe manifest path")
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            raise ValueError("scraper integrity: hash mismatch: " + relative)
    vet_path = SECURE_ROOT / "scripts" / "vet_skill.py"
    if vet_path.is_symlink() or hashlib.sha256(vet_path.read_bytes()).hexdigest() != manifest.get("secure_vet_sha256"):
        raise ValueError("secure-job-finder preflight source changed; static review required")
    helper_path = str(SECURE_ROOT / "scripts")
    if helper_path not in sys.path:
        sys.path.insert(0, helper_path)
    import vet_skill
    output = io.StringIO()
    with redirect_stdout(output):
        code = vet_skill.main(["--skill-root", str(SECURE_ROOT), "--manifest", "manifest.json"])
    if code:
        raise ValueError("secure-job-finder integrity failed: " + output.getvalue().strip())


def output_root(workspace: Path) -> Path:
    root = workspace / "job-search-daily"
    if root.is_symlink():
        raise ValueError("daily output directory must not be a symlink")
    return root


def open_state(root: Path, now: datetime) -> sqlite3.Connection:
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / ".state.sqlite3"
    if path.is_symlink():
        raise ValueError("daily state database must not be a symlink")
    connection = sqlite3.connect(str(path), timeout=5)
    try:
        connection.execute("PRAGMA synchronous=FULL")
        connection.executescript("""
            CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS runs (
                local_date TEXT PRIMARY KEY, scheduled_date TEXT NOT NULL,
                started_at TEXT NOT NULL, finished_at TEXT, status TEXT NOT NULL,
                report_path TEXT, error TEXT
            );
            CREATE TABLE IF NOT EXISTS seen (
                url TEXT PRIMARY KEY, company TEXT NOT NULL, title TEXT NOT NULL,
                location TEXT NOT NULL, job_id TEXT, provider TEXT, board_token TEXT,
                first_seen TEXT NOT NULL, run_date TEXT
            );
        """)
        columns = {row[1] for row in connection.execute("PRAGMA table_info(seen)")}
        if "run_date" not in columns:
            connection.execute("ALTER TABLE seen ADD COLUMN run_date TEXT")
            connection.execute("UPDATE seen SET run_date=substr(first_seen,1,10)")
        connection.execute("INSERT OR IGNORE INTO meta VALUES ('installed_at', ?)", (now.isoformat(),))
        connection.commit()
        return connection
    except BaseException:
        connection.close()
        raise


def claim_day(connection: sqlite3.Connection, now: datetime, scheduled: bool,
              calendar: tuple[int, int] = (7, 0)) -> tuple[bool, str]:
    """Commit the local-date claim before the first network call. Never reset it."""
    today = now.date().isoformat()
    due = now.replace(hour=calendar[0], minute=calendar[1], second=0, microsecond=0)
    before_calendar = now < due
    if before_calendar:
        due -= timedelta(days=1)
    due_date = due.date().isoformat()
    connection.execute("BEGIN IMMEDIATE")
    try:
        if connection.execute("SELECT 1 FROM runs WHERE local_date=?", (today,)).fetchone():
            connection.rollback()
            return False, "already_attempted_today"
        if scheduled and before_calendar:
            installed = datetime.fromisoformat(connection.execute("SELECT value FROM meta WHERE key='installed_at'").fetchone()[0])
            # Compare calendar walls, not yesterday with today's fixed DST offset.
            installed_after_due = (installed.date(), installed.hour, installed.minute, installed.second) > (due.date(), *calendar, 0)
            prior = connection.execute("SELECT 1 FROM runs WHERE local_date>=? OR scheduled_date>=?", (due_date, due_date)).fetchone()
            if installed_after_due or prior:
                connection.rollback()
                return False, "waiting_for_schedule"
        connection.execute("INSERT INTO runs(local_date,scheduled_date,started_at,status) VALUES (?,?,?,'running')",
                           (today, due_date if scheduled else today, now.isoformat()))
        connection.commit()
        return True, "claimed"
    except BaseException:
        connection.rollback()
        raise


def latest_run(connection: sqlite3.Connection) -> dict:
    row = connection.execute("SELECT local_date,started_at,finished_at,status,report_path,error FROM runs ORDER BY rowid DESC LIMIT 1").fetchone()
    return dict(zip(("local_date", "started_at", "finished_at", "status", "report_path", "error"), row)) if row else {"status": "not_run"}


@contextmanager
def exclusive(root: Path):
    root.mkdir(mode=0o700, parents=True, exist_ok=True)
    path = root / ".run.lock"
    if path.is_symlink():
        raise ValueError("daily lock must not be a symlink")
    with path.open("a") as lock:
        fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        yield


@contextmanager
def deadline(seconds: int):
    def interrupted(signum, frame):
        if signum == signal.SIGALRM:
            raise TimeoutError("daily execution exceeded its configured deadline")
        raise InterruptedError("daily execution interrupted by SIGTERM")
    original = {sig: signal.signal(sig, interrupted) for sig in (signal.SIGALRM, signal.SIGTERM)}
    signal.alarm(seconds)
    try:
        yield
    finally:
        signal.alarm(0)
        for sig, handler in original.items():
            signal.signal(sig, handler)


def load_inputs(workspace: Path) -> tuple[dict, dict, list[dict]]:
    from rank_candidates import load_profile
    from schedule import calendar_time
    from sources import _endpoint
    from storage import read_json, safe_path

    config = read_json(safe_path(workspace, "job-search-config.json"))
    if not isinstance(config, dict) or not isinstance(config.get("daily_scraper"), dict):
        raise ValueError("job-search-config.json requires daily_scraper settings")
    settings = config["daily_scraper"]
    calendar_time(config)
    if settings.get("output_directory", "job-search-daily") != "job-search-daily":
        raise ValueError("output_directory must stay job-search-daily to preserve daily history")
    quotas = settings.get("track_targets")
    if not isinstance(quotas, dict) or set(quotas) != TRACKS or any(type(value) is not int or not 0 <= value <= 100 for value in quotas.values()) or not 1 <= sum(quotas.values()) <= 100:
        raise ValueError("track_targets must define Marketing, IT and Hybrid with integer quotas totaling 1-100")
    maximum_years = settings.get("max_required_years", 5)
    if type(maximum_years) not in (int, float) or not math.isfinite(maximum_years) or not 0 <= maximum_years <= 5:
        raise ValueError("max_required_years must be between 0 and 5 for the entry/junior search")
    seconds = settings.get("max_run_seconds", 600)
    if type(seconds) is not int or not 30 <= seconds <= 1800:
        raise ValueError("max_run_seconds must be an integer between 30 and 1800")
    cities = settings.get("local_cities", [])
    if not isinstance(cities, list) or not all(isinstance(city, str) and city.strip() for city in cities):
        raise ValueError("local_cities must contain nonempty city names")
    age = config.get("verification", {}).get("max_age_hours", 72)
    if type(age) not in (int, float) or not math.isfinite(age) or age <= 0:
        raise ValueError("verification.max_age_hours must be finite and positive")
    profile_path = safe_path(workspace, config["ranking_profile"])
    read_json(profile_path)
    profile = load_profile(profile_path)
    if not isinstance(profile.get("skills"), list) or not all(isinstance(item, str) for item in profile["skills"]):
        raise ValueError("profile skills must be a list of strings")
    title_terms = profile.get("target_title_terms")
    if not isinstance(title_terms, dict) or any(not isinstance(title_terms.get(track), list) or not all(isinstance(term, str) and term for term in title_terms[track]) for track in TRACKS):
        raise ValueError("profile target_title_terms needs a list of strings for each track")
    years = profile.get("experience_years")
    if not isinstance(years, dict) or any(type(years.get(track)) not in (int, float) or not math.isfinite(years[track]) or years[track] < 0 for track in TRACKS):
        raise ValueError("profile needs finite, nonnegative experience_years for each track")
    for field in ("minimum_hourly", "minimum_annual", "max_commute_minutes"):
        value = profile.get(field)
        if type(value) not in (int, float) or not math.isfinite(value) or value < 0:
            raise ValueError("invalid numeric profile field: " + field)
    if set(config.get("resume_by_track", {})) != TRACKS:
        raise ValueError("resume_by_track must define all three tracks")
    for resume in config["resume_by_track"].values():
        if not isinstance(resume, str) or Path(resume).name != resume or not safe_path(workspace, resume).is_file():
            raise ValueError("configured resume is missing or unsafe")
    tracker = safe_path(workspace, "job-application-tracker.csv")
    with tracker.open(encoding="utf-8-sig", newline="") as handle:
        fields = set(csv.DictReader(handle).fieldnames or [])
    if not {"company", "job_title", "location", "job_url", "application_status"}.issubset(fields):
        raise ValueError("application tracker is missing required columns")
    read_json(safe_path(workspace, settings["evidence_snapshot"]))
    registry = read_json(safe_path(workspace, settings["source_registry"]))["sources"]
    if not isinstance(registry, list) or not 1 <= len(registry) <= 100:
        raise ValueError("source registry must contain 1-100 reviewed employers")
    keys, enabled = set(), 0
    for source in registry:
        if not isinstance(source, dict) or type(source.get("enabled")) is not bool:
            raise ValueError("each source must explicitly set enabled true/false")
        if not source["enabled"]:
            continue
        endpoint = _endpoint(source)
        if source.get("feed_url") != endpoint or endpoint in keys:
            raise ValueError("duplicate or noncanonical source endpoint")
        keys.add(endpoint)
        enabled += 1
    if not enabled:
        raise ValueError("no reviewed employer source is enabled")
    return config, profile, registry


def balanced_rows(rows: list[dict], quotas: dict[str, int], maximum: int) -> list[dict]:
    def order(row: dict) -> tuple:
        return (not row.get("entry_level_evidence", False), -int(row["fit_score"]), str(row["company"]).lower(), str(row["job_title"]).lower())
    pools = {track: sorted((row for row in rows if row["track"] == track), key=order) for track in quotas}
    chosen = []
    for index in range(max(quotas.values(), default=0)):
        for track, quota in quotas.items():
            if index < quota and index < len(pools[track]) and len(chosen) < maximum:
                chosen.append(pools[track][index])
    used = {row["application_url"] for row in chosen}
    remaining = sorted((row for row in rows if row["application_url"] not in used), key=order)
    chosen.extend(remaining[:max(0, maximum - len(chosen))])
    for index, row in enumerate(chosen, 1):
        row["priority"] = index
    return chosen


def csv_text(rows: list[dict], fieldnames: list[str]) -> str:
    buffer = io.StringIO(newline="")
    writer = csv.DictWriter(buffer, fieldnames=fieldnames, lineterminator="\n", extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        safe = {}
        for key in fieldnames:
            value = row.get(key, "")
            if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@", "\t", "\r")):
                value = "'" + value
            safe[key] = value
        writer.writerow(safe)
    return buffer.getvalue()


def build_report(workspace: Path, output_dir: Path, connection: sqlite3.Connection,
                 capture: dict, config: dict, profile: dict, rebuild: bool = False) -> dict:
    from audit_shortlist import audit, normalized_identity
    from matching import exclude_candidates, prepare_candidate
    from rank_candidates import FIELDNAMES, score
    from storage import atomic_write, write_json
    from verify_candidates import load_allowed

    day = capture["local_date"]
    query = "SELECT url,company,title,location,job_id,provider,board_token FROM seen"
    prior_rows = connection.execute(query + " WHERE run_date IS NULL OR run_date<>?", (day,)) if rebuild else connection.execute(query)
    prior = [dict(zip(("url", "company", "title", "location", "job_id", "provider", "board_token"), row)) for row in prior_rows]
    candidates, source_reports = capture["candidates"], capture["sources"]
    if not any(item.get("status") == "ok" or item.get("candidate_count", 0) for item in source_reports):
        raise ValueError("no source returned usable published postings; inspect sources.json")
    unique, exclusions = exclude_candidates(candidates, workspace / "job-application-tracker.csv", prior)
    rejected, prepared, scored = [], [], []
    by_url, identities = {}, set()
    for candidate in unique:
        enriched, reason = prepare_candidate(candidate, profile, config)
        if enriched is None:
            rejected.append({"company": candidate.get("company"), "title": candidate.get("title"), "url": candidate.get("url"), "reason": reason})
            continue
        prepared.append(enriched)
        row, rejection = score(enriched, profile)
        if rejection:
            rejected.append(rejection)
            continue
        if not row:
            continue
        url, identity = row["application_url"], normalized_identity(row)
        if url in by_url or identity in identities:
            rejected.append({"company": row["company"], "title": row["job_title"], "url": url, "reason": "duplicate URL or company/title/location in this daily batch"})
            continue
        identities.add(identity)
        row["resume"] = config["resume_by_track"][row["track"]]
        row["entry_level_evidence"] = enriched.get("entry_level_evidence", False)
        row["skill_evidence"] = "; ".join(enriched.get("mentioned_skills", []))
        notes = list(enriched.get("review_notes", []))
        if enriched.get("low_pay_exception"):
            notes.append(enriched["pay_exception_reason"])
        row["review_notes"] = "; ".join(dict.fromkeys(notes))
        row["verification_method"] = enriched.get("verification_method", "")
        scored.append(row)
        by_url[url] = enriched
    selected = balanced_rows(scored, config["daily_scraper"]["track_targets"], 100)
    fields = FIELDNAMES + ["entry_level_evidence", "skill_evidence", "review_notes", "verification_method"]
    text = csv_text(selected, fields)
    # Audit the actual escaped serialization, not a different in-memory view.
    audit_report = audit(list(csv.DictReader(io.StringIO(text))), load_allowed(workspace / "job-search-config.json"), 50, 100)
    unsafe = [error for error in audit_report["errors"] if not error.startswith("candidate count ")]
    write_json(output_dir / "audit.json", audit_report)
    write_json(output_dir / "verified.json", {"candidates": prepared})
    write_json(output_dir / "rejected.json", {"candidates": rejected, "exclusions": exclusions})
    write_json(output_dir / "exclusions.json", exclusions)
    write_json(output_dir / "inputs.json", {"config": config, "profile": profile, "skill_manifest": json.loads((SKILL_ROOT / "manifest.json").read_text())})
    if unsafe:
        raise ValueError("shortlist failed hard audit gates: " + "; ".join(unsafe))
    delivered = [by_url[row["application_url"]] for row in selected]
    atomic_write(output_dir / "ranked.csv", text)
    write_json(output_dir / "selected.json", {"candidates": delivered})
    counts = dict(Counter(row["track"] for row in selected))
    warnings = audit_report["warnings"] + audit_report["errors"]
    deficits = {track: max(0, target - counts.get(track, 0)) for track, target in config["daily_scraper"]["track_targets"].items()}
    for track, missing in deficits.items():
        if missing:
            warnings.append(f"{track}: {counts.get(track, 0)} new matches; short by {missing}; never padded")
    incomplete = [item for item in source_reports if item.get("status") not in {"ok", "disabled"} or not item.get("complete", False)]
    if incomplete:
        warnings.append(f"{len(incomplete)} sources blocked, failed or incomplete; inspect source diagnostics")
    stale = (datetime.now().astimezone() - datetime.fromisoformat(capture["collected_at"])).total_seconds() > config.get("verification", {}).get("max_age_hours", 72) * 3600
    if stale:
        warnings.append("Saved listing evidence is stale; rebuilding does not re-verify active status")
    status_value = "partial" if incomplete or stale else "shortfall" if audit_report["status"] != "pass" or any(deficits.values()) else "ready"
    report = {
        "status": status_value, "local_date": day, "collected_at": capture["collected_at"], "finished_at": timestamp(),
        "mode": "offline_rebuild" if rebuild else "daily_collection", "network_collection_performed": not rebuild,
        "evidence_stale": stale, "candidate_count": len(selected), "unique_urls": audit_report["unique_urls"],
        "track_counts": counts, "fit_band_counts": audit_report["fit_band_counts"], "track_shortfalls": deficits,
        "entry_level_count": sum(bool(row.get("entry_level_evidence")) for row in selected),
        "low_pay_count": sum(bool(item.get("low_pay_exception")) for item in delivered),
        "commute_unverified_count": sum(bool(item.get("commute_unverified")) for item in delivered),
        "discovered_count": len(candidates), "eligible_count": len(scored), "excluded": exclusions,
        "source_count": len(source_reports), "incomplete_source_count": len(incomplete), "warnings": warnings,
        "csv_path": str(output_dir / "ranked.csv"), "audit_path": str(output_dir / "audit.json"),
        "source_report_path": str(workspace / "job-search-daily" / day / "sources.json"),
        "first_ten": [{key: row.get(key) for key in ("priority", "company", "job_title", "track", "fit_band", "application_url", "main_gap")} for row in selected[:10]],
        "application_status": "not_applied", "repositories_accessed_by_scraper": False,
    }
    write_json(output_dir / "report.json", report)
    return report


def failure(root: Path, run_dir: Path, connection: sqlite3.Connection, day: str, exc: BaseException) -> None:
    from storage import write_json
    error = f"{type(exc).__name__}: {exc}"
    interrupted = isinstance(exc, (KeyboardInterrupt, InterruptedError))
    report = {"status": "interrupted" if interrupted else "failed", "local_date": day, "finished_at": timestamp(),
              "error": error, "run_directory": str(run_dir), "daily_attempt_consumed": True,
              "next_action": "Use doctor/status. Rebuild a complete saved capture offline; never reset the daily claim."}
    if (root / ".publication.json").exists():
        emit({"status": "publication_pending", "error": error, "next_action": "Run rebuild or run to finish local publication without a second scrape."})
        return
    # Store a durable failure in SQLite even when the filesystem cannot accept a report.
    with connection:
        connection.execute("UPDATE runs SET finished_at=?,status=?,report_path=?,error=? WHERE local_date=?",
                           (report["finished_at"], report["status"], str(run_dir / "report.json"), error, day))
    try:
        write_json(run_dir / "report.json", report)
        write_json(root / "latest.json", report)
    except OSError as write_error:
        report["report_write_error"] = str(write_error)
    emit(report)


def run(workspace: Path, scheduled: bool = False, rebuild: bool = False) -> int:
    os.umask(0o077)
    preflight()
    from schedule import calendar_time
    from sources import collect_sources
    from storage import publish, read_capture, recover_publication, safe_path, write_capture, write_json

    root = output_root(workspace)
    try:
        with exclusive(root):
            connection = open_state(root, datetime.now().astimezone())
            try:
                recovered = recover_publication(root, connection)
                if recovered:
                    emit({"status": "publication_recovered", "network_collection_performed": False, "latest": str(root / "latest.json")})
                    return 0
                # This process owns the global lock; any abandoned running row
                # belongs to a crashed process, never to a second live collector.
                with connection:
                    connection.execute("UPDATE runs SET status='interrupted',finished_at=?,error=? WHERE status='running'",
                                       (timestamp(), "Prior process exited before completing the daily attempt"))
                config, profile, registry = load_inputs(workspace)
                if rebuild:
                    record = latest_run(connection)
                    if record["status"] == "not_run":
                        raise ValueError("no daily attempt is available to rebuild")
                    day = record["local_date"]
                    original_dir = safe_path(root, day)
                    capture = read_capture(original_dir)
                    if capture.get("local_date") != day:
                        raise ValueError("capture does not belong to the claimed day")
                    output_dir = Path(tempfile.mkdtemp(prefix="rebuild-", dir=original_dir))
                    try:
                        with deadline(config["daily_scraper"].get("max_run_seconds", 600)):
                            report = build_report(workspace, output_dir, connection, capture, config, profile, True)
                            publish(root, output_dir, day, connection)
                    except (Exception, KeyboardInterrupt) as exc:
                        # An unsuccessful rebuild cannot replace the previous published report.
                        write_json(output_dir / "rebuild-error.json", {"error": str(exc), "network_collection_performed": False})
                        raise
                else:
                    now = datetime.now().astimezone()
                    claimed, reason = claim_day(connection, now, scheduled, calendar_time(config))
                    if not claimed:
                        emit({"status": "skipped", "reason": reason, "state": latest_run(connection), "latest": str(root / "latest.json")})
                        return 0
                    day = now.date().isoformat()
                    output_dir = safe_path(root, day)
                    try:
                        output_dir.mkdir(mode=0o700, exist_ok=True)
                        with deadline(config["daily_scraper"].get("max_run_seconds", 600)):
                            candidates, reports = collect_sources(registry)
                            write_json(output_dir / "sources.json", reports)
                            capture = {"version": 1, "local_date": day, "collected_at": timestamp(), "candidates": candidates, "sources": reports}
                            write_capture(output_dir, capture)
                            report = build_report(workspace, output_dir, connection, capture, config, profile)
                            publish(root, output_dir, day, connection)
                    except (Exception, KeyboardInterrupt) as exc:
                        failure(root, output_dir, connection, day, exc)
                        return 130 if isinstance(exc, KeyboardInterrupt) else 1
                # Keep launchd logs small. Full details and priorities remain in latest.json.
                emit({key: report[key] for key in ("status", "local_date", "mode", "candidate_count", "track_counts", "incomplete_source_count", "csv_path")})
                return 0
            finally:
                connection.close()
    except BlockingIOError:
        emit({"status": "skipped", "reason": "already_running"})
        return 0


def state_report(workspace: Path) -> dict:
    root = output_root(workspace)
    path = root / ".state.sqlite3"
    if path.is_symlink():
        raise ValueError("daily database must not be a symlink")
    if not path.exists():
        return {"status": "not_run"}
    connection = sqlite3.connect(path.as_uri() + "?mode=ro", uri=True, timeout=5)
    try:
        report = latest_run(connection)
        report["database_ok"] = connection.execute("PRAGMA quick_check").fetchone()[0] == "ok"
        if (root / ".publication.json").exists():
            report["status"] = "publication_pending"
            report["next_action"] = "Run or rebuild completes pending local publication without another scrape"
        elif report["status"] == "running":
            try:
                with exclusive(root):
                    report["status"] = "interrupted"
                    report["note"] = "Process exited before completion; daily claim is retained"
            except BlockingIOError:
                pass
        published = connection.execute("SELECT report_path FROM runs WHERE status IN ('ready','shortfall','partial') AND report_path IS NOT NULL ORDER BY rowid DESC LIMIT 1").fetchone()
        report["last_published_report"] = published[0] if published else None
        return report
    finally:
        connection.close()


def doctor(workspace: Path) -> int:
    checks = {}
    try:
        preflight()
        checks["integrity"] = "pass"
        config, profile, registry = load_inputs(workspace)
        checks["inputs"] = "pass"
        checks["enabled_sources"] = sum(source["enabled"] for source in registry)
        from schedule import inspect
        from storage import read_json
        checks["schedule"] = inspect(workspace, Path(__file__).resolve(), config)
        checks["state"] = state_report(workspace)
        issues = []
        schedule_check = checks["schedule"]
        if not schedule_check.get("loaded") or not schedule_check.get("definition_matches") or not schedule_check.get("loaded_trigger_matches"):
            issues.append("automatic schedule missing or changed; use schedule install, or run manually")
        if checks["state"].get("database_ok") is False:
            issues.append("daily state database failed integrity check; preserve it and investigate")
        if checks["state"]["status"] in {"failed", "interrupted", "publication_pending", "partial"}:
            issues.append("last run needs attention: " + checks["state"]["status"])
        latest = output_root(workspace) / "latest.json"
        if latest.exists():
            report = read_json(latest)
            checks["latest_result"] = {key: report.get(key) for key in ("status", "candidate_count", "incomplete_source_count", "evidence_stale", "csv_path")}
            if report.get("incomplete_source_count"):
                issues.append("latest collection has incomplete source coverage; inspect its source diagnostics")
            if report.get("evidence_stale"):
                issues.append("latest listing evidence is stale; an offline rebuild does not refresh it")
        emit({"status": "attention" if issues else "ok", "network_requests": 0, "checks": checks, "issues": issues})
        return 1 if issues else 0
    except (Exception, KeyboardInterrupt) as exc:
        emit({"status": "error", "network_requests": 0, "checks": checks, "error": f"{type(exc).__name__}: {exc}"})
        return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="action")
    command = sub.add_parser("run", help="collect at most once per local date")
    command.add_argument("--scheduled", action="store_true", help="respect the configured local calendar and catch up missed days")
    sub.add_parser("rebuild", help="requalify the latest complete capture offline; never fetch listings")
    sub.add_parser("status", help="inspect local run state without network")
    sub.add_parser("preflight", help="check both reviewed skill manifests")
    sub.add_parser("doctor", help="check integrity, inputs, state and macOS schedule without network")
    calendar = sub.add_parser("schedule", help="manage this user's LaunchAgent only")
    calendar.add_argument("operation", choices=("install", "status", "remove"))
    args = parser.parse_args()
    try:
        if args.action == "status":
            emit(state_report(WORKSPACE))
            return 0
        if args.action == "preflight":
            preflight()
            emit({"status": "pass", "scope": "scraper and secure-job-finder integrity"})
            return 0
        if args.action == "doctor":
            return doctor(WORKSPACE)
        if args.action == "schedule":
            preflight()
            config, _, _ = load_inputs(WORKSPACE)
            from schedule import manage
            if args.operation == "install":
                connection = open_state(output_root(WORKSPACE), datetime.now().astimezone())
                connection.close()
            emit(manage(args.operation, WORKSPACE, Path(__file__).resolve(), config))
            return 0
        return run(WORKSPACE, getattr(args, "scheduled", False), args.action == "rebuild")
    except (Exception, KeyboardInterrupt) as exc:
        emit({"status": "error", "error": f"{type(exc).__name__}: {exc}", "next_action": "Run doctor; do not delete or reset daily history"})
        return 130 if isinstance(exc, KeyboardInterrupt) else 1


if __name__ == "__main__":
    raise SystemExit(main())
