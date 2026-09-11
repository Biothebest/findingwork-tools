"""Regressions for qualification parsing and posting-specific eligibility."""
from __future__ import annotations

import copy
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT.parent / "secure-job-finder" / "scripts"))

from matching import prepare_candidate
from rank_candidates import score, skill_points

PROFILE = {
    "skills": ["python", "seo", "troubleshooting", "sql"],
    "degree_level": "associate_in_progress",
    "experience_years": {"IT": 0.1, "Marketing": 0, "Hybrid": 0},
    "minimum_hourly": 0, "minimum_annual": 0, "max_commute_minutes": 75,
    "target_title_terms": {"IT": ["support", "technician", "operations"],
                           "Marketing": ["marketing", "seo"],
                           "Hybrid": ["engineer", "developer", "implementation"]},
}
CONFIG = {"daily_scraper": {"local_cities": ["Los Angeles", "Pasadena"], "max_required_years": 5}}


def posting(**changes):
    value = {
        "company": "Example", "title": "Junior AI Implementation Specialist",
        "location": "Remote - United States", "workplace_type": "remote",
        "url": "https://jobs.lever.co/example/posting-1",
        "description_text": "Requirements\n0-2 years of relevant experience.\nPython skills.",
        "pay": "Not posted", "verification_status": "active_candidate",
        "security_flags": [], "source_type": "official_ats",
    }
    value.update(changes)
    return value


class QualificationParsingTests(unittest.TestCase):
    def prepared(self, **changes):
        result, reason = prepare_candidate(posting(**changes), PROFILE, CONFIG)
        self.assertEqual(reason, "")
        self.assertIsNotNone(result)
        return result

    def test_audio_volume_and_employer_age_are_not_work_experience(self):
        # Captured Deepgram descriptions contain 50,000 years of processed audio.
        result = self.prepared(description_text=(
            "Our company has 20 years of experience.\n"
            "Deepgram has processed over 50,000 years of audio.\n"
            "It's Important To Us That You Have\n3+ years of experience in an industry role.\n"
            "Python skills.\nIt Would Be Great If You Had\n8 years of experience preferred."
        ))
        self.assertEqual(result["required_years"], 3)
        self.assertFalse(result["entry_level_evidence"])
        self.assertFalse(any("50,000" in item or "20 years" in item for item in result["requirement_gaps"]))

    def test_word_years_and_work_activity_minimum(self):
        result = self.prepared(description_text="Qualifications and Skills:\nTwo years working with Python.\nPython required.")
        self.assertEqual(result["required_years"], 2)
        self.assertTrue(result["entry_level_evidence"])

    def test_combined_heading_exposes_required_degree(self):
        result, reason = prepare_candidate(posting(description_text="Qualifications & Skills\nBachelor’s degree, or equivalent work experience"), PROFILE, CONFIG)
        self.assertIsNone(result)
        self.assertTrue(reason.startswith("hold:"))

    def test_section_end_prevents_benefit_tools_becoming_requirements(self):
        result = self.prepared(description_text=(
            "Qualifications and Skills:\nPython required.\n"
            "Benefits & Perks:\nTraining on Kubernetes and Salesforce.\n"
            "US Pay Range\n$60,000-$80,000\nWhat We Offer\nCCNA training."
        ))
        self.assertEqual(result["required_skills"], ["python"])
        self.assertFalse(any("Training" in item for item in result["requirements_evidence"]))

    def test_preference_does_not_mask_separate_required_credential(self):
        for clause in (
            "Bachelor's degree preferred, but CCNA certification required.",
            "Bachelor's degree preferred and CCNA certification required.",
            "No bachelor's degree required, but CCNA certification required.",
            "CCNA not required; CISSP required.",
            "Preferred qualifications\nPython preferred.\nCCNA certification is mandatory.",
        ):
            with self.subTest(clause=clause):
                result, reason = prepare_candidate(posting(description_text="Requirements\n" + clause), PROFILE, CONFIG)
                self.assertIsNone(result)
                self.assertIn("certification not confirmed", reason)

    def test_required_and_preferred_skills_remain_separate(self):
        result = self.prepared(description_text="Requirements\nPython required and Kubernetes preferred.\nNice to Have\nSQL skills.")
        self.assertEqual(result["required_skills"], ["python"])
        self.assertIn("sql", result["mentioned_skills"])
        self.assertTrue(any("SQL" in clause for clause in result["preferred_requirements"]))

    def test_unknown_hard_platform_does_not_score_as_full_skill_match(self):
        result = self.prepared(description_text=(
            "Requirements\n0 years of experience.\nPython required.\n"
            "Mandatory: hands-on working proficiency with 6Sense, Demandbase, or an equivalent ABM/intent data platform."
        ))
        ranked, rejected = score(result, PROFILE)
        self.assertIsNone(rejected)
        self.assertLess(ranked["fit_score"], 80)
        self.assertIn("6Sense", ranked["main_gap"])

    def test_zero_required_skill_overlap_earns_no_overlap_points(self):
        self.assertEqual(skill_points({"required_skills": ["kubernetes"]}, {"python"}), (0, [], ["kubernetes"]))

    def test_junior_title_cannot_override_higher_required_experience(self):
        result = self.prepared(description_text="Requirements\n4 years of experience.\nPython skills.")
        self.assertEqual(result["junior_priority"], 2)
        self.assertFalse(result["entry_level_evidence"])
        rejected, reason = prepare_candidate(posting(description_text="Requirements\n6 years of experience.\nPython skills."), PROFILE, CONFIG)
        self.assertIsNone(rejected)
        self.assertTrue(reason.startswith("reject:"))

    def test_clinical_and_retail_support_need_actual_computer_duties(self):
        for description in (
            "Assist patients with tests and redraws. Troubleshooting appointment scheduling. Our company employs software engineers.",
            "Resolve customer returns and troubleshoot retail orders. Preferred skills: Salesforce and technical support.",
        ):
            with self.subTest(description=description):
                result, reason = prepare_candidate(posting(title="Customer Support Specialist", description_text=description), PROFILE, CONFIG)
                self.assertIsNone(result)
                self.assertIn("outside supported", reason)
        result = self.prepared(title="Customer Support Specialist", description_text="Troubleshoot software and API integration errors for hospital customers.\nRequirements\nPython skills.")
        self.assertEqual(result["track"], "IT")

    def test_previously_unmatched_configured_title_families(self):
        for title, description, track in (
            ("Speech Application Engineer", "Build voice applications.", "Hybrid"),
            ("API Developer", "Build REST endpoints.", "Hybrid"),
            ("Professional Services Engineer", "Implement software for customers.", "Hybrid"),
            ("Quality Engineer I", "Perform API testing with Postman.", "Hybrid"),
            ("Technical Operations Specialist", "Configure computers and maintain IT systems.", "IT"),
            ("MSP Support Technician", "Support computers and desktop applications.", "IT"),
            ("SIP Support Specialist", "Troubleshoot SIP and VoIP services.", "IT"),
        ):
            with self.subTest(title=title):
                result = self.prepared(title=title, description_text=description)
                self.assertEqual(result["track"], track)


class GeographyAndSecurityTests(unittest.TestCase):
    def test_multiple_posting_countries_preserve_primary_and_idempotence(self):
        original = posting(location="Remote (United States)", locations=["Remote (United States)", "Canada", "United States"])
        before = copy.deepcopy(original)
        first, reason = prepare_candidate(original, PROFILE, CONFIG)
        self.assertEqual(reason, "")
        self.assertEqual(first["location"], original["location"])
        second, reason = prepare_candidate(first, PROFILE, CONFIG)
        self.assertEqual(reason, "")
        self.assertEqual(second, first)
        self.assertEqual(original, before)

    def test_foreign_primary_allows_explicit_us_posting_alternative(self):
        result, reason = prepare_candidate(posting(
            location="Canada", country="CA", locations=["Canada", "Remote - United States"],
            location_details=[{"location": "Canada", "country": "CA"}, {"location": "Remote - United States", "country": "US"}],
        ), PROFILE, CONFIG)
        self.assertEqual(reason, "")
        self.assertEqual(result["location"], "Canada")
        self.assertEqual(result["geography_status"], "remote_us_ca")

    def test_offices_and_ambiguous_city_are_not_posting_eligibility(self):
        for changes in (
            {"location": "Remote", "employer_office_locations": ["Los Angeles, CA", "United States"], "description_text": "Our offices are in Los Angeles, California.\nPython implementation work."},
            {"location": "Remote", "provider": "greenhouse", "locations": ["Remote", "Los Angeles, CA"]},
            {"location": "Pasadena", "workplace_type": "onsite"},
            {"location": "Remote - New York, NY", "description_text": "This role is remote in the United States. Python skills."},
        ):
            with self.subTest(changes=changes):
                result, reason = prepare_candidate(posting(**changes), PROFILE, CONFIG)
                self.assertIsNone(result)
                self.assertTrue(reason.startswith(("hold:", "reject:")))

    def test_noncalifornia_exclusion_is_not_an_allowlist(self):
        result, reason = prepare_candidate(posting(description_text="This role is remote in the United States, excluding NY and TX.\nPython skills."), PROFILE, CONFIG)
        self.assertEqual(reason, "")
        self.assertEqual(result["geography_status"], "remote_us_ca")
        result, reason = prepare_candidate(posting(description_text="This role is remote in the United States, excluding California.\nPython skills."), PROFILE, CONFIG)
        self.assertIsNone(result)
        self.assertTrue(reason.startswith("reject:"))

    def test_warning_does_not_mask_separate_payment_demand(self):
        safe = "We will never ask you to pay a fee. These engagements may be an attempt to induce you to pay a fee.\nPython skills."
        result, reason = prepare_candidate(posting(description_text=safe), PROFILE, CONFIG)
        self.assertEqual(reason, "")
        for demand in (
            "We never ask for fees, but you must purchase equipment.",
            "We never ask for fees and you must pay a deposit.",
            "To prevent recruiting fraud, pay a fee.",
            "Pay an application fee before your interview.",
            "Please cash our check to buy equipment.",
        ):
            with self.subTest(demand=demand):
                result, reason = prepare_candidate(posting(description_text=demand + "\nPython skills."), PROFILE, CONFIG)
                self.assertIsNone(result)
                self.assertTrue(reason.startswith("hold:"))
        result, reason = prepare_candidate(posting(source_fraud_context="We never ask for fees.\nYou must purchase equipment."), PROFILE, CONFIG)
        self.assertIsNone(result)
        self.assertTrue(reason.startswith("hold:"))


if __name__ == "__main__":
    unittest.main()
