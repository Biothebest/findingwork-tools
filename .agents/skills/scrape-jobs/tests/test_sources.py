"""Offline regressions for source integrity, bounded reads and partial coverage."""
from __future__ import annotations

import io
import json
import socket
import ssl
import sys
import time
import unittest
from contextlib import contextmanager
from email.message import Message
from pathlib import Path
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "scripts"))
sys.path.insert(0, str(ROOT.parent / "secure-job-finder" / "scripts"))

import sources


STAMP = "2030-01-01T00:00:00+00:00"


def source(provider="lever", token="example"):
    value = {"provider": provider, "board_token": token, "company": token, "enabled": True}
    value["feed_url"] = sources._endpoint(value)
    return value


def posting(provider="lever", token="example", job_id="posting-1", **changes):
    value = {"title": "Support Specialist", "descriptionPlain": "Help customers troubleshoot software problems."}
    if provider == "lever":
        value.update(id=job_id, text=value.pop("title"), categories={"location": "Los Angeles, CA"},
                     hostedUrl=f"https://jobs.lever.co/{token}/{job_id}",
                     applyUrl=f"https://jobs.lever.co/{token}/{job_id}/apply")
    elif provider == "ashby":
        value.update(isListed=True, location="Toronto, Canada",
                     jobUrl=f"https://jobs.ashbyhq.com/{token}/{job_id}",
                     applyUrl=f"https://jobs.ashbyhq.com/{token}/{job_id}/application")
    else:
        value.update(id=123, internal_job_id=456, content=value.pop("descriptionPlain"),
                     location={"name": "New York, NY"},
                     absolute_url=f"https://boards.greenhouse.io/{token}/jobs/123")
    value.update(changes)
    return value


class Response:
    def __init__(self, body=b"", status=200, content_type="application/json", **headers):
        self.status = status
        self.headers = Message()
        self.headers["Content-Type"] = content_type
        for key, value in headers.items():
            self.headers[key.replace("_", "-")] = value
        self.body = io.BytesIO(body)
        self.closed = False

    def getheader(self, name, default=None):
        return self.headers.get(name, default)

    def read1(self, count):
        return self.body.read(count)

    def close(self):
        self.closed = True
        self.body.close()


def feed(records, provider="lever"):
    value = records if provider == "lever" else {"jobs": records}
    if provider == "greenhouse":
        value["meta"] = {"total": len(records)}
    return Response(json.dumps(value).encode("utf-8"))


@contextmanager
def offline_http(responses):
    """Exercise the real HTTP policy with in-memory response streams only."""
    pending = iter(responses)

    class Connection:
        def __init__(self, *args):
            self.transport = Mock()
            self.response = next(pending)

        def request(self, *args, **kwargs):
            pass

        def getresponse(self):
            return self.response

        def close(self):
            pass

        def interrupt(self):
            pass

    with patch.object(sources, "_PinnedHTTPS", Connection), \
            patch.object(sources, "_public_addresses", return_value=[]), \
            patch.object(sources.threading, "Timer"), \
            patch.object(sources.time, "sleep"):
        yield


class CollectionIntegrityTests(unittest.TestCase):
    def test_unsafe_json_isolated_from_next_board(self):
        attacks = {
            "nonfinite_constant": b'[{"id":"bad", "number":NaN}]',
            "exponent_overflow": b'[{"id":"bad", "number":1e400}]',
            "ambiguous_identity": b'[{"id":"first", "id":"second"}]',
            "unpaired_surrogate": b'[{"id":"bad", "text":"\\ud800"}]',
            "excessive_depth": b'[' * 80 + b'0' + b']' * 80,
        }
        for boundary, body in attacks.items():
            with self.subTest(boundary=boundary), offline_http([
                    Response(status=404), Response(body), feed([posting(token="healthy")])]):
                candidates, diagnostics = sources.collect_sources([source(), source(token="healthy")])
            self.assertEqual([item["company"] for item in candidates], ["healthy"])
            self.assertFalse(diagnostics[0]["complete"])
            self.assertEqual(diagnostics[1]["status"], "ok")

    def test_malformed_only_feed_is_not_clean_zero_coverage(self):
        with offline_http([Response(status=404), feed([posting(text=["not a title"])])]):
            candidates, diagnostics = sources.collect_sources([source()])
        self.assertEqual(candidates, [])
        self.assertEqual(diagnostics[0]["status"], "incomplete")
        self.assertFalse(diagnostics[0]["complete"])
        self.assertEqual(diagnostics[0]["malformed_count"], 1)
        self.assertEqual(diagnostics[0]["filtered_count"], 1)

    def test_bad_html_record_does_not_abort_good_record_or_other_board(self):
        bad = posting(job_id="bad", descriptionPlain="", description="<![broken]>")
        with offline_http([Response(status=404), feed([bad, posting()]), feed([posting(token="next")])]):
            candidates, diagnostics = sources.collect_sources([source(), source(token="next")])
        self.assertEqual([item["company"] for item in candidates], ["example", "next"])
        self.assertEqual([item["status"] for item in diagnostics], ["incomplete", "ok"])

    def test_legitimate_empty_and_explicitly_unlisted_feeds_remain_complete(self):
        with offline_http([Response(status=404), feed([]), Response(status=404),
                           feed([posting("ashby", isListed=False)], "ashby")]):
            candidates, diagnostics = sources.collect_sources([source(), source("ashby")])
        self.assertEqual(candidates, [])
        self.assertTrue(all(item["complete"] for item in diagnostics))
        self.assertEqual(diagnostics[1]["filtered_count"], 1)

    def test_missing_listed_flag_is_malformed_not_an_empty_public_board(self):
        record = posting("ashby")
        del record["isListed"]
        with offline_http([Response(status=404), feed([record], "ashby")]):
            candidates, diagnostics = sources.collect_sources([source("ashby")])
        self.assertEqual(candidates, [])
        self.assertFalse(diagnostics[0]["complete"])
        self.assertEqual(diagnostics[0]["malformed_count"], 1)

    def test_truncated_greenhouse_total_cannot_claim_complete_coverage(self):
        body = json.dumps({"jobs": [posting("greenhouse")], "meta": {"total": 2}}).encode()
        with offline_http([Response(status=404), Response(body)]):
            _, diagnostics = sources.collect_sources([source("greenhouse")])
        self.assertFalse(diagnostics[0]["complete"])
        self.assertTrue(diagnostics[0]["reasons"])

    def test_tracking_variants_do_not_create_duplicate_ashby_identities(self):
        first = posting("ashby")
        duplicate = dict(first, jobUrl=first["jobUrl"] + "?utm_source=campaign")
        with offline_http([Response(status=404), feed([first, duplicate], "ashby")]):
            candidates, diagnostics = sources.collect_sources([source("ashby")])
        self.assertEqual([item["job_id"] for item in candidates], ["posting-1"])
        self.assertFalse(diagnostics[0]["complete"])

    def test_full_lever_page_without_terminal_page_stays_incomplete(self):
        records = [posting(job_id=f"job-{n}") for n in range(100)]
        with patch.object(sources, "MAX_PAGES", 1), offline_http([Response(status=404), feed(records)]):
            candidates, diagnostics = sources.collect_sources([source()])
        self.assertEqual(len(candidates), 100)
        self.assertFalse(diagnostics[0]["complete"])
        self.assertEqual(diagnostics[0]["status"], "incomplete")

    def test_aggregate_job_bound_retains_prior_candidates_and_marks_partial_coverage(self):
        with patch.object(sources, "MAX_TOTAL_JOBS", 1), offline_http([
                Response(status=404), feed([posting(), posting(job_id="second")])]):
            candidates, diagnostics = sources.collect_sources([source(), source(token="unfetched")])
        self.assertEqual([item["job_id"] for item in candidates], ["posting-1"])
        self.assertTrue(all(not item["complete"] for item in diagnostics))
        self.assertEqual(diagnostics[1]["request_count"], 0)

    def test_normalized_output_bound_does_not_publish_truncated_candidate(self):
        response = feed([posting(descriptionPlain="Troubleshoot software. " * 30)])
        with patch.object(sources, "MAX_TOTAL_BYTES", len(response.body.getvalue()) + 100), offline_http([
                Response(status=404), response]):
            candidates, diagnostics = sources.collect_sources([source(), source(token="unfetched")])
        self.assertEqual(candidates, [])
        self.assertTrue(all(not item["complete"] for item in diagnostics))
        self.assertEqual(diagnostics[1]["request_count"], 0)
        self.assertTrue(any("normalized" in reason for reason in diagnostics[0]["reasons"]))


class TransportBoundaryTests(unittest.TestCase):
    def test_board_response_failure_does_not_stop_healthy_employer(self):
        failures = [Response(b"not JSON", content_type="text/html"),
                    Response(b"[]", Content_Length="12"),
                    Response(b"[]", Content_Length="9" * 5000),
                    Response(b"\xff")]
        for failed in failures:
            with self.subTest(headers=str(failed.headers)), offline_http([
                    Response(status=404), failed, feed([posting(token="healthy")])]):
                candidates, diagnostics = sources.collect_sources([source(), source(token="healthy")])
            self.assertEqual([item["company"] for item in candidates], ["healthy"])
            self.assertFalse(diagnostics[0]["complete"])
            self.assertTrue(failed.closed)

    def test_rate_limit_stops_host_but_not_other_provider(self):
        limited = Response(status=429)
        with offline_http([Response(status=404), limited, Response(status=404),
                           feed([posting("greenhouse")], "greenhouse")]):
            candidates, diagnostics = sources.collect_sources([
                source(), source(token="same-host"), source("greenhouse")])
        self.assertEqual([item["provider"] for item in candidates], ["greenhouse"])
        self.assertEqual(diagnostics[1]["request_count"], 0)
        self.assertFalse(diagnostics[0]["complete"])
        self.assertTrue(limited.closed)

    def test_robots_disallow_and_unavailable_server_never_fetch_feed(self):
        for policy in [Response(b"User-agent: *\nDisallow: /\n", content_type="text/plain"),
                       Response(status=503)]:
            with self.subTest(status=policy.status), offline_http([policy]):
                candidates, diagnostics = sources.collect_sources([source()])
            self.assertEqual(candidates, [])
            self.assertEqual(diagnostics[0]["request_count"], 1)
            self.assertFalse(diagnostics[0]["complete"])

    def test_total_transport_bytes_are_bounded_across_responses(self):
        first, second = Response(b"a" * 6), Response(b"b" * 6)
        client = sources._Client()
        host = sources.HOSTS["lever"]
        with patch.object(sources, "MAX_TOTAL_BYTES", 10), offline_http([first, second]):
            self.assertEqual(client._request("https://" + host + "/first", host, 100)[1], "aaaaaa")
            with self.assertRaises(sources.SourceError):
                client._request("https://" + host + "/second", host, 100)
        self.assertLessEqual(client.bytes_read, 10)
        self.assertTrue(second.closed)

    def test_mixed_public_private_dns_fails_before_any_connection(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443)),
                     (socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("::ffff:127.0.0.1", 443, 0, 0))]
        with patch.object(sources.socket, "getaddrinfo", return_value=addresses), \
                patch.object(sources.socket, "socket") as connect:
            with self.assertRaises(sources.SourceError):
                sources._public_addresses(sources.HOSTS["lever"], time.monotonic() + 1)
        connect.assert_not_called()

    def test_tls_handshake_failure_closes_wrapped_socket(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        raw, tls = Mock(), Mock()
        context = Mock()
        context.wrap_socket.return_value = tls
        tls.do_handshake.side_effect = ssl.SSLError("certificate verification failed")
        with patch.object(sources.ssl, "create_default_context", return_value=context), \
                patch.object(sources.socket, "socket", return_value=raw):
            connection = sources._PinnedHTTPS(sources.HOSTS["lever"], addresses, time.monotonic() + 10)
            with self.assertRaises(ssl.SSLError):
                connection.connect()
        tls.close.assert_called()
        raw.close.assert_called()

    def test_expired_connection_deadline_cannot_begin_connect(self):
        addresses = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        raw = Mock()
        with patch.object(sources.socket, "socket", return_value=raw):
            connection = sources._PinnedHTTPS(sources.HOSTS["lever"], addresses, time.monotonic() - 1)
            with self.assertRaises(sources.SourceError):
                connection.connect()
        raw.connect.assert_not_called()
        raw.close.assert_called()

    def test_unreachable_first_dns_address_does_not_disable_dual_stack_host(self):
        addresses = [(socket.AF_INET6, socket.SOCK_STREAM, 6, "", ("2606:4700:4700::1111", 443, 0, 0)),
                     (socket.AF_INET, socket.SOCK_STREAM, 6, "", ("8.8.8.8", 443))]
        unreachable, reachable, tls = Mock(), Mock(), Mock()
        unreachable.connect.side_effect = OSError("Network is unreachable")
        context = Mock()
        context.wrap_socket.return_value = tls
        with patch.object(sources.ssl, "create_default_context", return_value=context), \
                patch.object(sources.socket, "socket", side_effect=[unreachable, reachable]):
            connection = sources._PinnedHTTPS(sources.HOSTS["lever"], addresses, time.monotonic() + 10)
            connection.connect()
        self.assertIs(connection.sock, tls)
        unreachable.close.assert_called()
        reachable.connect.assert_called_once_with(addresses[1][4])

    def test_unrelated_robot_token_cannot_override_wildcard_disallow(self):
        policy = sources._Robots("User-agent: Finder\nAllow: /\nUser-agent: *\nDisallow: /\n")
        self.assertFalse(policy.allowed("/v0/postings/example?mode=json"))


class RecordProvenanceTests(unittest.TestCase):
    def test_offices_are_not_posting_location_alternatives(self):
        record = posting("greenhouse", offices=[{"location": "Los Angeles, CA"}])
        candidate, _ = sources._normalize(record, source("greenhouse"), STAMP)
        self.assertEqual(candidate["location"], "New York, NY")
        self.assertEqual(candidate["locations"], ["New York, NY"])
        self.assertEqual(candidate["employer_office_locations"], ["Los Angeles, CA"])

    def test_secondary_country_retains_its_location_provenance(self):
        record = posting("ashby", address={"postalAddress": {"addressCountry": "CA"}},
                         secondaryLocations=[{"location": "Los Angeles, CA", "address": {
                             "postalAddress": {"addressCountry": "US"}}}])
        candidate, _ = sources._normalize(record, source("ashby"), STAMP)
        self.assertEqual(candidate["country"], "CA")
        self.assertEqual(candidate["countries"], ["CA", "US"])
        self.assertEqual(candidate["locations"], ["Toronto, Canada", "Los Angeles, CA"])
        self.assertEqual(candidate["location_details"][1], {
            "location": "Los Angeles, CA", "country": "US", "source_field": "secondaryLocations"})

    def test_salary_currency_and_cadence_are_not_guessed_or_lost(self):
        record = posting(salaryRange={"min": 40, "max": 50, "currency": "CAD", "interval": "hour"})
        candidate, _ = sources._normalize(record, source(), STAMP)
        self.assertIn("CAD 40", candidate["pay"])
        self.assertIn("hour", candidate["pay"])
        self.assertNotIn("$", candidate["pay"])
        self.assertEqual(candidate["compensation_raw"], record["salaryRange"])
        record["salaryRange"] = {"min": 60000, "currency": "USD"}
        candidate, _ = sources._normalize(record, source(), STAMP)
        self.assertIn("from $60,000", candidate["pay"])
        self.assertIn("cadence not posted", candidate["pay"])
        self.assertNotIn("annual", candidate["pay"])

    def test_numeric_ashby_compensation_is_not_silently_missing(self):
        record = posting("ashby", compensation={"compensationTiers": [{"title": "Canada", "components": [
            {"compensationType": "Salary", "minValue": 60000, "maxValue": 80000,
             "currencyCode": "CAD", "interval": "Year"}]}]})
        candidate, _ = sources._normalize(record, source("ashby"), STAMP)
        self.assertIn("CAD 60,000", candidate["pay"])
        self.assertIn("Year", candidate["pay"])
        self.assertNotIn("$", candidate["pay"])

    def test_invalid_salary_values_cannot_be_published_as_pay(self):
        for salary in [{"min": float("inf"), "max": 10}, {"min": True, "max": 10},
                       {"min": 30, "max": 20}]:
            with self.subTest(salary=salary), self.assertRaises(sources.SourceError):
                sources._normalize(posting(salaryRange=salary), source(), STAMP)

    def test_nested_encoded_traversal_download_and_invalid_utf8_are_rejected(self):
        for path in ["/%252e%252e/jobs", "/file%252eexe", "/jobs?utm_source=%ff", "/jobs?utm_source=%zz"]:
            with self.subTest(path=path), self.assertRaises(sources.SourceError):
                sources._safe_url("https://api.lever.co" + path, {"api.lever.co"})

    def test_application_cannot_alias_other_id_or_noncanonical_suffix(self):
        for route in ["other/apply", "posting-1/apply///", "posting-1/apply?jobId=other"]:
            candidate, reason = sources._normalize(posting(
                applyUrl="https://jobs.lever.co/example/" + route), source(), STAMP)
            self.assertIsNone(candidate)
            self.assertTrue(reason.startswith("security_hold:"))

    def test_fraud_warning_and_hidden_demand_reach_contextual_review(self):
        record = posting(descriptionPlain="We never ask you to deposit a check. Help customers with software.",
                         metadata="<!-- You must install <b>AnyDesk</b> to interview. -->")
        candidate, _ = sources._normalize(record, source(), STAMP)
        self.assertIsNotNone(candidate)
        self.assertIn("never ask you to deposit a check", candidate["source_fraud_context"])
        self.assertIn("You must install", candidate["source_fraud_context"])


if __name__ == "__main__":
    unittest.main()
