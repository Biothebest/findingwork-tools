# Search playbook

Goal: produce 50–100 current, unique, application-ready jobs without lowering the security or fit gates.

## Source order

Search in waves and deduplicate after every wave:

1. Official employer and ATS pages.
2. LinkedIn Jobs.
3. Indeed.
4. Established staffing firms.
5. Professional associations, schools, nonprofits, and public institutions with official career pages.

Do not search generic aggregators until the primary sources are exhausted, and never use an aggregator as final proof when an official route is unavailable.

## Geography

Read the home location and commute limits from ignored local configuration. The following Los Angeles-area clusters are search examples, not evidence of the candidate's residence.

Primary clusters:

- North Hollywood, Studio City, Burbank, Glendale, Sherman Oaks
- Los Angeles, Hollywood, West Hollywood, Beverly Hills
- Santa Monica, Culver City, El Segundo, Inglewood
- Van Nuys, Encino, Woodland Hills, Calabasas
- Pasadena, Monterey Park, Commerce
- Santa Clarita

Stretch clusters requiring an explicit commute check:

- Long Beach
- Torrance
- Downey
- Santa Fe Springs
- City of Industry
- Orange County locations
- Rialto, Riverside, and similarly distant inland locations

Remote roles must be available to California residents.

## AI voice and conversational AI query families

Search exact titles and adjacent skill combinations separately:

- `"AI Voice Engineer" OR "Voice AI Engineer"`
- `"AI Voice Agent" (engineer OR developer OR implementation OR support)`
- `"Conversational AI Engineer" junior OR associate OR "level I"`
- `"Voice Agent Engineer" OR "Voice Automation Engineer"`
- `"Conversational AI Developer" OR "Voice Application Developer"`
- `"Voice AI Solutions Engineer" OR "Conversational AI Support Engineer"`
- `"AI Voice Implementation" OR "Conversational AI Implementation"`
- `"Telephony Integration Engineer" Twilio OR SIP OR WebSocket`
- `"Twilio Developer" Python OR FastAPI OR voice`
- `"AI Agent Engineer" voice OR speech OR telephony`
- `("OpenAI Realtime" OR "speech-to-speech" OR "real-time voice") engineer`
- `(Vapi OR Retell OR Bland OR LiveKit OR Deepgram OR ElevenLabs OR Cartesia OR Twilio) (implementation OR support OR solutions OR engineer)`
- `("conversation designer" OR "voice UX") technical junior`

Search remote U.S./California and Los Angeles variants. Search LinkedIn and Indeed for discovery, then prefer the employer or ATS application page. Search official ATS hosts and credible startup sources including Wellfound, YC Work at a Startup, Built In, Welcome to the Jungle, and employer career pages; retain only pages that pass the configured allowlist and verifier.

Adjacent roles remain relevant when their core work includes voice APIs, real-time media, telephony, speech recognition, text-to-speech, conversational workflows, contact-center AI, customer implementation, or technical support. Do not retain generic machine-learning research roles merely because they mention speech.

## IT query families

Use one title family and one geography/source constraint at a time:

- `"IT Support Technician" "Los Angeles"`
- `"Help Desk Technician" (Burbank OR Glendale OR Los Angeles)`
- `"Desktop Support" (Los Angeles OR Santa Monica OR El Segundo)`
- `"IT Support Specialist" "entry level" California`
- `"Field Service Technician" IT Los Angeles`
- `"VoIP Support" OR "3CX Support" California`
- `"Unified Communications Technician" California`
- `"MSP Support Technician" Los Angeles`
- `"Technical Operations Specialist" Los Angeles`

Add official-source constraints in separate searches:

- `site:jobs.lever.co`
- `site:job-boards.greenhouse.io`
- `site:myworkdayjobs.com`
- `site:jobs.ashbyhq.com`
- employer career domain

## Operations query families

- `"Marketing Operations Coordinator" Los Angeles`
- `"Marketing Automation Coordinator" Los Angeles`
- `"Sales Operations Associate" Los Angeles`
- `"Business Development Coordinator" Los Angeles`
- `"GTM Coordinator" Los Angeles`
- `"Lead Generation Specialist" Los Angeles`
- `"Web Operations Specialist" Los Angeles`
- `"CRM Operations" coordinator Los Angeles`

Exclude manager/director/senior-lead terms during discovery unless intentionally sourcing a small reach segment.

## Discovery record

Capture only:

- company and exact title;
- location/work arrangement;
- compensation as posted;
- source URL and canonical application URL;
- posted date/age when visible;
- description text or explicit extracted requirements;
- source type;
- found timestamp.

Do not infer active status from a search snippet. Stage first; verify with the script.

## Search expansion

If fewer than 50 pass:

1. Expand title synonyms within the same lane.
2. Expand employer categories: schools, law firms, media, healthcare, aerospace, retail HQ, manufacturing, MSPs, nonprofits.
3. Expand from primary to stretch geography and run commute checks.
4. Add contract and temporary roles that meet compensation.
5. Add the secondary operations lane.
6. Add selected underqualified roles with strong task overlap.

Never expand by accepting expired listings, unsafe domains, unrelated jobs, senior management, or wholly below-floor pay.

## Freshness

Prefer pages posted or reverified within 72 hours. Always perform a final active-page check on the delivery day. Store both `found_at` and `verified_at`; never replace one with the other.

## Dedupe keys

Exclude when any matches an existing active/applied/queued record:

1. canonical application URL;
2. employer job/requisition ID;
3. normalized company + title + location;
4. same job syndicated across boards;
5. same role reposted by a staffing firm without a distinct requisition.

Separate applications for the same title at materially different locations may remain, but flag the relationship.
