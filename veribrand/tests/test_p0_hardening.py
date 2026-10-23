import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _run_import(extra_env):
    with tempfile.TemporaryDirectory(prefix="veribrand-p0-test-") as data_dir:
        env = os.environ.copy()
        env.update({
            "APP_ENV": "production",
            "DATA_DIR": data_dir,
            "DEMO_MODE": "0",
            "EXPOSE_DEV_OTP": "0",
        })
        for key in ("SMTP_HOST", "SMTP_PORT", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
            env.pop(key, None)
        env.update(extra_env)
        return subprocess.run(
            [sys.executable, "-c", "import backend_main"],
            cwd=ROOT, env=env, text=True, capture_output=True, timeout=20,
        )


def test_production_startup_fails_without_smtp():
    result = _run_import({})
    assert result.returncode != 0
    assert "Production startup blocked: configure SMTP settings" in result.stderr


def test_production_startup_rejects_demo_mode():
    result = _run_import({"DEMO_MODE": "1"})
    assert result.returncode != 0
    assert "DEMO_MODE=1 is forbidden" in result.stderr


def test_production_startup_rejects_dev_token_exposure():
    result = _run_import({"EXPOSE_DEV_OTP": "1"})
    assert result.returncode != 0
    assert "EXPOSE_DEV_OTP=1 is forbidden" in result.stderr


def test_production_startup_allows_safe_configured_settings():
    result = _run_import({
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USER": "user@example.test",
        "SMTP_PASSWORD": "not-a-real-password",
        "SMTP_FROM": "no-reply@example.test",
        "ADMIN_EMAIL": "admin@example.test",
    })
    # Import should succeed; no connection is attempted during app startup.
    assert result.returncode == 0, result.stderr


def test_email_sender_returns_false_when_not_configured(monkeypatch):
    import backend_main as bm
    for key in ("SMTP_HOST", "SMTP_USER", "SMTP_PASSWORD", "SMTP_FROM"):
        monkeypatch.delenv(key, raising=False)
    assert bm._send_email("user@example.test", "subject", "body") is False


def test_email_sender_rejects_invalid_port(monkeypatch):
    import backend_main as bm
    monkeypatch.setenv("SMTP_HOST", "smtp.example.test")
    monkeypatch.setenv("SMTP_USER", "user@example.test")
    monkeypatch.setenv("SMTP_PASSWORD", "fake-test-secret")
    monkeypatch.setenv("SMTP_FROM", "no-reply@example.test")
    monkeypatch.setenv("SMTP_PORT", "not-a-port")
    try:
        bm._send_email("user@example.test", "subject", "body")
    except RuntimeError as exc:
        assert "SMTP_PORT must be a valid TCP port" in str(exc)
    else:
        raise AssertionError("Invalid SMTP_PORT should be rejected")


def test_first_public_signup_is_not_admin_without_explicit_bootstrap():
    with tempfile.TemporaryDirectory(prefix="veribrand-admin-bootstrap-") as data_dir:
        env = os.environ.copy()
        env.update({"DATA_DIR": data_dir, "DEMO_MODE": "0", "EXPOSE_DEV_OTP": "1", "APP_ENV": "development"})
        env.pop("ADMIN_EMAIL", None)
        env.pop("ADMIN_INVITE", None)
        code = r'''
from fastapi.testclient import TestClient
import backend_main
c = TestClient(backend_main.app)
r = c.post('/api/auth/signup', json={'email':'first@example.test','name':'First','password':'LongPassword123'})
assert r.status_code == 200, r.text
assert r.json()['granted_role'] == 'Creator', r.text
'''
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env,
                                text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr


def test_signup_rejects_weak_and_overlong_passwords():
    # Import the app in the current test process; use a fresh app client and temporary DB.
    import backend_main as bm
    from fastapi.testclient import TestClient
    old_db = bm.DB
    try:
        with tempfile.TemporaryDirectory(prefix="veribrand-password-policy-") as data_dir:
            bm.DB = Path(data_dir) / "test.db"
            bm.init()
            client = TestClient(bm.app)
            weak = client.post('/api/auth/signup', json={'email':'weak@example.test','name':'Weak','password':'Password1'})
            long = client.post('/api/auth/signup', json={'email':'long@example.test','name':'Long','password':'A' * 129})
            assert weak.status_code == 400
            assert long.status_code == 400
    finally:
        bm.DB = old_db


def test_production_startup_requires_designated_admin():
    result = _run_import({
        "SMTP_HOST": "smtp.example.test",
        "SMTP_PORT": "587",
        "SMTP_USER": "user@example.test",
        "SMTP_PASSWORD": "not-a-real-password",
        "SMTP_FROM": "no-reply@example.test",
    })
    assert result.returncode != 0
    assert "set ADMIN_EMAIL" in result.stderr


def test_production_does_not_seed_fictional_brand_facts():
    with tempfile.TemporaryDirectory(prefix="veribrand-no-demo-data-") as data_dir:
        env = os.environ.copy()
        env.update({"DATA_DIR": data_dir, "DEMO_MODE": "0", "EXPOSE_DEV_OTP": "0", "APP_ENV": "development"})
        code = "import backend_main as b;\nwith b.db() as c: assert c.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0; assert c.execute('SELECT COUNT(*) FROM rules').fetchone()[0] == 0"
        result = subprocess.run([sys.executable, "-c", code], cwd=ROOT, env=env, text=True, capture_output=True, timeout=20)
        assert result.returncode == 0, result.stderr
