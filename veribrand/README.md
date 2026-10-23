# VeriBrand — Check it before you publish it

VeriBrand is a brand-governance prototype that checks marketing drafts against approved brand facts and rules, explains findings, cites the stored source, and keeps humans in control of approval.

**Team:** Single Thread · CodeAxis Hackathon · ABES EC  
**Theme:** Aubergine `#321C3B`, Silver `#C0C6D0`, Marigold `#F5AE2B`

## Run locally

```bash
python -m venv .venv
# Windows PowerShell: .venv\\Scripts\\Activate.ps1
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env  # Windows: copy .env.example .env
python run.py
```

Open `http://127.0.0.1:8000`.

### Google sign-in (local development)

Google sign-in is optional and does not work until OAuth credentials are configured. In Google Cloud Console, configure the OAuth consent screen and create an OAuth client of type **Web application**. Add `http://127.0.0.1:8000` as an authorized JavaScript origin and `http://127.0.0.1:8000/api/auth/oauth/google/callback` as an authorized redirect URI. Copy the client ID and client secret into the matching `GOOGLE_CLIENT_ID` and `GOOGLE_CLIENT_SECRET` entries in the local `.env`; keep the redirect URI exactly as registered. Do not commit `.env`.

Start the app with `python run.py`, then use **Continue with Google**. If sign-in is unavailable, run `python scripts/production_preflight.py` to see whether configuration values are present; it never prints their contents. Google credentials are not the same as a Gemini API key.

### Optional Gemini / OpenAI-compatible AI

The external AI pass is optional. For Gemini, set `AI_PROVIDER=gemini`, `GEMINI_API_KEY`, and optionally `GEMINI_MODEL=gemini-2.5-flash`. The default endpoint is Google’s OpenAI-compatible API endpoint. Alternatively set `AI_PROVIDER=openai` and the `OPENAI_API_KEY` / `OPENAI_MODEL` / `OPENAI_BASE_URL` variables. With `AI_PROVIDER=auto` (default), Gemini is preferred when `GEMINI_API_KEY` exists; otherwise OpenAI is used when configured. The app discloses external AI use, redacts emails/phone numbers before the call, and verifies cited brand-kit passages. This is not a privacy guarantee or live-provider validation. Use synthetic drafts first when testing with a real API key.
Run `python scripts/gemini_smoke_test.py` to perform a live connectivity check with a synthetic prompt. This checks API wiring only; it does not evaluate VeriBrand finding quality. Do not commit `.env` or paste API keys into chat.

### Demo mode

For a local demo, set `DEMO_MODE=1` in `.env`. The seeded fictional Lumio accounts use password `Demo@12345`:

| Email | Role |
|---|---|
| `creator@lumio.demo` | Creator |
| `reviewer@lumio.demo` | Reviewer |
| `admin@lumio.demo` | Admin |

The default `.env.example` is intentionally safe for non-demo use (`DEMO_MODE=0`, `EXPOSE_DEV_OTP=0`). Fictional Lumio facts, rules and demo accounts are seeded only when `DEMO_MODE=1`; real deployments start with an empty brand kit and must add approved brand data. Never deploy the demo password/accounts for real users.

## P0: public deployment safety checklist

Before deploying a real-user instance:

1. Set `APP_ENV=production`, `DEMO_MODE=0`, `EXPOSE_DEV_OTP=0`, `ADMIN_EMAIL` (the designated initial admin), and an explicitly configured persistent `DATA_DIR`. Public first-signup Admin promotion is disabled. The backend now refuses to start in production if demo mode or development OTP exposure is enabled, and also refuses production startup until all five SMTP settings are present.
2. Configure all SMTP values as hosting secrets: `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD`, and `SMTP_FROM`. Without SMTP, account verification and password reset messages cannot reliably reach users.
3. Attach persistent storage and set `DATA_DIR` to its mounted path. The included Render blueprint uses `/data`; you must attach a persistent disk there in the hosting dashboard before relying on saved data. The blueprint currently uses the free plan; if that plan does not support persistent disks, choose a plan/provider that does or use an external managed database/storage.
4. Run `python scripts/production_preflight.py` to check common mistakes, then `python scripts/production_preflight.py --strict` after SMTP is configured. The preflight is a configuration check, not a security audit.
5. Test signup → OTP email → verify → login → forgot password → reset → login with the reset password on the deployed URL.
6. Confirm HTTPS, review the host's secret settings and logs, and verify that no demo accounts or development tokens are exposed.

Do not put SMTP passwords or API keys in source control. A successful preflight does not replace a security review or a backup/restore test.

## Suggested demo path

1. Sign in as Creator and submit a draft that contains an unsupported ranking claim and a price mismatch.
2. Inspect the finding explanations, evidence, confidence and suggested fix.
3. Accept/edit/dismiss a finding, preview the diff and create a revised version.
4. Sign in as Reviewer, add a comment and approve or request changes.
5. Show the audit log and guideline-health metrics.
6. Optionally configure an LLM key and demonstrate the disclosed grounded AI path. Live provider behavior is not covered by the local tests.

## Tests and evaluation

```bash
python -m pytest -q
python eval/run_eval.py
```

PDF upload supports selectable text only; scanned PDFs require OCR, which is not implemented.

The labelled evaluation set is small and authored alongside the rules; it is a regression check, not independent evidence of broad real-world accuracy. `eval/stress_set.json` records known misses on paraphrases, spelled-out numbers and unlisted slang.

## Deployment

- Docker: `docker build -t veribrand .` then `docker run --rm -p 8000:8000 -e DATA_DIR=/data -e DEMO_MODE=0 -e EXPOSE_DEV_OTP=0 -v vb-data:/data veribrand`
- Render: use `render.yaml` as a starting point. For persistent data, configure a persistent disk mounted at `/data` and `DATA_DIR=/data`.
- Set SMTP variables for email; `GEMINI_API_KEY` (Gemini) or `OPENAI_API_KEY` (OpenAI-compatible) is optional for the external AI pass. Do not commit `.env` or secrets.

## Current scope and known gaps

This remains a single-workspace application build, not a multi-tenant SaaS. It uses SQLite and token-overlap retrieval; PDF/OCR/image analysis, vector RAG, OAuth, billing, per-organization tenant isolation, and a formal security audit are not implemented. Production use requires a separate isolated deployment/database per organization unless tenant isolation is implemented and independently tested. See [`FEATURE_STATUS.md`](FEATURE_STATUS.md) for the complete implemented/partial/not-implemented matrix and public-deployment checklist.

## Responsible-use / AI disclosure

The optional LLM pass calls an external API only when configured. The application attempts to redact emails and phone numbers before that call and verifies proposed source IDs/quotes before accepting findings. This is not a guarantee of zero data leakage; do not submit confidential content until your data-handling requirements have been reviewed. AI-generated findings require human review.

The demo brand and its facts are synthetic. Do not present them as facts about a real company.

## P1 capabilities added in this package

- Scanned-PDF OCR (Gemini Vision, up to 8 pages), image/creative review, and an embedding-based knowledge search endpoint with a local vector fallback.
- Optional PostgreSQL via `DATABASE_URL`; install requirements and run `alembic upgrade head` for the initial migration. The app also ensures tables exist on startup. SQLite remains the local default.
- Optional Google/GitHub OAuth. Register callback URLs exactly as `https://YOUR_HOST/api/auth/oauth/google/callback` and `/api/auth/oauth/github/callback`; configure client ID, secret and matching `*_REDIRECT_URI` values.
- Optional Stripe subscription checkout. Configure `STRIPE_SECRET_KEY`, `STRIPE_WEBHOOK_SECRET`, `STRIPE_PRICE_STARTER`, and `STRIPE_PRICE_TEAM`; register `/api/billing/webhook` for checkout-session and subscription events. Do not use live payments until test-mode webhook flows are verified.
- Account data export/deletion endpoints, optimistic edit conflict detection, PWA app shell, and browser voice dictation.

For Gemini visual/OCR/RAG features configure `GEMINI_API_KEY`, `GEMINI_MODEL`, `GEMINI_VISION_MODEL`, and optionally `GEMINI_EMBEDDING_MODEL`. No live external provider was called during the automated test suite; provider tests are mocked. See `FEATURE_STATUS.md` for exact limitations and deployment checks.

### Database

- Local SQLite: default, no `DATABASE_URL` needed.
- PostgreSQL: set `DATABASE_URL` to your PostgreSQL DSN and install `psycopg[binary]` (included in requirements). Run `alembic upgrade head` before starting the app. Keep `DATA_DIR` explicitly set for the production startup guard; it is still used for local SQLite and other app data.

## P2 governance and operations endpoints
- `GET /api/ready` — readiness check for database and writable storage.
- `GET /api/brand-kit/export` and `POST /api/brand-kit/import` — Admin-only portable brand-kit bundle.
- `GET /api/reviews/{id}/report.html` — authenticated HTML report attachment.
- `GET /api/audit/export.csv` — Reviewer/Admin audit export.
- `POST /api/rules/simulate` — Admin-only rule dry run; does not persist the rule.
- `POST /api/maintenance/cleanup` — Admin-only cleanup of expired auth artifacts.

See `P2_IMPLEMENTATION_REPORT.md` for verification results and remaining limitations. Passing local tests does not imply live-provider or production certification.
