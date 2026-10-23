"""VeriBrand API - brand governance platform (CodeAxis Round 2/3 build).

Roles are enforced server-side on every protected endpoint:
  Creator  : submit drafts, accept/edit/dismiss fixes on OWN drafts, comment, revise own drafts
  Reviewer : see all drafts, comment, approve / request changes, view audit + guideline health
  Admin    : everything above + brand kit, rules, users
"""
import os, re, io, json, time, hashlib, secrets, smtplib, sqlite3, zipfile, urllib.request, urllib.parse, base64, math, mimetypes, hmac, csv, html, uuid, logging
from collections import defaultdict
from contextlib import contextmanager
from datetime import date, datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from fastapi import FastAPI, HTTPException, Depends, Header, Request, UploadFile, File, Form
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from fastapi.responses import RedirectResponse, HTMLResponse, StreamingResponse, JSONResponse
from pydantic import BaseModel

import engine

ROOT = Path(__file__).resolve().parent

def _load_env(env_path: str | Path | None = None):
    env_file = Path(env_path) if env_path is not None else ROOT / '.env'
    if not env_file.exists():
        return
    for line in env_file.read_text(encoding='utf-8').splitlines():
        line = line.strip()
        if line and not line.startswith('#') and '=' in line:
            key, value = line.split('=', 1)
            key = key.strip(); value = value.strip().strip('"\'')
            if key and key not in os.environ:
                os.environ[key] = value
_load_env()

DATA_DIR = Path(os.getenv('DATA_DIR', ROOT / 'data')); DATA_DIR.mkdir(parents=True, exist_ok=True)
DATABASE_URL = os.getenv('DATABASE_URL', '').strip()
DB = DATA_DIR / 'veribrand.db'
DEMO_MODE = os.getenv('DEMO_MODE', '0') == '1'
APP_ENV = os.getenv('APP_ENV', 'development').strip().lower()
EXPOSE_DEV_OTP = os.getenv('EXPOSE_DEV_OTP', '0') == '1'
DEMO_PASSWORD = 'Demo@12345'
ROLES = ('Creator', 'Reviewer', 'Admin')

# Fail closed on the two most dangerous demo-only settings in production.
# This prevents accidental public exposure of seeded accounts or OTP/reset tokens.
if APP_ENV == 'production' and DEMO_MODE:
    raise RuntimeError('Unsafe configuration: DEMO_MODE=1 is forbidden when APP_ENV=production')
if APP_ENV == 'production' and EXPOSE_DEV_OTP:
    raise RuntimeError('Unsafe configuration: EXPOSE_DEV_OTP=1 is forbidden when APP_ENV=production')
if APP_ENV == 'production':
    _required_smtp = ('SMTP_HOST', 'SMTP_PORT', 'SMTP_USER', 'SMTP_PASSWORD', 'SMTP_FROM')
    _missing_smtp = [key for key in _required_smtp if not os.getenv(key, '').strip()]
    if _missing_smtp:
        raise RuntimeError('Production startup blocked: configure SMTP settings: ' + ', '.join(_missing_smtp))
    _admin_email = os.getenv('ADMIN_EMAIL', '').strip().lower()
    if not _admin_email or not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', _admin_email):
        raise RuntimeError('Production startup blocked: set ADMIN_EMAIL to the designated initial administrator email')
    if not os.getenv('DATA_DIR', '').strip():
        raise RuntimeError('Production startup blocked: set DATA_DIR to an explicitly configured persistent storage path')

app = FastAPI(title='VeriBrand API', version='2.2.0', description='Check it before you publish it.')
logger = logging.getLogger('veribrand')
if os.getenv('CORS_ORIGINS'):
    app.add_middleware(CORSMiddleware, allow_origins=[o.strip() for o in os.getenv('CORS_ORIGINS').split(',')],
                       allow_credentials=False, allow_methods=['*'], allow_headers=['*'])

@app.middleware('http')
async def security_headers(request: Request, call_next):
    request_id = request.headers.get('X-Request-ID', '')[:80]
    if not request_id or not re.fullmatch(r'[A-Za-z0-9._-]+', request_id):
        request_id = uuid.uuid4().hex
    request.state.request_id = request_id
    started = time.monotonic()
    try:
        resp = await call_next(request)
    except Exception:
        logger.exception('request_failed request_id=%s method=%s path=%s', request_id, request.method, request.url.path)
        raise
    resp.headers['X-Request-ID'] = request_id
    resp.headers.setdefault('X-Content-Type-Options', 'nosniff'); resp.headers.setdefault('X-Frame-Options', 'DENY')
    resp.headers.setdefault('Referrer-Policy', 'no-referrer')
    resp.headers.setdefault('Permissions-Policy', 'camera=(), microphone=(self), geolocation=()')
    resp.headers.setdefault('Content-Security-Policy', "default-src 'self'; style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; img-src 'self' data: blob:; frame-ancestors 'none'")
    if request.url.path.startswith(('/api/auth/', '/api/account/')):
        resp.headers['Cache-Control'] = 'no-store'
    logger.info('request_complete request_id=%s method=%s path=%s status=%s duration_ms=%d', request_id, request.method, request.url.path, resp.status_code, int((time.monotonic()-started)*1000))
    return resp

# ------------------------------------------------------------------ database
class _HybridRow(dict):
    def __getitem__(self,key):
        if isinstance(key,int): return list(self.values())[key]
        return super().__getitem__(key)

class _PgCursor:
    def __init__(self, cursor, lastrowid=None): self._cursor=cursor; self.lastrowid=lastrowid
    def fetchone(self):
        row=self._cursor.fetchone()
        return _HybridRow(row) if isinstance(row,dict) else row
    def fetchall(self):
        rows=self._cursor.fetchall()
        return [_HybridRow(row) if isinstance(row,dict) else row for row in rows]
    def __iter__(self): return iter(self._cursor)
    def __getattr__(self,n): return getattr(self._cursor,n)

class _PgConnection:
    def __init__(self, conn): self.conn=conn
    def execute(self, sql, params=()):
        import re
        query=sql
        is_insert=bool(re.match(r"\s*INSERT\s+INTO\s+(users|facts|rules|reviews|review_versions|decisions|audit|comments|feedback|knowledge_chunks|subscriptions)\b",sql,re.I))
        if is_insert and not re.search(r"\bRETURNING\b",query,re.I): query=query.rstrip().rstrip(';')+' RETURNING id'
        query=query.replace('?', '%s')
        query=re.sub(r'\bCURRENT_TIMESTAMP\b(?!\s*::)', 'CURRENT_TIMESTAMP::text', query)
        cur=self.conn.cursor(); cur.execute(query,params)
        last=None
        if is_insert:
            try:
                row=cur.fetchone()
                if row: last=row.get('id') if isinstance(row,dict) else row[0]
            except Exception: pass
        return _PgCursor(cur,last)
    def executescript(self, script):
        # Each statement in SCHEMA is a simple CREATE TABLE IF NOT EXISTS.
        for statement in script.split(';'):
            statement=statement.strip()
            if not statement: continue
            statement=statement.replace('INTEGER PRIMARY KEY','BIGSERIAL PRIMARY KEY').replace('TEXT DEFAULT CURRENT_TIMESTAMP','TEXT DEFAULT (CURRENT_TIMESTAMP::text)')
            cur=self.conn.cursor(); cur.execute(statement)
    def commit(self): self.conn.commit()
    def rollback(self): self.conn.rollback()
    def close(self): self.conn.close()

@contextmanager
def db():
    c=None
    try:
        if DATABASE_URL:
            try: import psycopg
            except ImportError as exc: raise RuntimeError('DATABASE_URL is set but psycopg is missing; install requirements') from exc
            from psycopg.rows import dict_row
            raw=psycopg.connect(DATABASE_URL, row_factory=dict_row, connect_timeout=10)
            c=_PgConnection(raw)
        else:
            c=sqlite3.connect(DB, timeout=15); c.row_factory=sqlite3.Row
        yield c; c.commit()
    except Exception:
        if c: c.rollback()
        raise
    finally:
        if c: c.close()

SCHEMA = '''
CREATE TABLE IF NOT EXISTS users(id INTEGER PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL,
  role TEXT NOT NULL DEFAULT 'Creator', verified INTEGER NOT NULL DEFAULT 0, otp_hash TEXT, otp_expires INTEGER, otp_last_sent INTEGER,
  otp_attempts INTEGER NOT NULL DEFAULT 0, reset_hash TEXT, reset_expires INTEGER, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires INTEGER NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS facts(id INTEGER PRIMARY KEY, name TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL,
  category TEXT NOT NULL DEFAULT 'claim', review_by TEXT DEFAULT '', updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS rules(id INTEGER PRIMARY KEY, rule TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, pattern TEXT,
  source TEXT NOT NULL, replacement TEXT, suggestion TEXT, enabled INTEGER NOT NULL DEFAULT 1);
CREATE TABLE IF NOT EXISTS reviews(id INTEGER PRIMARY KEY, title TEXT, owner_id INTEGER, owner_name TEXT, status TEXT DEFAULT 'In review',
  intent TEXT DEFAULT '', protected TEXT DEFAULT '[]', version INTEGER DEFAULT 1, draft TEXT, findings TEXT, ledger TEXT, stats TEXT,
  approved_by TEXT, approved_at TEXT, content_hash TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS review_versions(id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, version INTEGER NOT NULL, draft TEXT, findings TEXT,
  ledger TEXT, note TEXT, created_by TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS decisions(id INTEGER PRIMARY KEY, review_id INTEGER, version INTEGER, finding_id TEXT, rule_id TEXT, decision TEXT,
  note TEXT, actor TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS audit(id INTEGER PRIMARY KEY, actor TEXT, action TEXT NOT NULL, entity TEXT, entity_id TEXT, details TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS comments(id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, author TEXT NOT NULL, role TEXT, body TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS feedback(id INTEGER PRIMARY KEY, email TEXT, rating INTEGER, message TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS chatlog(id INTEGER PRIMARY KEY, question TEXT, answer TEXT, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS knowledge_chunks(id INTEGER PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, vector TEXT NOT NULL, updated TEXT DEFAULT CURRENT_TIMESTAMP, UNIQUE(kind,source_id));
CREATE TABLE IF NOT EXISTS subscriptions(id INTEGER PRIMARY KEY, user_id INTEGER NOT NULL, provider TEXT NOT NULL, customer_id TEXT, subscription_id TEXT, status TEXT NOT NULL DEFAULT 'pending', plan TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP, updated TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS oauth_states(state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, expires INTEGER NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS stripe_events(event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created TEXT DEFAULT CURRENT_TIMESTAMP);
CREATE TABLE IF NOT EXISTS review_events(id INTEGER PRIMARY KEY, review_id INTEGER NOT NULL, actor TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created TEXT DEFAULT CURRENT_TIMESTAMP);
'''

def _pw(password, salt=None):
    salt = salt or secrets.token_hex(16)
    return f"{salt}${hashlib.pbkdf2_hmac('sha256', password.encode(), salt.encode(), 180000).hex()}"
def _verify_pw(password, stored):
    try:
        salt, digest = stored.split('$', 1); return secrets.compare_digest(_pw(password, salt).split('$', 1)[1], digest)
    except Exception: return False
def _hash_token(t): return hashlib.sha256(t.encode()).hexdigest()

def init():
    with db() as c:
        c.executescript(SCHEMA)
        # Never seed fictional Lumio brand facts into a real-user deployment.
        if DEMO_MODE and c.execute('SELECT COUNT(*) FROM facts').fetchone()[0] == 0:
            d = lambda n: (date.today() + timedelta(days=n)).isoformat()
            c.executemany('INSERT INTO facts(name,value,source,category,review_by) VALUES(?,?,?,?,?)', [
                ('Starter plan price', 'Rs 399', 'Lumio Pricing Sheet v3', 'price', d(90)),
                ('Annual plan price', 'Rs 3,999', 'Lumio Pricing Sheet v3', 'price', d(90)),
                ('Premium plan price', 'Rs 999', 'Lumio Pricing Sheet v2 (overdue)', 'price', d(-30)),
                ('Active users', 'Over 2 lakh users', 'Lumio Impact Report Q2', 'claim', d(120)),
                ('Data security', 'Bank-grade 256-bit encryption', 'Lumio Security Whitepaper', 'claim', d(180)),
                ('Brand tone', 'Calm, helpful, confident; avoid hype', 'Lumio Brand Voice Guide §2.1', 'policy', ''),
                ('Approved product name', 'Lumio budgeting app', 'Lumio Terminology List v1', 'term', '')])
        if DEMO_MODE and c.execute('SELECT COUNT(*) FROM rules').fetchone()[0] == 0:
            c.executemany('INSERT INTO rules(rule,kind,severity,pattern,source,replacement,suggestion) VALUES(?,?,?,?,?,?,?)', [
                ('Avoid stacked exclamation marks', 'tone', 'Low', r'!{2,}', 'Lumio Brand Voice Guide §2.1', '.', 'Use one calm call to action.'),
                ('Avoid hype-heavy wording', 'tone', 'Medium', r'\b(?:miracle|unbelievable|mind-?blowing|game-?changer|revolutionary|insane)\b', 'Lumio Brand Voice Guide §2.2', None, 'Use specific, factual language instead.'),
                ('Avoid slang the brand voice forbids', 'tone', 'Medium', r'\b(?:yolo|lit af|dope|bro|fam|dudes?)\b', 'Lumio Brand Voice Guide §2.3', None, 'Keep the tone friendly but professional.'),
                ('Avoid ALL-CAPS emphasis', 'tone', 'Low', r'(?-i:\b(?:[A-Z]{3,}\s+)+[A-Z]{3,}\b)', 'Lumio Brand Voice Guide §2.4', None, 'Use sentence case; emphasise with wording, not capitals.'),
                ('Use the approved product name "Lumio budgeting app"', 'term', 'Medium', r'\blumio\s+(?:budget|money|finance)\s+app\b', 'Lumio Terminology List v1', 'Lumio budgeting app', 'Use the approved product name.'),
                ('Say "affordable", not "cheap"', 'term', 'Low', r'\b(?:cheap|dirt[- ]cheap)\b', 'Lumio Terminology List v1', 'affordable', 'Use the approved term.')])
        if DEMO_MODE:
            for email, name, role in (('creator@lumio.demo', 'Demo Creator', 'Creator'), ('reviewer@lumio.demo', 'Demo Reviewer', 'Reviewer'), ('admin@lumio.demo', 'Demo Admin', 'Admin')):
                if not c.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone():
                    c.execute('INSERT INTO users(email,name,password_hash,role,verified) VALUES(?,?,?,?,1)', (email, name, _pw(DEMO_PASSWORD), role))
init()

def _audit(action, entity='', entity_id='', details='', actor='system'):
    with db() as c: c.execute('INSERT INTO audit(actor,action,entity,entity_id,details) VALUES(?,?,?,?,?)', (actor, action, entity, str(entity_id), str(details)[:2000]))

# ------------------------------------------------------------------ auth plumbing
_hits = defaultdict(list)
def _rate(request: Request, bucket, limit=None, window=60):
    limit = limit or int(os.getenv('RATE_LIMIT', '30')); key = (request.client.host if request.client else '?', bucket); now = time.time()
    _hits[key] = [t for t in _hits[key] if now - t < window]
    # Bound the in-process limiter's memory footprint under high-cardinality traffic.
    if len(_hits) > 10000:
        for old_key in list(_hits)[:2000]:
            if not _hits[old_key] or now - _hits[old_key][-1] >= 3600: _hits.pop(old_key, None)
    if len(_hits[key]) >= limit: raise HTTPException(429, 'Too many attempts. Please wait a minute and try again.')
    _hits[key].append(now)

def _current_user(authorization):
    if not authorization or not authorization.lower().startswith('bearer '): return None
    with db() as c:
        row = c.execute('SELECT u.* FROM sessions s JOIN users u ON u.id=s.user_id WHERE s.token_hash=? AND s.expires>?',
                        (_hash_token(authorization.split(' ', 1)[1]), int(time.time()))).fetchone()
    return dict(row) if row else None

def current_user(authorization: str | None = Header(default=None)):
    u = _current_user(authorization)
    if not u: raise HTTPException(401, 'Sign in required')
    return u

def need(*roles):
    def dep(u=Depends(current_user)):
        if u['role'] not in roles: raise HTTPException(403, 'This action needs the role: ' + ' or '.join(roles))
        return u
    return dep

def _public_user(u): return {'id': u['id'], 'email': u['email'], 'name': u['name'], 'role': u['role'], 'verified': bool(u['verified'])}

def _smtp_settings(strict: bool = False):
    host = os.getenv('SMTP_HOST', '').strip()
    user = os.getenv('SMTP_USER', '').strip()
    pw = os.getenv('SMTP_PASSWORD', '')
    sender = os.getenv('SMTP_FROM', user).strip()
    missing = [name for name, value in (
        ('SMTP_HOST', host),
        ('SMTP_USER', user),
        ('SMTP_PASSWORD', pw),
        ('SMTP_FROM', sender),
    ) if not value]
    if missing:
        if strict:
            raise RuntimeError('SMTP settings are missing: ' + ', '.join(missing))
        return None
    try:
        port = int(os.getenv('SMTP_PORT', '587'))
    except (TypeError, ValueError):
        raise RuntimeError('SMTP_PORT must be a valid TCP port')
    if not (1 <= port <= 65535):
        raise RuntimeError('SMTP_PORT must be between 1 and 65535')
    return {'host': host, 'port': port, 'user': user, 'password': pw, 'sender': sender}


def _send_email(to, subject, body):
    """Send transactional email using configured STARTTLS SMTP."""
    settings = _smtp_settings(strict=False)
    if not settings:
        return False
    message = EmailMessage()
    message['Subject'] = subject
    message['From'] = settings['sender']
    message['To'] = to
    message.set_content(body)
    try:
        # Gmail requires STARTTLS on port 587; this also validates the remote certificate.
        with smtplib.SMTP(settings['host'], settings['port'], timeout=12) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(settings['user'], settings['password'])
            smtp.send_message(message)
    except (smtplib.SMTPException, TimeoutError, OSError):
        logger.exception('smtp_send_failed category=email delivery host=%s port=%s recipient=%s', settings['host'], settings['port'], to)
        raise
    return True

def _expose_dev(): return os.getenv('EXPOSE_DEV_OTP', '0') == '1'

class SignupIn(BaseModel): email: str; name: str; password: str; role: str = 'Creator'; invite_code: str = ''
class LoginIn(BaseModel): email: str; password: str
class OTPIn(BaseModel): email: str; otp: str
class EmailIn(BaseModel): email: str
class ResetIn(BaseModel): email: str; token: str; new_password: str
class RoleIn(BaseModel): role: str

@app.post('/api/auth/signup')
def signup(x: SignupIn, request: Request):
    _rate(request, 'signup', 10)
    email = x.email.strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+', email): raise HTTPException(400, 'Enter a valid email address')
    if len(x.password) < 12: raise HTTPException(400, 'Password must be at least 12 characters')
    if len(x.password) > 128: raise HTTPException(400, 'Password must be at most 128 characters')
    if len(email) > 254: raise HTTPException(400, 'Email address is too long')
    if not x.name.strip(): raise HTTPException(400, 'Enter your name')
    role, note = 'Creator', ''
    with db() as c:
        if c.execute('SELECT 1 FROM users WHERE email=?', (email,)).fetchone(): raise HTTPException(409, 'An account with this email already exists')
        # Never grant Admin based on signup order: public first-signup admin is takeover-prone.
        # Provision the initial admin by setting ADMIN_EMAIL before signup, or use a secret ADMIN_INVITE.
        admin_email = os.getenv('ADMIN_EMAIL', '').strip().lower()
        if admin_email and email == admin_email: role = 'Admin'
        elif x.role == 'Reviewer' and os.getenv('REVIEWER_INVITE') and secrets.compare_digest(x.invite_code, os.getenv('REVIEWER_INVITE')): role = 'Reviewer'
        elif x.role == 'Admin' and os.getenv('ADMIN_INVITE') and secrets.compare_digest(x.invite_code, os.getenv('ADMIN_INVITE')): role = 'Admin'
        elif x.role != 'Creator': note = f' You were given the Creator role; an Admin can upgrade you to {x.role}.'
        otp = f'{secrets.randbelow(10**6):06d}'; now = int(time.time())
        uid = c.execute('INSERT INTO users(email,name,password_hash,role,otp_hash,otp_expires,otp_last_sent) VALUES(?,?,?,?,?,?,?)',
                        (email, x.name.strip()[:100], _pw(x.password), role, _hash_token(otp), now + 600, now)).lastrowid
    try: sent = _send_email(email, 'VeriBrand email verification', f'Your verification code is {otp}. It expires in 10 minutes.')
    except Exception:
        logger.exception('signup_email_delivery_failed request_id=%s email=%s', getattr(request.state, 'request_id', 'n/a'), email)
        sent = False
    _audit('signup', 'user', uid, f'role={role}', email)
    if sent:
        message = 'Account created. Verification email sent.' + note
    else:
        message = 'Account created, but the verification email could not be sent. Please use resend after the cooldown window.' + note
    out = {'ok': True, 'message': message, 'granted_role': role, 'email_sent': sent}
    if not sent and _expose_dev(): out['development_otp'] = otp
    return out

@app.post('/api/auth/verify-otp')
def verify_otp(x: OTPIn, request: Request):
    _rate(request, 'otp', 15); email = x.email.strip().lower(); now = int(time.time())
    if not re.fullmatch(r'\d{6}', x.otp.strip()): raise HTTPException(400, 'OTP must be 6 digits')
    with db() as c:
        u = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if not u: raise HTTPException(404, 'Account not found')
        if u['verified']: return {'ok': True, 'message': 'Email already verified'}
        if u['otp_attempts'] >= 5: raise HTTPException(429, 'Too many wrong codes. Request a new OTP.')
        if not u['otp_hash'] or not u['otp_expires'] or u['otp_expires'] < now: raise HTTPException(400, 'OTP expired. Request a new code.')
        if not secrets.compare_digest(_hash_token(x.otp.strip()), u['otp_hash']):
            c.execute('UPDATE users SET otp_attempts=otp_attempts+1 WHERE id=?', (u['id'],)); c.commit(); raise HTTPException(400, 'Incorrect OTP')
        c.execute('UPDATE users SET verified=1,otp_hash=NULL,otp_expires=NULL,otp_attempts=0 WHERE id=?', (u['id'],))
    _audit('email_verified', 'user', u['id'], '', email); return {'ok': True, 'message': 'Email verified. You can now sign in.'}

@app.post('/api/auth/resend-otp')
def resend_otp(x: EmailIn, request: Request):
    _rate(request, 'resend', 8); email = x.email.strip().lower(); now = int(time.time())
    with db() as c:
        u = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
        if not u: raise HTTPException(404, 'Account not found')
        if u['verified']: return {'ok': True, 'message': 'Email is already verified'}
        if u['otp_last_sent']:
            retry_in = 45 - (now - u['otp_last_sent'])
            if retry_in > 0:
                raise HTTPException(429, f'Please wait {max(1, int(retry_in))} seconds before requesting another OTP')
        otp = f'{secrets.randbelow(10**6):06d}'
        c.execute('UPDATE users SET otp_hash=?,otp_expires=?,otp_last_sent=?,otp_attempts=0 WHERE id=?', (_hash_token(otp), now + 600, now, u['id']))
    try: sent = _send_email(email, 'Your new VeriBrand verification code', f'Your code is {otp}. It expires in 10 minutes.')
    except Exception:
        logger.exception('resend_email_delivery_failed request_id=%s email=%s', getattr(request.state, 'request_id', 'n/a'), email)
        sent = False
    out = {'ok': True, 'email_sent': sent, 'message': 'Verification email sent.' if sent else 'We could not send a verification email right now. Please try again in a few moments.'}
    if not sent and _expose_dev(): out['development_otp'] = otp
    return out

@app.post('/api/auth/login')
def login(x: LoginIn, request: Request):
    _rate(request, 'login', 12); email = x.email.strip().lower()
    if len(x.password) > 128: raise HTTPException(401, 'Email or password is incorrect')
    with db() as c: u = c.execute('SELECT * FROM users WHERE email=?', (email,)).fetchone()
    if not u or not _verify_pw(x.password, u['password_hash']): raise HTTPException(401, 'Email or password is incorrect')
    if not u['verified']: raise HTTPException(403, 'Please verify your email first')
    token = secrets.token_urlsafe(36); exp = int(time.time()) + 7 * 86400
    with db() as c:
        c.execute('DELETE FROM sessions WHERE expires<?', (int(time.time()),)); c.execute('INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)', (_hash_token(token), u['id'], exp))
    _audit('login', 'user', u['id'], '', email)
    return {'token': token, 'expires_at': exp, 'user': _public_user(dict(u))}

@app.post('/api/auth/logout')
def logout(authorization: str | None = Header(default=None)):
    if authorization and authorization.lower().startswith('bearer '):
        with db() as c: c.execute('DELETE FROM sessions WHERE token_hash=?', (_hash_token(authorization.split(' ', 1)[1]),))
    return {'ok': True}

@app.get('/api/auth/me')
def me(u=Depends(current_user)): return _public_user(u)

@app.post('/api/auth/forgot-password')
def forgot(x: EmailIn, request: Request):
    _rate(request, 'forgot', 8); email = x.email.strip().lower(); token = secrets.token_urlsafe(32)
    with db() as c:
        u = c.execute('SELECT id FROM users WHERE email=?', (email,)).fetchone()
        if u: c.execute('UPDATE users SET reset_hash=?,reset_expires=? WHERE id=?', (_hash_token(token), int(time.time()) + 1800, u['id']))
    if u:
        try: _send_email(email, 'Reset your VeriBrand password', f'Reset token: {token}. Expires in 30 minutes.')
        except Exception:
            logger.exception('password_reset_email_failed request_id=%s email=%s', getattr(request.state, 'request_id', 'n/a'), email)
    out = {'ok': True, 'message': 'If the account exists, reset instructions have been sent.'}
    if u and _expose_dev(): out['development_reset_token'] = token
    return out

@app.post('/api/auth/reset-password')
def reset(x: ResetIn, request: Request):
    _rate(request, 'reset', 8)
    if len(x.new_password) < 12: raise HTTPException(400, 'Password must be at least 12 characters')
    if len(x.new_password) > 128: raise HTTPException(400, 'Password must be at most 128 characters')
    with db() as c:
        u = c.execute('SELECT * FROM users WHERE email=?', (x.email.strip().lower(),)).fetchone()
        if not u or not u['reset_hash'] or (u['reset_expires'] or 0) < time.time() or not secrets.compare_digest(_hash_token(x.token), u['reset_hash']):
            raise HTTPException(400, 'Reset token is invalid or expired')
        c.execute('UPDATE users SET password_hash=?,reset_hash=NULL,reset_expires=NULL WHERE id=?', (_pw(x.new_password), u['id'])); c.execute('DELETE FROM sessions WHERE user_id=?', (u['id'],))
    _audit('password_reset', 'user', u['id'], '', u['email']); return {'ok': True, 'message': 'Password updated. Sign in again.'}

@app.get('/api/demo-accounts')
def demo_accounts():
    if not DEMO_MODE: return {'demo_mode': False, 'accounts': []}
    return {'demo_mode': True, 'password': DEMO_PASSWORD, 'accounts': [{'email': 'creator@lumio.demo', 'role': 'Creator'}, {'email': 'reviewer@lumio.demo', 'role': 'Reviewer'}, {'email': 'admin@lumio.demo', 'role': 'Admin'}]}

# ------------------------------------------------------------------ users (admin)
@app.get('/api/users')
def users(u=Depends(need('Admin'))):
    with db() as c: return [{k: r[k] for k in ('id', 'email', 'name', 'role', 'verified', 'created')} for r in c.execute('SELECT * FROM users ORDER BY id')]

@app.patch('/api/users/{uid}/role')
def set_role(uid: int, x: RoleIn, u=Depends(need('Admin'))):
    if x.role not in ROLES: raise HTTPException(400, 'Invalid role')
    with db() as c:
        t = c.execute('SELECT * FROM users WHERE id=?', (uid,)).fetchone()
        if not t: raise HTTPException(404, 'User not found')
        if t['role'] == 'Admin' and x.role != 'Admin' and c.execute("SELECT COUNT(*) FROM users WHERE role='Admin'").fetchone()[0] <= 1:
            raise HTTPException(409, 'There must be at least one Admin')
        c.execute('UPDATE users SET role=? WHERE id=?', (x.role, uid))
    _audit('role_changed', 'user', uid, f"{t['role']} -> {x.role}", u['email']); return {'ok': True}

# ------------------------------------------------------------------ brand kit
class FactIn(BaseModel): name: str; value: str; source: str = 'User supplied brand kit'; category: str = 'claim'; review_by: str = ''
class RuleIn(BaseModel): rule: str; kind: str = 'tone'; severity: str = 'Medium'; pattern: str; source: str; replacement: str | None = None; suggestion: str = ''

def _fact_out(r):
    d = dict(r); rb = engine.parse_date(d.get('review_by')); d['stale'] = bool(rb and rb < date.today())
    d['days_to_review'] = (rb - date.today()).days if rb else None; return d
def _check_fact(f: FactIn):
    if f.category not in ('price', 'claim', 'policy', 'term'): raise HTTPException(400, 'Category must be price, claim, policy or term')
    if f.review_by and not engine.parse_date(f.review_by): raise HTTPException(400, 'Review-by date must be YYYY-MM-DD')
    if not f.name.strip() or not f.value.strip(): raise HTTPException(400, 'Name and value are required')
    if f.category == 'price' and engine._num(f.value) is None: raise HTTPException(400, 'A price fact needs a number, e.g. Rs 399')

@app.get('/api/facts')
def facts(u=Depends(current_user)):
    with db() as c: return [_fact_out(r) for r in c.execute('SELECT * FROM facts ORDER BY category, id')]
@app.post('/api/facts')
def add_fact(f: FactIn, u=Depends(need('Admin'))):
    _check_fact(f)
    with db() as c:
        i = c.execute('INSERT INTO facts(name,value,source,category,review_by) VALUES(?,?,?,?,?)', (f.name.strip(), f.value.strip(), f.source.strip(), f.category, f.review_by)).lastrowid
        c.execute('DELETE FROM knowledge_chunks')
        r = _fact_out(c.execute('SELECT * FROM facts WHERE id=?', (i,)).fetchone())
    _audit('fact_added', 'fact', i, f'{f.name}={f.value}', u['email']); return r
@app.put('/api/facts/{fid}')
def update_fact(fid: int, f: FactIn, u=Depends(need('Admin'))):
    _check_fact(f)
    with db() as c:
        if not c.execute('SELECT 1 FROM facts WHERE id=?', (fid,)).fetchone(): raise HTTPException(404, 'Fact not found')
        c.execute('UPDATE facts SET name=?,value=?,source=?,category=?,review_by=?,updated=CURRENT_TIMESTAMP WHERE id=?', (f.name.strip(), f.value.strip(), f.source.strip(), f.category, f.review_by, fid))
        c.execute('DELETE FROM knowledge_chunks')
        r = _fact_out(c.execute('SELECT * FROM facts WHERE id=?', (fid,)).fetchone())
    _audit('fact_updated', 'fact', fid, f'{f.name}={f.value}', u['email']); return r
@app.delete('/api/facts/{fid}')
def del_fact(fid: int, u=Depends(need('Admin'))):
    with db() as c:
        c.execute('DELETE FROM facts WHERE id=?', (fid,)); c.execute('DELETE FROM knowledge_chunks')
    _audit('fact_removed', 'fact', fid, '', u['email']); return {'ok': True}
@app.get('/api/rules')
def rules(u=Depends(current_user)):
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM rules ORDER BY id')]
@app.post('/api/rules')
def add_rule(x: RuleIn, u=Depends(need('Admin'))):
    if len(x.pattern) > 300: raise HTTPException(400, 'Pattern too long')
    if x.kind not in ('tone', 'term') or x.severity not in ('High', 'Medium', 'Low'): raise HTTPException(400, 'Invalid kind or severity')
    try: re.compile(x.pattern)
    except re.error as e: raise HTTPException(400, f'Invalid pattern: {e}')
    with db() as c:
        i = c.execute('INSERT INTO rules(rule,kind,severity,pattern,source,replacement,suggestion) VALUES(?,?,?,?,?,?,?)', (x.rule, x.kind, x.severity, x.pattern, x.source, x.replacement, x.suggestion)).lastrowid
        c.execute('DELETE FROM knowledge_chunks')
    _audit('rule_added', 'rule', i, x.rule, u['email']); return {'id': i}
@app.patch('/api/rules/{rid}')
def toggle_rule(rid: int, u=Depends(need('Admin'))):
    with db() as c:
        r = c.execute('SELECT enabled FROM rules WHERE id=?', (rid,)).fetchone()
        if not r: raise HTTPException(404, 'Rule not found')
        c.execute('UPDATE rules SET enabled=? WHERE id=?', (0 if r['enabled'] else 1, rid)); c.execute('DELETE FROM knowledge_chunks')
    _audit('rule_toggled', 'rule', rid, '', u['email']); return {'ok': True}

# ------------------------------------------------------------------ AI (optional) with disclosure
# Supports OpenAI-compatible chat completions, including Google's Gemini
# OpenAI-compatibility endpoint. Provider calls are still unverified until the
# deployer runs a live smoke test with their own key/model.
def _ai_config():
    provider = os.getenv('AI_PROVIDER', 'auto').strip().lower()
    if provider == 'gemini' or (provider == 'auto' and os.getenv('GEMINI_API_KEY')):
        return {'provider': 'gemini', 'key': os.getenv('GEMINI_API_KEY', '').strip(),
                'base': os.getenv('GEMINI_BASE_URL', 'https://generativelanguage.googleapis.com/v1beta/openai').rstrip('/'),
                'model': os.getenv('GEMINI_MODEL', 'gemini-2.5-flash').strip()}
    if provider in ('openai', 'auto'):
        return {'provider': 'openai', 'key': os.getenv('OPENAI_API_KEY', '').strip(),
                'base': os.getenv('OPENAI_BASE_URL', 'https://api.openai.com/v1').rstrip('/'),
                'model': os.getenv('OPENAI_MODEL', 'gpt-4o-mini').strip()}
    return {'provider': provider, 'key': '', 'base': '', 'model': ''}

def _llm():
    cfg = _ai_config()
    if not cfg['key']: return None
    if cfg['provider'] not in ('gemini', 'openai'): return None
    def call(system, user):
        body_obj = {'model': cfg['model'], 'temperature': 0,
                    'messages': [{'role': 'system', 'content': system}, {'role': 'user', 'content': user}]}
        # JSON mode is supported by many OpenAI-compatible endpoints but not
        # consistently implemented by every Gemini model; the system prompt
        # already requests the strict JSON schema and engine verifies output.
        if cfg['provider'] == 'openai': body_obj['response_format'] = {'type': 'json_object'}
        body = json.dumps(body_obj).encode()
        req = urllib.request.Request(cfg['base'] + '/chat/completions', data=body,
            headers={'Authorization': 'Bearer ' + cfg['key'], 'Content-Type': 'application/json'}, method='POST')
        with urllib.request.urlopen(req, timeout=35) as r:
            payload = json.loads(r.read().decode())
        return payload['choices'][0]['message']['content']
    return call

@app.get('/api/ai-disclosure')
def ai_disclosure():
    cfg = _ai_config(); on = bool(cfg['key']) and cfg['provider'] in ('gemini', 'openai')
    return {'external_ai_enabled': on, 'provider': cfg['provider'] if on else None,
            'provider_endpoint': cfg['base'] if on else None,
            'model': cfg['model'] if on else None, 'redaction_before_external_call': True,
            'what_is_sent': 'Only the draft text (emails and phone numbers replaced by [EMAIL]/[PHONE]) and the few brand-kit passages retrieved for it.' if on else 'Nothing leaves this server: rules and the claim ledger run locally.',
            'verification': 'Every AI finding must cite a brand-kit passage id and quote it exactly, and the flagged text must exist in the draft. Anything else is dropped.',
            'human_in_control': 'No suggestion is applied automatically. People accept, edit or dismiss; reviewers approve.'}

# ------------------------------------------------------------------ reviews
class ReviewIn(BaseModel): title: str = 'Untitled draft'; draft: str; intent: str = ''; protected: list[str] = []; use_ai: bool = True
class DecisionIn(BaseModel): finding_id: str; decision: str; note: str = ''; replacement: str = ''
class WorkflowIn(BaseModel): action: str; note: str = ''; force: bool = False
class ReviseIn(BaseModel): draft: str = ''; note: str = ''; expected_version: int | None = None
class CommentIn(BaseModel): body: str

def _out(r):
    d = dict(r)
    for k, dflt in (('findings', []), ('ledger', []), ('stats', {}), ('protected', [])):
        d[k] = json.loads(d[k]) if d.get(k) else dflt
    return d

def _get(c, rid, u):
    r = c.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone()
    if not r: raise HTTPException(404, 'Review not found')
    if u['role'] == 'Creator' and r['owner_id'] != u['id']: raise HTTPException(403, 'You can only open your own drafts')
    return r

def _run(draft, protected, use_ai=True):
    with db() as c:
        fs = [dict(r) for r in c.execute('SELECT * FROM facts')]; rs = [dict(r) for r in c.execute('SELECT * FROM rules')]
    return engine.analyze(draft, fs, rs, protected, call_llm=_llm() if use_ai else None)

def _clean_protected(p): return [x.strip()[:120] for x in p if x.strip()][:10]

def _create(u, title, draft, intent, protected, use_ai=True):
    # Default free-tier quota; active Team subscriptions remove the limit.
    try: free_limit=max(1,int(os.getenv('FREE_MONTHLY_REVIEW_LIMIT','25')))
    except ValueError: free_limit=25
    with db() as quota_db:
        active_team=quota_db.execute("SELECT 1 FROM subscriptions WHERE user_id=? AND status='active' AND plan='team' LIMIT 1",(u['id'],)).fetchone()
        if not active_team:
            month_start=datetime.now().strftime('%Y-%m-01')
            count=quota_db.execute("SELECT COUNT(*) FROM reviews WHERE owner_id=? AND created >= ?",(u['id'],month_start)).fetchone()[0]
            if count >= free_limit: raise HTTPException(402,f'Monthly review limit reached ({free_limit}). Upgrade to Team to continue.')
    draft = draft.replace('\r\n', '\n')
    if not draft.strip(): raise HTTPException(400, 'Please enter draft text first')
    if len(draft) > 20000: raise HTTPException(413, 'Draft is too long (20,000 characters max)')
    protected = _clean_protected(protected); res = _run(draft, protected, use_ai)
    with db() as c:
        rid = c.execute('INSERT INTO reviews(title,owner_id,owner_name,intent,protected,draft,findings,ledger,stats) VALUES(?,?,?,?,?,?,?,?,?)',
                        (title.strip()[:250] or 'Untitled draft', u['id'], u['name'], intent.strip()[:500], json.dumps(protected), draft,
                         json.dumps(res['findings']), json.dumps(res['ledger']), json.dumps(res['stats']))).lastrowid
        c.execute('INSERT INTO review_versions(review_id,version,draft,findings,ledger,note,created_by) VALUES(?,?,?,?,?,?,?)',
                  (rid, 1, draft, json.dumps(res['findings']), json.dumps(res['ledger']), 'Submitted', u['email']))
        row = c.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone()
    ai = res['stats']['ai']
    _audit('review_created', 'review', rid, f"{len(res['findings'])} findings; ai_used={ai['used']} model={os.getenv('OPENAI_MODEL','') if ai['used'] else '-'} redactions={ai['redactions']} dropped_ai={ai['dropped']}", u['email'])
    return _out(row)

@app.post('/api/reviews')
def create_review(r: ReviewIn, u=Depends(current_user)): return _create(u, r.title, r.draft, r.intent, r.protected, r.use_ai)

@app.post('/api/reviews/upload-docx')
async def upload_docx(file: UploadFile = File(...), title: str = Form('Uploaded document'), intent: str = Form(''), protected: str = Form(''), u=Depends(current_user)):
    # Kept the historical route for compatibility; accepts DOCX and text-based PDF.
    filename = (file.filename or 'document').strip()
    suffix = Path(filename).suffix.lower()
    if suffix not in ('.docx', '.pdf'):
        raise HTTPException(400, 'Only .docx and text-based .pdf files are supported')
    data = await file.read()
    if len(data) > 5 * 1024 * 1024: raise HTTPException(413, 'File exceeds 5 MB limit')
    try:
        if suffix == '.docx':
            with zipfile.ZipFile(io.BytesIO(data)) as z:
                if sum(i.file_size for i in z.infolist()) > 50 * 1024 * 1024: raise HTTPException(413, 'Document expands too large')
            from docx import Document
            doc = Document(io.BytesIO(data)); extracted = '\n'.join(p.text for p in doc.paragraphs)
            for table in doc.tables:
                for row in table.rows: extracted += '\n' + ' | '.join(cell.text for cell in row.cells)
        else:
            from pypdf import PdfReader
            reader = PdfReader(io.BytesIO(data), strict=True)
            if reader.is_encrypted:
                raise HTTPException(400, 'Encrypted PDFs are not supported')
            if len(reader.pages) > 100:
                raise HTTPException(413, 'PDF exceeds 100-page limit')
            chunks = []
            for page in reader.pages:
                chunks.append(page.extract_text() or '')
            extracted = '\n'.join(chunks)
            if not extracted.strip():
                extracted = _gemini_ocr_pdf(data, max_pages=min(len(reader.pages), 8))
                if not extracted.strip():
                    raise HTTPException(422, 'No selectable text found and OCR was unavailable or returned no text. Configure Gemini and retry, or provide a text-based PDF.')
        if not extracted.strip(): raise HTTPException(422, 'The document contains no extractable text')
        if len(extracted) > 250_000: raise HTTPException(413, 'Extracted text exceeds 250,000 characters')
    except HTTPException: raise
    except ImportError as exc:
        raise HTTPException(503, 'Document parser unavailable. Install project requirements.') from exc
    except Exception as exc:
        raise HTTPException(400, 'Could not read this document; check that the file is valid and not corrupted') from exc
    resolved_title = title if title and title != 'Uploaded document' else filename
    return _create(u, resolved_title, extracted, intent, [p for p in protected.split(',')])

@app.get('/api/reviews')
def list_reviews(u=Depends(current_user)):
    with db() as c:
        q, a = ('SELECT * FROM reviews WHERE owner_id=? ORDER BY id DESC', (u['id'],)) if u['role'] == 'Creator' else ('SELECT * FROM reviews ORDER BY id DESC', ())
        return [_out(r) for r in c.execute(q, a)]

@app.get('/api/reviews/{rid}')
def get_review(rid: int, u=Depends(current_user)):
    with db() as c:
        d = _out(_get(c, rid, u))
        d['versions'] = [dict(r) for r in c.execute('SELECT version,note,created_by,created FROM review_versions WHERE review_id=? ORDER BY version', (rid,))]
    return d

@app.post('/api/reviews/{rid}/decisions')
def decide(rid: int, d: DecisionIn, u=Depends(current_user)):
    if d.decision not in ('Accept', 'Edit', 'Dismiss'): raise HTTPException(400, 'Decision must be Accept, Edit or Dismiss')
    with db() as c:
        r = _get(c, rid, u)
        if r['status'] == 'Approved': raise HTTPException(409, 'This version is approved and locked')
        findings = json.loads(r['findings']); f = next((x for x in findings if x['id'] == d.finding_id), None)
        if not f: raise HTTPException(404, 'Finding not found')
        if d.decision == 'Dismiss' and len(d.note.strip()) < 5: raise HTTPException(400, 'Dismissals need a short reason (at least 5 characters)')
        if d.decision == 'Accept' and f['replacement'] is None: raise HTTPException(400, 'This finding has no automatic rewrite. Use Edit to supply wording, or Dismiss with a reason.')
        if d.decision == 'Edit' and not d.replacement.strip(): raise HTTPException(400, 'Edit needs the wording you want to use')
        f.update(decision=d.decision, decision_note=d.note.strip()[:500], custom_replacement=d.replacement if d.decision == 'Edit' else '', decided_by=u['email'])
        warn = ''
        if d.decision == 'Edit': warn = engine.intent_conflict(r['draft'], f['start'], f['end'], d.replacement, json.loads(r['protected']))
        c.execute('UPDATE reviews SET findings=?,updated=CURRENT_TIMESTAMP WHERE id=?', (json.dumps(findings), rid))
        c.execute('INSERT INTO decisions(review_id,version,finding_id,rule_id,decision,note,actor) VALUES(?,?,?,?,?,?,?)', (rid, r['version'], f['id'], f['rule_id'], d.decision, d.note.strip()[:500], u['email']))
        out = _out(c.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone())
    _audit('finding_' + d.decision.lower(), 'review', rid, f"{f['id']} {f['issue']} | {d.note.strip()[:200]}", u['email'])
    out['warning'] = f'This wording removes protected text “{warn}”.' if warn else ''
    return out

@app.post('/api/reviews/{rid}/workflow')
def workflow(rid: int, w: WorkflowIn, u=Depends(need('Reviewer', 'Admin'))):
    if w.action not in ('approve', 'request_changes'): raise HTTPException(400, 'Action must be approve or request_changes')
    with db() as c:
        r = _get(c, rid, u)
        if r['status'] == 'Approved': raise HTTPException(409, 'Already approved and locked')
        findings = json.loads(r['findings']); note = w.note.strip(); override = False
        if w.action == 'approve':
            if r['owner_id'] == u['id'] and u['role'] != 'Admin': raise HTTPException(403, 'Separation of duties: a different reviewer must approve your own draft.')
            pending = [f['id'] for f in findings if f['decision'] == 'Pending']
            if pending:
                if not (w.force and len(note) >= 5): raise HTTPException(409, f"{len(pending)} finding(s) still undecided ({', '.join(pending)}). Resolve them, or approve with override and a written reason.")
                override = True
            c.execute("UPDATE reviews SET status='Approved',approved_by=?,approved_at=CURRENT_TIMESTAMP,content_hash=?,updated=CURRENT_TIMESTAMP WHERE id=?", (u['email'], hashlib.sha256(r['draft'].encode()).hexdigest(), rid))
        else:
            if len(note) < 5: raise HTTPException(400, 'Tell the creator what needs to change (at least 5 characters)')
            c.execute("UPDATE reviews SET status='Changes requested',updated=CURRENT_TIMESTAMP WHERE id=?", (rid,))
        c.execute('INSERT INTO decisions(review_id,version,finding_id,rule_id,decision,note,actor) VALUES(?,?,?,?,?,?,?)', (rid, r['version'], 'workflow', '', w.action, note[:500], u['email']))
        out = _out(c.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone())
    _audit('review_' + w.action, 'review', rid, ('OVERRIDE with undecided findings. ' if override else '') + note[:300], u['email']); _review_event(rid,u['email'],'workflow',{'action':w.action,'status':out.get('status')}); return out

@app.get('/api/reviews/{rid}/preview')
def preview(rid: int, u=Depends(current_user)):
    with db() as c: r = _get(c, rid, u)
    findings = json.loads(r['findings']); new, applied, skipped = engine.apply_decisions(r['draft'], findings)
    return {'revised': new, 'diff': engine.word_diff(r['draft'], new), 'applied': applied, 'skipped': skipped, 'changed': new != r['draft']}

@app.post('/api/reviews/{rid}/revise')
def revise(rid: int, b: ReviseIn, u=Depends(current_user)):
    with db() as c:
        r = _get(c, rid, u)
        if r['owner_id'] != u['id'] and u['role'] != 'Admin': raise HTTPException(403, 'Only the draft owner (or an Admin) can create a new version')
        if r['status'] == 'Approved': raise HTTPException(409, 'Approved versions are locked')
        if b.expected_version is not None and b.expected_version != r['version']:
            raise HTTPException(409, f'Version conflict: latest is v{r["version"]}; refresh before editing')
        old = json.loads(r['findings']); protected = json.loads(r['protected'])
        new = b.draft.replace('\r\n', '\n') if b.draft.strip() else engine.apply_decisions(r['draft'], old)[0]
        if new == r['draft']: raise HTTPException(400, 'Nothing to change yet: accept or edit a fix first, or type new wording.')
        if len(new) > 20000: raise HTTPException(413, 'Draft is too long')
        res = _run(new, protected)
        kept = {(f['category'], f['rule_id'], f['matched'].lower()): f for f in old if f['decision'] == 'Dismiss'}
        for nf in res['findings']:
            prev = kept.get((nf['category'], nf['rule_id'], nf['matched'].lower()))
            if prev: nf.update(decision='Dismiss', decision_note=(prev['decision_note'] + f" (carried from v{r['version']})")[:500], decided_by=prev.get('decided_by', ''))
        v = r['version'] + 1
        c.execute("UPDATE reviews SET draft=?,findings=?,ledger=?,stats=?,version=?,status='In review',updated=CURRENT_TIMESTAMP WHERE id=?", (new, json.dumps(res['findings']), json.dumps(res['ledger']), json.dumps(res['stats']), v, rid))
        c.execute('INSERT INTO review_versions(review_id,version,draft,findings,ledger,note,created_by) VALUES(?,?,?,?,?,?,?)', (rid, v, new, json.dumps(res['findings']), json.dumps(res['ledger']), b.note.strip()[:300] or 'Revised', u['email']))
        out = _out(c.execute('SELECT * FROM reviews WHERE id=?', (rid,)).fetchone())
    _audit('review_revised', 'review', rid, f'v{v}: {len(res["findings"])} findings', u['email']); _review_event(rid,u['email'],'version_created',{'version':v}); return out

@app.get('/api/reviews/{rid}/diff')
def diff(rid: int, from_v: int, to_v: int, u=Depends(current_user)):
    with db() as c:
        _get(c, rid, u); g = lambda v: c.execute('SELECT draft FROM review_versions WHERE review_id=? AND version=?', (rid, v)).fetchone()
        a, b = g(from_v), g(to_v)
    if not a or not b: raise HTTPException(404, 'Version not found')
    return {'diff': engine.word_diff(a['draft'], b['draft'])}

@app.get('/api/reviews/{rid}/versions/{v}')
def version(rid: int, v: int, u=Depends(current_user)):
    with db() as c:
        _get(c, rid, u); r = c.execute('SELECT * FROM review_versions WHERE review_id=? AND version=?', (rid, v)).fetchone()
    if not r: raise HTTPException(404, 'Version not found')
    d = dict(r); d['findings'] = json.loads(d['findings']); d['ledger'] = json.loads(d['ledger']); return d

@app.post('/api/reviews/{rid}/comments')
def add_comment(rid: int, x: CommentIn, u=Depends(current_user)):
    if not x.body.strip(): raise HTTPException(400, 'Write a comment first')
    with db() as c:
        _get(c, rid, u); i = c.execute('INSERT INTO comments(review_id,author,role,body) VALUES(?,?,?,?)', (rid, u['name'], u['role'], x.body.strip()[:4000])).lastrowid
        row = dict(c.execute('SELECT * FROM comments WHERE id=?', (i,)).fetchone())
    _audit('comment_added', 'review', rid, x.body[:300], u['email']); _review_event(rid,u['email'],'comment',{'body':x.body[:300]}); return row
@app.get('/api/reviews/{rid}/comments')
def get_comments(rid: int, u=Depends(current_user)):
    with db() as c: _get(c, rid, u); return [dict(r) for r in c.execute('SELECT * FROM comments WHERE review_id=? ORDER BY id', (rid,))]

# ------------------------------------------------------------------ insights
@app.get('/api/metrics')
def metrics(u=Depends(current_user)):
    rs = list_reviews(u)
    return {'reviews': len(rs), 'approved': sum(r['status'] == 'Approved' for r in rs), 'in_review': sum(r['status'] == 'In review' for r in rs),
            'changes_requested': sum(r['status'] == 'Changes requested' for r in rs), 'findings': sum(len(r['findings']) for r in rs),
            'uncertain_open': sum(1 for r in rs if r['status'] != 'Approved' for f in r['findings'] if f.get('uncertain') and f['decision'] == 'Pending')}

@app.get('/api/analytics')
def analytics(u=Depends(need('Reviewer', 'Admin'))):
    with db() as c:
        rs = [_out(r) for r in c.execute('SELECT * FROM reviews')]
        daily = [dict(r) for r in c.execute('SELECT substr(created,1,10) day, count(*) reviews FROM reviews GROUP BY 1 ORDER BY day DESC LIMIT 14')]
        roles = [dict(r) for r in c.execute('SELECT role,count(*) count FROM users GROUP BY role')]
    sev = defaultdict(int); status = defaultdict(int)
    for r in rs:
        status[r['status']] += 1
        for f in r['findings']: sev[f['severity']] += 1
    return {'daily_reviews': daily, 'severity_breakdown': dict(sev), 'status_breakdown': dict(status), 'users_by_role': roles,
            'avg_versions_per_review': round(sum(r['version'] for r in rs) / len(rs), 2) if rs else 0}

@app.get('/api/guideline-health')
def guideline_health(u=Depends(need('Reviewer', 'Admin'))):
    with db() as c:
        names = {f"rule:{r['id']}": r['rule'] for r in c.execute('SELECT id,rule FROM rules')}
        trig = defaultdict(int)
        for r in c.execute('SELECT findings FROM review_versions'):
            for f in json.loads(r['findings']): trig[f['rule_id']] += 1
        dec = defaultdict(lambda: defaultdict(int))
        for r in c.execute("SELECT rule_id,decision,count(*) n FROM decisions WHERE finding_id!='workflow' GROUP BY rule_id,decision"): dec[r['rule_id']][r['decision']] = r['n']
    out = []
    for rid, n in trig.items():
        d = dec[rid]; decided = sum(d.values()); rate = round(d['Dismiss'] / decided, 2) if decided else 0
        hint = 'Often dismissed: this guideline may be unclear or too strict. Consider rewording it.' if decided >= 3 and rate >= 0.5 else \
               'Broken repeatedly: consider a creator tip or template.' if n >= 5 else ''
        out.append({'rule_id': rid, 'name': names.get(rid) or engine.LABEL_RULE.get(rid, rid), 'triggered': n, 'accepted': d['Accept'], 'edited': d['Edit'], 'dismissed': d['Dismiss'], 'dismissal_rate': rate, 'hint': hint})
    out.sort(key=lambda x: (-x['triggered'], x['name'])); return out

@app.get('/api/audit')
def get_audit(limit: int = 100, u=Depends(need('Reviewer', 'Admin'))):
    with db() as c: return [dict(r) for r in c.execute('SELECT * FROM audit ORDER BY id DESC LIMIT ?', (max(1, min(limit, 500)),))]

# ------------------------------------------------------------------ VeriBot (grounded Q&A)
class ChatIn(BaseModel): question: str
@app.post('/api/chat')
def veribot(x: ChatIn, u=Depends(current_user)):
    q = x.question.strip()
    if not q: raise HTTPException(400, 'Enter a question')
    with db() as c: fs = [dict(r) for r in c.execute('SELECT name,value,source,review_by FROM facts ORDER BY id')]
    terms = engine.tokens(q); ranked = []
    for f in fs:
        score = sum(1 for t in terms if len(t) > 2 and t in (f['name'] + ' ' + f['value'] + ' ' + f['source']).lower())
        if score: ranked.append((score, f))
    ranked.sort(key=lambda z: -z[0]); today = date.today()
    def line(f):
        stale = ' ⚠ source overdue for review' if engine.is_stale(f, today) else ''
        return f"• {f['name']}: {f['value']} (source: {f['source']}){stale}"
    if ranked: answer = 'Here is what the approved brand kit supports:\n' + '\n'.join(line(f) for _, f in ranked[:4]) + '\n\nI used only matching saved facts. For anything not listed, ask a reviewer; missing evidence is never treated as verified.'
    else: answer = 'I could not find a matching approved fact in the brand kit, so I cannot verify this. Ask an Admin to add the relevant fact, or ask a reviewer.'
    mode, call = 'local-grounded', _llm()
    if call and ranked:
        try:
            ev = '\n'.join(f"{f['name']}: {f['value']} [source: {f['source']}]" for _, f in ranked[:8]); safe, _n = engine.redact(q)
            raw = call('You are VeriBot. Answer ONLY from the approved evidence. Cite source titles. If insufficient, say you cannot verify. Reply as JSON {"answer": str}.\nEVIDENCE:\n' + ev, safe)
            answer = str(json.loads(raw).get('answer') or answer); mode = 'llm-grounded'
        except Exception: mode = 'local-grounded-fallback'
    with db() as c: c.execute('INSERT INTO chatlog(question,answer) VALUES(?,?)', (q[:1000], answer))
    return {'assistant': 'VeriBot', 'answer': answer, 'grounded': bool(ranked), 'sources': [f['source'] for _, f in ranked[:4]], 'mode': mode}

# ------------------------------------------------------------------ misc
class FeedbackIn(BaseModel): email: str = ''; rating: int; message: str = ''
@app.post('/api/feedback')
def feedback(x: FeedbackIn, request: Request):
    _rate(request, 'feedback', 10)
    if not 1 <= x.rating <= 5: raise HTTPException(400, 'Rating must be 1 to 5')
    with db() as c: c.execute('INSERT INTO feedback(email,rating,message) VALUES(?,?,?)', (x.email[:250], x.rating, x.message[:4000]))
    return {'ok': True, 'message': 'Thanks for your feedback'}

@app.get('/api/health')
def health():
    return {'ok': True, 'service': 'VeriBrand', 'version': '2.2.0', 'database': 'PostgreSQL' if DATABASE_URL else 'SQLite'}

@app.get('/api/ready')
def readiness():
    checks = {'database': False, 'storage': False}
    try:
        with db() as c: c.execute('SELECT 1').fetchone()
        checks['database'] = True
    except Exception:
        logger.exception('readiness_database_failed')
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        probe = DATA_DIR / '.ready-probe'
        probe.write_text('ok', encoding='utf-8'); probe.unlink()
        checks['storage'] = True
    except Exception:
        logger.exception('readiness_storage_failed')
    ok = all(checks.values())
    if not ok: return JSONResponse(status_code=503, content={'ok': False, 'checks': checks})
    return {'ok': True, 'checks': checks}


@app.get('/api/features')
def features():
    cfg = _ai_config(); ai_on = bool(cfg['key']) and cfg['provider'] in ('gemini', 'openai')
    return {'demo_mode': DEMO_MODE, 'external_llm': ai_on,
            'features': {'role_enforcement': True, 'claim_evidence_ledger': True, 'stale_source_detection': True, 'intent_lock_diff': True,
                         'revision_history': True, 'guideline_health': True, 'ai_disclosure': True, 'docx_review': True, 'pdf_text_review': True,
                         'grounded_ai_with_verification': ai_on, 'image_checks': False, 'pdf_ocr': False, 'oauth': False}}

# ------------------------------------------------------------------ P1 capabilities

def _review_event(review_id, actor, event_type, payload=None):
    with db() as c:
        c.execute('INSERT INTO review_events(review_id,actor,event_type,payload) VALUES(?,?,?,?)',(review_id,actor,event_type,json.dumps(payload or {})))

class ContrastIn(BaseModel): foreground: str; background: str
class AltTextIn(BaseModel): text: str; context: str = ''

@app.post('/api/accessibility/contrast')
def accessibility_contrast(x: ContrastIn, u=Depends(current_user)):
    def rgb(value):
        v=value.strip().lstrip('#')
        if not re.fullmatch(r'[0-9a-fA-F]{6}',v): raise HTTPException(400,'Colours must be six-digit hex values such as #321C3B')
        return [int(v[i:i+2],16)/255 for i in (0,2,4)]
    def lum(v):
        vals=[z/12.92 if z<=0.04045 else ((z+0.055)/1.055)**2.4 for z in v]
        return 0.2126*vals[0]+0.7152*vals[1]+0.0722*vals[2]
    a,b=lum(rgb(x.foreground)),lum(rgb(x.background)); ratio=(max(a,b)+0.05)/(min(a,b)+0.05)
    return {'foreground':x.foreground,'background':x.background,'contrast_ratio':round(ratio,2),'wcag_aa_normal_text':ratio>=4.5,'wcag_aa_large_text':ratio>=3,'wcag_aaa_normal_text':ratio>=7,'note':'Calculated from the supplied solid hex colours only; does not measure gradients, transparency, images or actual font size.'}

@app.post('/api/accessibility/alt-text')
def accessibility_alt_text(x: AltTextIn, u=Depends(current_user)):
    if not x.text.strip(): raise HTTPException(400,'Provide the visible content or image description')
    prompt='Suggest concise accessible alt text for the described image/content. Do not invent visual details. Return JSON {"alt_text":"...","rationale":"...","needs_human_review":true}.'
    call=_llm()
    if not call: return {'alt_text':None,'message':'Configure a Gemini/OpenAI key for AI alt-text suggestions. Provide a human-written description for now.','needs_human_review':True}
    try:
        raw=call(prompt, json.dumps({'visible_content':x.text[:4000],'context':x.context[:1000]})); m=re.search(r'\{.*\}',raw,re.S); out=json.loads(m.group(0)) if m else {'alt_text':raw[:500],'rationale':'Model output requires review','needs_human_review':True}
        out['needs_human_review']=True; return out
    except Exception: raise HTTPException(502,'Alt-text suggestion provider failed')

@app.get('/api/reviews/{rid}/events')
def review_events(rid: int, after_id: int=0, u=Depends(current_user)):
    with db() as c:
        _get(c,rid,u)
        rows=[dict(r) for r in c.execute('SELECT * FROM review_events WHERE review_id=? AND id>? ORDER BY id LIMIT 100',(rid,after_id))]
    for r in rows: r['payload']=json.loads(r['payload'] or '{}')
    return {'events':rows,'latest_id':rows[-1]['id'] if rows else after_id}

# Local embedding fallback uses deterministic signed feature hashing. If Gemini
# embedding credentials are configured, embeddings are requested from the provider.
def _tokens(text):
    return re.findall(r"[a-z0-9]+(?:['-][a-z0-9]+)?", (text or '').lower())

def _vector(text, dims=384):
    v=[0.0]*dims
    toks=_tokens(text)
    for tok in toks:
        digest=hashlib.blake2b(tok.encode(),digest_size=8).digest(); n=int.from_bytes(digest[:4],'big')%dims
        v[n] += 1.0 if digest[4] % 2 else -1.0
    norm=math.sqrt(sum(x*x for x in v)) or 1.0
    return [round(x/norm,6) for x in v]

def _cos(a,b): return sum(x*y for x,y in zip(a,b)) / ((math.sqrt(sum(x*x for x in a))*math.sqrt(sum(y*y for y in b))) or 1)

def _gemini_embed(text):
    key=os.getenv('GEMINI_API_KEY','').strip()
    if not key: return None
    model=os.getenv('GEMINI_EMBEDDING_MODEL','text-embedding-004')
    body=json.dumps({'model':'models/'+model,'content':{'parts':[{'text':text[:8000]}]}}).encode()
    req=urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models/'+model+':embedContent?key='+key,data=body,headers={'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=20) as r: return json.loads(r.read().decode())['embedding']['values']
    except Exception: return None

def _embed(text): return _gemini_embed(text) or _vector(text)

def _reindex_knowledge():
    with db() as c:
        sources=[]
        sources += [('fact',str(r['id']),r['name'],f"{r['name']}: {r['value']}\nSource: {r['source']}\nCategory: {r['category']}\nReview by: {r['review_by']}") for r in c.execute('SELECT * FROM facts')]
        sources += [('rule',str(r['id']),r['rule'],f"{r['rule']}\nKind: {r['kind']}\nSeverity: {r['severity']}\nPattern: {r['pattern']}\nSource: {r['source']}\nSuggestion: {r['suggestion']}") for r in c.execute('SELECT * FROM rules WHERE enabled=1')]
        c.execute('DELETE FROM knowledge_chunks')
        for kind,sid,title,body in sources: c.execute('INSERT INTO knowledge_chunks(kind,source_id,title,body,vector) VALUES(?,?,?,?,?)',(kind,sid,title,body,json.dumps(_embed(body))))
    return len(sources)

@app.post('/api/knowledge/reindex')
def knowledge_reindex(u=Depends(need('Admin'))):
    n=_reindex_knowledge(); _audit('knowledge_reindexed','knowledge','',f'{n} chunks',u['email']); return {'ok':True,'chunks':n,'embedding_provider':'gemini' if os.getenv('GEMINI_API_KEY') else 'local-feature-vector'}

@app.get('/api/knowledge/search')
def knowledge_search(q: str, limit: int=5, u=Depends(current_user)):
    if not q.strip(): raise HTTPException(400,'Search query is required')
    if len(q)>1000: raise HTTPException(413,'Query too long')
    limit=max(1,min(limit,10))
    with db() as c:
        rows=list(c.execute('SELECT * FROM knowledge_chunks'))
        if not rows:
            _reindex_knowledge()
            with db() as c2: rows=list(c2.execute('SELECT * FROM knowledge_chunks'))
    qv=_embed(q); ranked=sorted(rows,key=lambda r:_cos(qv,json.loads(r['vector'])),reverse=True)[:limit]
    return {'query':q,'retrieval':'gemini-embedding-preferred-with-local-vector-fallback' if os.getenv('GEMINI_API_KEY') else 'local-vector-fallback','results':[{'kind':r['kind'],'id':r['source_id'],'title':r['title'],'text':r['body'],'score':round(_cos(qv,json.loads(r['vector'])),4)} for r in ranked]}

def _gemini_ocr_pdf(data, max_pages=8):
    key=os.getenv('GEMINI_API_KEY','').strip()
    if not key: return ''
    try:
        import fitz
        doc=fitz.open(stream=data,filetype='pdf'); outputs=[]
        for i,page in enumerate(doc):
            if i>=max_pages: break
            png=page.get_pixmap(matrix=fitz.Matrix(1.4,1.4),alpha=False).tobytes('png')
            payload={'contents':[{'parts':[{'text':'Transcribe all readable text from this document page faithfully. Preserve headings, numbers, punctuation and line breaks. Do not summarize, infer missing text, or add commentary.'},{'inline_data':{'mime_type':'image/png','data':base64.b64encode(png).decode()}}]}]}
            req=urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models/'+os.getenv('GEMINI_VISION_MODEL','gemini-2.5-flash')+':generateContent?key='+key,data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'},method='POST')
            with urllib.request.urlopen(req,timeout=45) as r: result=json.loads(r.read().decode())
            outputs.append(result['candidates'][0]['content']['parts'][0].get('text',''))
        return '\n'.join(outputs)[:250000]
    except Exception: return ''

@app.post('/api/reviews/upload-image')
async def upload_image(file: UploadFile=File(...), title: str=Form('Visual brand review'), u=Depends(current_user)):
    data=await file.read()
    if len(data)>5*1024*1024: raise HTTPException(413,'Image exceeds 5 MB limit')
    mime=(file.content_type or mimetypes.guess_type(file.filename or '')[0] or '').lower()
    if mime not in ('image/png','image/jpeg','image/webp'): raise HTTPException(400,'Only PNG, JPEG and WebP images are supported')
    if not os.getenv('GEMINI_API_KEY','').strip(): raise HTTPException(503,'Visual analysis requires GEMINI_API_KEY')
    prompt='Analyze this marketing creative for brand governance. Inspect visible text, logo/brand consistency only if evident, contrast/readability, potential misleading claims, and accessibility risks. Do not infer facts not visible. Return JSON with observations array; each item must include category, observation, evidence (exact visible text or visual description), severity (Low/Medium/High), confidence (0-1), and needs_human=true. Do not claim exact WCAG contrast ratios unless pixel colors can be measured reliably. This is advisory, not an approval.'
    payload={'contents':[{'parts':[{'text':prompt},{'inline_data':{'mime_type':mime,'data':base64.b64encode(data).decode()}}]}]}
    req=urllib.request.Request('https://generativelanguage.googleapis.com/v1beta/models/'+os.getenv('GEMINI_VISION_MODEL','gemini-2.5-flash')+':generateContent?key='+os.getenv('GEMINI_API_KEY','').strip(),data=json.dumps(payload).encode(),headers={'Content-Type':'application/json'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=45) as r: out=json.loads(r.read().decode())
        txt=out['candidates'][0]['content']['parts'][0]['text']; m=re.search(r'\{.*\}',txt,re.S); parsed=json.loads(m.group(0)) if m else {'observations':[{'category':'visual','observation':txt[:2000],'evidence':'Model response; verify manually','severity':'Info','confidence':0.4,'needs_human':True}]}
    except Exception as exc: raise HTTPException(502,'Visual analysis provider failed; retry later') from exc
    _audit('visual_review','image',file.filename or 'upload',f'{len(parsed.get("observations",[]))} observations; human verification required',u['email'])
    return {'title':title,'provider':'gemini','model':os.getenv('GEMINI_VISION_MODEL','gemini-2.5-flash'),'observations':parsed.get('observations',[])[:50],'disclaimer':'AI visual observations are not a formal accessibility audit or approval. Verify manually.'}

@app.get('/api/account/export')
def export_account(u=Depends(current_user)):
    with db() as c:
        user=dict(c.execute('SELECT id,email,name,role,verified,created FROM users WHERE id=?',(u['id'],)).fetchone())
        reviews=[_out(r) for r in c.execute('SELECT * FROM reviews WHERE owner_id=? ORDER BY id',(u['id'],))]
        comments=[dict(r) for r in c.execute('SELECT * FROM comments WHERE author=?',(u['email'],))]
        decisions=[dict(r) for r in c.execute('SELECT * FROM decisions WHERE actor=?',(u['email'],))]
        audit=[dict(r) for r in c.execute('SELECT * FROM audit WHERE actor=? ORDER BY id DESC LIMIT 1000',(u['email'],))]
    return {'exported_at':datetime.now().astimezone().isoformat(),'user':user,'owned_reviews':reviews,'comments':comments,'decisions':decisions,'audit_events':audit}

class DeleteAccountIn(BaseModel): password: str; confirmation: str
@app.delete('/api/account')
def delete_account(x: DeleteAccountIn, u=Depends(current_user)):
    if x.confirmation != 'DELETE MY ACCOUNT': raise HTTPException(400,'Type DELETE MY ACCOUNT to confirm')
    with db() as c:
        full=c.execute('SELECT * FROM users WHERE id=?',(u['id'],)).fetchone()
        if not full or not _verify_pw(x.password,full['password_hash']): raise HTTPException(403,'Password confirmation failed')
        if full['role']=='Admin' and c.execute("SELECT COUNT(*) FROM users WHERE role='Admin'").fetchone()[0] <= 1: raise HTTPException(409,'Transfer Admin role before deleting the last Admin account')
        owned=[r['id'] for r in c.execute('SELECT id FROM reviews WHERE owner_id=?',(u['id'],))]
        for rid in owned:
            c.execute('DELETE FROM review_versions WHERE review_id=?',(rid,)); c.execute('DELETE FROM decisions WHERE review_id=?',(rid,)); c.execute('DELETE FROM comments WHERE review_id=?',(rid,)); c.execute('DELETE FROM reviews WHERE id=?',(rid,))
        c.execute('DELETE FROM comments WHERE author=?',(full['name'],)); c.execute('DELETE FROM decisions WHERE actor=?',(full['email'],)); c.execute('DELETE FROM feedback WHERE email=?',(full['email'],)); c.execute('DELETE FROM sessions WHERE user_id=?',(u['id'],)); c.execute('DELETE FROM subscriptions WHERE user_id=?',(u['id'],)); c.execute('DELETE FROM users WHERE id=?',(u['id'],))
    _audit('account_deleted','user',u['id'],'User requested account deletion; owned reviews removed',u['email'])
    return {'ok':True,'message':'Account and owned drafts deleted. Audit records may be retained for security/compliance.'}

def _oauth_state(provider):
    state=secrets.token_urlsafe(32)
    with db() as c:
        c.execute('DELETE FROM oauth_states WHERE expires<?',(int(time.time()),))
        c.execute('INSERT INTO oauth_states(state_hash,provider,expires) VALUES(?,?,?)',(_hash_token(state),provider,int(time.time())+600))
    return state

def _consume_oauth_state(state,provider):
    with db() as c:
        row=c.execute('SELECT * FROM oauth_states WHERE state_hash=? AND provider=?',(_hash_token(state),provider)).fetchone()
        c.execute('DELETE FROM oauth_states WHERE state_hash=?',(_hash_token(state),))
    if not row or row['expires']<int(time.time()): raise HTTPException(400,'OAuth state invalid or expired')

def _oauth_session(email,name,provider):
    email=email.strip().lower()
    if not re.fullmatch(r'[^@\s]+@[^@\s]+\.[^@\s]+',email): raise HTTPException(400,'OAuth provider did not return a verified email')
    with db() as c:
        user=c.execute('SELECT * FROM users WHERE email=?',(email,)).fetchone()
        if user:
            if not user['verified']: c.execute('UPDATE users SET verified=1 WHERE id=?',(user['id'],))
            uid=user['id']
        else:
            role='Admin' if email==os.getenv('ADMIN_EMAIL','').strip().lower() and os.getenv('ADMIN_EMAIL') else 'Creator'
            uid=c.execute('INSERT INTO users(email,name,password_hash,role,verified) VALUES(?,?,?,?,1)',(email,(name or email.split('@')[0])[:100],_pw(secrets.token_urlsafe(32)),role)).lastrowid
            user=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
        token=secrets.token_urlsafe(36); exp=int(time.time())+7*86400
        c.execute('INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)',(_hash_token(token),uid,exp))
        user=c.execute('SELECT * FROM users WHERE id=?',(uid,)).fetchone()
    _audit('oauth_login','user',uid,provider,email)
    return token,dict(user),exp

def _oauth_callback_redirect(token):
    # Fragment is not sent to the server in HTTP requests; JS consumes and removes it.
    return RedirectResponse(url='/#oauth_token='+token, status_code=303)

def _oauth_provider_configured(provider):
    prefix=provider.upper()
    client=os.getenv(prefix+'_CLIENT_ID','').strip()
    secret=os.getenv(prefix+'_CLIENT_SECRET','').strip()
    redirect=os.getenv(prefix+'_REDIRECT_URI','').strip()
    parsed=urllib.parse.urlparse(redirect)
    callback_path=f'/api/auth/oauth/{provider}/callback'
    is_placeholder=lambda value: not value or value.lower().startswith('your-') or '.example' in value.lower()
    return (
        not is_placeholder(client)
        and not is_placeholder(secret)
        and '.example' not in redirect.lower()
        and parsed.scheme in ('http','https')
        and bool(parsed.netloc)
        and parsed.path == callback_path
    )

@app.get('/api/auth/oauth/{provider}/start')
def oauth_start(provider: str):
    if provider not in ('google','github'): raise HTTPException(404,'Unsupported OAuth provider')
    client=os.getenv(provider.upper()+'_CLIENT_ID','').strip(); secret=os.getenv(provider.upper()+'_CLIENT_SECRET','').strip()
    if not client or not secret or client.lower().startswith('your-') or secret.lower().startswith('your-'):
        raise HTTPException(503,f'{provider.title()} sign-in is not configured. Set real {provider.upper()}_CLIENT_ID and {provider.upper()}_CLIENT_SECRET values in the project .env file.')
    redirect=os.getenv(provider.upper()+'_REDIRECT_URI','').strip()
    expected_path=f'/api/auth/oauth/{provider}/callback'
    parsed_redirect=urllib.parse.urlparse(redirect)
    if parsed_redirect.scheme not in ('http','https') or not parsed_redirect.netloc or parsed_redirect.path != expected_path or '.example' in redirect.lower():
        raise HTTPException(503,f'Configure {provider.upper()}_REDIRECT_URI as an absolute URL ending in {expected_path}. It must exactly match the callback URL registered with the provider.')
    state=_oauth_state(provider)
    if provider=='google':
        url='https://accounts.google.com/o/oauth2/v2/auth?'+urllib.parse.urlencode({'client_id':client,'redirect_uri':redirect,'response_type':'code','scope':'openid email profile','state':state,'prompt':'select_account'})
    else:
        url='https://github.com/login/oauth/authorize?'+urllib.parse.urlencode({'client_id':client,'redirect_uri':redirect,'scope':'read:user user:email','state':state})
    return RedirectResponse(url, status_code=302)

@app.get('/api/auth/oauth/{provider}/callback')
def oauth_callback(provider: str, code: str, state: str):
    if provider not in ('google','github'): raise HTTPException(404,'Unsupported OAuth provider')
    _consume_oauth_state(state,provider)
    client=os.getenv(provider.upper()+'_CLIENT_ID','').strip(); secret=os.getenv(provider.upper()+'_CLIENT_SECRET','').strip(); redirect=os.getenv(provider.upper()+'_REDIRECT_URI','').strip()
    if not client or not secret or not redirect: raise HTTPException(503,'OAuth provider is not configured')
    if provider=='google':
        token_url='https://oauth2.googleapis.com/token'; fields={'code':code,'client_id':client,'client_secret':secret,'redirect_uri':redirect,'grant_type':'authorization_code'}
    else:
        token_url='https://github.com/login/oauth/access_token'; fields={'code':code,'client_id':client,'client_secret':secret,'redirect_uri':redirect}
    req=urllib.request.Request(token_url,data=urllib.parse.urlencode(fields).encode(),headers={'Accept':'application/json','Content-Type':'application/x-www-form-urlencoded'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=20) as r: token_data=json.loads(r.read().decode())
        access=token_data['access_token']
        if provider=='google':
            req=urllib.request.Request('https://openidconnect.googleapis.com/v1/userinfo',headers={'Authorization':'Bearer '+access})
            with urllib.request.urlopen(req,timeout=20) as r: profile=json.loads(r.read().decode())
            if not profile.get('email_verified'): raise HTTPException(403,'Google account email is not verified')
            email=profile['email']; name=profile.get('name','')
        else:
            req=urllib.request.Request('https://api.github.com/user',headers={'Authorization':'Bearer '+access,'Accept':'application/vnd.github+json','User-Agent':'VeriBrand'})
            with urllib.request.urlopen(req,timeout=20) as r: profile=json.loads(r.read().decode())
            email=profile.get('email')
            if not email:
                req=urllib.request.Request('https://api.github.com/user/emails',headers={'Authorization':'Bearer '+access,'Accept':'application/vnd.github+json','User-Agent':'VeriBrand'})
                with urllib.request.urlopen(req,timeout=20) as r: emails=json.loads(r.read().decode())
                primary=next((e for e in emails if e.get('primary') and e.get('verified')),None); email=primary.get('email') if primary else None
            name=profile.get('name') or profile.get('login','')
        if not email: raise HTTPException(403,'OAuth provider did not return a verified email')
        token,user,exp=_oauth_session(email,name,provider)
        return _oauth_callback_redirect(token)
    except HTTPException: raise
    except Exception as exc: raise HTTPException(502,'OAuth login failed; retry or use email login') from exc

@app.get('/api/billing/plans')
def billing_plans():
    return {'provider':'stripe','enabled':bool(os.getenv('STRIPE_SECRET_KEY')),'plans':[{'id':'starter','name':'Starter','description':'Limited monthly reviews'},{'id':'team','name':'Team','description':'Shared brand workspace and higher limits'}], 'note':'Billing is disabled until Stripe price IDs and webhook secret are configured.'}

class CheckoutIn(BaseModel): plan: str; success_url: str; cancel_url: str
@app.post('/api/billing/checkout')
def billing_checkout(x: CheckoutIn, request: Request, u=Depends(current_user)):
    _rate(request,'billing',5)
    if x.plan not in ('starter','team'): raise HTTPException(400,'Unknown plan')
    key=os.getenv('STRIPE_SECRET_KEY','').strip(); price=os.getenv('STRIPE_PRICE_'+x.plan.upper(),'').strip()
    if not key or not price: raise HTTPException(503,'Stripe billing is not configured; set secret key and plan price IDs')
    for url in (x.success_url,x.cancel_url):
        if not url.startswith('https://') or len(url)>500: raise HTTPException(400,'Redirect URLs must use HTTPS')
    fields={'mode':'subscription','line_items[0][price]':price,'line_items[0][quantity]':'1','success_url':x.success_url,'cancel_url':x.cancel_url,'client_reference_id':str(u['id']),'customer_email':u['email'],'metadata[user_id]':str(u['id']),'metadata[plan]':x.plan,'subscription_data[metadata][user_id]':str(u['id']),'subscription_data[metadata][plan]':x.plan}
    body=urllib.parse.urlencode(fields).encode()
    req=urllib.request.Request('https://api.stripe.com/v1/checkout/sessions',data=body,headers={'Authorization':'Bearer '+key,'Content-Type':'application/x-www-form-urlencoded'},method='POST')
    try:
        with urllib.request.urlopen(req,timeout=20) as r: payload=json.loads(r.read().decode())
    except Exception as exc: raise HTTPException(502,'Stripe checkout could not be created') from exc
    return {'checkout_url':payload.get('url'),'session_id':payload.get('id'),'provider':'stripe'}

@app.post('/api/billing/webhook')
async def billing_webhook(request: Request):
    secret=os.getenv('STRIPE_WEBHOOK_SECRET','').strip()
    if not secret: raise HTTPException(503,'Stripe webhook is not configured')
    raw=await request.body(); sig=request.headers.get('stripe-signature','')
    parts=[item.split('=',1) for item in sig.split(',') if '=' in item]
    timestamp=next((v for k,v in parts if k=='t'),None); signatures=[v for k,v in parts if k=='v1']
    if not timestamp or not signatures: raise HTTPException(400,'Invalid Stripe signature')
    try:
        if abs(int(time.time())-int(timestamp))>300: raise HTTPException(400,'Stripe signature timestamp expired')
        expected=hmac.new(secret.encode(),timestamp.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    except ValueError: raise HTTPException(400,'Invalid Stripe signature timestamp')
    if not any(hmac.compare_digest(expected,x) for x in signatures): raise HTTPException(400,'Invalid Stripe signature')
    try: event=json.loads(raw or b'{}')
    except Exception: raise HTTPException(400,'Invalid event JSON')
    event_id=event.get('id',''); event_type=event.get('type','unknown'); obj=(event.get('data') or {}).get('object') or {}
    with db() as c:
        if event_id and c.execute('SELECT 1 FROM stripe_events WHERE event_id=?',(event_id,)).fetchone(): return {'received':True,'duplicate':True}
        if event_id: c.execute('INSERT INTO stripe_events(event_id,event_type) VALUES(?,?)',(event_id,event_type))
        if event_type=='checkout.session.completed' and obj.get('mode')=='subscription':
            try: uid=int(obj.get('client_reference_id') or (obj.get('metadata') or {}).get('user_id'))
            except (TypeError,ValueError): uid=0
            sub_id=obj.get('subscription'); customer=obj.get('customer'); plan=(obj.get('metadata') or {}).get('plan','team')
            if uid and sub_id:
                c.execute('INSERT INTO subscriptions(user_id,provider,customer_id,subscription_id,status,plan) VALUES(?,?,?,?,?,?)',(uid,'stripe',customer,sub_id,'active',plan))
        elif event_type in ('customer.subscription.updated','customer.subscription.deleted'):
            sid=obj.get('id'); status='canceled' if event_type.endswith('deleted') else obj.get('status','unknown')
            if sid: c.execute('UPDATE subscriptions SET status=?,updated=CURRENT_TIMESTAMP WHERE subscription_id=?',(status,sid))
    _audit('stripe_webhook','billing',event_id,f'type={event_type}')
    return {'received':True,'event_type':event_type}

@app.get('/api/billing/status')
def billing_status(u=Depends(current_user)):
    with db() as c: rows=[dict(r) for r in c.execute('SELECT provider,subscription_id,status,plan,created,updated FROM subscriptions WHERE user_id=? ORDER BY id DESC',(u['id'],))]
    return {'subscriptions':rows,'active':any(r['status']=='active' for r in rows)}

@app.get('/api/oauth/providers')
def oauth_providers():
    return {'google':_oauth_provider_configured('google'),'github':_oauth_provider_configured('github'),'note':'OAuth requires a client ID, client secret, and an exact registered callback URL.'}

@app.get('/api/features/p1')
def p1_features():
    return {'pdf_ocr':'gemini-vision-optional','vector_rag':'gemini-embeddings-with-local-vector-fallback','image_analysis':'gemini-vision-optional','account_export_delete':True,'optimistic_concurrency':True,'pwa':True,'voice_input':'browser-web-speech-api','contrast_checker':True,'alt_text_suggestions':'optional-grounded-ai','review_event_feed':True,'billing':'stripe-checkout-and-signed-webhook','oauth':'google-and-github-authorization-code-flow','limitations':['Live OAuth provider setup and real credential testing remain pending','Stripe live-mode price/webhook setup and end-to-end subscription verification remain pending','Collaborative editing uses optimistic version conflicts and comments; it is not character-level realtime co-editing']}



# ------------------------------------------------------------------ P2 operations and governance
@app.get('/api/brand-kit/export')
def export_brand_kit(u=Depends(need('Admin'))):
    """Export brand facts and rules in a portable, versioned JSON bundle."""
    with db() as c:
        facts_data = [dict(r) for r in c.execute('SELECT name,value,source,category,review_by FROM facts ORDER BY id')]
        rules_data = [dict(r) for r in c.execute('SELECT rule,kind,severity,pattern,source,replacement,suggestion,enabled FROM rules ORDER BY id')]
    return {'schema_version': 1, 'exported_at': datetime.now(timezone.utc).isoformat().replace('+00:00', 'Z'), 'facts': facts_data, 'rules': rules_data}

class BrandKitImportIn(BaseModel):
    schema_version: int = 1
    facts: list[dict] = __import__('pydantic').Field(default_factory=list)
    rules: list[dict] = __import__('pydantic').Field(default_factory=list)
    replace_existing: bool = False

@app.post('/api/brand-kit/import')
def import_brand_kit(bundle: BrandKitImportIn, u=Depends(need('Admin'))):
    """Validate a portable bundle completely before applying it atomically."""
    if bundle.schema_version != 1: raise HTTPException(400, 'Unsupported brand-kit schema_version')
    if len(bundle.facts) > 1000 or len(bundle.rules) > 1000: raise HTTPException(413, 'Import limited to 1000 facts and 1000 rules')
    validated_facts = []
    for item in bundle.facts:
        try: f = FactIn(**item)
        except Exception as exc: raise HTTPException(400, 'Invalid fact entry') from exc
        _check_fact(f)
        if any(len(str(getattr(f, k) or '')) > 2000 for k in ('name','value','source')): raise HTTPException(400, 'Fact field too long')
        validated_facts.append((f.name.strip(), f.value.strip(), f.source.strip(), f.category, f.review_by))
    validated_rules = []
    for item in bundle.rules:
        try: rule = RuleIn(**item)
        except Exception as exc: raise HTTPException(400, 'Invalid rule entry') from exc
        if rule.kind not in ('tone','term') or rule.severity not in ('High','Medium','Low'): raise HTTPException(400, 'Invalid rule kind or severity')
        if not rule.rule.strip() or len(rule.pattern) > 300 or len(rule.source) > 2000: raise HTTPException(400, 'Invalid or oversized rule')
        try: re.compile(rule.pattern)
        except re.error as exc: raise HTTPException(400, 'Invalid regular expression in imported rule') from exc
        validated_rules.append((rule.rule.strip(), rule.kind, rule.severity, rule.pattern, rule.source.strip(), rule.replacement, rule.suggestion))
    with db() as c:
        if bundle.replace_existing:
            c.execute('DELETE FROM facts'); c.execute('DELETE FROM rules')
        c.executemany('INSERT INTO facts(name,value,source,category,review_by) VALUES(?,?,?,?,?)', validated_facts)
        c.executemany('INSERT INTO rules(rule,kind,severity,pattern,source,replacement,suggestion) VALUES(?,?,?,?,?,?,?)', validated_rules)
        c.execute('DELETE FROM knowledge_chunks')
    _audit('brand_kit_imported','brand_kit','',json.dumps({'facts':len(validated_facts),'rules':len(validated_rules),'replace_existing':bundle.replace_existing}),u['email'])
    return {'ok': True, 'facts_imported': len(validated_facts), 'rules_imported': len(validated_rules), 'reindex_required': True}

@app.get('/api/reviews/{rid}/report.html', response_class=HTMLResponse)
def review_report_html(rid: int, u=Depends(current_user)):
    """Portable, self-contained review report; all draft text is HTML-escaped."""
    with db() as c:
        row = _get(c, rid, u); review = _out(row)
    esc = lambda value: html.escape(str(value if value is not None else ''), quote=True)
    findings = ''.join('<li><strong>' + esc(f.get('severity','')) + ' — ' + esc(f.get('issue','Finding')) + '</strong><p>' + esc(f.get('explanation','')) + '</p><p><small>Evidence: ' + esc(f.get('source','')) + ' | Decision: ' + esc(f.get('decision','Pending')) + '</small></p></li>' for f in review['findings'])
    page = '<!doctype html><html lang="en"><meta charset="utf-8"><meta name="viewport" content="width=device-width"><title>VeriBrand review report</title><style>body{font:16px system-ui;max-width:900px;margin:2rem auto;padding:0 1rem;color:#211426}h1{color:#321c3b}li{margin:1rem 0;padding:.75rem;border-bottom:1px solid #ddd}small{color:#555}</style><body><h1>VeriBrand review report</h1><h2>' + esc(review.get('title')) + '</h2><p>Status: ' + esc(review.get('status')) + ' · Version: ' + esc(review.get('version')) + '</p><h3>Draft</h3><pre style="white-space:pre-wrap">' + esc(review.get('draft')) + '</pre><h3>Findings</h3><ol>' + findings + '</ol><p>Generated by VeriBrand. This report supports human review and is not legal advice.</p></body></html>'
    return HTMLResponse(page, headers={'Content-Disposition': f'attachment; filename="veribrand-review-{rid}.html"', 'Cache-Control':'no-store'})

@app.get('/api/audit/export.csv')
def export_audit_csv(limit: int = 1000, u=Depends(need('Reviewer','Admin'))):
    limit = max(1, min(limit, 5000))
    with db() as c: rows = [dict(r) for r in c.execute('SELECT id,actor,action,entity,entity_id,details,created FROM audit ORDER BY id DESC LIMIT ?', (limit,))]
    out = io.StringIO(newline=''); writer = csv.DictWriter(out, fieldnames=['id','actor','action','entity','entity_id','details','created'], extrasaction='ignore')
    writer.writeheader(); writer.writerows(rows)
    return StreamingResponse(iter([out.getvalue()]), media_type='text/csv; charset=utf-8', headers={'Content-Disposition':'attachment; filename="veribrand-audit.csv"','Cache-Control':'no-store'})

@app.post('/api/maintenance/cleanup')
def cleanup_expired_records(u=Depends(need('Admin'))):
    """Delete expired auth/session artifacts. Audit log records counts, not tokens."""
    now = int(time.time())
    with db() as c:
        sessions = c.execute('DELETE FROM sessions WHERE expires<?', (now,)).rowcount
        states = c.execute('DELETE FROM oauth_states WHERE expires<?', (now,)).rowcount
        otp = c.execute('UPDATE users SET otp_hash=NULL, otp_expires=NULL, otp_attempts=0 WHERE otp_expires IS NOT NULL AND otp_expires<?', (now,)).rowcount
        reset = c.execute('UPDATE users SET reset_hash=NULL, reset_expires=NULL WHERE reset_expires IS NOT NULL AND reset_expires<?', (now,)).rowcount
    counts = {'expired_sessions_deleted': max(0,sessions), 'expired_oauth_states_deleted': max(0,states), 'expired_otps_cleared': max(0,otp), 'expired_reset_tokens_cleared': max(0,reset)}
    _audit('expired_records_cleaned','maintenance','',json.dumps(counts),u['email'])
    return {'ok':True,'counts':counts}

class RuleSimIn(BaseModel):
    rule: str
    kind: str = 'tone'
    severity: str = 'Medium'
    pattern: str
    source: str = 'Simulation only'
    replacement: str | None = None
    suggestion: str = ''
    draft: str

@app.post('/api/rules/simulate')
def simulate_rule(x: RuleSimIn, u=Depends(need('Admin'))):
    """Dry-run a proposed deterministic rule without saving it or changing the brand kit."""
    draft = x.draft
    if len(draft) > 50000: raise HTTPException(413,'Draft exceeds 50000 characters')
    if x.kind not in ('tone','term') or x.severity not in ('High','Medium','Low'): raise HTTPException(400,'Invalid rule kind or severity')
    if len(x.pattern) > 300: raise HTTPException(400,'Pattern too long')
    try: pattern = re.compile(x.pattern)
    except re.error: raise HTTPException(400,'Invalid regular expression')
    matches = []
    for m in list(pattern.finditer(draft))[:100]:
        matches.append({'excerpt':m.group(0)[:300], 'start':m.start(), 'end':m.end(), 'replacement':x.replacement, 'suggestion':x.suggestion})
    return {'saved':False,'matches':matches,'match_count':len(matches),'truncated':len(matches)>=100,'message':'Simulation only; no rule or draft was changed.'}

# Mount the static frontend last so it cannot shadow API routes registered below.
app.mount('/', StaticFiles(directory=ROOT / 'frontend', html=True), name='frontend')
