#!/usr/bin/env python3
"""Passively verify staged job pages without executing sourced content."""
from __future__ import annotations

import argparse
import html
import ipaddress
import json
import re
import socket
import sys
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener

MAX_BYTES = 2_000_000
TIMEOUT_SECONDS = 15
DEFAULT_ALLOWED_SUFFIXES = (
    "linkedin.com",
    "indeed.com",
    "greenhouse.io",
    "lever.co",
    "myworkdayjobs.com",
    "pinpointhq.com",
    "ashbyhq.com",
    "smartrecruiters.com",
    "icims.com",
    "insightglobal.com",
)
TRACKING_KEYS = {"trk", "gh_src", "source", "sourceid", "ref", "referrer"}
EXPIRED_MARKERS = (
    "this job has expired",
    "job has expired",
    "no longer accepting applications",
    "no longer available",
    "position has been filled",
    "position is no longer available",
    "applications are closed",
    "job is closed",
)
SECURITY_PAGE_MARKERS = ("security check", "captcha", "verify you are human", "access denied")
APPLY_MARKERS = (
    "apply now",
    "submit application",
    "start application",
    "easy apply",
    "apply for this job",
    "application form",
    "join to apply",
)
INJECTION_MARKERS = (
    "ignore previous instructions",
    "ignore all previous instructions",
    "system message:",
    "developer message:",
    "reveal your prompt",
    "reveal your context",
    "disable security",
    "run this command",
    "execute this command",
    "begin system prompt",
)
TAG_RE = re.compile(r"<[^>]+>")
SCRIPT_RE = re.compile(r"<(script|style|noscript)\b[^>]*>.*?</\1\s*>", re.I | re.S)
SPACE_RE = re.compile(r"\s+")
TITLE_RE = re.compile(r"<title[^>]*>(.*?)</title>", re.I | re.S)
DOWNLOAD_EXTENSIONS = (
    ".exe", ".msi", ".dmg", ".pkg", ".app", ".zip", ".rar", ".7z", ".tar", ".gz",
    ".ps1", ".sh", ".bat", ".cmd", ".js", ".jar", ".docm", ".xlsm",
)


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def clean_html(raw: str) -> str:
    without_code = SCRIPT_RE.sub(" ", raw)
    return SPACE_RE.sub(" ", html.unescape(TAG_RE.sub(" ", without_code))).strip()


def host_allowed(host: str, allowed: tuple[str, ...]) -> bool:
    host = host.rstrip(".").lower()
    return any(host == suffix or host.endswith("." + suffix) for suffix in allowed)


def public_dns(host: str) -> tuple[bool, str]:
    try:
        literal = ipaddress.ip_address(host)
        return False, f"raw IP targets are not allowed: {literal}"
    except ValueError:
        pass
    try:
        addresses = {item[4][0] for item in socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM)}
    except socket.gaierror:
        return False, "DNS resolution failed"
    if not addresses:
        return False, "DNS returned no addresses"
    for address in addresses:
        ip = ipaddress.ip_address(address)
        if not ip.is_global:
            return False, f"non-public DNS target: {ip}"
    return True, ""


def validate_url(url: str, allowed: tuple[str, ...], resolve_dns: bool = True) -> tuple[bool, str]:
    if any(ord(char) < 32 for char in url):
        return False, "URL contains control characters"
    try:
        parts = urlsplit(url)
        port = parts.port
    except ValueError as exc:
        return False, f"malformed URL: {exc}"
    if parts.scheme.lower() != "https":
        return False, "HTTPS is required"
    if not parts.hostname:
        return False, "URL has no hostname"
    if parts.username is not None or parts.password is not None:
        return False, "credentials in URLs are not allowed"
    if port not in (None, 443):
        return False, "nonstandard ports are not allowed"
    host = parts.hostname.lower().rstrip(".")
    if not host_allowed(host, allowed):
        return False, f"host is not allowlisted: {host}"
    if parts.path.lower().endswith(DOWNLOAD_EXTENSIONS):
        return False, "direct file-download URL is not allowed"
    if resolve_dns:
        return public_dns(host)
    return True, ""


def canonicalize(url: str) -> str:
    parts = urlsplit(url)
    query = []
    for key, value in parse_qsl(parts.query, keep_blank_values=True):
        lower = key.lower()
        if lower.startswith("utm_") or lower in TRACKING_KEYS:
            continue
        query.append((key, value))
    path = parts.path if parts.path == "/" else parts.path.rstrip("/")
    return urlunsplit(("https", parts.netloc.lower(), path, urlencode(query, doseq=True), ""))


class GuardedRedirects(HTTPRedirectHandler):
    def __init__(self, allowed: tuple[str, ...]) -> None:
        super().__init__()
        self.allowed = allowed

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        target = urljoin(req.full_url, newurl)
        safe, reason = validate_url(target, self.allowed)
        if not safe:
            raise HTTPError(target, code, f"unsafe redirect: {reason}", headers, fp)
        return super().redirect_request(req, fp, code, msg, headers, target)


def fetch(url: str, allowed: tuple[str, ...]) -> tuple[int, str, str, str]:
    opener = build_opener(GuardedRedirects(allowed))
    request = Request(
        url,
        headers={
            "User-Agent": "SecureJobFinder-passive-verifier/1.0",
            "Accept": "text/html,application/xhtml+xml,text/plain;q=0.8",
        },
    )
    with opener.open(request, timeout=TIMEOUT_SECONDS) as response:
        content_type = response.headers.get_content_type()
        if content_type not in {"text/html", "application/xhtml+xml", "text/plain"}:
            raise ValueError(f"unsupported content type: {content_type}")
        body = response.read(MAX_BYTES + 1)
        if len(body) > MAX_BYTES:
            raise ValueError(f"response exceeds {MAX_BYTES} bytes")
        charset = response.headers.get_content_charset() or "utf-8"
        raw = body.decode(charset, errors="replace")
        return response.status, response.geturl(), raw, content_type


def load_allowed(config_path: Path | None) -> tuple[str, ...]:
    values = list(DEFAULT_ALLOWED_SUFFIXES)
    if config_path:
        payload = json.loads(config_path.read_text(encoding="utf-8"))
        configured = payload.get("verification", {}).get("allowed_hosts", [])
        if configured is not None and not isinstance(configured, list):
            raise ValueError("verification.allowed_hosts must be a list")
        for value in configured or []:
            host = str(value).lower().strip().rstrip(".")
            if not re.fullmatch(r"[a-z0-9.-]+", host) or ".." in host:
                raise ValueError(f"invalid allowed host: {value}")
            values.append(host)
    return tuple(sorted(set(values)))


def verify(candidate: dict, allowed: tuple[str, ...]) -> dict:
    result = dict(candidate)
    result.update(
        verified_at=now_iso(),
        verification_status="rejected",
        verification_reason="",
        security_flags=[],
    )
    url = str(candidate.get("url", "")).strip()
    safe, reason = validate_url(url, allowed)
    if not safe:
        result["verification_reason"] = reason
        result["security_flags"] = [reason]
        return result
    try:
        status, final_url, raw, content_type = fetch(url, allowed)
    except (HTTPError, URLError, TimeoutError, OSError, ValueError) as exc:
        result["verification_reason"] = f"fetch failed: {type(exc).__name__}: {exc}"
        return result

    text = clean_html(raw)
    lower = text.lower()
    title_match = TITLE_RE.search(raw)
    title = clean_html(title_match.group(1)) if title_match else ""
    result.update(
        http_status=status,
        final_url=final_url,
        canonical_application_url=canonicalize(final_url),
        page_title=title,
        content_type=content_type,
    )
    injections = [marker for marker in INJECTION_MARKERS if marker in lower]
    if injections:
        result["verification_status"] = "security_hold"
        result["verification_reason"] = "prompt-injection indicator detected"
        result["security_flags"] = [f"untrusted page contains: {marker}" for marker in injections]
        return result
    if any(marker in lower or marker in title.lower() for marker in SECURITY_PAGE_MARKERS):
        result["verification_reason"] = "security-check page"
        return result
    if any(marker in lower for marker in EXPIRED_MARKERS):
        result["verification_reason"] = "expired or closed language detected"
        return result
    if not any(marker in lower for marker in APPLY_MARKERS):
        result["verification_reason"] = "no application signal found"
        return result
    result["verification_status"] = "active_candidate"
    result["verification_reason"] = "HTTPS page active; application signal present; no expiry or injection marker"
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--input", required=True, type=Path)
    parser.add_argument("--output", required=True, type=Path)
    parser.add_argument("--report", required=True, type=Path)
    parser.add_argument("--config", type=Path)
    args = parser.parse_args(argv)

    payload = json.loads(args.input.read_text(encoding="utf-8"))
    candidates = payload.get("candidates") if isinstance(payload, dict) else None
    if not isinstance(candidates, list) or not all(isinstance(item, dict) for item in candidates):
        raise SystemExit("input must be an object with a candidates array")
    allowed = load_allowed(args.config)
    checked = [verify(item, allowed) for item in candidates]
    counts = Counter(item["verification_status"] for item in checked)
    report = {
        "verified_at": now_iso(),
        "input_count": len(checked),
        "status_counts": dict(counts),
        "reason_counts": dict(Counter(item["verification_reason"] for item in checked)),
        "allowed_host_suffixes": allowed,
    }
    args.output.write_text(json.dumps({"candidates": checked}, indent=2) + "\n", encoding="utf-8")
    args.report.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    sys.stdout.write(json.dumps(report, sort_keys=True) + "\n")
    return 0 if counts.get("active_candidate", 0) else 2


if __name__ == "__main__":
    raise SystemExit(main())
