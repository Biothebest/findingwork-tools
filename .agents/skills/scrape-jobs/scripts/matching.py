"""Pure, conservative matching; no network access and no tracker writes.

prepare_candidate returns (enriched_copy, '') or (None, 'reject: ...'/'hold: ...').
Activity/verification fields are never created or upgraded. See function docstrings
for scoring handoff: explicit required_years=None means reviewed but unknown.
"""
from __future__ import annotations

import csv
import re
from collections import Counter, defaultdict
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from build_tracker_exclusions import ACTIVE_STATUSES
from reconcile_gmail_confirmations import (
    canonicalize_url, company_aliases, job_identifier, normalize_text,
)
from rank_candidates import DEGREE_LEVELS, canonical_skill, pay_bounds
from verify_candidates import INJECTION_MARKERS, validate_url
from sources import FRAUD_RE

NUMBER_WORDS = {word: str(number) for number, word in enumerate(("zero", "one", "two", "three", "four", "five", "six", "seven", "eight", "nine", "ten", "eleven", "twelve"))}
WORD_YEARS = re.compile(r"\b(" + "|".join(NUMBER_WORDS) + r")(?=\s+(?:years?|yrs?)\b)", re.I)
DEMAND = re.compile(FRAUD_RE.pattern + r"|\b(?:pay (?:a |an |the )?(?:fee|deposit)|purchase (?:your |the )?equipment|send (?:gift cards|cryptocurrency)|provide .{0,25}(?:bank details|social security number|one.time code))\b", re.I)


DEFAULT_LOCAL_CITIES = (
    "North Hollywood", "Los Angeles", "Hollywood", "Studio City", "Burbank",
    "Glendale", "Universal City", "Valley Village", "Sherman Oaks", "Van Nuys",
    "Encino", "San Fernando", "Sun Valley", "North Hills", "Northridge",
    "Pasadena", "West Hollywood", "Culver City", "Santa Monica",
)
US_REGION = r"(?i:\b(?:united states(?: of america)?|california)\b)|(?-i:\bUS(?:A)?\b|\bU\.S\.(?:A\.)?)"
STATES = dict(zip(
    "AL AK AZ AR CA CO CT DE FL GA HI ID IL IN IA KS KY LA ME MD MA MI MN MS MO MT NE NV NH NJ NM NY NC ND OH OK OR PA RI SC SD TN TX UT VT VA WA WV WI WY DC".split(),
    ("Alabama|Alaska|Arizona|Arkansas|California|Colorado|Connecticut|Delaware|Florida|Georgia|Hawaii|Idaho|Illinois|Indiana|Iowa|Kansas|Kentucky|Louisiana|Maine|Maryland|Massachusetts|Michigan|Minnesota|Mississippi|Missouri|Montana|Nebraska|Nevada|New Hampshire|New Jersey|New Mexico|New York|North Carolina|North Dakota|Ohio|Oklahoma|Oregon|Pennsylvania|Rhode Island|South Carolina|South Dakota|Tennessee|Texas|Utah|Vermont|Virginia|Washington|West Virginia|Wisconsin|Wyoming|District of Columbia").split("|"),
))
JUNIOR = re.compile(r"\b(?:junior|jr\.?|entry[ -]level|associate|assistant|apprentice|trainee|new grad(?:uate)?|early[ -]career|level (?:i|1))\b|\b(?:engineer|developer|technician|analyst|specialist) (?:i|1)\b", re.I)
SENIOR = re.compile(r"\b(?:senior|sr\.?|staff|principal|architect|manager|director|head|vp|vice president|lead|founding|chief)\b", re.I)
PREFERRED = re.compile(r"\b(?:preferred|preferably|nice[ -]to[ -]have|bonus|a plus|ideally|desirable|not required|optional)\b", re.I)
HARD = re.compile(r"\b(?:must|required|minimum|at least|need to|shall|mandatory|essential|eligible only)\b", re.I)
YEAR = re.compile(r"(?<![\w,])(?P<low>\d+(?:\.\d+)?)\s*(?:(?:[-–—]|to)\s*(?P<high>\d+(?:\.\d+)?))?\s*\+?\s*(?:years?|yrs?)\b['’]?", re.I)
REQUIREMENT_HEADER = re.compile(r"^(?:(?:role|job|position) )?(?:minimum |basic |required |essential )?(?:requirements|qualifications(?: (?:&|and) skills)?|skills (?:&|and) qualifications|what you(?:'|’)ll (?:bring|need)|what you bring|what we(?:(?:'|’)re| are) looking for|you (?:have|bring)|your (?:skills|experience|qualifications)|must haves?|who you are|it(?:'|’)s important to us that you have)\s*:?$", re.I)
PREFERRED_HEADER = re.compile(r"^(?:(?:preferred|desired|additional|bonus) (?:qualifications|requirements|skills)|nice[ -]to[ -]haves?|it would be great if you had)\s*:?$", re.I)
SECTION_HEADER = re.compile(r"^(?:(?:role|job|position|key) )?(?:responsibilities|duties(?: (?:&|and) responsibilities)?|benefits(?: (?:&|and) perks)?|about (?:us|you|the role)|what (?:we offer|you(?:'|’)ll do)|(?:US )?pay range|compensation|our (?:team|benefits|values)|the role|perks|equal opportunity|key performance indicators(?: \(KPIs\))?|most important things(?: \(MITs\))?|company (?:overview|operating rhythm)|opportunity|why .+|you(?:'|’)ll love this role if you)\s*:?$", re.I)
# Fixed vocabulary, deliberately independent of Matthew's skills. Unknown required
# clauses remain gaps; matching never cherry-picks only profile overlaps.
SKILL_TERMS = (
    "Windows", "Windows 11", "Microsoft 365", "Office 365", "macOS", "Linux",
    "Active Directory", "Entra ID", "Intune", "Jamf", "ServiceNow", "Zendesk",
    "Salesforce", "HubSpot", "Marketo", "Pardot", "Braze", "Klaviyo", "Mailchimp",
    "Meta Ads", "Facebook Ads", "Amazon Ads", "TikTok Ads", "GA4", "Google Tag Manager",
    "Ahrefs", "Semrush", "WordPress", "Adobe", "Canva",
    "Google Analytics", "Google Ads", "SEO", "CRM", "Excel", "Tableau", "Power BI",
    "SQL", "Python", "Java", "C++", "C#", "Go", "Rust", "Ruby", "PHP", "HTML",
    "CSS", "JavaScript", "TypeScript", "React", "Angular", "Vue", "Node.js",
    "FastAPI", "Django", "Flask", "PostgreSQL", "MySQL", "MongoDB", "Redis",
    "Docker", "Kubernetes", "Terraform", "AWS", "Azure", "GCP", "Git",
    "GitHub Actions", "Jenkins", "REST API", "GraphQL", "Selenium", "Cypress",
    "Playwright", "pytest", "Postman", "API testing", "QA automation",
    "Twilio", "OpenAI API", "3CX", "VoIP", "SIP", "DNS", "DHCP", "TCP/IP",
    "networking", "troubleshooting", "help desk", "desktop support", "ticketing",
    "customer service", "documentation", "email marketing", "content marketing",
    "marketing operations", "sales operations", "revenue operations", "analytics",
    "copywriting", "social media", "webhooks", "authentication", "automation",
)
SKILL_PATTERNS = [(canonical_skill(term), re.compile(r"(?<![\w])" + re.escape(term) + r"(?![\w])", re.I)) for term in SKILL_TERMS]


def _sentences(text: str) -> list[str]:
    # Contrast/independent clauses must not let a denial or preference suppress
    # a separate mandatory credential or payment demand.
    parts = re.split(r";\s*|(?<!\be\.g\.)(?<!\bi\.e\.)(?<=[.!?])\s+(?=[A-Z])|,?\s+\b(?:but|however|whereas)\b[:,]?\s*", text)
    result = []
    for part in parts:
        chunks = re.split(r"(,\s*|\s+(?:and|while)\s+)", part)
        current = chunks[0]
        for index in range(1, len(chunks), 2):
            separator, chunk = chunks[index:index + 2]
            marked = PREFERRED.search(current) or HARD.search(current) or re.search(r"\b(?:never|do not|won't|will not|don't|no)\b", current, re.I)
            independent = HARD.search(chunk) or PREFERRED.search(chunk) or re.search(r"\b(?:you|applicants?|candidates?) (?:must|need|should)|^(?:pay|purchase|deposit|send|provide)\b", chunk, re.I)
            if marked and independent:
                result.append(current.strip())
                current = chunk
            else:
                current += separator + chunk
        result.append(current.strip())
    return [part for part in result if part]


def _clauses(text: str) -> list[tuple[str, bool, bool]]:
    result = []
    section = False
    preference = False
    for raw in text.splitlines():
        line = raw.strip(" \t•*-–")
        if not line:
            continue
        heading, separator, remainder = line.partition(":")
        if REQUIREMENT_HEADER.fullmatch(heading) or PREFERRED_HEADER.fullmatch(heading):
            preference = bool(PREFERRED_HEADER.fullmatch(heading))
            section = not preference
            line = remainder.strip() if separator else ""
            if not line:
                continue
        if SECTION_HEADER.fullmatch(line):
            section, preference = False, False
            continue
        for sentence in _sentences(line):
            preferred = bool(PREFERRED.search(sentence) or re.search(r"\bno (?:[\w'’]+\s+){0,4}(?:degree|certification|licen[cs]e|clearance|experience|citizenship) (?:is )?(?:required|needed)\b", sentence, re.I))
            explicit = bool(HARD.search(sentence)) and not preferred
            preferred = preferred or (preference and not explicit)
            result.append((sentence, (section or explicit) and not preferred, preferred))
    return result


def _experience_years(clause: str) -> list[float]:
    normalized = WORD_YEARS.sub(lambda match: NUMBER_WORDS[match.group().lower()], clause)
    # A number of years is not itself work experience (company age, audio
    # volume, vesting and product history occur in captured descriptions).
    if re.search(r"\b(?:we|our (?:company|team|product)|the company)\b.{0,50}\b(?:have|has|brings?|offers?|provides?|been)\b", normalized, re.I):
        return []
    values = []
    for match in YEAR.finditer(normalized):
        after = normalized[match.end():]
        before = normalized[:match.start()]
        if re.match(r"\s*(?:old|of age|of (?:audio|history)|ago)\b", after, re.I):
            continue
        experience = re.match(r"[\s-]*(?:of\s+)?(?:[\w/+()-]+[\s-]+){0,8}experience\b", after, re.I)
        activity = re.match(r"\s+(?:(?:of|in|with)\s+)?(?:working|building|developing|supporting|managing|implementing|programming|coding|administering|troubleshooting|technical support|IT support|customer service|sales|marketing|software engineering)\b", after, re.I)
        preceding = re.search(r"\bexperience\s*(?::|of|for)?\s*$", before, re.I)
        if experience or activity or preceding:
            values.append(float(match.group("low")))
    return values


def _states(text: str) -> set[str]:
    found = {code for code, name in STATES.items() if re.search(r"\b" + re.escape(name) + r"\b", text, re.I)}
    # Case-sensitive abbreviations avoid interpreting 'in', 'or', 'me', 'us'.
    found.update(code for code in STATES if re.search(r"(?<![A-Za-z])" + code + r"(?![A-Za-z])", text))
    return found


def _local(text: str, cities: list[str]) -> bool:
    return any(re.search(r"(?<!\w)" + re.escape(city) + r"(?!\w)", text, re.I) for city in cities if city)


def _geography(candidate: dict, config: dict) -> tuple[dict, str]:
    cities = config.get("daily_scraper", {}).get("local_cities", DEFAULT_LOCAL_CITIES)
    locations = candidate.get("locations") or []
    if not isinstance(locations, list) or not all(isinstance(item, str) for item in locations):
        return {}, "hold: malformed posting locations"
    primary = str(candidate.get("location") or "")
    # locations are posting alternatives only; employer_office_locations and
    # incidental offices in prose must never establish applicant eligibility.
    locations = [primary] if candidate.get("provider") == "greenhouse" else list(dict.fromkeys([primary] + locations))
    country = str(candidate.get("country") or "").strip()
    us_countries = {"us", "usa", "united states", "united states of america"}
    workplace = str(candidate.get("workplace_type") or "").lower()
    remote = workplace == "remote" or any(re.search(r"\bremote\b", value, re.I) for value in locations)
    hybrid = workplace in {"hybrid", "onsite", "on-site", "on site"} or bool(re.search(r"\bhybrid\b", primary, re.I))
    description = str(candidate.get("description_text") or "")
    constraints = [clause for clause, _, _ in _clauses(description) if re.search(r"\b(?:this (?:role|position|job) (?:is|will be|can|requires)|you (?:must|will need to)|candidates? must|applicants? must|must (?:be |currently )?(?:reside|live|work|based)|remote (?:work|employees|candidates|positions?) (?:is|are)|eligible (?:states|locations)|hiring (?:only )?in|(?:cannot|unable to|do not) hire|(?:not|only) (?:available|eligible|hiring|open) (?:in|to))\b", clause, re.I)]
    for clause in constraints:
        states = _states(clause)
        excluded = re.search(r"\b(?:excluding|except|not (?:available|eligible|hiring|open) (?:in|to)|cannot (?:reside|live) in|(?:cannot|unable to|do not) hire)\b", clause, re.I)
        if excluded and "CA" in _states(clause[excluded.end():]):
            return {}, "reject: California explicitly excluded from remote eligibility"
        if not excluded and states and "CA" not in states and re.search(r"\b(?:must|only|eligible|limited|restricted|based|located|reside|live)\b", clause, re.I):
            return {}, "reject: posting-specific location restriction outside California"
        if re.search(r"\b(?:office|onsite|on-site|hybrid)\b", clause, re.I) and not _local(clause, cities) and not re.search(r"\b(?:optional|not required|no office)\b", clause, re.I):
            return {}, "hold: nonlocal office attendance requirement needs review"
        if re.search(r"\b(?:must|only|required)\b.{0,80}\b(?:reside|live|based|located|commuting distance)\b", clause, re.I) and not _local(clause, cities) and not re.search(US_REGION, clause):
            return {}, "hold: mandatory residence/office proximity is not proven local"
    local_evidence = ""
    remote_evidence = ""
    for location in locations:
        if not location.strip():
            continue
        states = _states(location)
        foreign = re.search(r"\b(?:Canada|United Kingdom|UK|Europe|EMEA|APAC|India|Australia|Germany|France|Mexico|Brazil|Singapore|Philippines)\b", location, re.I)
        location_country = country if location == primary else ""
        for detail in candidate.get("location_details", []):
            if isinstance(detail, dict) and detail.get("location") == location:
                location_country = str(detail.get("country") or location_country)
        if foreign or (location_country and location_country.lower() not in us_countries):
            continue
        if _local(location, cities) and "CA" in states and not (states - {"CA"}):
            local_evidence = local_evidence or location
        if states - {"CA"} and "CA" not in states:
            continue
        residual = re.sub(US_REGION, "", location)
        residual = re.sub(r"\b(?:remote|anywhere|nationwide|continental|CA|US|USA|full[ -]time|part[ -]time)\b|[\s,|/()–—:-]", "", residual, flags=re.I)
        if not residual and (re.search(US_REGION, location) or "CA" in states or location_country.lower() in us_countries):
            remote_evidence = remote_evidence or location
    if hybrid or not remote:
        if local_evidence:
            return {"geography_status": "local", "geography_evidence": local_evidence, "commute_unverified": True}, ""
        return {}, "reject: onsite/hybrid location outside proven local city clusters"
    if not remote_evidence:
        remote_evidence = local_evidence
    if not remote_evidence and all(not re.sub(r"\bremote\b|[\s|()–—:-]", "", value, flags=re.I) for value in locations):
        for clause in constraints:
            if re.search(US_REGION, clause) and re.search(r"\b(?:remote|anywhere|reside|live|based)\b", clause, re.I):
                # Prose cannot override an explicit contrary posting country.
                if country and country.lower() not in us_countries:
                    break
                remote_evidence = clause
                break
    if not remote_evidence:
        return {}, "hold: remote eligibility for United States/California is not explicit"
    return {"geography_status": "remote_us_ca", "geography_evidence": remote_evidence, "commute_unverified": False}, ""


def _lane(title: str, description: str) -> str:
    if re.search(r"\b(?:executive assistant|administrative assistant|personal assistant|recruiter|talent acquisition|accountant|accounting|legal counsel)\b", title, re.I):
        return ""
    technical_title = re.search(r"\b(?:(?:technical|product|application|API|SaaS|integration) support|(?:support|customer success) (?:engineer|specialist|associate)|technical operations)\b", title, re.I)
    technical_duties = technical_title and any(re.search(r"\b(?:debug(?:ging)?|troubleshoot(?:ing)?|support(?:ing)?|resolv(?:e|ing)|repair(?:ing)?|configur(?:e|ing)|install(?:ing)?|maintain(?:ing)?|implement(?:ing)?)\b.{0,90}\b(?:software|applications?|APIs?|computers?|desktops?|laptops?|networks?|connectivity|VoIP|SIP|IT systems?|technical issues)\b|\b(?:API|software|application|IT|network|desktop|technical) (?:support|troubleshooting|debugging)\b", clause, re.I) for clause, _, pref in _clauses(description) if not pref and not re.search(r"\b(?:our company|we (?:employ|hire)|our (?:team|engineers))\b", clause, re.I))
    if re.search(r"\b(?:help[ -]?desk|service desk|desktop support|(?:computer|PC|IT hardware) technician|IT (?:support|technician|assistant|service)|information technology|VoIP|3CX|SIP support|cloud communications support|unified communications|network (?:support|technician|voice)|systems? administrator|MSP support)\b", title, re.I):
        return "IT"
    if technical_title:
        return "IT" if technical_duties else ""
    if re.search(r"\bfield (?:service )?technician\b", title, re.I):
        return "IT" if re.search(r"\b(?:computers?|desktops?|laptops?|IT hardware|network equipment|IP phones?)\b", description, re.I) and not re.search(r"\b(?:HVAC|elevators?|solar panels?|automotive|medical equipment)\b", title + " " + description, re.I) else ""
    if re.search(r"\b(?:marketing|email|CRM|lifecycle|campaign|content|SEO|social media|copywriter|organic search|paid (?:media|social|search|advertising)|ABM|lead generation|web (?:content|operations)|(?:sales|revenue|marketing) (?:ops|operations)|growth (?:associate|coordinator|analyst))\b", title, re.I):
        return "Marketing"
    if re.search(r"\bsecurity\b", title, re.I) and re.search(r"\b(?:research|researcher|scientist)\b", title, re.I):
        return ""
    if re.search(r"\b(?:QA (?:analyst|engineer|tester)|quality engineer)\b", title, re.I) and re.search(r"\b(?:automation|API testing|Python|Selenium|Playwright|Postman|software testing)\b", description, re.I):
        return "Hybrid"
    if re.search(r"\b(?:voice AI|AI voice|voice agent|voice automation|voice application|speech application|communications platform|conversational AI|applied AI|OpenAI|Twilio|telephony|conversation design(?:er)?|voice UX|AI (?:integration|implementation|operations|agent|software)|implementation|integration|onboarding engineer|professional services engineer|workflow automation|internal tools|QA automation|API test(?:ing)?|API developer|software test|quality assurance|security automation)\b", title, re.I):
        return "Hybrid"
    if re.search(r"\b(?:solutions|backend|back-end|Python|FastAPI|React|Rust|Tauri|(?:desktop|application|software) developer|developer (?:tools|support)|full[ -]stack|front[ -]end|web developer|software engineer)\b", title, re.I):
        return "Hybrid" if JUNIOR.search(title) or re.search(r"\b(?:Python|FastAPI|React|REST API|API integration|web applications?)\b", description, re.I) else ""
    return ""


def _security(text: str) -> str:
    lower = text.lower()
    if any(marker in lower for marker in INJECTION_MARKERS) or re.search(r"\b(?:reveal (?:your |the )?(?:secrets|system prompt)|send .{0,30}(?:credentials|tokens|passwords) to|install (?:this |our )?(?:extension|remote.access)|download and (?:run|execute)|move .{0,20}(?:telegram|whatsapp|signal))\b", lower):
        return "hold: untrusted-content instruction/security indicator"
    for clause, _, _ in _clauses(text):
        for demand in DEMAND.finditer(clause):
            if re.fullmatch(r"gift cards?", demand.group(), re.I) and not re.search(r"\b(?:pay|send|buy|purchase|provide|required)\b", clause, re.I):
                continue
            prefix = clause[:demand.start()]
            denied = re.search(r"\b(?:never|do not|don't|will not|won't|not ask)\b[^.!?;]{0,65}$", prefix, re.I)
            warning = re.search(r"\b(?:(?:may|might|could) be an attempt to|beware (?:of )?(?:anyone|recruiters?|requests?)|(?:scammers?|fraudsters?) (?:may |will |might )?(?:ask|require|request))\b[^.!?;]*$", prefix, re.I)
            if not denied and not warning:
                return "hold: possible recruiting-payment or sensitive-data demand"
    return ""


def prepare_candidate(candidate: dict, profile: dict, config: dict) -> tuple[dict | None, str]:
    """Return copy plus reason; do not mutate input or manufacture verification.

    Enrichment: track, junior_priority (0 explicit title/1 <=2 required years/2
    other), junior_evidence, geography_status/evidence, commute_unverified,
    required_years (number or None), required_degree (ranker enum),
    degree_equivalent_experience, required_skills, mentioned_skills,
    requirements_evidence, requirements_incomplete, requirement_gaps, preferred_requirements,
    low_pay_exception, pay_exception_reason, compensation_policy, review_notes.
    Main must merge requirement_gaps into the resulting main_gap. Full
    description_text and original verification fields are preserved untouched.
    """
    result = dict(candidate)
    title = str(candidate.get("title") or "").strip()
    text = str(candidate.get("description_text") or "")
    if not title or not text or not candidate.get("company"):
        return None, "hold: missing title, company, or job description"
    if candidate.get("security_flags"):
        return None, "hold: unresolved source security flags"
    security = _security(title + "\n" + text) or _security(str(candidate.get("source_fraud_context") or ""))
    if security:
        return None, security
    allowed = tuple(config.get("verification", {}).get("allowed_hosts", [])) + ("greenhouse.io", "lever.co", "ashbyhq.com")
    for key in ("url", "application_url", "canonical_application_url", "final_url"):
        if candidate.get(key):
            safe, reason = validate_url(str(candidate[key]), allowed, resolve_dns=False)
            if not safe:
                return None, "hold: unsafe " + key + ": " + reason
    if not candidate.get("url"):
        return None, "hold: missing application URL"
    seniority_title = re.sub(r"\blead generation\b", "demand generation", title, flags=re.I)
    if SENIOR.search(seniority_title) or re.search(r"\b(?:iii|iv|[3-9])\b", title, re.I):
        return None, "reject: senior/leadership or advanced-level title"
    if re.search(r"\b(?:commission[ -]only|100% commission|door[ -]to[ -]door|outside sales|street canvass(?:ing|er))\b", title + " " + text, re.I) or re.search(r"\bunpaid\b", title, re.I) or re.search(r"\b(?:unpaid (?:role|position|internship|work)|(?:role|position|internship) is unpaid)\b", text, re.I):
        return None, "reject: unpaid, commission-only, or outside/door-to-door sales"
    track = _lane(title, text)
    if not track:
        return None, "reject: title/duties outside supported computer/marketing lanes"
    geography, reason = _geography(candidate, config)
    if reason:
        return None, reason
    result.update(geography)
    result.pop("commute_minutes", None)
    clauses = _clauses(text)
    if candidate.get("requirements_text"):
        clauses += _clauses("Requirements\n" + str(candidate["requirements_text"]))
    clauses = list(dict.fromkeys(clauses))
    required = [clause for clause, hard, _ in clauses if hard]
    preferred = [clause for clause, _, pref in clauses if pref]
    years = []
    gaps = []
    degree = "none"
    equivalent = False
    skill_set = set()
    unparsed = []
    credentials = {normalize_text(str(item)) for item in profile.get("certifications", [])}
    # Ordinary driver's licenses use the existing exception below; commercial
    # licenses and all certifications require separate evidence.
    for clause, hard, pref in clauses:
        if pref:
            continue
        year_matches = _experience_years(clause)
        if year_matches:
            if hard:
                years.extend(year_matches)
            else:
                gaps.append("Experience language not clearly required/preferred: " + clause)
        degree_match = re.search(r"\b(?:bachelor(?:['’]s)?|baccalaureate|B\.?[AS]\.?|master(?:['’]s)?|M\.?[AS]\.?|associate(?:['’]s)?|high school|GED|Ph\.?D\.?|doctorate)\b", clause, re.I)
        if degree_match and not re.search(r"\b(?:no (?:college |university )?degree required|degree (?:is )?not required|regardless of .{0,15}education)\b", clause, re.I):
            # EEO demographic lists aren't candidate qualifications.
            if re.search(r"\b(?:equal opportunity|without regard|does not discriminate)\b", clause, re.I):
                continue
            if not hard:
                gaps.append("Degree language needs review: " + clause)
            else:
                word = degree_match.group().lower()
                needed = "master" if word.startswith(("master", "m.", "ma", "ms")) else "bachelor" if word.startswith(("bachelor", "baccalaureate", "b.", "ba", "bs")) else "associate" if word.startswith("associate") else "high_school"
                if word.startswith(("ph", "doctor")):
                    return None, "reject: completed doctoral degree required"
                eq = bool(re.search(r"\b(?:or (?:equivalent|comparable)|equivalent (?:practical |work |professional )?experience|experience (?:in lieu|may substitute))\b", clause, re.I))
                if DEGREE_LEVELS.get(profile.get("degree_level", "none"), 0) < DEGREE_LEVELS[needed]:
                    if not eq:
                        return None, "reject: completed " + needed + " degree required but not confirmed"
                    return None, "hold: degree-equivalent experience eligibility requires review: " + clause
                if DEGREE_LEVELS[needed] > DEGREE_LEVELS[degree]:
                    degree, equivalent = needed, eq
        if not hard:
            if re.search(r"\b(?:citizen(?:ship)?|ITAR|export.control|clearance)\b", clause, re.I) and not re.search(r"\b(?:equal opportunity|without regard|does not discriminate|regardless)\b", clause, re.I):
                gaps.append("Eligibility/clearance language needs review: " + clause)
            if re.search(r"\b(?:certification|licen[cs]e|CCNA|CISSP|CDL)\b", clause, re.I):
                gaps.append("Credential language not clearly required/preferred: " + clause)
            continue
        if re.search(r"\b(?:citizen(?:ship)?|U\.?S\.? persons?|ITAR|EAR|export[ -]control|security clearance|secret clearance|TS/SCI|public trust)\b", clause, re.I) and not re.search(r"\b(?:without regard|equal opportunity|does not discriminate|no .{0,20}(?:clearance|citizenship) required)\b", clause, re.I):
            return None, "hold: unconfirmed citizenship/export-control/clearance gate: " + clause
        if re.search(r"\b(?:work authori[sz]ation|authorized to work|legally (?:eligible|authorized) to work|without .{0,20}sponsorship)\b", clause, re.I):
            gaps.append("Work authorization/sponsorship not answered for user: " + clause)
        certs = re.findall(r"(?<!\w)(?:CCNA|CCNP|CCIE|CISSP|CISM|Security\+|Network\+|A\+|CompTIA [A-Za-z+]+|PMP|ITIL(?: v[34])?|AWS Certified [A-Za-z ]+)(?!\w)", clause, re.I)
        for cert in certs:
            if normalize_text(cert) not in credentials and not re.search(r"\b(?:obtain|earn|achieve|within|after (?:hire|hiring))\b", clause, re.I):
                return None, "reject: required certification not confirmed: " + cert
            if normalize_text(cert) not in credentials:
                gaps.append("Post-hire certification requirement: " + clause)
        if re.search(r"\b(?:certification|certified|licen[cs]e)\b", clause, re.I):
            driver = bool(re.search(r"\bdriver(?:['’]s|s)? licen[cs]e\b", clause, re.I))
            if driver and re.search(r"\b(?:commercial|CDL|class [AB])\b", clause, re.I):
                return None, "reject: unconfirmed commercial driver's license"
            if not driver and not certs:
                return None, "hold: unrecognized certification/license requirement: " + clause
        found_skills = {skill for skill, pattern in SKILL_PATTERNS if pattern.search(clause)}
        if found_skills and re.search(r"\b(?:or|such as|e\.g\.|for example)\b", clause, re.I):
            gaps.append("Alternative/example skill requirements need review: " + clause)
            unparsed.append(clause)
        else:
            skill_set.update(found_skills)
    minimum = max(years) if years else None
    maximum = min(5, float(config.get("daily_scraper", {}).get("max_required_years", 5)))
    if minimum is not None and minimum > maximum:
        return None, "reject: requires more than " + str(maximum).rstrip("0").rstrip(".") + " years (minimum " + format(minimum, "g") + ")"
    if minimum is None:
        gaps.append("Minimum required experience not explicitly established")
    if degree == "none":
        gaps.append("No explicit required degree established; not a claim that no degree is needed")
    if not required:
        gaps.append("Required qualifications not explicitly labelled; manual review needed")
    elif not skill_set:
        gaps.append("Required skill inventory is unstructured/unknown")
    # Unknown qualifications are evidence gaps, not a fully matched inventory.
    unparsed.extend(clause for clause in required if not _experience_years(clause) and not any(pattern.search(clause) for _, pattern in SKILL_PATTERNS) and not re.search(r"\b(?:degree|driver|licen[cs]e)\b", clause, re.I))
    gaps[:0] = ["Review required qualification: " + clause for clause in unparsed]
    if geography["commute_unverified"]:
        gaps.append("Local commute unverified; no travel-time estimate supplied")
    profile_skills = {canonical_skill(str(item)) for item in profile.get("skills", [])}
    normalized_description = canonical_skill(text)
    mentioned = sorted(skill for skill in profile_skills if re.search(r"(?<!\w)" + re.escape(skill) + r"(?!\w)", normalized_description))
    bounds = pay_bounds(str(candidate.get("pay") or ""))
    low_pay = bool(bounds and bounds[0] < (25 if bounds[2] == "hourly" else 52000))
    pay_reason = "Low-pay exception: legitimate target-lane role; compensation is a preference, not a hard floor" if low_pay else ""
    junior = JUNIOR.search(title) if minimum is None or minimum <= 2 else None
    if JUNIOR.search(title) and minimum is not None and minimum > 2:
        gaps.append("Junior title conflicts with required experience: " + format(minimum, "g") + " years")
    result.update(
        track=track, required_years=minimum, required_degree=degree,
        degree_equivalent_experience=equivalent, required_skills=sorted(skill_set),
        mentioned_skills=mentioned, requirements_evidence=required,
        requirements_incomplete=bool(unparsed or not required or not skill_set),
        preferred_requirements=preferred, requirement_gaps=list(dict.fromkeys(gaps)),
        junior_priority=0 if junior else 1 if minimum is not None and minimum <= 2 else 2,
        entry_level_evidence=bool(junior) or (minimum is not None and minimum <= 2),
        junior_evidence=junior.group() if junior else ("explicit required minimum <=2 years" if minimum is not None and minimum <= 2 else ""),
        review_notes=list(dict.fromkeys(gaps)),
        low_pay_exception=low_pay, pay_exception_reason=pay_reason,
        compensation_policy="No hard pay floor; below $25/hour or $52,000/year labelled",
    )
    return result, ""


def _urls(row: dict) -> set[str]:
    values = set()
    for key in ("job_url", "url", "application_url", "canonical_application_url", "final_url"):
        raw = row.get(key)
        if raw:
            try:
                canonical = canonicalize_url(str(raw))
            except ValueError:
                continue
            if canonical:
                values.add(canonical)
    return values


def _aliases(company: str) -> set[str]:
    aliases = company_aliases(company)
    normalized = normalize_text(company)
    if normalized:
        aliases.add(normalized)
    return aliases


def _ids(row: dict) -> set[tuple[str, str, str]]:
    """(provider/host, normalized employer, requisition); never naked IDs."""
    result = set()
    aliases = _aliases(str(row.get("company", "")))
    for url in _urls(row):
        parts = urlsplit(url)
        host = (parts.hostname or "").lower()
        segments = [part for part in parts.path.split("/") if part]
        query = dict(parse_qsl(parts.query))
        provider, identifier = host.removeprefix("www."), job_identifier(url)
        if host == "greenhouse.io" or host.endswith(".greenhouse.io"):
            provider = "greenhouse"
            identifier = query.get("gh_jid") or query.get("token") or identifier
        elif host == "lever.co" or host.endswith(".lever.co"):
            provider = "lever"
            if len(segments) >= 2:
                identifier = segments[1]
            if "postings" in segments and segments.index("postings") + 2 < len(segments):
                identifier = segments[segments.index("postings") + 2]
        elif host == "ashbyhq.com" or host.endswith(".ashbyhq.com"):
            provider = "ashby"
            identifier = segments[1] if len(segments) >= 2 else ""
        if identifier:
            result.update((provider, alias, str(identifier).lower()) for alias in aliases)
    if row.get("job_id") and row.get("provider") in {"greenhouse", "lever", "ashby"}:
        result.update((str(row["provider"]), alias, str(row["job_id"]).lower()) for alias in aliases)
    return result


def _identities(row: dict) -> set[tuple[str, str, str]]:
    title = normalize_text(str(row.get("title") or row.get("job_title") or ""))
    location = normalize_text(str(row.get("location") or ""))
    if not title or not location:
        return set()
    return {(alias, title, location) for alias in _aliases(str(row.get("company") or ""))}


def _distinct_ids(left: set, right: set) -> bool:
    """Only IDs in the same provider/employer scope can prove distinct jobs."""
    comparable = {(provider, company) for provider, company, _ in left} & {(provider, company) for provider, company, _ in right}
    return bool(comparable) and not (left & right)


def exclude_candidates(candidates: list[dict], tracker_path: Path, seen_candidates: list[dict]) -> tuple[list[dict], dict]:
    """Read tracker anew, return retained records and deterministic diagnostics.

    Summary: tracker_rows/status_counts, tracker_all_urls, tracker_active_records,
    seen_records, input_count, retained_count, excluded_count, reason_counts,
    exclusions [{company,title,location,url,reason}]. All tracker URLs (including
    ATS host variants) exclude; active/applied identities and scoped IDs exclude.
    Seen URLs/IDs/identities also exclude. Same identity with provably different
    same-provider requisitions is retained. This function never writes the tracker.
    """
    with Path(tracker_path).open(encoding="utf-8-sig", newline="") as handle:
        reader = csv.DictReader(handle)
        needed = {"company", "job_title", "location", "job_url", "application_status"}
        if not reader.fieldnames or needed - set(reader.fieldnames):
            raise ValueError("tracker missing required exclusion columns")
        rows = list(reader)
    urls = {"tracker_url": set(), "seen_url": set()}
    ids = {"tracker_job_id": set(), "seen_job_id": set()}
    variants = set()
    identities = defaultdict(list)
    active_count = 0
    for row in rows:
        urls["tracker_url"].update(_urls(row))
        row_ids = _ids(row)
        variants.update(row_ids)
        # Applied dates remain exclusion evidence after a status transition.
        active = str(row.get("application_status", "")).strip() in ACTIVE_STATUSES or bool(str(row.get("date_applied", "")).strip())
        if active:
            active_count += 1
            ids["tracker_job_id"].update(row_ids)
            for identity in _identities(row):
                identities[identity].append(("tracker_identity", row_ids))
    for row in seen_candidates:
        urls["seen_url"].update(_urls(row))
        row_ids = _ids(row)
        ids["seen_job_id"].update(row_ids)
        for identity in _identities(row):
            identities[identity].append(("seen_identity", row_ids))
    kept, excluded = [], []
    for candidate in candidates:
        candidate_urls, candidate_ids = _urls(candidate), _ids(candidate)
        reason = next((label for label, values in urls.items() if candidate_urls & values), "")
        if not reason and candidate_ids & variants:
            reason = "tracker_url_ats_variant"
        if not reason:
            reason = next((label for label, values in ids.items() if candidate_ids & values), "")
        if not reason:
            for identity in sorted(_identities(candidate)):
                for label, previous_ids in identities.get(identity, []):
                    if not _distinct_ids(candidate_ids, previous_ids):
                        reason = label
                        break
                if reason:
                    break
        if reason:
            excluded.append({"company": candidate.get("company", ""), "title": candidate.get("title", candidate.get("job_title", "")), "location": candidate.get("location", ""), "url": candidate.get("url", candidate.get("application_url", "")), "reason": reason})
        else:
            kept.append(candidate)
    return kept, {
        "tracker_rows": len(rows), "status_counts": dict(sorted(Counter(str(row.get("application_status", "")).strip() for row in rows).items())),
        "tracker_all_urls": len(urls["tracker_url"]), "tracker_active_records": active_count,
        "seen_records": len(seen_candidates), "input_count": len(candidates),
        "retained_count": len(kept), "excluded_count": len(excluded),
        "reason_counts": dict(sorted(Counter(item["reason"] for item in excluded).items())),
        "exclusions": excluded,
    }
