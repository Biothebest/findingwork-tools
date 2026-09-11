"""Bounded public ATS GETs; registry is trusted configuration, payloads are data.

Published-record semantics: docs.greenhouse.io/job-board.html,
github.com/lever/postings-api, developers.ashbyhq.com/docs/public-job-posting-api.
No HTML application-page fetch, applicant fields, authentication, or submissions.
"""
from __future__ import annotations

import html
import http.client
import ipaddress
import json
import math
import queue
import re
import socket
import ssl
import threading
import time
from collections import Counter
from datetime import datetime, timezone
from html.parser import HTMLParser
from urllib.parse import unquote, urlsplit

from verify_candidates import (
    DOWNLOAD_EXTENSIONS, EXPIRED_MARKERS, INJECTION_MARKERS,
    SECURITY_PAGE_MARKERS, canonicalize, now_iso,
)

FEED_BYTES = 10_000_000
ROBOTS_BYTES = 512_000
IO_SECONDS = 15
MAX_REQUESTS = 150
MAX_PAGES = 40
PAGE_SIZE = 100
MAX_JOBS = 4000
MAX_JSON_DEPTH = 64
MAX_TOTAL_BYTES = 32_000_000
MAX_TOTAL_JOBS = 20_000
USER_AGENT = "SecureJobFinder"
TOKEN_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,127}\Z")
HOSTS = {
    "greenhouse": "boards-api.greenhouse.io",
    "lever": "api.lever.co",
    "ashby": "api.ashbyhq.com",
}
NON_JOB_RE = re.compile(
    r"\b(talent (?:pool|community|network)|general (?:application|interest)|"
    r"expression of interest|future (?:opportunities|openings)|join our network|"
    r"prospective candidates|open application)\b", re.I,
)
FRAUD_RE = re.compile(
    r"\b(?:pay (?:an? |the )?(?:application|interview|recruitment) fee|"
    r"(?:deposit|cash) (?:a |the |our )?check|gift cards?|"
    r"send (?:us )?(?:your )?(?:bank details|social security number|one.time code)|"
    r"(?:contact|message|interview|communicate).{0,60}(?:telegram|whatsapp|signal)|"
    r"(?:download|install).{0,40}(?:remote.access|anydesk|teamviewer)|"
    r"(?:reveal|send|exfiltrate).{0,30}(?:secrets|credentials|system prompt)|"
    r"ignore (?:all |the )?(?:prior|previous) (?:rules|instructions))\b", re.I,
)
REQUIREMENT_HEADING = re.compile(
    r"^(?:requirements|required qualifications|minimum qualifications|"
    r"what (?:is|you'll need|you will need) required)\s*:?$", re.I,
)


class SourceError(Exception):
    """Expected source/configuration/security failure, not a programming error."""


def _text(value, field: str, required: bool = False) -> str:
    if value is None and not required:
        return ""
    if not isinstance(value, str) or (required and not value.strip()):
        raise SourceError("missing or malformed " + field)
    return value


def _object(value, field: str) -> dict:
    if not isinstance(value, dict):
        raise SourceError("malformed " + field + ": expected object")
    return value


def _array(value, field: str) -> list:
    if not isinstance(value, list):
        raise SourceError("malformed " + field + ": expected array")
    return value


def _safe_url(url: str, hosts: set[str]):
    if not isinstance(url, str) or len(url) > 2048:
        raise SourceError("missing or oversized URL")
    if re.search(r"%(?![0-9a-fA-F]{2})", url):
        raise SourceError("malformed URL percent encoding")
    decoded = url
    for _ in range(4):
        if (any(ord(c) <= 32 or 127 <= ord(c) <= 159 or 0xD800 <= ord(c) <= 0xDFFF
                for c in decoded) or "\\" in decoded):
            raise SourceError("URL contains whitespace, controls, surrogate, or backslash")
        try:
            next_value = unquote(decoded, errors="strict")
        except UnicodeError as exc:
            raise SourceError("URL encoding is not UTF-8") from exc
        if next_value == decoded:
            break
        decoded = next_value
    else:
        raise SourceError("excessively nested URL encoding")
    try:
        parts = urlsplit(url)
        port = parts.port
        decoded_path = urlsplit(decoded).path
    except ValueError as exc:
        raise SourceError("malformed URL") from exc
    if (parts.scheme != "https" or parts.hostname not in hosts or
            parts.username is not None or parts.password is not None or
            port not in (None, 443) or parts.fragment):
        raise SourceError("URL outside exact HTTPS host/credential/port policy")
    if decoded_path.lower().endswith(DOWNLOAD_EXTENSIONS):
        raise SourceError("download destination rejected")
    if any(segment in (".", "..") for segment in decoded_path.split("/")):
        raise SourceError("URL path traversal rejected")
    try:
        ipaddress.ip_address(parts.hostname)
    except ValueError:
        return parts
    raise SourceError("raw IP destination rejected")


def _endpoint(source: dict) -> str:
    provider = source.get("provider")
    token = source.get("board_token")
    if not isinstance(provider, str) or provider not in HOSTS or not isinstance(token, str) or not TOKEN_RE.fullmatch(token):
        raise SourceError("unsupported provider or invalid board token")
    _text(source.get("company"), "company", required=True)
    if provider == "greenhouse":
        return f"https://{HOSTS[provider]}/v1/boards/{token}/jobs?content=true"
    if provider == "lever":
        return f"https://{HOSTS[provider]}/v0/postings/{token}?mode=json"
    return f"https://{HOSTS[provider]}/posting-api/job-board/{token}?includeCompensation=true"


def _public_addresses(host: str, deadline: float) -> list:
    # getaddrinfo has no Python timeout. A daemon resolver has a bounded wait;
    # failed hosts are stopped, so a stuck resolver is never retried this run.
    result = queue.Queue(maxsize=1)

    def resolve():
        try:
            result.put(socket.getaddrinfo(host, 443, type=socket.SOCK_STREAM))
        except OSError as exc:
            result.put(exc)

    threading.Thread(target=resolve, daemon=True).start()
    try:
        addresses = result.get(timeout=max(0.001, deadline - time.monotonic()))
    except queue.Empty as exc:
        raise SourceError("DNS deadline exceeded") from exc
    if isinstance(addresses, OSError):
        raise SourceError("DNS resolution failed") from addresses
    if not addresses:
        raise SourceError("DNS returned no addresses")
    for entry in addresses:
        address = ipaddress.ip_address(entry[4][0])
        mapped = getattr(address, "ipv4_mapped", None)
        if not address.is_global or (mapped is not None and not mapped.is_global):
            raise SourceError("DNS returned non-public address")
    return addresses


class _PinnedHTTPS(http.client.HTTPSConnection):
    def __init__(self, host: str, addresses: list, deadline: float):
        super().__init__(host, timeout=IO_SECONDS, context=ssl.create_default_context())
        self.addresses = addresses
        self.deadline = deadline
        self.transport = None
        self.interrupted = threading.Event()

    def connect(self):
        # Try validated addresses only until TCP connects; never retry an HTTP
        # request or a failed TLS handshake. Dual-stack DNS may list an
        # unreachable IPv6 address before a usable IPv4 address on local Wi-Fi.
        for index, (family, socktype, proto, _, sockaddr) in enumerate(self.addresses):
            raw = socket.socket(family, socktype, proto)
            self.transport = raw
            try:
                if self.interrupted.is_set() or time.monotonic() >= self.deadline:
                    raise SourceError("connection deadline exceeded")
                raw.settimeout(max(0.001, self.deadline - time.monotonic()))
                raw.connect(sockaddr)
            except OSError:
                raw.close()
                if index + 1 == len(self.addresses):
                    raise
                continue
            except SourceError:
                raw.close()
                raise
            try:
                self.sock = self._context.wrap_socket(raw, server_hostname=self.host,
                                                     do_handshake_on_connect=False)
                self.transport = self.sock
                if self.interrupted.is_set() or time.monotonic() >= self.deadline:
                    raise SourceError("TLS deadline exceeded")
                self.sock.settimeout(max(0.001, self.deadline - time.monotonic()))
                self.sock.do_handshake()
                return
            except (OSError, ValueError, SourceError):
                self.interrupt()
                raw.close()
                raise
        raise SourceError("no validated connection addresses")

    def interrupt(self):
        self.interrupted.set()
        transport = self.transport
        if transport is not None:
            try:
                transport.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            transport.close()


def _robots_octets(value: str) -> str:
    unreserved = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~"
    value = re.sub(r"%([0-9a-fA-F]{2})", lambda m: (
        chr(int(m[1], 16)) if chr(int(m[1], 16)) in unreserved else "%" + m[1].upper()
    ), value)
    return "".join(c if ord(c) < 128 else "".join("%%%02X" % b for b in c.encode("utf-8"))
                   for c in value)


def _wildcard_prefix(pattern: str, path: str) -> bool:
    # Linear-memory glob matching with a single backtracking point; no regex
    # expansion of attacker-controlled wildcard patterns.
    anchored = pattern.endswith("$")
    pattern = pattern[:-1] if anchored else pattern + "*"
    p = s = 0
    star = -1
    retry = 0
    while s < len(path):
        if p < len(pattern) and pattern[p] == "*":
            star, retry = p, s
            p += 1
        elif p < len(pattern) and pattern[p] == path[s]:
            p += 1
            s += 1
        elif star >= 0:
            retry += 1
            s, p = retry, star + 1
        else:
            return False
    return all(c == "*" for c in pattern[p:])


class _Robots:
    def __init__(self, text: str):
        groups = []
        agents, rules, delays = [], [], []
        has_records = False
        for line in text.splitlines():
            if len(line) > 8192:
                raise SourceError("robots line exceeds parsing bound")
            line = line.split("#", 1)[0].strip()
            if ":" not in line:
                continue
            key, value = (item.strip() for item in line.split(":", 1))
            key = key.lower()
            if key == "user-agent":
                if has_records:
                    groups.append((agents, rules, delays))
                    agents, rules, delays = [], [], []
                    has_records = False
                agents.append(value.lower())
            elif agents:
                has_records = True
                if key in ("allow", "disallow") and value.startswith("/"):
                    rules.append((key == "allow", _robots_octets(value)))
                elif key == "crawl-delay":
                    try:
                        delay = float(value)
                    except ValueError as exc:
                        raise SourceError("invalid robots crawl-delay") from exc
                    if not math.isfinite(delay) or delay < 0:
                        raise SourceError("invalid robots crawl-delay")
                    delays.append(delay)
        groups.append((agents, rules, delays))
        selected = [g for g in groups if USER_AGENT.lower() in g[0]]
        if not selected:
            selected = [g for g in groups if "*" in g[0]]
        self.rules = [rule for _, rules, _ in selected for rule in rules]
        self.delay = max([1.0] + [d for _, _, delays in selected for d in delays])
        if self.delay > 60:
            raise SourceError("robots crawl-delay exceeds run pacing bound; host held")

    def allowed(self, target: str) -> bool:
        path = _robots_octets(target)
        matches = [(len(pattern.rstrip("$").replace("*", "")), allow)
                   for allow, pattern in self.rules if _wildcard_prefix(pattern, path)]
        return max(matches)[1] if matches else True


class _Client:
    def __init__(self):
        self.requests = 0
        self.by_host = Counter()
        self.last_request = {}
        self.stopped = {}
        self.robots = {}
        self.robots_notes = {}
        self.dns = {}
        self.bytes_read = 0
        self.jobs_seen = 0

    def _request(self, url: str, host: str, cap: int, robots: bool = False):
        parts = _safe_url(url, {host})
        if host in self.stopped:
            raise SourceError("host stopped: " + self.stopped[host])
        if self.requests >= MAX_REQUESTS:
            raise SourceError("overall request limit reached")
        # Reserve the one-byte overrun probe without exceeding the run budget.
        cap = min(cap, MAX_TOTAL_BYTES - self.bytes_read - 1)
        if cap <= 0:
            raise SourceError("aggregate response byte bound exhausted; incomplete collection")
        delay = self.robots[host].delay if host in self.robots else 1.0
        time.sleep(max(0.0, self.last_request.get(host, 0) + delay - time.monotonic()))
        deadline = time.monotonic() + IO_SECONDS
        connection = None
        response = None
        timer = None
        # Board-specific payload failures must not poison other employers on
        # the same provider. Robots, network, authentication and rate failures do.
        stop_host_on_error = robots
        try:
            if host not in self.dns:
                self.dns[host] = _public_addresses(host, deadline)
            connection = _PinnedHTTPS(host, self.dns[host], deadline)
            timer = threading.Timer(max(0.001, deadline - time.monotonic()), connection.interrupt)
            timer.daemon = True
            timer.start()
            self.requests += 1
            self.by_host[host] += 1
            self.last_request[host] = time.monotonic()
            target = parts.path + ("?" + parts.query if parts.query else "")
            connection.request("GET", target, headers={
                "User-Agent": USER_AGENT + "/1.0 (passive public job discovery)",
                "Accept": "text/plain" if robots else "application/json",
                "Accept-Encoding": "identity", "Connection": "close",
            })
            response = connection.getresponse()
            status = response.status
            if status in (403, 429) or response.getheader("Retry-After") is not None:
                self.stopped[host] = f"host access/rate limit: HTTP {status}"
                raise SourceError(self.stopped[host])
            if status != 200:
                if robots and (status in (404, 410) or (status == 401 and host == HOSTS["ashby"])):
                    return status, ""
                if not robots and status in (400, 404, 410):
                    # A removed/misconfigured employer board must not disable
                    # unrelated employers sharing this otherwise healthy API.
                    stop_host_on_error = False
                if status == 401 or status >= 500:
                    self.stopped[host] = f"API access/server failure: HTTP {status}"
                raise SourceError(f"HTTP {status}; redirects/authentication/errors are not followed")
            if any(response.getheader(key, "").strip() == "0"
                   for key in ("RateLimit-Remaining", "X-RateLimit-Remaining")):
                self.stopped[host] = "server reports no remaining rate-limit allowance"
            content_type = response.headers.get_content_type()
            allowed = {"text/plain"} if robots else {"application/json"}
            if content_type not in allowed:
                raise SourceError("unsupported content-type: " + content_type)
            if response.getheader("Content-Disposition", "").lower().startswith("attachment"):
                raise SourceError("attachment response rejected")
            if response.getheader("Content-Encoding", "identity").lower() != "identity":
                raise SourceError("compressed response rejected")
            length = response.getheader("Content-Length")
            if length is not None:
                if (not re.fullmatch(r"[0-9]{1,12}", length) or int(length) > cap):
                    raise SourceError("response Content-Length exceeds bound or is malformed")
            body = bytearray()
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise SourceError("response read deadline exceeded")
                connection.transport.settimeout(remaining)
                block = response.read1(min(65536, cap + 1 - len(body)))
                if not block:
                    break
                body.extend(block)
                self.bytes_read += len(block)
                if len(body) > cap:
                    raise SourceError("response byte bound exceeded")
            if time.monotonic() > deadline:
                raise SourceError("response deadline exceeded")
            if length is not None and len(body) != int(length):
                raise SourceError("incomplete response body")
            try:
                text = body.decode("utf-8-sig")
            except UnicodeError as exc:
                raise SourceError("response is not UTF-8") from exc
            return status, text
        except http.client.HTTPException as exc:
            reason = "malformed/incomplete HTTP response: " + type(exc).__name__
            if robots:
                self.stopped[host] = reason
            raise SourceError(reason) from exc
        except OSError as exc:
            reason = "network/TLS failure: " + type(exc).__name__
            self.stopped[host] = reason
            raise SourceError(reason) from exc
        except SourceError as exc:
            if stop_host_on_error:
                self.stopped[host] = str(exc)
            raise
        finally:
            if timer is not None:
                timer.cancel()
            if response is not None:
                response.close()
            if connection is not None:
                connection.close()
                connection.interrupt()

    def get_json(self, url: str, source: dict):
        host = HOSTS[source["provider"]]
        endpoint = _endpoint(source)
        # The only fetchable non-robots paths are generated here, never payload URLs.
        if source["provider"] == "lever":
            if not re.fullmatch(re.escape(endpoint) + r"&skip=\d+&limit=100", url):
                raise SourceError("noncanonical Lever request")
        elif url != endpoint:
            raise SourceError("noncanonical API request")
        if host not in self.robots:
            status, text = self._request("https://" + host + "/robots.txt", host, ROBOTS_BYTES, True)
            if status == 401:
                note = ("unavailable HTTP 401; RFC9309 2.3.1.3 permits only the "
                        "documented public GET posting-api/job-board route; no authorization sent")
            elif status in (404, 410):
                note = f"unavailable HTTP {status}; RFC9309 2.3.1.3; public API GET only"
            else:
                note = "HTTP 200 robots rules enforced"
            self.robots_notes[host] = note
            try:
                self.robots[host] = _Robots(text)
            except SourceError as exc:
                self.stopped[host] = str(exc)
                raise
        parts = urlsplit(url)
        if not self.robots[host].allowed(parts.path + "?" + parts.query):
            raise SourceError("robots disallows API route")
        _, text = self._request(url, host, FEED_BYTES)
        payload = _json_payload(text)
        if isinstance(payload, dict) and any(key in payload for key in ("error", "errors")):
            message = str(payload.get("error", payload.get("errors", ""))).lower()
            if any(term in message for term in ("rate", "limit", "too many", "forbidden", "unauthorized")):
                self.stopped[host] = "API error/rate-limit payload"
            raise SourceError("API returned an error object")
        return payload


def _json_payload(text: str):
    # Bound nesting before the decoder allocates a recursive object graph.
    depth = 0
    quoted = escaped = False
    for char in text:
        if quoted:
            if escaped:
                escaped = False
            elif char == "\\":
                escaped = True
            elif char == '"':
                quoted = False
        elif char == '"':
            quoted = True
        elif char in "[{":
            depth += 1
            if depth > MAX_JSON_DEPTH:
                raise SourceError("excessively nested JSON")
        elif char in "]}":
            depth -= 1

    def number(value):
        if len(value) > 128:
            raise ValueError("oversized JSON number")
        result = float(value) if any(c in value for c in ".eE") else int(value)
        if isinstance(result, float) and not math.isfinite(result):
            raise ValueError("nonfinite JSON number")
        return result

    def pairs(values):
        result = {}
        for key, value in values:
            if key in result:
                raise ValueError("duplicate JSON object key")
            result[key] = value
        return result

    def constant(value):
        raise ValueError("nonfinite JSON number")

    try:
        payload = json.loads(text, parse_constant=constant, parse_float=number,
                             parse_int=number, object_pairs_hook=pairs)
        pending = [payload]
        while pending:
            value = pending.pop()
            if isinstance(value, dict):
                pending.extend(value.keys())
                pending.extend(value.values())
            elif isinstance(value, list):
                pending.extend(value)
            elif isinstance(value, str):
                value.encode("utf-8")  # Reject unpaired surrogates before publication.
    except (ValueError, RecursionError) as exc:
        raise SourceError("malformed or unsafe JSON") from exc
    return payload


class _PlainHTML(HTMLParser):
    BLOCKS = {"p", "div", "li", "ul", "ol", "br", "hr", "h1", "h2", "h3", "h4", "h5", "h6", "section", "tr"}
    HIDDEN = {"script", "style", "noscript", "template"}

    def __init__(self, include_hidden: bool = False):
        super().__init__(convert_charrefs=True)
        self.parts = []
        self.hidden = []
        self.include_hidden = include_hidden

    def handle_starttag(self, tag, attrs):
        if self.include_hidden:
            self.parts.extend("\n" + value + "\n" for _, value in attrs if value)
            if tag in self.BLOCKS or tag in self.HIDDEN:
                self.parts.append("\n")
        elif tag in self.HIDDEN:
            self.hidden.append(tag)
        elif not self.hidden and tag in self.BLOCKS:
            self.parts.append("\n")

    def handle_endtag(self, tag):
        if self.hidden:
            if tag == self.hidden[-1]:
                self.hidden.pop()
        elif tag in self.BLOCKS or (self.include_hidden and tag in self.HIDDEN):
            self.parts.append("\n")

    def handle_comment(self, data):
        if self.include_hidden:
            self.parts.extend(("\n", data, "\n"))

    def handle_data(self, data):
        if not self.hidden:
            self.parts.append(data)


def _plain(raw: str, encoded: bool = False, include_hidden: bool = False) -> str:
    if encoded:
        for _ in range(2):
            raw = html.unescape(raw)
    parser = _PlainHTML(include_hidden=include_hidden)
    try:
        parser.feed(raw)
        parser.close()
    except (AssertionError, ValueError, RecursionError, NotImplementedError) as exc:
        raise SourceError("malformed HTML description or metadata") from exc
    return "\n".join(re.sub(r"[^\S\n]+", " ", line).strip()
                     for line in "".join(parser.parts).splitlines() if line.strip())


def _description(record: dict, plain: str, rich: str) -> str:
    value = _text(record.get(plain), plain)
    return value if value.strip() else _plain(_text(record.get(rich), rich))


def _locations(primary: str, extra: list) -> list[str]:
    result = []
    for item in [primary] + extra:
        value = _text(item, "location")
        if value and value not in result:
            result.append(value)
    return result


def _salary_range(lo, hi, currency: str, interval: str) -> str:
    for value in (lo, hi):
        if value is not None and (isinstance(value, bool) or not isinstance(value, (int, float))
                                  or not math.isfinite(value) or value < 0):
            raise SourceError("malformed salary amount")
    if lo is None and hi is None:
        return ""
    if lo is not None and hi is not None and lo > hi:
        raise SourceError("salary minimum exceeds maximum")
    prefix = "$" if currency == "USD" else (currency or "Currency not posted") + " "
    if lo is None:
        amount = f"up to {prefix}{hi:,}"
    elif hi is None:
        amount = f"from {prefix}{lo:,}"
    else:
        amount = f"{prefix}{lo:,}–{prefix}{hi:,}"
    return amount + " / " + (interval or "cadence not posted")


def _address_country(value) -> str:
    address = _object({} if value is None else value, "address")
    postal = address.get("postalAddress")
    if postal is not None:
        return _text(_object(postal, "postalAddress").get("addressCountry"), "addressCountry")
    return _text(address.get("addressCountry"), "addressCountry")


def _pay(record: dict, provider: str, description: str) -> str:
    parts = []
    if provider == "lever":
        summary = _description(record, "salaryDescriptionPlain", "salaryDescription")
        if summary:
            parts.append(summary)
        value = record.get("salaryRange")
        if value is not None:
            value = _object(value, "salaryRange")
            lo, hi = value.get("min"), value.get("max")
            currency = _text(value.get("currency"), "salary currency")
            cadence = _text(value.get("interval"), "salary interval")
            amount = _salary_range(lo, hi, currency, cadence)
            if amount:
                parts.append(amount)
    elif provider == "ashby" and record.get("compensation") is not None:
        comp = _object(record["compensation"], "compensation")
        summary = _text(comp.get("compensationTierSummary"), "compensation summary")
        if not summary:
            summary = _text(comp.get("scrapeableCompensationSalarySummary"), "salary summary")
        if summary:
            parts.append(summary)
        for tier in _array(comp.get("compensationTiers", []), "compensation tiers"):
            tier = _object(tier, "compensation tier")
            title = _text(tier.get("title"), "tier title")
            for component in _array(tier.get("components", []), "compensation components"):
                component = _object(component, "compensation component")
                text = _text(component.get("summary"), "component summary")
                currency = _text(component.get("currencyCode"), "currency")
                interval = _text(component.get("interval"), "interval")
                amount = _salary_range(component.get("minValue"), component.get("maxValue"), currency, interval)
                kind = _text(component.get("compensationType"), "compensation type")
                if text or amount:
                    parts.append(" | ".join(v for v in (title, kind, text, amount, currency, interval) if v))
    if not parts:
        # Preserve posted compensation lines, never infer annual/hourly cadence.
        parts = [line.strip() for line in description.splitlines()
                 if re.search(r"(?:[$€£]\s*\d|\b(?:USD|CAD|EUR|GBP)\s*\d)", line)]
    return "\n".join(dict.fromkeys(parts)) or "Not posted"


def _application(record: dict, provider: str, token: str, job_id: str) -> tuple[str, str]:
    if provider == "greenhouse":
        url = _text(record.get("absolute_url"), "absolute_url", True)
        parts = _safe_url(url, {"boards.greenhouse.io", "job-boards.greenhouse.io"})
        if urlsplit(canonicalize(url)).query:
            raise SourceError("application URL contains non-tracking query parameters")
        if parts.path.removesuffix("/") != f"/{token}/jobs/{job_id}":
            raise SourceError("Greenhouse hosted route does not match board/job identity")
        return url, url
    host = "jobs.lever.co" if provider == "lever" else "jobs.ashbyhq.com"
    url = _text(record.get("hostedUrl" if provider == "lever" else "jobUrl"), "hosted job URL", True)
    apply = _text(record.get("applyUrl"), "applyUrl", True)
    job_parts = _safe_url(url, {host})
    apply_parts = _safe_url(apply, {host})
    if urlsplit(canonicalize(url)).query or urlsplit(canonicalize(apply)).query:
        raise SourceError("application URL contains non-tracking query parameters")
    application_suffix = "application" if provider == "ashby" else "apply"
    if (job_parts.path.removesuffix("/") != f"/{token}/{job_id}" or
            apply_parts.path.removesuffix("/") != f"/{token}/{job_id}/{application_suffix}"):
        raise SourceError("hosted application route does not match board/job identity")
    return url, apply


def _normalize(record: dict, source: dict, fetched_at: str) -> tuple[dict | None, str]:
    provider, token = source["provider"], source["board_token"]
    title = _text(record.get("text" if provider == "lever" else "title"), "title", True)
    if NON_JOB_RE.search(title):
        return None, "non-job/talent-pool title"
    if provider == "greenhouse" and record.get("internal_job_id") is None:
        return None, "Greenhouse prospect post (no internal_job_id)"
    if provider == "ashby":
        if record.get("isListed") is False:
            return None, "Ashby isListed is false"
        if record.get("isListed") is not True:
            raise SourceError("Ashby isListed must be a boolean")
    requirements = []
    country = ""
    location_details = []
    if provider == "greenhouse":
        identity = record.get("id")
        if not isinstance(identity, int) or isinstance(identity, bool) or identity <= 0:
            raise SourceError("invalid Greenhouse job ID")
        job_id = str(identity)
        description = _plain(_text(record.get("content"), "content"), encoded=True)
        location = _text(_object(record.get("location", {}), "location").get("name"), "location.name")
        offices = _array(record.get("offices", []), "offices")
        locations = _locations(location, [])
        office_locations = _locations("", [_text(_object(o, "office").get("location"), "office.location") for o in offices])
        location_details = [{"location": value, "country": "", "source_field": "location.name"}
                            for value in locations]
        posted = _text(record.get("first_published"), "first_published")
        workplace = "Not posted"
    elif provider == "lever":
        job_id = _text(record.get("id"), "id", True)
        categories = _object(record.get("categories", {}), "categories")
        location = _text(categories.get("location"), "location")
        locations = _locations(location, _array(categories.get("allLocations", []), "allLocations"))
        country = _text(record.get("country"), "country")
        location_details = [{"location": value, "country": country if value == location else "",
                             "source_field": "categories.location" if value == location else "categories.allLocations"}
                            for value in locations]
        pieces = [_description(record, "descriptionPlain", "description")]
        if not pieces[0]:
            pieces = [_description(record, "openingPlain", "opening"), _description(record, "descriptionBodyPlain", "descriptionBody")]
        for entry in _array(record.get("lists", []), "lists"):
            entry = _object(entry, "list entry")
            label = _text(entry.get("text"), "list label")
            content = _plain(_text(entry.get("content"), "list content"))
            pieces.append(label + "\n" + content)
            if REQUIREMENT_HEADING.fullmatch(label.strip()):
                requirements.append(content)
        pieces.extend([_description(record, "additionalPlain", "additional"),
                       _description(record, "salaryDescriptionPlain", "salaryDescription")])
        description = "\n\n".join(piece for piece in pieces if piece.strip())
        posted = ""  # Lever's published API contract does not expose a posting date.
        workplace = _text(record.get("workplaceType"), "workplaceType") or "Not posted"
    else:
        job_url = _text(record.get("jobUrl"), "jobUrl", True)
        parts = _safe_url(job_url, {"jobs.ashbyhq.com"})
        job_id = parts.path.rstrip("/").rsplit("/", 1)[-1]
        if record.get("id") is not None and record["id"] != job_id:
            raise SourceError("Ashby explicit ID conflicts with hosted job ID")
        description = _description(record, "descriptionPlain", "descriptionHtml")
        location = _text(record.get("location"), "location")
        secondary = _array(record.get("secondaryLocations", []), "secondaryLocations")
        locations = _locations(location, [_text(_object(o, "secondary location").get("location"), "secondary location") for o in secondary])
        country = _address_country(record.get("address"))
        location_details = [{"location": location, "country": country, "source_field": "location"}]
        for item in secondary:
            location_details.append({"location": _text(item.get("location"), "secondary location"),
                                     "country": _address_country(item.get("address")),
                                     "source_field": "secondaryLocations"})
        posted = _text(record.get("publishedAt"), "publishedAt")
        workplace = _text(record.get("workplaceType"), "workplaceType") or ("Remote" if record.get("isRemote") is True else "Not posted")
    if not ID_RE.fullmatch(job_id):
        raise SourceError("invalid job identifier")
    if not description.strip() or not re.search(r"[A-Za-z]{3}", description):
        raise SourceError("missing usable job description")
    lower = (title + "\n" + description).lower()
    # Also inspect inert HTML/metadata text for indicators hidden from plain text.
    raw_lower = html.unescape(html.unescape(json.dumps(record, ensure_ascii=False))).lower()
    flags = [marker for marker in INJECTION_MARKERS if marker in lower or marker in raw_lower]
    if flags:
        return None, "security_hold: prompt-injection indicator"
    if any(marker in lower for marker in EXPIRED_MARKERS):
        return None, "expired/closed language"
    if any(marker in lower for marker in SECURITY_PAGE_MARKERS):
        return None, "security-check language"
    for key in ("application_deadline", "expiresAt", "validThrough"):
        value = record.get(key)
        if value:
            try:
                expiry = datetime.fromisoformat(_text(value, key).replace("Z", "+00:00"))
            except ValueError as exc:
                raise SourceError("unparseable expiry date") from exc
            if expiry.tzinfo is None:
                expiry = expiry.replace(tzinfo=timezone.utc)
            if expiry <= datetime.now(timezone.utc):
                return None, "explicit application deadline has passed"
    try:
        url, application = _application(record, provider, token, job_id)
    except SourceError as exc:
        return None, "security_hold: " + str(exc)
    result = {
        "company": source["company"], "title": title, "location": location or "Not posted",
        "locations": locations, "url": url, "application_url": application,
        "source_url": source["feed_url"], "source_type": "official_ats",
        "job_id": job_id, "description_text": description, "found_at": fetched_at,
        "posted_date_text": posted or "Not posted", "pay": _pay(record, provider, description),
        "workplace_type": workplace, "provider": provider, "board_token": token,
        "source_lanes": source.get("lanes", []), "verification_status": "active_candidate",
        "verification_method": provider + "_published_public_api",
        "verification_reason": (
            "Current published public API record; explicit ATS-hosted application route matches "
            "board and job identity; usable description; no expiry/prompt-injection indicators. "
            "Recruiter-fraud language requires contextual fit-stage review. " +
            ("Greenhouse absolute_url is the documented hosted job/application page; "
             "prospect posts excluded; no invented apply text or applicant-data request."
             if provider == "greenhouse" else
             "Explicit hosted job URL and applyUrl are first-class published API proof" +
             ("; Ashby isListed=true." if provider == "ashby" else "."))
        ),
        "verified_at": fetched_at, "canonical_application_url": canonicalize(application),
        "http_status": 200, "content_type": "application/json", "security_flags": [],
        "location_details": location_details,
    }
    if country:
        result["country"] = country
    if requirements:
        result["requirements_text"] = "\n\n".join(requirements)
    result["countries"] = list(dict.fromkeys(item["country"] for item in location_details if item["country"]))
    if provider == "greenhouse":
        result["employer_office_locations"] = office_locations
    compensation = record.get("salaryRange" if provider == "lever" else "compensation")
    if provider != "greenhouse" and compensation is not None:
        result["compensation_raw"] = compensation
    # Matching owns contextual warning/negation interpretation. Preserve
    # metadata and hidden HTML evidence as text with actual clause boundaries.
    contexts = []
    pending = [record]
    while pending:
        value = pending.pop()
        if isinstance(value, dict):
            pending.extend(value.values())
        elif isinstance(value, list):
            pending.extend(value)
        elif isinstance(value, str):
            text = html.unescape(html.unescape(value))
            if "<" in text:
                text = _plain(text, include_hidden=True)
            if FRAUD_RE.search(text):
                contexts.append(text)
    if contexts:
        result["source_fraud_context"] = "\n\n".join(dict.fromkeys(contexts))
    if provider == "greenhouse" and record.get("updated_at"):
        result["updated_date_text"] = _text(record["updated_at"], "updated_at")
    return result, ""


def _records(client: _Client, source: dict, diagnostic: dict):
    endpoint = _endpoint(source)
    seen = set()
    for page in range(MAX_PAGES if source["provider"] == "lever" else 1):
        url = endpoint + f"&skip={page * PAGE_SIZE}&limit={PAGE_SIZE}" if source["provider"] == "lever" else endpoint
        payload = client.get_json(url, source)
        diagnostic["pages_fetched"] += 1
        if source["provider"] == "lever":
            records = _array(payload, "Lever response")
            if len(records) > PAGE_SIZE:
                raise SourceError("Lever pagination ignored requested limit")
        else:
            payload = _object(payload, "API response")
            records = _array(payload.get("jobs"), "jobs")
            if source["provider"] == "greenhouse":
                total = _object(payload.get("meta"), "meta").get("total")
                if not isinstance(total, int) or isinstance(total, bool) or total != len(records):
                    raise SourceError("Greenhouse total does not match returned jobs; incomplete feed")
            if len(records) > MAX_JOBS:
                raise SourceError("source job bound exceeded; incomplete feed")
        for record in records:
            if client.jobs_seen >= MAX_TOTAL_JOBS:
                raise SourceError("aggregate job bound exhausted; incomplete collection")
            client.jobs_seen += 1
            record = _object(record, "job record")
            identity = record.get("jobUrl") if source["provider"] == "ashby" else record.get("id")
            if not isinstance(identity, (str, int)) or isinstance(identity, bool):
                raise SourceError("job record lacks stable identity")
            if source["provider"] == "ashby":
                _safe_url(identity, {"jobs.ashbyhq.com"})
                identity = canonicalize(identity)
            if identity in seen:
                raise SourceError("duplicate job identity; unstable or repeated pagination")
            seen.add(identity)
            diagnostic["fetched_count"] += 1
            yield record
        if source["provider"] != "lever" or len(records) < PAGE_SIZE:
            return
    raise SourceError("Lever page/job bound reached without terminal short page; incomplete pagination")


def collect_sources(registry: list[dict]) -> tuple[list[dict], list[dict]]:
    """Sequential collection; expected failures are isolated, never hidden.

    Diagnostics: status=ok/disabled/failed/incomplete, complete, fetched_count,
    candidate_count, filtered_count, request_count, pages_fetched, reasons,
    filter_reasons, robots, provider/company/board_token/feed_url. Candidates
    from completed pages remain API-verified when later pagination fails, but
    their source diagnostic explicitly reports incomplete coverage.
    """
    if not isinstance(registry, list) or not all(isinstance(item, dict) for item in registry):
        raise ValueError("registry must be a list of source objects")
    client = _Client()
    candidates, diagnostics = [], []
    candidate_bytes = 0
    output_exhausted = False
    for source in registry:
        diagnostic = {key: source.get(key, "") for key in ("company", "provider", "board_token", "feed_url")}
        diagnostic.update(status="failed", complete=False, fetched_count=0,
                          candidate_count=0, filtered_count=0, malformed_count=0, request_count=0,
                          pages_fetched=0, reasons=[], filter_reasons={}, held_candidates=[], robots="not fetched")
        diagnostics.append(diagnostic)
        start_requests = client.requests
        filters = Counter()
        try:
            if source.get("enabled") is False:
                diagnostic.update(status="disabled", complete=True)
                continue
            if source.get("enabled") is not True:
                raise SourceError("enabled must be explicitly true or false")
            if output_exhausted:
                raise SourceError("aggregate normalized byte bound exhausted; incomplete collection")
            if client.jobs_seen >= MAX_TOTAL_JOBS:
                raise SourceError("aggregate job bound exhausted; incomplete collection")
            endpoint = _endpoint(source)
            if source.get("feed_url") != endpoint:
                raise SourceError("registry feed_url does not equal canonical provider endpoint")
            for record in _records(client, source, diagnostic):
                try:
                    candidate, reason = _normalize(record, source, now_iso())
                except SourceError as exc:
                    diagnostic["malformed_count"] += 1
                    candidate, reason = None, "malformed record: " + str(exc)
                if candidate is not None:
                    size = len(json.dumps(candidate, ensure_ascii=False, allow_nan=False).encode("utf-8"))
                    if candidate_bytes + size > MAX_TOTAL_BYTES:
                        output_exhausted = True
                        raise SourceError("aggregate normalized byte bound exhausted; incomplete collection")
                    candidate_bytes += size
                    candidates.append(candidate)
                    diagnostic["candidate_count"] += 1
                else:
                    filters[reason] += 1
                    if reason.startswith("security_hold:") and len(diagnostic["held_candidates"]) < 200:
                        diagnostic["held_candidates"].append({
                            "title": str(record.get("title") or record.get("text") or "")[:300],
                            "url": str(record.get("absolute_url") or record.get("hostedUrl") or record.get("jobUrl") or "")[:2048],
                            "found_at": now_iso(), "reason": reason,
                        })
            if diagnostic["malformed_count"]:
                diagnostic.update(status="incomplete", complete=False)
                diagnostic["reasons"].append("malformed records excluded; coverage is incomplete")
            else:
                diagnostic.update(status="ok", complete=True)
        except SourceError as exc:
            diagnostic["status"] = "incomplete" if diagnostic["fetched_count"] else "failed"
            diagnostic["reasons"].append(str(exc))
        finally:
            diagnostic["request_count"] = client.requests - start_requests
            diagnostic["filtered_count"] = sum(filters.values())
            diagnostic["filter_reasons"] = dict(sorted(filters.items()))
            provider = source.get("provider")
            host = HOSTS.get(provider, "") if isinstance(provider, str) else ""
            diagnostic["robots"] = client.robots_notes.get(host, client.stopped.get(host, "not fetched"))
    return candidates, diagnostics
