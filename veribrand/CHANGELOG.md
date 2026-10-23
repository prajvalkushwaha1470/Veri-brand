# Changelog — Updated Round 2 package

- Rebased the deliverable on the more complete VeriBrand implementation with 18 end-to-end API tests.
- Added an explicit implemented/partial/not-implemented feature matrix and public-deployment checklist.
- Made local default configuration non-demo (`DEMO_MODE=0`, `EXPOSE_DEV_OTP=0`); tests explicitly enable demo OTP only inside their isolated test process.
- Added pytest configuration so `python -m pytest -q` runs the real tests cleanly.
- Added GitHub Actions CI to run tests and the evaluation script.
- Removed nested archives, Git metadata, virtual environments and caches from the distributable package.

## P0 hardening pass
- Render blueprint now defaults to production mode with demo accounts and development OTP exposure disabled.
- Backend fails closed if production mode is combined with either unsafe demo-only setting.
- Added a production preflight script to check demo flags, SMTP configuration, and writable data directory.
- Added deployment documentation for SMTP, persistent storage, and end-to-end auth verification.
- Verification: 18/18 automated tests pass; Python syntax compilation passes; unsafe production configurations were confirmed to fail startup.
- Not verified: live SMTP delivery, persistent hosting disk, HTTPS/deployed browser flows, and formal security audit.
- P0 follow-up: production startup now also blocks when SMTP settings are incomplete; SMTP port validation is explicit; added production-guard and SMTP config tests.

- Disabled public first-signup Admin promotion; production now requires `ADMIN_EMAIL`, explicit `DATA_DIR`, and complete SMTP configuration.
- Raised signup/reset password minimum to 12 characters and capped passwords at 128 characters; OTP format is validated as exactly six digits.
- Demo Lumio facts and rules now seed only in `DEMO_MODE=1`, avoiding synthetic sample data in a new real deployment.
- Render blueprint now configures a billable Starter service with a 1 GB persistent disk; review provider billing before deploy.
- Added regression coverage for admin bootstrap, password bounds, production config, and no demo knowledge seeding. Latest checks: 29 tests pass; syntax checks pass; authored regression set is 29/29 detections with correct labels, but the stress set remains 0/5.
- Added regression checks for security headers and password-reset token single-use/session revocation.


## P1 implementation pass
- Added selectable-text PDF review using pypdf with 5 MB upload, 100-page, encrypted-file and extracted-text limits. Scanned PDF OCR remains unsupported.
- Added Gemini support through Google’s OpenAI-compatible chat-completions endpoint, plus explicit provider/model disclosure and environment configuration.
- Added tests for PDF extraction/rejection and mocked Gemini request wiring, plus `scripts/gemini_smoke_test.py` for the final live-provider connectivity check with a user-owned key. Real Gemini API calls are intentionally left for the user’s final real-world validation with their own key.

## P2 governance and operations pass
- Added request IDs, request-duration logging, auth/account no-store headers, and a database/storage readiness endpoint.
- Added Admin brand-kit JSON import/export with validation and index invalidation.
- Added escaped HTML review report export and Reviewer/Admin audit CSV export.
- Added non-persistent rule simulation and Admin cleanup for expired auth artifacts.
- Registered the static frontend mount after API routes to prevent route shadowing.
- Verification: 53 automated tests pass; live provider/deployment checks remain pending.
