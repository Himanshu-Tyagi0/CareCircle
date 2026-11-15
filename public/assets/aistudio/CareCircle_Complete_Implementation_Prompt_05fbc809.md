# CareCircle — Complete implementation prompt

You are a senior Python engineer, security engineer, test engineer, and accessibility-minded product designer. Build **CareCircle**, an Elderly Care & Medication Management Platform, as a polished, deployable hackathon project. Optimize for Code Quality, Security, Efficiency, Testing, Accessibility, and Problem Statement Alignment through working features and verifiable evidence.

A perfect score is an objective, not a claim. Do not promise a score, call an unverified feature secure, fabricate test results, or describe a scaffold as a completed implementation. Complete each authorized phase with runnable code, appropriate tests, and an honest report.

## 1. Mission, scope, and execution rules

Create a shared care ecosystem centered on an elderly person's choices. The elder, caretakers, and family use secure email-and-passcode access to coordinate medications, dose confirmations, appointments, daily wellbeing check-ins, and doctor visit preparation. The elder controls membership, consent, escalation contacts, and sharing.

The distinguishing experience is a respectful **Care Chain**: the app says the elder “hasn't confirmed” a dose, invites someone to check on them, and stops escalating when someone accepts responsibility. It must never assume an unconfirmed dose was not taken.

Implement the complete product in six phases described in section 21. **Start with Phase 1 only. At the end of each phase, stop and wait for my confirmation before starting the next phase.** Within an authorized phase, make routine implementation decisions and finish the work without repeatedly seeking permission.

Inspect the workspace and applicable repository instructions first. Preserve unrelated changes. State important assumptions briefly. Use deterministic, testable defaults where requirements permit a choice. Ask only for information that genuinely blocks the current phase. Missing external credentials must not block local development: provide an explicit isolated demo mode.

The completed application must contain real implementations of every required feature. Do not leave placeholder service methods, fake success messages, hardcoded patient records in production paths, TODO-only security controls, or buttons that do nothing. Optional integrations may be disabled with a clear localized explanation while the core application remains usable.

## 2. Technology and deployment

- Python 3.11; Streamlit multipage UI; Supabase Postgres through supabase-py.
- Google Gemini through google-genai, with model selected by GEMINI_MODEL. AI is optional and disabled cleanly when credentials or elder consent are absent.
- Gmail SMTP using an app password, certificate-validated TLS, explicit timeouts, and a replaceable delivery gateway. Telegram Bot API is optional and individually opted into.
- bcrypt, pydantic v2, pytest, pytest-cov, ruff, and fpdf2 with uharfbuzz for Hindi-capable PDF shaping. Use additional small dependencies only when justified.
- GitHub Actions for CI and a best-effort five-minute reminder schedule, plus manual dispatch.
- Hugging Face **Docker Spaces**, with a pinned Python 3.11 base, non-root runtime, correct writable directories, port 7860, health check, and a persistent database outside the container. Do not depend on an obsolete native Streamlit Spaces template.
- Pin exact dependency versions after checking compatibility and running tests. Separate runtime and development requirements. Do not invent installed versions or rely on unbounded latest versions.
- Secrets come only from environment variables or Streamlit secrets. Local .env loading is development-only. Include .env.example with placeholders, .streamlit/secrets.toml.example, .streamlit/config.toml, and a correct .gitignore. Never commit real .env files, credentials, session values, uploaded prescriptions, database dumps, or logs containing health information.
- No paid service is required for the core demo. Document free-tier quotas, sleeping containers, email limits, database retention, and scheduler limitations honestly.

## 3. Architecture and source layout

Use strict boundaries: **UI -> services -> repositories -> database**. External network calls use injected integration gateways called by services. UI code must never call Supabase, Gemini, SMTP, or Telegram directly.

Required structure, extended only when useful:

    app.py
    core/              # config, errors, models, security, permissions, i18n, clock
    services/          # business operations and pure policy functions
    repositories/      # all database access; interfaces and demo implementations
    integrations/      # Gemini, SMTP, Telegram gateways
    ui/                # bootstrap, components, localization helpers, styles
    pages/             # thin Streamlit pages
    locales/           # en.json and hi.json
    db/                # schema.sql, migrations, development role setup
    scripts/           # run_reminders.py, seed_demo.py, diagnostics
    tests/             # unit, integration, contract, accessibility checks
    static/fonts/      # bundled Latin/Devanagari fonts and their licenses
    deployment/        # entrypoint and proxy configuration if needed
    docs/evidence/     # genuine verification evidence
    .github/workflows/ # ci.yml and reminders.yml
    .streamlit/
    Dockerfile
    requirements.txt
    requirements-dev.txt
    pyproject.toml
    .env.example
    .gitignore
    README.md
    SECURITY.md
    ACCESSIBILITY.md

Use type hints throughout, public-function docstrings, small cohesive functions, dependency injection, and structured logging. Keep Python files approximately 250 lines or fewer by splitting responsibilities, not by compressing code. SQL, locale catalogs, generated lockfiles, and documentation are exempt from this size guideline.

Use explicit domain exceptions carrying locale keys and safe parameters. Render friendly errors at the UI boundary. Never show raw database, pydantic, provider, or traceback messages to users. Logs contain event codes, opaque identifiers, counts, timings, and correlation IDs; exclude passcodes, tokens, emails, medical text, model prompts, and provider response bodies.

All public services that read protected data or perform operations must validate inputs and authorize access. Pure calculation helpers such as schedule expansion, adherence math, build_css, and next_action have no I/O and do not require an artificial authentication parameter.

## 4. Database model and transactional invariants

Provide a complete, runnable, canonical db/schema.sql, plus ordered migrations for subsequent changes. Use UUID keys, UTC timestamptz values for instants, IANA timezone names, explicit constraints, and indexed foreign keys. Schedule-local dates and times must retain their intended local meaning.

Model at least:

- Accounts: normalized unique email, bcrypt passcode hash, account status, persistent login counters/lockout, language and accessibility preferences.
- Care circles / elder profiles: exactly one active elder owner per circle and at most one active owned circle per elder account, timezone, check-in preferences, quiet hours, primary/secondary contacts, emergency contact preferences, and AI consent.
- Memberships: account, circle, role, active status, specific edit grants, and authorization revision. A session selects one valid membership; never infer authorization from an email or a UI-selected circle ID.
- Per-member category consents: medications, adherence, check-ins, appointments, and doctor brief; explicit grants and revocations with timestamps.
- Invitations and sessions: expiring hashed invitation/session values, revocation, idle and absolute session expiry.
- Medication records, versioned schedules, generated dose instances, dose events/corrections, and stock adjustments.
- Appointments, daily check-in instances/responses, care acknowledgements, circle notes, and elder questions.
- Doctor brief snapshots, source-category metadata, content hashes, and approvals tied to a version and allowed recipients.
- Action-token hashes, notification_log / transactional outbox, append-only audit records, and worker run/health records.

Enforce cross-circle ownership in the database, preferably with composite foreign keys where related records carry circle/profile IDs. Prevent assigning contacts from another circle. Enforce one check-in instance per circle/local date, unique medication/due-time occurrences, and unique logical notification events.

Audit every create/update/delete, consent or role change, care acknowledgement, escalation decision, token redemption, and brief approval/share. Insert audit entries in the same transaction as the corresponding change. Store the actor, operation, target, timestamp, and minimal safe change metadata; do not copy secret values or unnecessary medical notes into the audit table.

Archive medications and historical schedules instead of deleting evidence needed for adherence or briefs. Define account/circle deletion and retention behavior explicitly; do not invent a legally compliant retention period. Keep audit integrity while supporting a documented deletion process for the whole circle.

## 5. Authentication, database isolation, and sessions

Implement email-and-passcode authentication on the trusted server. Allow passcodes of at least 6 characters for the specified product requirement; recommend a longer passphrase in the UI. Enforce bcrypt's UTF-8 input byte limit without silently truncating. Use bcrypt with an appropriate production work factor, at least 12, and cryptographically secure randomness.

Provide real first-elder onboarding, not only demo seeding: collect a chosen passcode, email, timezone, and display name; verify email ownership using a throttled, expiring, single-use verification flow; then atomically activate the account, owned circle, and elder membership. Pending registrations cannot receive health notifications or invite members. Keep verification messages free of health data. Handle duplicate/concurrent registrations without account takeover or duplicate ownership. Existing authenticated accounts can create their own eligible circle without replacing their credentials. SMTP-disabled onboarding is available only in the explicitly isolated demo mode.

Onboarding and invitation links also follow the harmless-GET rule: opening a link only displays a preview, and explicit confirmation performs the atomic redemption. Implement this authentication-link foundation in Phase 1; do not defer it to the later medication action-token phase.

Persist failed-login counters atomically in Postgres. After five consecutive failed attempts, lock that account for 15 minutes. Concurrent attempts must not defeat the limit. Normalize email consistently, perform equivalent expensive verification for unknown accounts, and return generic sign-in failures to limit account enumeration. Add a bounded global login throttle; use source-based throttling only if the deployment provides a trustworthy client address.

Issue an opaque random session after successful authentication. Store only its hash in the database. Keep the raw value in server-side Streamlit session state. Default idle timeout: 30 minutes; absolute lifetime: 12 hours. Revalidate expiry, account status, membership, and current authorization revision on every protected operation. Logout, membership removal, and passcode changes revoke applicable sessions. Do not implement insecure email-only passcode recovery; document the supported recovery procedure and its limits.

Use this explicit Supabase authorization architecture:

1. Keep application tables in a private schema. Revoke direct table access and RPC execution from anon and authenticated unless specifically required and proven safe.
2. Access data through narrowly scoped server-only Postgres RPCs. The Supabase service-role credential stays exclusively in the trusted application/worker process.
3. Every user-context RPC resolves the actor from a valid session hash and checks active membership, role, relevant consent, grants, and resource ownership. A supplied role, member ID, circle ID, or consent flag is never proof of authority.
4. Authentication, invitation redemption, action-token redemption, and worker RPCs have distinct restricted contracts. None is a generic bypass or arbitrary SQL endpoint.
5. Enable RLS as defense in depth, but explicitly document that service-role access bypasses RLS. Isolation comes from restricted privileges and validated RPCs; do not falsely claim RLS protects unrestricted service-role queries.
6. SECURITY DEFINER functions use explicit schema qualification, a safe search_path, narrowly granted execution, and no dynamic SQL. Application services also enforce the permission model.
7. Transactional RPCs handle race-sensitive operations: authentication counters, session issuance/revocation, dose transitions and inventory, schedule generation, token consumption, acknowledgements, consent revisions, outbox insertion/claims, and audit creation.

Provide tests against a real ephemeral Postgres instance proving circle isolation, revoked-session rejection, consent enforcement, safe RPC privileges, and concurrency behavior. Parameterized RPC arguments are mandatory; no string-built SQL.

## 6. Roles, consent, and elder control

Define all actions centrally in core/permissions.py and an explicit matrix for elder, caretaker, and family. Test every role x action combination, including denial cases.

- Elder: full control of their circle, membership, consent, clinical-record coordination, contact policies, and brief approval.
- Caretaker: manage medications, schedules, appointments, and dose logging only when the relevant category is visible and the membership is active. Cannot manage members, grant rights, approve sharing for the elder, or enable cloud AI on the elder's behalf.
- Family: read-only medical records by default, within granted consent. The elder can grant particular edit actions individually. No broad implicit editor switch.
- Every member can update their own language, text size, contrast preference, and passcode through authorized services.

New memberships receive no sensitive category access until the elder explicitly chooses consent during invitation/activation. Writes require both the applicable role/grant and visibility of the relevant category. A category grant is not permission to access another circle.

Resolve the acknowledgement exception explicitly: a person whom the elder designates as a care contact may receive a narrow acknowledge-support permission for consented doses/check-ins. This changes coordination state only; it does not grant medication editing or mark a dose taken. Family remains read-only for medical records unless separate edit rights are granted.

Enforce consent on every read surface: pages, metrics, cached queries, charts, exports, AI input, brief viewing, notification recipients, message bodies, and token actions. Hidden UI controls are not a security boundary. A contact whose category consent is revoked must no longer receive those events.

The elder can inspect an understandable consent dashboard and audit log. Invitations expire, are single-use, and are bound to the intended verified email. A new invitee chooses their own passcode; an existing account must authenticate and accept the invitation without its passcode being replaced. No shared circle passcode, default administrator backdoor, or plaintext passcode distribution.

## 7. Medication management and inventory

Medication CRUD includes name, prescribed dose text, instructions, start/end dates, criticality (critical/normal/optional), stock count, stock unit, refill threshold, and an explicit numeric stock-units-per-dose when known. Do not infer tablets consumed from “500 mg.” Criticality is chosen by the user, never inferred by AI.

Validate lengths, date ordering, finite numbers, nonnegative inventory, reasonable list sizes, and allowed enum values. Preserve historical medication/schedule versions for reports. Editing future schedules must not rewrite past confirmations.

Generate a refill forecast from stock units and actual future scheduled consumption. Account for selected weekdays and end dates. If units or consumption are unknown, show an honest unavailable result rather than inventing a forecast. Send low-stock alerts only to consented, configured recipients; deduplicate each low-stock episode and reset the episode after a meaningful restock.

Stock changes are transactional and audited. A first Taken transition decrements stock once. Repeated taps do not decrement it again. Recording a genuine confirmation with an inconsistent zero stock value must show an inventory discrepancy and must not create negative stock. A permitted correction applies the compensating inventory adjustment once, with a reason.

## 8. Schedules and dose logging

Support multiple times per day, selected weekdays, inclusive start/end dates, and the elder's IANA timezone. Use zoneinfo. Store UTC instants and enough local schedule/version context to explain how each occurrence was generated.

Define daylight-saving behavior: for nonexistent local times, shift forward by the DST gap and label the adjustment; for repeated local times, choose the first occurrence (fold=0) and generate one dose. Deduplicate any resulting coincident occurrence for the same medication. Test spring/fall transitions, midnight, timezone changes, end dates, and repeated generation.

Generate a rolling 48-hour horizon idempotently. Reconcile generation gaps in bounded date chunks, without sending historical notifications. Adherence must not improve artificially because downtime prevented dose rows from being generated. Preserve schedule history and expected historical occurrences.

Dose states include pending, snoozed, taken, can't_take, explicitly skipped, cancelled, and expired/unconfirmed, with separate immutable events where appropriate. “Can't take” reasons: unwell, nausea or side effect, out of stock, doctor advised skip, other. Require a bounded note for other; never interpret a reason as clinical advice.

- Taken: show a clear confirmation and timestamp. A duplicate tap displays the previous confirmation and performs no second mutation. A correction is a separate authorized flow with a reason.
- Snooze: postpone reminders for 10 minutes. Store snoozed_until. Retain the original due timestamp for adherence and escalation calculations; repeated snoozes do not reset the original care-chain clock.
- Can't take: record the reason and create a primary-contact event immediately, subject to consent. The UI explains whom it will notify. Suggest contacting the clinician/pharmacist when appropriate; never recommend a dose change.
- Explicitly skipped: requires a permitted actor and reason, stops reminders, and remains visible in reporting. Distinguish it from unconfirmed.

Use a documented occurrence expiry: by default the earlier of the next scheduled occurrence for that medication and 12 hours after due time. Expired occurrences no longer generate reminder actions. An authorized historical correction remains possible through a deliberate authenticated flow; expired action tokens cannot record a late confirmation.

## 9. Adherence and Elder Mode

Provide 7- and 30-day adherence, medication-level and overall figures, a weekly heatmap with an equivalent labeled table, and a current streak. All time windows use the elder's timezone.

Define the denominator transparently: scheduled occurrences whose due time has passed in the selected window, excluding cancellations. Future doses are excluded. Taken is the numerator. Skipped and can't_take remain in the denominator and are reported separately; this is a record-confirmation metric, not a clinical judgment. A zero denominator shows “No scheduled doses,” not a misleading 0% or 100%.

Show on-time/late confirmation separately using a configurable, disclosed grace interval. A confirmation does not prove the medication was swallowed. Define the streak as consecutive eligible completed local days with all scheduled occurrences confirmed; no-dose days neither earn nor break a streak. Today's unfinished schedule must not prematurely break yesterday's streak.

Elder Mode is the default for elders: one calm screen with Today's medicines, next due item, Taken / Snooze / Can't take controls, the daily check-in, current care acknowledgement, text-size controls, language toggle, and read-aloud. Display medicine, dose, instructions, due time, and textual state without a dense dashboard. Keep advanced management available through clearly labeled navigation.

## 10. Appointments and Doctor Visit Brief

Appointments include doctor, specialty, place, timezone-aware datetime, notes, and derived reminder state. Validate and audit CRUD. Default reminders: 24 hours and 1 hour before the appointment. Cancel/edit invalidates obsolete queued reminders. Late cron runs send only the currently relevant reminder, not a burst of old ones.

Allow bounded factual family notes with authorship, and allow the elder to maintain questions for the doctor. Notes are governed by doctor-brief consent plus explicit add_own_note, edit_own_note, and delete_own_note grants for non-elders. These grants apply only to the author's own notes; the elder can manage all circle notes. Being able to read a brief does not implicitly permit writing notes. Only authorized preparers can include selected notes in a brief. Do not treat notes as instructions to an AI model.

Prepare a visit brief for a configurable period, default 30 days, containing deterministic sections:

1. Current medications, doses, and instructions.
2. Per-medication counts and adherence with explicit denominators.
3. Unconfirmed, skipped, and can't_take patterns and recorded reasons.
4. Check-in mood trend and response counts.
5. Selected family notes and the elder's questions.
6. Generation time, covered dates/timezone, and “Generated from app records; not medical advice.”

Deterministic code owns all counts, dates, facts, tables, and trend statements. Optional Gemini narration uses the bounded method in section 16. If AI is disabled or fails validation, the complete deterministic brief still works.

Provide on-screen viewing and English/Hindi PDF export. Use embedded, openly licensed fonts and actual complex-text shaping. Generate and visually inspect a Hindi PDF containing conjuncts, matras, and mixed numerals; verify selectable text, wrapping, and page boundaries. A one-page summary is the default; overflow belongs in a clearly labeled appendix rather than unreadably small type.

Draft briefs are visible to the elder and explicitly permitted preparers with access to every included source category. Before another member can receive or download a final brief, the elder approves its exact content hash/version and recipient set. Viewing also requires current doctor-brief consent and access to included source categories. A content change, membership change, or relevant consent revocation invalidates access/approval as applicable. Regeneration must never inherit stale approval. Document that a PDF already downloaded outside the app cannot be recalled.

## 11. Pure Care Chain policy

Implement a pure function in services/escalation_service.py:

    next_action(due_at, now, status, sent_levels, policy, acknowledged) -> Action

Use typed immutable Policy and Action dataclasses. The status argument is a typed value that includes state, snoozed_until, and a can't_take report ID/reason where applicable. Policy contains criticality, windows, timezone, quiet hours, and occurrence expiry. Preserve the requested sent_levels parameter name, but give it an explicit immutable EscalationProgress type containing a monotonic level high-water mark, per-level queued/sent/uncertain/superseded dispositions, and handled explicit-report IDs. A bare set of successfully sent levels is insufficient to represent retry/catch-up state safely. Inject all time; do not read the wall clock inside this function.

Default windows from the original due timestamp:

| Criticality | Elder reminder | Elder again | Primary contact | Secondary contact |
|---|---:|---:|---:|---:|
| Normal | 0 min | 15 min | 30 min | 60 min |
| Critical | 0 min | 10 min | 20 min | 40 min |
| Optional | 0 min | none | none | none |

Rules:

- Taken, explicitly skipped, cancelled, or expired returns no further dose reminder/escalation. Acknowledgement stops automatic reminders/escalation for its care episode; it does not suppress acknowledgement notices or a new explicit help report.
- A snoozed occurrence emits nothing before snoozed_until. Afterwards, evaluate against its original due timestamp.
- Can't take produces one immediately queued primary-contact event with the required reason, including for optional medicines; this is a deliberate help report, not an automatic overdue escalation. The first such report can notify even if someone acknowledged the earlier unconfirmed-dose episode. Avoid repeated alerts for the same report; editing its reason alone does not create a fresh report. A reported can't_take state no longer runs the unconfirmed-dose chain.
- Quiet hours suppress automatic non-critical contact escalation. Critical escalation can continue. Elder reminders follow the elder's separate notification preference. Explicit can't_take / Not well / urgent-help reports can notify configured contacts immediately, with this behavior disclosed when submitted.
- At the end of quiet hours, recompute current eligibility; never replay every suppressed level.
- For delayed cron runs, choose only the highest currently eligible level above the persisted high-water mark. Atomically advance progress when its logical event is created; lower levels remain suppressed thereafter. Older unsent levels become superseded, never falsely marked sent. Retrying the selected event is outbox work and cannot regress the policy to an older level. Do not send several accumulated levels in one run. Expired occurrences produce no catch-up message.
- If a configured contact is missing or lacks current consent, skip that delivery and surface a configuration issue to the elder. Do not invent a recipient or silently disclose details to another member.
- An authorized contact can say “I'm checking on them.” Atomically record the first acknowledgement, stop queued/future escalation, and notify other currently authorized care contacts who accepted. Acknowledgement does not mark a medication taken. Handle simultaneous acknowledgements without conflicting ownership messages.

Every user-facing notification uses respectful language: “hasn't confirmed,” never an assertion that the elder missed or refused a medication. Use equivalent natural Hindi. Audit queued, suppressed/superseded, acknowledged, sent, and failed outcomes using distinct event types.

## 12. Daily check-in and non-diagnostic safety layer

Show a large “How are you today?” card with Good / Okay / Not well and an optional short note. Include an “I'm okay” quick option mapping to Good. One response per elder/local date can be corrected through an audited flow. Any submitted mood counts toward the streak, including Not well. Eligible days begin when check-ins are enabled; intentionally disabled days neither earn nor break a streak. An unanswered current instance cannot break the streak until its response window closes; a submitted current instance can extend it. Corrections never count as additional days.

The elder configures a morning check-in time and a waiting window, default 2 hours. Generate a daily instance. Remind the elder at the configured time; if still unanswered after the window, gently notify the consented primary contact that the elder “hasn't checked in yet.” A Not well response immediately notifies the configured consented caretaker; when unavailable, use only an elder-approved fallback contact.

Reuse the action-token, outbox, acknowledgement, quiet-hour, and idempotency infrastructure. Provide a separate pure check-in next_action function and tests. A daily check-in expires at the next local day's check-in boundary; do not send old reminders after a new daily instance replaces it. The first transition to Not well creates one explicit wellbeing-report event per daily instance, even if its earlier unanswered-check-in episode was acknowledged. Repeated edits/toggling do not send duplicate reports. Changing back to another mood cancels an unsent Not well event; an accepted message cannot be recalled, and the app must not imply otherwise. A later urgent-help request is a separately identified event. Show response streaks and mood trends only with check-in consent.

If an unsent Not well event was cancelled and the elder selects Not well again, reactivate that same logical event when it remains within its valid window; do not create a second event key. Already sent or uncertain events are not automatically sent again.

Implement the red-flag layer as explicit support controls, not AI diagnosis:

- An “I need urgent help” action and an always-available, elder-configured emergency contact panel.
- Show a local emergency number only after country/region is explicitly configured; never infer it from language, IP, or timezone.
- Tell users to contact local emergency services for an immediate emergency. App notifications are not emergency dispatch and may be delayed.
- Do not infer an emergency from an unconfirmed dose or a mood chart. Do not score clinical risk, diagnose symptoms, recommend extra doses, or advise stopping treatment.
- A failed contact notification must leave visible status and accessible phone/contact options. Never claim that someone was notified before confirmed provider acceptance.

## 13. Signed actions, notification delivery, and worker reliability

### Safe email actions

Emails contain large accessible links for Taken, Snooze, Can't take, and I'm checking on them as appropriate to the recipient. **Links open a confirmation screen; page loads and GET requests never change state.** Require one clear button confirmation, and a reason for Can't take. This deliberately prevents email security scanners from recording doses by following links.

Use HMAC-SHA256-signed, expiring, single-use tokens with a version/key ID, random nonce, allowlisted action, resource binding, recipient membership binding, and expiry. Use opaque identifiers, no health details in tokens. Compare signatures in constant time. Default expiry is the earlier of 2 hours and the resource's remaining action window. Handle key rotation with a bounded previous-key validation period.

Store only hashes of complete tokens, including in outbox records. Generate raw tokens in memory immediately before rendering/sending; do not persist token-bearing HTML or raw links. For a definitive pre-send failure, invalidate that attempt's tokens and issue fresh ones on retry. Preserve potentially delivered tokens after an uncertain send until their normal expiry.

After cryptographic validation, a single database transaction rechecks expiry, hash, unused state, current membership/consent, action eligibility, and resource status, then consumes the token, performs the action, and inserts the audit entry. Concurrent redemptions produce one mutation. A token for one action cannot authorize another action or another resource.

Capture query tokens in server-side session state and clear them from the visible URL promptly. Keep Streamlit XSRF protections enabled. Do not load external fonts, analytics, or other third-party content on action pages. Set Referrer-Policy: no-referrer and appropriate no-store headers through deployment configuration; include a minimal local reverse proxy if needed on Hugging Face. Redact token-bearing URLs from application/proxy logs. Document any hosting access-log exposure that cannot be controlled. Never silently claim these controls are present without verification.

### Outbox and delivery semantics

Use notification_log as a transactional outbox, with a unique logical key covering event type, resource ID, escalation level, recipient, and channel. For dose events this includes the required dose_id/level/recipient uniqueness. Store localized rendering inputs and safe metadata, not plaintext tokens or complete sensitive messages.

Outbox states distinguish pending, leased, sending, sent, retryable, uncertain, failed, cancelled, and superseded. Claim eligible rows atomically with leases and FOR UPDATE SKIP LOCKED. Before beginning a send, recheck current recipient consent, dose/check-in status, acknowledgement, appointment version, and expiry; cancel stale work using event-specific eligibility. An acknowledgement cancels automatic episode reminders, while the corresponding acknowledgement notice and newly submitted explicit-help events remain independently eligible and deduplicated.

Retry with bounded backoff only when failure is known to precede provider acceptance. SMTP cannot guarantee exactly-once delivery across an ambiguous timeout or a crash after acceptance. Mark these cases uncertain, avoid blind retries, and expose them for authorized reconciliation. An expired sending lease is uncertain, not automatically retryable. Document the possible-delivery-loss tradeoff. A deterministic Message-ID is useful for tracing but is not a Gmail deduplication guarantee.

Logical events and claims must be idempotent even under overlapping workers. Do not describe delivery as exactly once. Already accepted or in-flight messages cannot always be recalled after a simultaneous consent revocation/acknowledgement; minimize and document that boundary.

Keep emails minimal: elder display name, due/appointment context needed to act, short status, and safe action links. Include a can't_take reason only for a currently authorized recipient who needs it. Avoid medication lists, detailed notes, diagnoses, or full profiles. Every recipient receives their selected language. Telegram has separate opt-in, chat binding, consent checks, and equivalent localized minimal messages.

### Worker

In this specification, “immediate” means transactionally queue the event at submission, then attempt bounded best-effort post-commit dispatch through the same outbox/claim service for explicit help reports and acknowledgements. The fast path must not bypass consent, deduplication, delivery-state rules, or user-action authorization. A delivery failure does not roll back the recorded dose/check-in. Pending work is handled by the next available worker run; uncertain sends follow reconciliation rules. UI messages distinguish queued, provider-accepted, uncertain, and failed. Never promise immediate email arrival.

scripts/run_reminders.py is a thin entrypoint into services. Read only indexed active windows, plus bounded reconciliation chunks and due outbox jobs. Use pagination, batch joins, explicit network/database timeouts, a total execution budget, and structured run summaries.

reminders.yml runs every five minutes and supports workflow_dispatch. Use least-privilege workflow permissions, protected secrets, concurrency controls, and a job timeout. Database leases remain necessary even with workflow concurrency. Record last successful run, last error category, due-event lag, and uncertain-send count. Display a localized stale-worker status when appropriate.

GitHub Actions and sleeping Hugging Face free-tier apps are best effort, not exact timers. Compute eligibility from timestamps, make delays visible, and document an optional continuously running deployment using the same worker. Do not represent this system as life-critical or guaranteed delivery.

## 14. English and Hindi throughout

Implement core/i18n.py with t(key, **params), plus an explicit language argument/context for background jobs. Load locales/en.json and locales/hi.json without importing Streamlit into core. Detect missing keys/placeholders during tests and fail clearly in development; production errors must remain friendly.

Every application-controlled user-facing string comes from locale files: navigation, headings, buttons, help, validation errors, statuses, chart labels, accessibility labels, PDF headings/disclaimers, emails, Telegram messages, and empty/error states. User-entered medical text and proper names remain as entered unless an explicitly reviewed translation is requested. Document unavoidable framework-owned browser text separately.

Include a localized About page with a visible medical disclaimer: the app supports coordination and recordkeeping, does not provide medical advice, and does not replace a clinician or emergency services. Keep the disclaimer concise and understandable.

Use natural, simple Hindi suited to older readers. Include English / हिन्दी toggles in the sidebar and Elder Mode, persisted per account. Format dates/times consistently with language and elder timezone. Never silently change a medication's dose or translate a drug name into an invented equivalent.

Bundle licensed Latin and Devanagari fonts locally with fallbacks. Validate identical locale keys and identical named placeholders, including pluralized/count variants if used. Exercise both languages in page smoke tests and notification/PDF tests.

## 15. Efficiency and maintainability

Cache reusable clients with st.cache_resource through a service-container boundary. Do not mutate a shared client's authentication headers for individual users. Use st.cache_data with short TTLs for appropriate read-heavy views, and invalidate after writes.

Authorize before every sensitive cache lookup. Cache keys include an opaque account/membership scope, circle, current authorization/consent revision, filters, timezone, and language where relevant. Never put raw secrets in cache keys or return data cached for another member. Do not cache session validity or consent decisions in a way that delays revocation.

Use batched repository reads, server-side filtering/aggregation where appropriate, and bounded result sets. Avoid per-medication or per-recipient query loops. Include indexes on dose (profile_id, due_at), check-in time/status, session hash/expiry, membership/circle, outbox status/next_attempt_at, and foreign-key access paths. Add partial indexes when justified by measured query patterns.

Measure representative query counts and active-window query plans. Document the dataset and environment for any timing claim. Keep pure functions separate from infrastructure so they can be tested without Streamlit or a network. Prefer understandable code over elaborate abstractions or premature optimization.

## 16. Bounded Gemini features and voice assistance

Implement services/ai_service.py with injected gateways, pydantic output schemas, explicit input/output/token/time limits, and stable error codes. Treat model output and uploaded document text as untrusted data. Never execute generated code, SQL, HTML, tool calls, URLs, or instructions embedded in a prescription or note.

Cloud AI requires explicit elder consent for processing their health data. Explain what will be sent before the first use. Minimize payloads and omit names, contact details, and unrelated records. Consent is checked on every call. Do not claim a provider's retention or privacy guarantee unless supported by its configured terms. Core workflows remain usable with AI completely disabled.

Required features:

1. **Prescription scan:** accept only supported images with verified MIME/decoding, a size limit (default 10 MB), a pixel limit, and stripped unnecessary metadata. Extract bounded medicine drafts with name, strength, dose/instructions, frequency, duration, and uncertainty fields. Display the original alongside editable extracted fields. Ambiguous fields stay unresolved. Nothing is saved until the user reviews and confirms through the ordinary authorized medication service.
2. **Natural-language entry:** parse English/Hindi instructions into a schedule draft. “After breakfast” may use an existing elder-approved meal-time preference; otherwise request a time. Never invent a time, dose, route, strength, duration, or criticality. Require human review and confirmation.
3. **Instruction simplification:** retain the original and preserve all dose, timing, route, frequency, and caution facts. Use protected factual fields and constrained templates; validation of JSON alone is insufficient. If preservation cannot be established, show the original and an explanation. Never turn “as advised” into a new treatment instruction.
4. **Schedule suggestions:** suggest organizational reminder times only within the entered prescription and elder-approved preferences. Never suggest moving doses for clinical reasons, combining doses, changing intervals prescribed by a clinician, or resolving interactions. Mark unresolved constraints for human clarification. Confirm before saving.
5. **Medicine explanation:** explain the user's label/instructions and vetted educational terminology in plain language. Cite the available source. Do not invent indications, contraindications, interaction advice, or dosing guidance. If a vetted source is absent, use the existing instructions and suggest asking a pharmacist/doctor. Display a localized “For understanding your records; not medical advice” disclaimer beside explanatory AI output.
6. **Ask CareCircle:** answer administrative questions about permitted app records, such as today's schedule or recorded check-in dates. Assemble an allowlisted, bounded context in code; include source record/date references. Do not let the model choose database queries. Refuse diagnosis, treatment changes, interaction checking, emergencies, or other members' private data, using localized safe responses.
7. **Voice assistant:** optional, user-initiated st.audio_input, maximum 60 seconds and 10 MB with actual duration validation. Display the transcript for correction before processing. Support English and Hindi where the configured provider does. Voice cannot bypass any confirmation, role, or consent rule. Provide a typed alternative for every voice flow.
8. **Doctor brief narrative:** deterministic code supplies approved factual statements with stable IDs. Ask Gemini only to select/order relevant fact IDs and an allowlisted narrative template ID, then render the short narrative from those validated facts in code. Reject unknown IDs, contradictory selections, or free-form clinical claims. This preserves useful AI assistance without treating a schema-valid free-form paragraph as factual evidence.

For every feature, handle disabled AI, timeout, quota exhaustion, malformed JSON, wrong types, oversized output, unresolved fields, prompt injection, and contradictory content. Fallbacks are deterministic/manual workflows, not plausible-looking invented results.

AI never saves medication changes, records doses, sends notifications, changes permissions, selects emergency action, or approves a brief. Human confirmation uses the same secure business service as manual input.

## 17. Text-size controls, read-aloud, and accessibility

Implement the pure function:

    build_css(font_px: int, high_contrast: bool = False) -> str

Validate strict integer sizes from 18 through 32 px. Default to 24 px in Elder Mode and 18 px elsewhere. Persist text size and contrast preference per account. Generate CSS only from validated numeric/boolean values; never interpolate user text into CSS.

Accessibility requirements:

- Normal text contrast at least 4.5:1; meaningful controls/focus indicators at least 3:1 against adjacent colors.
- Actionable touch targets at least 48 x 48 px, with spacing that reduces accidental adjacent taps.
- Visible keyboard focus, logical tab order, descriptive labels, and no placeholder-only labels.
- Icons plus localized text for all states; color is supplementary.
- Layout works at narrow mobile widths and 200% browser zoom without lost controls or unintended horizontal page scrolling.
- Charts include textual summaries and equivalent accessible tables.
- Explicitly labeled busy, success, validation, empty, offline/provider-failure, and stale-data states. Do not rely on transient toast messages alone.
- Confirmation screens and dialogs preserve keyboard access and readable sizing.
- Respect reduced-motion preferences; avoid unnecessary animations and autoplay.

Centralize necessary Streamlit CSS selectors and document their dependency on the pinned Streamlit version. Preserve native labels, dialogs, and controls instead of hiding accessibility information.

Read-aloud uses an explicit user gesture and browser speech synthesis, with Stop and Replay controls. Read Today's medicines and important messages in the selected language. Detect unavailable Hindi voices and show a localized fallback. Pass text through a safe component argument mechanism; never concatenate untrusted text into executable JavaScript or raw HTML. No automatic recording or speech playback.

Perform documented keyboard and screen-reader walkthroughs plus automated checks where feasible. Verify English and Hindi at minimum/default/maximum font size, mobile width, high contrast, and zoom. Do not claim universal WCAG compliance based only on CSS or an automated score.

## 18. Tests and measurable acceptance

Use pytest and pytest-cov. Target at least **90% combined statement/branch coverage for core/ and services/** with the CI coverage gate set to 90%; this exceeds the original 75% requirement. Do not manipulate coverage by excluding difficult authentication, authorization, notification, or AI paths. Favor meaningful behavior/security tests over tests that merely repeat implementation details.

Unit tests mock all external network calls and inject clocks, gateways, and repositories. Database integration tests use an explicitly configured ephemeral local Postgres/Supabase environment and never a production project. Configure the necessary Supabase-style roles faithfully when testing SQL in plain Postgres. Mark test layers clearly.

Required coverage includes:

- Passcode hashing, UTF-8 length limits, generic failures, verified onboarding, concurrent registration, invitation email binding, existing-account acceptance, concurrent lockout counters, expiry, logout, passcode-change revocation, and session idle/absolute timeout.
- Every role x action; explicit family grants; consent filtering; revocation; cross-circle IDs; forged/stale actors; notification/export/AI/cache disclosure attempts.
- Schedule weekdays, timezone midnight, DST gaps/folds, end dates, edits, historical versions, concurrent generation, and downtime reconciliation.
- Dose duplicate taps, concurrent Taken updates, snooze, every can't_take reason, skip/correction rules, stock nonnegativity, and compensating inventory changes.
- Adherence zero denominator, cancelled/future doses, late confirmations, can't_take/skipped accounting, no-dose days, and timezone-local streak boundaries.
- Care Chain taken, skipped, cancelled, expired, acknowledged, snoozed, optional, critical, can't_take, quiet hours, absent/revoked contacts, delayed cron, monotonic progress, superseded/uncertain levels, explicit help after acknowledgement, and concurrent acknowledgements.
- Pure check-in escalation, configurable wait, Not well, midnight/DST, duplicate responses, mood corrections/toggling, acknowledgement, expiry, unfinished-day and disabled-day streaks, and consent.
- Token signature tampering, wrong resource/recipient/action, wrong key version, expiry boundaries, single use, concurrent redemption, state revalidation, and harmless link/page loads.
- Outbox unique event keys, competing claims, lease recovery, stale-event cancellation, definitive failures, provider acceptance, ambiguous timeouts, and crash-after-send behavior.
- Appointment rescheduling/cancellation, reminder deduplication, refill forecasts, and low-stock episode resets.
- Exact English/Hindi key and placeholder parity; both-language notification rendering, safe HTML escaping, and localized error mapping.
- Gemini valid/malformed/oversized/adversarial responses, disabled/unavailable providers, unknown fact IDs, forbidden facts, confirmation requirements, and consent-limited context.
- Deterministic brief counts/tables, date coverage, version-bound approval, consent revocation, English/Hindi PDF output, shaping setup, and readable overflow behavior.
- build_css boundaries, injection resistance, preference persistence, and representative UI accessibility checks.

CI runs ruff check, ruff format --check, unit tests/coverage, database integration tests, and dependency vulnerability checks with explicitly reviewed exceptions if needed. An optional browser smoke job must fail on real navigation errors, not fabricate visual evidence. Test reports must state what was executed, what passed, and what remains unverified.

## 19. Demo, configuration, and operational safety

Provide two clearly separated execution modes:

- **Production mode:** requires valid database configuration; missing credentials cause a friendly setup error, never silent fallback to fake patient data.
- **Demo mode:** explicitly selected, visibly labeled, isolated synthetic data, fake delivery/AI gateways by default, and no outbound health notifications. Demo repositories must obey the same service interfaces and permission rules.

The seed script creates English and Hindi demo circles with elder, caretaker, and family accounts; different consent/grant examples; scheduled medicines; historical confirmations/reasons; stock; appointments; check-ins; and sample notes. Use stable synthetic identifiers and public demo-only credentials, clearly labeled in README. Seed data must contain no real people or contact addresses.

For database seeding, require explicit opt-in and a dedicated empty demo project/schema. Never seed a production circle, overwrite existing data silently, or run seeding automatically at startup. The script is repeat-safe within its isolated demo namespace. Block real notification delivery in demo mode; a separate delivery smoke test may use only an explicitly configured allowlisted test recipient.

Include a safe configuration validator/diagnostic command that reports required variable names, connection status, and missing capabilities without printing values. A .env.example documents database URL/key, application URL, session/lockout settings, action-signing key versions, Gemini model/key, SMTP settings, optional Telegram settings, demo mode, and worker settings.

## 20. Documentation and scoring evidence

README.md must include:

- A concise pitch, the problem solved, elder autonomy, and the Care Chain differentiator.
- A feature-to-requirement table and a Mermaid architecture diagram matching the actual implementation.
- Clear local setup, isolated demo credentials, database initialization/migrations, tests, configuration, GitHub Actions, and Hugging Face Docker Spaces deployment steps.
- A CI badge referencing the actual repository when known. If the repository is not yet known, mark the badge URL as a placeholder; never display invented passing CI evidence.
- English/Hindi screenshots from the real running app when available, with text descriptions. Use clearly labeled placeholders only until real screenshots are captured.
- Notification timing/SMTP uncertainty, free-tier limits, account recovery, data retention, AI limits, supported browsers/voices, and remaining accessibility limitations.
- A reproducible five-minute judge walkthrough: sign in as elder, inspect consent, confirm/snooze/report a dose, observe a simulated delayed Care Chain, acknowledge as contact, complete a check-in, review a doctor brief, and demonstrate a denied family operation. Simulation is clearly labeled and cannot alter production clocks or send real notifications.

SECURITY.md documents trust boundaries, custom authentication, service-role limitations, RPC authorization, RLS, session/token lifecycle, XSS/CSRF and email-scanner defenses, database constraints, abuse controls, consent, audit behavior, uploads, AI data handling, dependency updates, secret rotation, retention/deletion, and responsible vulnerability reporting.

ACCESSIBILITY.md records actual choices, contrast values, font sizes, keyboard order, touch targets, Hindi support, screen-reader/chart alternatives, read-aloud limitations, manual walkthrough results, and remaining issues.

Create a final evidence matrix with one row per requirement: implementation file, relevant test/verification, observed result, and limitation. For each of the six scoring criteria, show concrete evidence rather than assigning yourself an unsupported score:

| Criterion | Evidence expected |
|---|---|
| Code Quality | Clear boundaries, small typed modules, passing lint/format, consistent domain errors |
| Security | Role/consent/circle isolation tests, transactional token/session rules, threat model |
| Efficiency | Bounded batch queries, justified indexes, safe caching, measured query counts |
| Testing | Passing CI, coverage artifact, pure-policy edge cases, real SQL concurrency tests |
| Accessibility | Both-language keyboard/zoom checks, contrast measurements, accessible PDF/UI evidence |
| Problem Alignment | Complete elder-centered judge journey, Care Chain, consent, check-ins, brief |

## 21. Six phases and confirmation gates

**Phase 1 — Secure, accessible foundation**

Deliver the source layout, pinned dependencies, initial schema and constraints, configuration, logging/errors, custom authentication/session repositories and services, verified onboarding, permission matrix, consent model/enforcement, invitations, English/Hindi catalogs, build_css/text-size persistence, minimal localized sign-in/home/About/Elder Mode shell, isolated demo seed, and tests. Provide initial setup documentation and CI for implemented code. Add the minimal injected email gateway needed for account verification/invitations now; the care-reminder outbox and full delivery worker arrive in Phase 3.

Acceptance: the app starts in explicit demo mode; English/Hindi sign-in works; incorrect credentials lock out persistently; session expiry/logout and role/consent denials are tested; database isolation is exercised; text size works; current core/service tests and lint pass. Future features are clearly identified as future phases rather than represented as working.

**Phase 2 — Daily medication workflow**

Deliver medication/schedule CRUD, versioned generation, dose logging and double-dose guard, transactional inventory changes, adherence/streaks, and a polished Elder Mode medication screen. Test timezone/DST behavior and the full permission-checked workflow.

**Phase 3 — Collaboration and reminders**

Deliver appointments, pure Care Chain, outbox/delivery gateways, action-token confirmation flow, acknowledgements, worker, reminders.yml, worker health, deployment headers, and concurrency/uncertainty tests. Demonstrate with mocked/sandbox delivery and injected time.

**Phase 4 — Wellbeing and elder control**

Deliver check-ins and their pure escalation logic, the complete consent/member dashboard, elder audit view, refill forecasts/alerts, contact configuration, and non-diagnostic safety/help controls. Prove revocation affects notifications, cached data, and tokens.

**Phase 5 — Safe assistance and doctor preparation**

Deliver every section 16 feature with confirmation/fallbacks, browser read-aloud, doctor notes/questions, deterministic briefs, constrained narration, versioned elder approval, and verified English/Hindi PDF output. Keep cloud AI optional and consent-bound.

**Phase 6 — Verification and delivery**

Complete accessibility walkthroughs, both-language screenshots, documentation, Docker/Hugging Face smoke checks, CI/dependency checks, final coverage, query-efficiency evidence, and the requirement matrix. Resolve defects discovered during verification before describing the project as complete.

At the end of **each** phase, report:

1. What works, with the relevant file paths and a short demonstration path.
2. Commands actually executed and their actual results, including current coverage.
3. Security/accessibility decisions and any genuine limitations or unverified checks.
4. Deliverables for that phase and the next phase's scope.

Then stop and ask for confirmation to proceed. Do not begin the next phase until I confirm. **Begin Phase 1 now, implementing and testing the foundation rather than returning only a plan.**
