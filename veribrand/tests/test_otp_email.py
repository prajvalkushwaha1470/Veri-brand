import os
import smtplib
import sys
import tempfile
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

os.environ.setdefault('DATA_DIR', tempfile.mkdtemp(prefix='veribrand-otp-tests-'))
os.environ.setdefault('DEMO_MODE', '1')
os.environ.setdefault('EXPOSE_DEV_OTP', '1')
import backend_main as bm


def _set_smtp_env(monkeypatch):
    monkeypatch.setenv('SMTP_HOST', 'smtp.gmail.com')
    monkeypatch.setenv('SMTP_PORT', '587')
    monkeypatch.setenv('SMTP_USER', 'user@gmail.com')
    monkeypatch.setenv('SMTP_PASSWORD', 'app-password-123')
    monkeypatch.setenv('SMTP_FROM', 'no-reply@gmail.com')


def test_load_env_reads_file_without_overwriting_existing_values(tmp_path, monkeypatch):
    env_file = tmp_path / '.env'
    env_file.write_text(
        'SMTP_HOST=smtp.gmail.com\n'
        'SMTP_PORT=587\n'
        'SMTP_USER=user@gmail.com\n'
        'SMTP_PASSWORD=app-pass\n'
        'SMTP_FROM=no-reply@gmail.com\n',
        encoding='utf-8',
    )
    monkeypatch.setenv('SMTP_HOST', 'smtp.preexisting.example')
    monkeypatch.setenv('SMTP_PORT', '2525')
    monkeypatch.delenv('SMTP_USER', raising=False)
    bm._load_env(env_file)
    assert os.getenv('SMTP_HOST') == 'smtp.preexisting.example'
    assert os.getenv('SMTP_PORT') == '2525'
    assert os.getenv('SMTP_USER') == 'user@gmail.com'
    assert os.getenv('SMTP_FROM') == 'no-reply@gmail.com'


def test_smtp_missing_values_raise_clear_error(monkeypatch):
    for key in ('SMTP_HOST', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_FROM'):
        monkeypatch.delenv(key, raising=False)
    monkeypatch.setenv('SMTP_PORT', '587')
    with pytest.raises(RuntimeError, match='SMTP settings are missing'):
        bm._smtp_settings(strict=True)


def test_smtp_invalid_port_raises(monkeypatch):
    _set_smtp_env(monkeypatch)
    monkeypatch.setenv('SMTP_PORT', 'not-a-port')
    with pytest.raises(RuntimeError, match='SMTP_PORT must be a valid TCP port'):
        bm._smtp_settings(strict=True)


def test_mocked_smtp_delivery_succeeds(monkeypatch):
    _set_smtp_env(monkeypatch)

    class FakeSMTP:
        def __init__(self, host, port, timeout=None):
            self.host = host
            self.port = port
            self.timeout = timeout
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def ehlo(self):
            return None
        def starttls(self):
            return None
        def login(self, user, password):
            assert user == 'user@gmail.com'
            assert password == 'app-password-123'
        def send_message(self, message):
            assert message['To'] == 'recipient@example.com'
            assert 'verification' in message['Subject'].lower()

    monkeypatch.setattr(bm.smtplib, 'SMTP', FakeSMTP)
    assert bm._send_email('recipient@example.com', 'Verification code', '123456') is True


def test_smtp_authentication_failure_raises(monkeypatch):
    _set_smtp_env(monkeypatch)

    class FailAuthSMTP:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def ehlo(self):
            return None
        def starttls(self):
            return None
        def login(self, *args, **kwargs):
            raise smtplib.SMTPAuthenticationError(535, b'Authentication failed')
        def send_message(self, message):
            raise AssertionError('send_message should not be reached')

    monkeypatch.setattr(bm.smtplib, 'SMTP', FailAuthSMTP)
    with pytest.raises(smtplib.SMTPAuthenticationError):
        bm._send_email('recipient@example.com', 'Test', 'Body')


def test_smtp_connection_and_tls_failure_raises(monkeypatch):
    _set_smtp_env(monkeypatch)

    class FailTLS:
        def __init__(self, *args, **kwargs):
            pass
        def __enter__(self):
            return self
        def __exit__(self, exc_type, exc, tb):
            return False
        def ehlo(self):
            return None
        def starttls(self):
            raise TimeoutError('STARTTLS failed')
        def login(self, *args, **kwargs):
            raise AssertionError('login should not be reached')
        def send_message(self, message):
            raise AssertionError('send_message should not be reached')

    monkeypatch.setattr(bm.smtplib, 'SMTP', FailTLS)
    with pytest.raises(TimeoutError):
        bm._send_email('recipient@example.com', 'Test', 'Body')


def test_signup_when_email_delivery_fails(monkeypatch, tmp_path):
    original_db = bm.DB
    test_db = tmp_path / 'otp-email.db'
    try:
        bm.DB = test_db
        bm.init()
        monkeypatch.setenv('EXPOSE_DEV_OTP', '0')
        monkeypatch.setattr(bm, '_send_email', lambda *args, **kwargs: False)
        client = TestClient(bm.app)
        resp = client.post('/api/auth/signup', json={'email': 'new@deliver-fail.test', 'name': 'Fails', 'password': 'LongPassword123'})
        assert resp.status_code == 200, resp.text
        payload = resp.json()
        assert payload['email_sent'] is False
        assert 'could not be sent' in payload['message'].lower()
    finally:
        bm.DB = original_db


def test_resend_otp_cooldown_and_retry(monkeypatch, tmp_path):
    original_db = bm.DB
    test_db = tmp_path / 'otp-cooldown.db'
    try:
        bm.DB = test_db
        bm.init()
        monkeypatch.setenv('EXPOSE_DEV_OTP', '1')
        monkeypatch.setattr(bm, '_send_email', lambda *args, **kwargs: False)
        client = TestClient(bm.app)
        signup = client.post('/api/auth/signup', json={'email': 'cooldown@test.com', 'name': 'Cooldown', 'password': 'LongPassword123'})
        otp = signup.json()['development_otp']
        assert otp and len(otp) == 6
        cooldown = client.post('/api/auth/resend-otp', json={'email': 'cooldown@test.com'})
        assert cooldown.status_code == 429, cooldown.text
        assert 'Please wait' in cooldown.json()['detail']
        with bm.db() as c:
            c.execute('UPDATE users SET otp_last_sent=0 WHERE email=?', ('cooldown@test.com',))
        retry = client.post('/api/auth/resend-otp', json={'email': 'cooldown@test.com'})
        assert retry.status_code == 200, retry.text
        assert retry.json()['email_sent'] is False
    finally:
        bm.DB = original_db


def test_otp_expiry_and_wrong_attempts(monkeypatch, tmp_path):
    original_db = bm.DB
    test_db = tmp_path / 'otp-expiry.db'
    try:
        bm.DB = test_db
        bm.init()
        monkeypatch.setenv('EXPOSE_DEV_OTP', '1')
        monkeypatch.setattr(bm, '_send_email', lambda *args, **kwargs: False)
        client = TestClient(bm.app)
        signup = client.post('/api/auth/signup', json={'email': 'otp-expiry@test.com', 'name': 'Expiry', 'password': 'LongPassword123'})
        otp = signup.json()['development_otp']
        for _ in range(5):
            resp = client.post('/api/auth/verify-otp', json={'email': 'otp-expiry@test.com', 'otp': '000000'})
            assert resp.status_code in (400, 429), resp.text
        blocked = client.post('/api/auth/verify-otp', json={'email': 'otp-expiry@test.com', 'otp': otp})
        assert blocked.status_code == 429, blocked.text
        with bm.db() as c:
            c.execute('UPDATE users SET otp_attempts=0, otp_expires=0 WHERE email=?', ('otp-expiry@test.com',))
        expired = client.post('/api/auth/verify-otp', json={'email': 'otp-expiry@test.com', 'otp': otp})
        assert expired.status_code == 400, expired.text
        assert 'expired' in expired.json()['detail'].lower()
    finally:
        bm.DB = original_db


def test_google_oauth_reports_missing_configuration(monkeypatch):
    for key in ('GOOGLE_CLIENT_ID', 'GOOGLE_CLIENT_SECRET', 'GOOGLE_REDIRECT_URI'):
        monkeypatch.delenv(key, raising=False)
    client = TestClient(bm.app)
    response = client.get('/api/auth/oauth/google/start')
    assert response.status_code == 503
    assert 'GOOGLE_CLIENT_ID' in response.json()['detail']
    assert client.get('/api/oauth/providers').json()['google'] is False


def test_google_oauth_rejects_placeholder_credentials_and_bad_callback(monkeypatch):
    client = TestClient(bm.app)
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'your-google-oauth-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'your-google-oauth-client-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'http://127.0.0.1:8000/api/auth/oauth/google/callback')
    assert client.get('/api/auth/oauth/google/start').status_code == 503
    assert client.get('/api/oauth/providers').json()['google'] is False

    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-client-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'http://127.0.0.1:8000/wrong-callback')
    response = client.get('/api/auth/oauth/google/start')
    assert response.status_code == 503
    assert 'GOOGLE_REDIRECT_URI' in response.json()['detail']
    assert client.get('/api/oauth/providers').json()['google'] is False


def test_google_oauth_start_redirects_with_configured_callback(monkeypatch):
    monkeypatch.setenv('GOOGLE_CLIENT_ID', 'test-client-id')
    monkeypatch.setenv('GOOGLE_CLIENT_SECRET', 'test-client-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI', 'http://127.0.0.1:8000/api/auth/oauth/google/callback')
    client = TestClient(bm.app)
    response = client.get('/api/auth/oauth/google/start', follow_redirects=False)
    assert response.status_code == 302
    assert response.headers['location'].startswith('https://accounts.google.com/o/oauth2/v2/auth?')
    assert 'redirect_uri=http%3A%2F%2F127.0.0.1%3A8000%2Fapi%2Fauth%2Foauth%2Fgoogle%2Fcallback' in response.headers['location']
