# Deterministic qualification scoring

The script applies this rubric. Human review may reject a result for supported evidence, but must not invent points.

## Hard gates

Reject before scoring when:

- verification status is not `active_candidate`;
- the application URL is missing or unsafe;
- title is manager/director/principal/architect/head/VP unless explicitly allowed;
- role is outside IT, VoIP, technical operations, marketing operations, sales operations, or business development;
- explicit commute exceeds the profile maximum;
- compensation maximum is below the profile floor without `pay_exception_reason`;
- a required license, clearance, citizenship status, or completed credential is not confirmed;
- required experience exceeds five years;
- description evidence is missing and the title/source alone cannot support fit.

## Score: 0–100

Start at 35.

### Title and track: up to 20

- 20: exact target title family for assigned track.
- 14: adjacent title with clearly matching core work.
- 6: partial overlap.
- 0: unrelated; hard-gate review.

### Required-skill overlap: up to 25

Normalize case, punctuation, common aliases, and whitespace.

- 25: at least 80% of explicit required skills match.
- 18: 60–79%.
- 10: 40–59%.
- 4: 1–39%.
- 0: a structured required-skill list exists but no skills match.
- 8: no structured required-skill list, but description/title supports the track.

Preferred skills do not count as required. They may be named as gaps. When `requirements_incomplete` indicates unparsed mandatory qualifications, a matching parsed subset is not full coverage: cap skill-overlap points at 8 and apply the missing-requirements confidence penalty.

### Experience: -15 to +15

Compare explicit minimum to the profile’s track experience:

- +15: meets/exceeds.
- +10: gap greater than 0 and at most 1 year.
- +5: gap greater than 1 and at most 2 years.
- -5: gap greater than 2 and at most 3 years.
- -15: gap greater than 3 years.
- +5: no explicit minimum.

Do not count education, projects, or unrelated work as extra years. Projects support skill overlap, not fabricated tenure.

### Education: -10 to +5

- +5: meets required level or no degree required.
- 0: degree is preferred only.
- -3: one level below or required degree still in progress, with equivalent experience language.
- -10: required completed degree is not met and no substitution is allowed.

### Compensation: -10 to +5

- +5: minimum of posted range meets profile floor.
- 0: range straddles floor or pay is not posted.
- -10: maximum is below floor; hard-gate unless an exception exists.

### Commute: -10 to +5

- +5: 45 minutes or less.
- 0: 46–60 minutes or unknown.
- -5: 61–75 minutes.
- -10: over 75 minutes; hard-gate unless explicitly allowed by the user.

### Confidence: -5 to +5

- +5: official/recognized source with explicit requirements and compensation.
- 0: recognized source with some missing evidence.
- -5: material requirements missing. A security concern is not a confidence penalty; it is a hold/rejection.

Clamp final score to 0–100.

## Bands

- `close_match`: 80–100.
- `slight_stretch`: 65–79.
- `underqualified`: 50–64.
- `reject`: below 50 or any hard gate.

Default batch aim:

- at least 40% close matches;
- no more than 40% underqualified;
- remainder slight stretches.

If the market cannot produce the mix, report the shortfall and continue searching. Do not relabel scores to satisfy the mix.

## Main gap

Choose the largest supported gap in this order:

1. hard credential or eligibility requirement;
2. years of experience;
3. required skill coverage;
4. education;
5. commute;
6. compensation;
7. industry-specific preferred tools.

Use one concise sentence. Never write “good fit” without evidence.
