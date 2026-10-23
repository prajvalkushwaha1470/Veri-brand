"""Pre-deployment configuration checks for VeriBrand.

Run:
  python scripts/production_preflight.py
  python scripts/production_preflight.py --strict

The non-strict mode reports missing SMTP as a warning. Strict mode exits non-zero
until required production settings are present. This script does not prove a
deployment is secure; it only catches common configuration mistakes.
"""
import argparse
import os
import re
import sys
from urllib.parse import urlparse
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def load_env_file():
    env_file = ROOT / ".env"
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip(chr(34)).strip(chr(39)))


def main():
    parser = argparse.ArgumentParser(description="Check VeriBrand production configuration.")
    parser.add_argument("--strict", action="store_true",
                        help="treat missing SMTP settings as an error")
    args = parser.parse_args()
    load_env_file()

    errors = []
    warnings = []
    env = os.getenv("APP_ENV", "development").strip().lower()
    demo = os.getenv("DEMO_MODE", "0") == "1"
    expose = os.getenv("EXPOSE_DEV_OTP", "0") == "1"
    data_dir = Path(os.getenv("DATA_DIR", ROOT / "data")).expanduser()
    smtp_required = ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM")
    missing_smtp = [key for key in smtp_required if not os.getenv(key)]
    smtp_status = {key: bool(os.getenv(key, '').strip()) for key in smtp_required}
    google_required = ("GOOGLE_CLIENT_ID", "GOOGLE_CLIENT_SECRET", "GOOGLE_REDIRECT_URI")
    google_status = {
        key: bool(os.getenv(key, '').strip())
        and not os.getenv(key, '').strip().lower().startswith('your-')
        and '.example' not in os.getenv(key, '').lower()
        for key in google_required
    }
    google_redirect = urlparse(os.getenv("GOOGLE_REDIRECT_URI", "").strip())
    google_callback_valid = (
        google_redirect.scheme in ("http", "https")
        and bool(google_redirect.netloc)
        and google_redirect.path == "/api/auth/oauth/google/callback"
        and ".example" not in google_redirect.netloc.lower()
    )
    admin_email = os.getenv('ADMIN_EMAIL', '').strip()
    data_dir_explicit = bool(os.getenv('DATA_DIR', '').strip())

    if env != "production":
        warnings.append("APP_ENV is not production; production startup guards are not active.")
    if demo:
        errors.append("DEMO_MODE=1 exposes seeded demo accounts; set DEMO_MODE=0.")
    if expose:
        errors.append("EXPOSE_DEV_OTP=1 can return OTP/reset tokens in API responses; set it to 0.")
    if env == 'production' and (not admin_email or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', admin_email)):
        errors.append('Set ADMIN_EMAIL to a valid designated initial administrator email; public first-signup admin is disabled.')
    if env == 'production' and not data_dir_explicit:
        errors.append('Set DATA_DIR explicitly to the hosting provider persistent mount or managed database path.')
    if os.getenv('SMTP_PORT', '').strip():
        try:
            smtp_port = int(os.getenv('SMTP_PORT', ''))
            if not 1 <= smtp_port <= 65535: raise ValueError()
        except ValueError:
            errors.append('SMTP_PORT must be an integer TCP port between 1 and 65535.')
    if missing_smtp:
        message = "SMTP is not fully configured; email verification/password reset may not reach users. Missing: " + ", ".join(missing_smtp)
        (errors if args.strict else warnings).append(message)

    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        probe = data_dir / ".veribrand_write_test"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink()
    except OSError as exc:
        errors.append(f"DATA_DIR is not writable ({data_dir}): {exc}")

    if env == "production" and (demo or expose):
        errors.append("Unsafe demo-only settings are incompatible with APP_ENV=production.")

    print("SMTP status: " + ", ".join(f"{key}={'set' if smtp_status[key] else 'missing'}" for key in smtp_required))
    print("Google OAuth status: " + ", ".join(
        f"{key}={'set' if google_status[key] else 'missing'}" for key in google_required
    ))
    if google_status["GOOGLE_REDIRECT_URI"] and not google_callback_valid:
        print("WARNING: GOOGLE_REDIRECT_URI must be an absolute URL ending in /api/auth/oauth/google/callback.")
    elif all(google_status.values()) and google_callback_valid:
        print("Google OAuth settings are present; confirm the redirect URI exactly matches Google Cloud Console.")
    else:
        print("Google sign-in is optional and unavailable until all three Google OAuth settings are configured.")
    for item in warnings:
        print(f"WARNING: {item}")
    for item in errors:
        print(f"ERROR: {item}")
    if errors:
        print(f"Preflight FAILED ({len(errors)} error(s), {len(warnings)} warning(s)).")
        return 1
    print(f"Preflight passed ({len(warnings)} warning(s)).")
    if not args.strict and missing_smtp:
        print("Tip: run with --strict before a real launch to require SMTP configuration.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
