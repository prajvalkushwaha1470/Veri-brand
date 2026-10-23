# VeriBrand feature status — updated build

This is a feature-status document, not a claim that every roadmap item is finished.

## Implemented in this build
- FastAPI backend and single-page responsive frontend using Aubergine, Silver and Marigold styling.
- Signup/login/logout, email OTP verification, OTP resend cooldown and lockout, password-reset flow; new passwords require 12–128 characters.
- PBKDF2 password hashing, hashed bearer session tokens, in-process auth-route rate limiting with bounded memory, security headers.
- Server-side Creator/Reviewer/Admin authorization and creator ownership checks on drafts.
- Brand Kit facts, rules and terminology CRUD with validation and review-by dates.
- Deterministic content checks for tone, terminology, banned words, ALL-CAPS, long sentences and stacked punctuation.
- Claim/evidence ledger for supported, contradicted, stale and unsupported claim classes; uncertain cases are routed for human review.
- DOCX upload and review with file-size/zip-bomb safeguards; text-based PDF extraction/review with page, size and extracted-text limits (scanned PDFs/OCR not supported).
- Finding accept/edit/dismiss decisions, intent-lock protection, suggested fixes and word-level diff preview.
- Review versions, reviewer comments, approval/request-changes actions, approved content hash and audit log.
- Dashboard analytics, guideline-health metrics, basic grounded VeriBot and AI-use disclosure.
- Optional external LLM analysis with source/quote verification and email/phone redaction before the API call. Requires a configured key and has not been live-provider tested in this package build.
- Dockerfile, Render blueprint (billable Starter + 1 GB disk), `.env.example`, GitHub Actions CI, labelled evaluation set and stress set. Production startup requires explicit SMTP settings, `ADMIN_EMAIL`, and `DATA_DIR`; public first-signup Admin promotion is disabled. Fictional brand facts/rules are seeded only in demo mode.

## Partial / limitations
- App is single-workspace/single-organization per deployment. Tenant separation requires a distinct isolated deployment and database per organization; true multi-tenant organization records and in-app isolation are not implemented.
- SQLite is used; no production-grade migration framework or PostgreSQL/pgvector implementation.
- Retrieval is token-overlap based, not embedding/vector retrieval.
- SMTP integration is configurable but delivery/provider setup and operational reliability have not been verified here.
- Bearer tokens are stored by the browser app in sessionStorage; CSRF is not the primary risk for this token pattern, but XSS and session handling still need security review. Do not treat this as a completed security audit.
- Analytics are basic and use the data currently stored by the app.
- AI findings are optional; Gemini/OpenAI provider wiring is covered by mocked tests, but live provider behavior, quota handling and model-specific output quality still need real-key testing.
- Test/evaluation data is small and was authored alongside rules. The stress set documents known misses; reported high scores must not be interpreted as general accuracy.

## Not implemented
- Scanned-PDF OCR. A live Gemini smoke-test script is included, but it must be run with the deployer’s own key; it has not been run in this build environment.
- Image/logo analysis, brand-colour extraction, contrast checking and alt-text verification.
- Google/GitHub OAuth.
- Billing/subscriptions and real payment processing.
- Real-time collaborative editing, offline PWA and voice input.
- Complete data-retention/deletion console, automated backups/restore drill and formal penetration test.
- Fully verified production deployment.

## Before public use
1. Set `APP_ENV=production`, `DEMO_MODE=0`, `EXPOSE_DEV_OTP=0`, a verified `ADMIN_EMAIL`, and an explicit persistent `DATA_DIR`.
2. Configure a real SMTP provider and test signup, OTP, reset and resend flows end-to-end.
3. Configure persistent storage (`DATA_DIR`) and test backups/restores.
4. Use HTTPS at the hosting layer and restrict allowed origins if hosting frontend separately.
5. Review dependencies, auth/session logic, rate limits, upload handling and logs with a security checklist.
6. Run `python -m pytest -q`, `python eval/run_eval.py`, and manual browser flows on the deployment.

## P0 hardening pass (configuration changes)
- Render blueprint now defaults to `APP_ENV=production`, `DEMO_MODE=0`, `EXPOSE_DEV_OTP=0`, and `DATA_DIR=/data`.
- Backend refuses startup when production mode is combined with demo accounts or development OTP/reset-token exposure, and blocks production startup until all SMTP settings, a valid `ADMIN_EMAIL`, and explicit `DATA_DIR` are present. Public first-signup Admin promotion is disabled.
- Added `scripts/production_preflight.py` (and `--strict` mode) to check demo flags, SMTP configuration/port, admin bootstrap, and writable data path; added tests for fail-closed startup, admin bootstrap, SMTP configuration validation, security headers, and password-reset token/session invalidation. Production-mode startup no longer seeds synthetic Lumio facts or rules.
- SMTP delivery, persistent-disk attachment, HTTPS, live browser flows, multi-organization tenant isolation, and formal security review still require environment-specific configuration or implementation and verification; they are not claimed as completed here.

## P1 implementation pass (this build)
- Gemini scanned-PDF OCR path: renders up to 8 pages with PyMuPDF and asks Gemini Vision to transcribe; if key/model/network fails, review returns a clear error. Not live-provider tested here.
- Image/creative review endpoint and UI: Gemini Vision observations for visible text, brand consistency, readability/contrast risks and accessibility risks. Output is explicitly advisory; exact WCAG contrast ratio measurement is not certified.
- Knowledge index and semantic retrieval endpoints. Uses Gemini `embedContent` when configured and deterministic local feature-hash vectors as a no-key fallback. Reindex is invalidated on brand kit changes; this is a lightweight vector store in the application DB, not pgvector.
- Optional PostgreSQL runtime path via `DATABASE_URL`, psycopg adapter and Alembic initial schema migration (`alembic upgrade head`). SQLite remains the default local path. PostgreSQL runtime has not been exercised against a real server in this environment.
- Google and GitHub OAuth authorization-code flows with single-use, expiring state; requires provider client IDs/secrets and exact callback URLs. Not live-provider tested.
- Optional Stripe subscription checkout, signed webhook validation, idempotent event storage and subscription status endpoint; requires real Stripe price IDs and webhook secret. Not live-payment tested.
- Version conflict detection using `expected_version`, existing comments, a review-event feed and frontend polling reduce lost updates. This is optimistic concurrency, not character-level simultaneous editing.
- PWA manifest, installable app shell/service worker, browser speech-recognition draft dictation where supported.
- Account JSON export and confirmed account deletion for owned drafts, versions, comments and decisions; last Admin cannot delete themselves. Security audit events are retained by design.

## P1 remaining caveats
- Do not call the app fully production-ready until real OAuth, Gemini OCR/vision/embeddings, Stripe checkout/webhooks, PostgreSQL migration/restore, browser flows and security tests pass in the target environment.
- Stripe events are recorded idempotently and subscriptions are tracked, but there is no complete metering/feature-entitlement enforcement or customer portal yet.
- Voice input relies on the browser's Web Speech implementation and may send audio to the browser vendor; support and privacy vary by browser.
- Image review does not replace deterministic pixel sampling or a formal WCAG audit. A separate solid-colour hex contrast calculator and optional alt-text suggestion endpoint are included; the alt-text suggestion still needs human review.
- PWA shell can be cached offline; authenticated API actions still require network connectivity. Service worker cache excludes API requests.
- OAuth users are linked by verified email; teams should review account-linking policy before enabling provider login in public production.
