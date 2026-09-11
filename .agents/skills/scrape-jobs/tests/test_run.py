"""Observable durability/recovery regressions; all source collection is offline."""
from __future__ import annotations

import contextlib
import csv
import io
import json
import signal
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

SKILL = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(SKILL / "scripts"))
sys.path.insert(0, str(SKILL.parent / "secure-job-finder" / "scripts"))

import run
import sources
import storage


class LocalRunTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.workspace = Path(self.temp.name)
        self.root = self.workspace / "job-search-daily"
        self.day = datetime.now().astimezone().date().isoformat()
        self.config = {
            "daily_scraper": {"schedule": "07:00", "track_targets": {"Marketing": 35, "IT": 35, "Hybrid": 30},
                              "source_registry": "sources.json", "evidence_snapshot": "evidence.json", "local_cities": ["Los Angeles"]},
            "ranking_profile": "profile.json", "verification": {"max_age_hours": 72},
            "resume_by_track": {"IT": "Matthew_Benitez_IT_Support_Resume.pdf", "Marketing": "Matthew_Benitez_Resume.pdf", "Hybrid": "Matthew_Benitez_Resume.pdf"},
        }
        profile = {"skills": ["python"], "degree_level": "associate_in_progress", "experience_years": {"IT": 0.1, "Marketing": 0, "Hybrid": 0},
                   "minimum_hourly": 0, "minimum_annual": 0, "max_commute_minutes": 75,
                   "target_title_terms": {"IT": ["support"], "Marketing": ["marketing"], "Hybrid": ["implementation"]}}
        for name, payload in [("job-search-config.json", self.config), ("profile.json", profile), ("evidence.json", {}),
                              ("sources.json", {"sources": [{"company": "Example", "provider": "lever", "board_token": "example", "enabled": True, "feed_url": "https://api.lever.co/v0/postings/example?mode=json"}]})]:
            (self.workspace / name).write_text(json.dumps(payload))
        for name in set(self.config["resume_by_track"].values()):
            (self.workspace / name).write_bytes(b"local fixture; never uploaded")
        (self.workspace / "job-application-tracker.csv").write_text("company,job_title,location,job_url,application_status,date_applied\n")
        self.candidate = {"company": "Example", "title": "Junior AI Implementation Specialist", "location": "Remote - United States",
                          "locations": ["Remote - United States"], "workplace_type": "remote", "provider": "lever", "board_token": "example", "job_id": "posting-1",
                          "url": "https://jobs.lever.co/example/posting-1", "application_url": "https://jobs.lever.co/example/posting-1/apply",
                          "canonical_application_url": "https://jobs.lever.co/example/posting-1/apply", "verification_status": "active_candidate",
                          "verification_method": "lever_published_public_api", "security_flags": [], "pay": "$60,000 - $75,000 annually",
                          "verified_at": datetime.now().astimezone().isoformat(), "source_type": "official_ats",
                          "description_text": "Requirements\n0-2 years of experience.\nPython skills required."}
        self.reports = [{"company": "Example", "status": "ok", "complete": True, "candidate_count": 1}]
        self.preflight = patch.object(run, "preflight")
        self.preflight.start()
        self.addCleanup(self.preflight.stop)
        self.addCleanup(self.temp.cleanup)

    def invoke(self, *, rebuild=False, result=None, error=None):
        with patch.object(sources, "collect_sources", return_value=result or ([self.candidate], self.reports), side_effect=error):
            with contextlib.redirect_stdout(io.StringIO()):
                return run.run(self.workspace, rebuild=rebuild)

    def rows(self):
        with (self.root / "latest.csv").open(newline="") as handle:
            return list(csv.DictReader(handle))

    def test_daily_publication_and_offline_rebuild_keep_one_network_claim(self):
        self.assertEqual(self.invoke(), 0)
        original = storage.read_json(self.root / "latest.json")
        self.assertEqual(len(self.rows()), 1)
        self.assertEqual(self.invoke(error=AssertionError("second network attempt")), 0)
        self.assertEqual(self.invoke(rebuild=True, error=AssertionError("rebuild performed network")), 0)
        rebuilt = storage.read_json(self.root / "latest.json")
        self.assertEqual(rebuilt["mode"], "offline_rebuild")
        self.assertFalse(rebuilt["network_collection_performed"])
        self.assertEqual(rebuilt["collected_at"], original["collected_at"])
        self.assertEqual(len(self.rows()), 1)
        with sqlite3.connect(self.root / ".state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 1)
            self.assertEqual(db.execute("SELECT count(*) FROM seen").fetchone()[0], 1)

    def test_publication_failure_recovers_local_delivery_without_rescraping(self):
        original_write = storage.atomic_write
        def full_disk(path, text):
            if path.name == "latest.csv":
                raise OSError("simulated disk full during publication")
            return original_write(path, text)
        with patch.object(storage, "atomic_write", side_effect=full_disk):
            self.assertEqual(self.invoke(), 1)
        self.assertTrue((self.root / ".publication.json").exists())
        self.assertEqual(run.state_report(self.workspace)["status"], "publication_pending")
        self.assertEqual(self.invoke(error=AssertionError("recovery performed network")), 0)
        self.assertEqual(len(self.rows()), 1)
        self.assertFalse((self.root / ".publication.json").exists())
        with sqlite3.connect(self.root / ".state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM seen").fetchone()[0], 1)

    def test_damaged_pending_output_is_not_published_or_silently_rescraped(self):
        original_write = storage.atomic_write
        def fail_alias(path, text):
            if path.name == "latest.csv":
                raise OSError("publication unavailable")
            return original_write(path, text)
        with patch.object(storage, "atomic_write", side_effect=fail_alias):
            self.assertEqual(self.invoke(), 1)
        (self.root / self.day / "ranked.csv").write_text("altered after durable intent")
        with self.assertRaises(ValueError):
            self.invoke(error=AssertionError("network after damaged publication"))
        self.assertFalse((self.root / "latest.csv").exists())
        self.assertTrue((self.root / ".publication.json").exists())

    def test_failed_collection_consumes_day_without_erasing_previous_csv(self):
        self.root.mkdir()
        (self.root / "latest.csv").write_text("previous publication")
        self.assertEqual(self.invoke(error=TimeoutError("network unavailable")), 1)
        self.assertEqual(run.state_report(self.workspace)["status"], "failed")
        self.assertEqual((self.root / "latest.csv").read_text(), "previous publication")
        self.assertEqual(self.invoke(error=AssertionError("same-day retry")), 0)

    def test_partial_feeds_are_truthfully_reported(self):
        partial = [{"company": "Example", "status": "incomplete", "complete": False, "candidate_count": 1}]
        self.assertEqual(self.invoke(result=([self.candidate], partial)), 0)
        report = storage.read_json(self.root / "latest.json")
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["incomplete_source_count"], 1)
        self.assertEqual(report["candidate_count"], 1)

    def test_hard_audit_failure_preserves_previous_publication(self):
        self.root.mkdir()
        (self.root / "latest.csv").write_text("previous publication")
        del self.candidate["verified_at"]
        self.assertEqual(self.invoke(), 1)
        self.assertEqual(run.state_report(self.workspace)["status"], "failed")
        self.assertEqual((self.root / "latest.csv").read_text(), "previous publication")

    def test_rebuild_applies_new_tracker_exclusions(self):
        self.assertEqual(self.invoke(), 0)
        with (self.workspace / "job-application-tracker.csv").open("a") as handle:
            handle.write("Example,Junior AI Implementation Specialist,Remote - United States,https://jobs.lever.co/example/posting-1,applied,2030-01-01\n")
        tracker_before = (self.workspace / "job-application-tracker.csv").read_bytes()
        self.assertEqual(self.invoke(rebuild=True, error=AssertionError("offline rebuild network")), 0)
        self.assertEqual(self.rows(), [])
        self.assertEqual((self.workspace / "job-application-tracker.csv").read_bytes(), tracker_before)

    def test_capture_damage_preserves_existing_publication(self):
        self.assertEqual(self.invoke(), 0)
        before = (self.root / "latest.csv").read_bytes()
        (self.root / self.day / "capture.json.gz").write_bytes(b"damaged capture")
        with self.assertRaises(ValueError):
            self.invoke(rebuild=True, error=AssertionError("network on invalid capture"))
        self.assertEqual((self.root / "latest.csv").read_bytes(), before)

    def test_config_error_occurs_before_network_claim(self):
        self.config["daily_scraper"]["track_targets"]["IT"] = -1
        (self.workspace / "job-search-config.json").write_text(json.dumps(self.config))
        with self.assertRaises(ValueError):
            self.invoke(error=AssertionError("network with invalid config"))
        with sqlite3.connect(self.root / ".state.sqlite3") as db:
            self.assertEqual(db.execute("SELECT count(*) FROM runs").fetchone()[0], 0)

    def test_stale_rebuild_does_not_refresh_listing_evidence(self):
        self.assertEqual(self.invoke(), 0)
        capture_dir = self.root / self.day
        capture = storage.read_capture(capture_dir)
        capture["collected_at"] = (datetime.now().astimezone() - timedelta(days=4)).isoformat()
        storage.write_capture(capture_dir, capture)
        self.assertEqual(self.invoke(rebuild=True, error=AssertionError("stale rebuild network")), 0)
        report = storage.read_json(self.root / "latest.json")
        self.assertTrue(report["evidence_stale"])
        self.assertEqual(report["status"], "partial")
        self.assertEqual(report["collected_at"], capture["collected_at"])

    def test_doctor_flags_incomplete_coverage_in_legacy_shortfall_reports(self):
        self.assertEqual(self.invoke(), 0)
        latest = self.root / "latest.json"
        report = storage.read_json(latest)
        report["incomplete_source_count"] = 2
        storage.write_json(latest, report)
        healthy_schedule = {"loaded": True, "definition_matches": True, "loaded_trigger_matches": True}
        output = io.StringIO()
        with patch("schedule.inspect", return_value=healthy_schedule), contextlib.redirect_stdout(output):
            code = run.doctor(self.workspace)
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(output.getvalue())["status"], "attention")


class ClockAndStorageTests(unittest.TestCase):
    def test_custom_calendar_and_dst_catchup_use_calendar_days(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            installed = datetime(2030, 3, 9, 6, 55, tzinfo=timezone(timedelta(hours=-8)))
            db = run.open_state(root, installed)
            try:
                wake = datetime(2030, 3, 10, 6, 55, tzinfo=timezone(timedelta(hours=-7)))
                self.assertTrue(run.claim_day(db, wake, True, (7, 0))[0])
                self.assertFalse(run.claim_day(db, wake.replace(hour=7), True, (7, 0))[0])
            finally:
                db.close()

    def test_existing_history_migrates_without_losing_claims_or_seen_urls(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            with sqlite3.connect(root / ".state.sqlite3") as db:
                db.execute("CREATE TABLE seen(url TEXT PRIMARY KEY,company TEXT,title TEXT,location TEXT,job_id TEXT,provider TEXT,board_token TEXT,first_seen TEXT)")
                db.execute("INSERT INTO seen VALUES ('https://jobs.lever.co/example/id','Example','Support','Remote','id','lever','example','2030-01-01T07:00:00-08:00')")
            db = run.open_state(root, datetime.now().astimezone())
            try:
                self.assertEqual(db.execute("SELECT url,first_seen,run_date FROM seen").fetchone(), ("https://jobs.lever.co/example/id", "2030-01-01T07:00:00-08:00", "2030-01-01"))
            finally:
                db.close()

    def test_sigterm_is_catchable_and_handler_is_restored(self):
        before = signal.getsignal(signal.SIGTERM)
        with self.assertRaises(InterruptedError):
            with run.deadline(30):
                signal.raise_signal(signal.SIGTERM)
        self.assertIs(signal.getsignal(signal.SIGTERM), before)


if __name__ == "__main__":
    unittest.main()
