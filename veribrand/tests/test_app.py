"""End-to-end tests. Run:  python -m pytest -q   (or: python tests/test_app.py)"""
import os, sys, tempfile, io
os.environ['DATA_DIR'] = tempfile.mkdtemp(); os.environ['DEMO_MODE'] = '1'; os.environ['EXPOSE_DEV_OTP'] = '1'; os.environ['RATE_LIMIT'] = '500'
os.environ.pop('OPENAI_API_KEY', None)
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from fastapi.testclient import TestClient
import backend_main as bm, engine

c = TestClient(bm.app)
def login(email):
    r = c.post('/api/auth/login', json={'email': email, 'password': 'Demo@12345'}); assert r.status_code == 200, r.text
    return {'Authorization': 'Bearer ' + r.json()['token']}
CRE, REV, ADM = login('creator@lumio.demo'), login('reviewer@lumio.demo'), login('admin@lumio.demo')
SAMPLE = "Lumio is India's #1 budgeting app. Plans start at Rs 499. Sign up now!!"

def make(h=CRE, **kw):
    body = {'title': 'T', 'draft': SAMPLE, 'intent': 'get sign-ups', 'protected': ['Sign up']}; body.update(kw)
    r = c.post('/api/reviews', json=body, headers=h); assert r.status_code == 200, r.text; return r.json()

def test_auth_required():
    for m, p in (('get', '/api/reviews'), ('get', '/api/facts'), ('get', '/api/audit'), ('post', '/api/reviews'), ('get', '/api/users')):
        assert getattr(c, m)(p).status_code == 401, p

def test_health_and_static():
    health = c.get('/api/health'); assert health.json()['ok']; assert 'VeriBrand' in c.get('/').text
    assert health.headers.get('x-content-type-options') == 'nosniff'
    assert health.headers.get('x-frame-options') == 'DENY'
    assert health.headers.get('content-security-policy') and health.headers.get('referrer-policy') == 'no-referrer'

def test_role_enforcement():
    assert c.post('/api/facts', json={'name': 'x', 'value': 'y'}, headers=CRE).status_code == 403
    assert c.post('/api/facts', json={'name': 'x', 'value': 'y'}, headers=REV).status_code == 403
    assert c.get('/api/audit', headers=CRE).status_code == 403
    assert c.get('/api/guideline-health', headers=CRE).status_code == 403
    assert c.get('/api/users', headers=REV).status_code == 403
    assert c.get('/api/audit', headers=REV).status_code == 200
    assert c.get('/api/users', headers=ADM).status_code == 200

def test_signup_cannot_self_promote():
    r = c.post('/api/auth/signup', json={'email': 'evil@x.com', 'name': 'E', 'password': 'Password1234', 'role': 'Admin', 'invite_code': 'guess'}).json()
    assert r['granted_role'] == 'Creator'

def test_otp_flow_and_lockout():
    r = c.post('/api/auth/signup', json={'email': 'new@x.com', 'name': 'N', 'password': 'Password1234'}).json(); otp = r['development_otp']
    assert c.post('/api/auth/login', json={'email': 'new@x.com', 'password': 'Password1234'}).status_code == 403
    for _ in range(5): c.post('/api/auth/verify-otp', json={'email': 'new@x.com', 'otp': '000000'})
    assert c.post('/api/auth/verify-otp', json={'email': 'new@x.com', 'otp': otp}).status_code == 429
    with bm.db() as db: db.execute("UPDATE users SET otp_last_sent=0 WHERE email='new@x.com'")
    otp = c.post('/api/auth/resend-otp', json={'email': 'new@x.com'}).json()['development_otp']
    assert c.post('/api/auth/verify-otp', json={'email': 'new@x.com', 'otp': otp}).status_code == 200
    assert c.post('/api/auth/login', json={'email': 'new@x.com', 'password': 'Password1234'}).status_code == 200

def test_sample_review_matches_plan_mockup():
    r = make(); fs = {f['issue']: f for f in r['findings']}
    assert fs['Unsupported claim']['uncertain'] and fs['Unsupported claim']['needs_human'] and fs['Unsupported claim']['source'] == 'none found'
    p = fs['Price differs from approved fact']; assert p['replacement'] == 'Rs 399' and 'Pricing Sheet v3' in p['source'] and p['severity'] == 'High'
    assert any(f['category'] == 'tone' and f['replacement'] == '.' for f in r['findings'])
    assert {e['status'] for e in r['ledger']} == {'unsupported', 'contradicted'}

def test_stale_source_detected():
    r = make(draft='The Premium plan costs Rs 999.'); e = r['ledger'][0]
    assert e['status'] == 'stale' and 'overdue' in r['findings'][0]['issue'].lower() and r['findings'][0]['needs_human']

def test_creator_isolation():
    r = make(h=REV); assert c.get(f"/api/reviews/{r['id']}", headers=CRE).status_code == 403
    assert all(x['owner_name'] == 'Demo Creator' for x in c.get('/api/reviews', headers=CRE).json())

def test_dismiss_needs_reason_and_accept_needs_rewrite():
    r = make(); rid = r['id']; fs = {f['issue']: f for f in r['findings']}
    unsupported, price = fs['Unsupported claim']['id'], fs['Price differs from approved fact']['id']
    assert c.post(f'/api/reviews/{rid}/decisions', json={'finding_id': price, 'decision': 'Dismiss'}, headers=CRE).status_code == 400
    assert c.post(f'/api/reviews/{rid}/decisions', json={'finding_id': unsupported, 'decision': 'Accept'}, headers=CRE).status_code == 400
    assert c.post(f'/api/reviews/{rid}/decisions', json={'finding_id': price, 'decision': 'Accept'}, headers=CRE).status_code == 200

def test_full_loop_revise_diff_approve_lock():
    r = make(); rid = r['id']
    for f in r['findings']:
        body = {'finding_id': f['id'], 'decision': 'Accept'} if f['replacement'] is not None else \
               {'finding_id': f['id'], 'decision': 'Dismiss', 'note': 'Reviewer will confirm the ranking source'}
        assert c.post(f'/api/reviews/{rid}/decisions', json=body, headers=CRE).status_code == 200
    pv = c.get(f'/api/reviews/{rid}/preview', headers=CRE).json(); assert 'Rs 399' in pv['revised'] and pv['changed'] and any(o['t'] == 'ins' for o in pv['diff'])
    v2 = c.post(f'/api/reviews/{rid}/revise', json={}, headers=CRE).json(); assert v2['version'] == 2 and 'Rs 399' in v2['draft']
    assert any(f['decision'] == 'Dismiss' and 'carried' in f['decision_note'] for f in v2['findings'])
    assert c.post(f'/api/reviews/{rid}/workflow', json={'action': 'approve'}, headers=CRE).status_code == 403
    rv = c.post(f'/api/reviews/{rid}/workflow', json={'action': 'request_changes', 'note': 'Add a source for the ranking'}, headers=REV).json(); assert rv['status'] == 'Changes requested'
    ok = c.post(f'/api/reviews/{rid}/workflow', json={'action': 'approve', 'note': 'ok'}, headers=REV); assert ok.status_code == 200, ok.text
    d = ok.json(); assert d['status'] == 'Approved' and d['content_hash'] and d['approved_by'] == 'reviewer@lumio.demo'
    assert c.post(f'/api/reviews/{rid}/revise', json={'draft': 'changed'}, headers=CRE).status_code == 409
    assert c.get(f'/api/reviews/{rid}/diff?from_v=1&to_v=2', headers=REV).status_code == 200
    assert len(c.get(f'/api/reviews/{rid}', headers=REV).json()['versions']) == 2

def test_approve_blocked_with_pending_unless_override():
    rid = make()['id']
    assert c.post(f'/api/reviews/{rid}/workflow', json={'action': 'approve'}, headers=REV).status_code == 409
    assert c.post(f'/api/reviews/{rid}/workflow', json={'action': 'approve', 'force': True, 'note': 'ok'}, headers=REV).status_code == 409
    assert c.post(f'/api/reviews/{rid}/workflow', json={'action': 'approve', 'force': True, 'note': 'Legal confirmed ranking offline'}, headers=REV).status_code == 200
    assert any('OVERRIDE' in a['details'] for a in c.get('/api/audit', headers=ADM).json())

def test_intent_lock_flags_conflict():
    r = make(draft='Sign up now!!', protected=['now!!']); f = r['findings'][0]
    assert f['intent_conflict'] == 'now!!'

def test_comments_and_guideline_health_and_audit():
    rid = make()['id']; assert c.post(f'/api/reviews/{rid}/comments', json={'body': 'Please check #1 source'}, headers=REV).status_code == 200
    assert c.get(f'/api/reviews/{rid}/comments', headers=CRE).json()[0]['role'] == 'Reviewer'
    gh = c.get('/api/guideline-health', headers=REV).json(); assert gh and gh[0]['triggered'] >= 1
    assert c.get('/api/analytics', headers=ADM).json()['severity_breakdown']

def test_brand_kit_admin_crud_and_validation():
    assert c.post('/api/facts', json={'name': 'Price', 'value': 'free', 'category': 'price'}, headers=ADM).status_code == 400
    assert c.post('/api/facts', json={'name': 'X', 'value': 'y', 'review_by': '31-12-2026'}, headers=ADM).status_code == 400
    f = c.post('/api/facts', json={'name': 'Ranking claim', 'value': 'Rated #1 budgeting app by FinSurvey 2026', 'source': 'FinSurvey', 'category': 'claim', 'review_by': '2099-01-01'}, headers=ADM)
    assert f.status_code == 200
    r = make(); assert not any(x['issue'] == 'Unsupported claim' for x in r['findings'])
    c.delete(f"/api/facts/{f.json()['id']}", headers=ADM)
    assert c.post('/api/rules', json={'rule': 'bad', 'pattern': '(', 'source': 's'}, headers=ADM).status_code == 400

def test_docx_upload():
    from docx import Document
    d = Document(); d.add_paragraph('Plans start at Rs 499. Sign up now!!'); b = io.BytesIO(); d.save(b)
    r = c.post('/api/reviews/upload-docx', files={'file': ('a.docx', b.getvalue(), 'application/octet-stream')}, data={'title': 'Doc'}, headers=CRE); assert r.status_code == 200 and r.json()['findings']
    assert c.post('/api/reviews/upload-docx', files={'file': ('a.txt', b'x')}, headers=CRE).status_code == 400

def test_veribot_grounded_and_ungrounded():
    a = c.post('/api/chat', json={'question': 'What is the Starter plan price?'}, headers=CRE).json(); assert a['grounded'] and 'Rs 399' in a['answer']
    b = c.post('/api/chat', json={'question': 'Who is the CEO of Zzyzx?'}, headers=CRE).json(); assert not b['grounded'] and 'cannot verify' in b['answer']

def test_ai_verification_drops_unverifiable_findings():
    passages = [dict(id='R1', text='Avoid slang the brand voice forbids. Keep the tone friendly but professional.', source='Voice §2.3', policy=True)]
    draft = 'Yo fam, this app is lit'
    good = '{"findings":[{"issue":"Slang","span":"fam","source_id":"R1","quote":"Avoid slang the brand voice forbids","reason":"slang"},' \
           '{"issue":"Invented rule","span":"lit","source_id":"R9","quote":"never say lit","reason":"x"},' \
           '{"issue":"Fake quote","span":"Yo","source_id":"R1","quote":"Greetings must be formal","reason":"x"},' \
           '{"issue":"Span missing","span":"nonexistent","source_id":"R1","quote":"Avoid slang the brand voice forbids","reason":"x"}]}'
    ok, st = engine.verify_ai(good, draft, passages); assert len(ok) == 1 and st == {'proposed': 4, 'verified': 1, 'dropped': 3}
    assert engine.verify_ai('not json', draft, passages)[1]['verified'] == 0
    t, n = engine.redact('mail me at a@b.com or +91 98765 43210'); assert n == 2 and '@' not in t

def test_features_and_disclosure():
    assert c.get('/api/ai-disclosure').json()['external_ai_enabled'] is False and c.get('/api/features').json()['demo_mode']

if __name__ == '__main__':
    fails = 0
    for n, fn in list(globals().items()):
        if n.startswith('test_'):
            try: fn(); print('PASS', n)
            except Exception as e: fails += 1; print('FAIL', n, type(e).__name__, e)
    sys.exit(1 if fails else 0)


def test_password_reset_revokes_existing_sessions(monkeypatch):
    global CRE
    captured = {}
    def fake_send(to, subject, body):
        captured['to'] = to; captured['body'] = body
        return True
    monkeypatch.setattr(bm, '_send_email', fake_send)
    old_auth = CRE
    r = c.post('/api/auth/forgot-password', json={'email':'creator@lumio.demo'})
    assert r.status_code == 200 and 'message' in r.json()
    token = captured['body'].split('Reset token: ', 1)[1].split('.', 1)[0]
    changed = c.post('/api/auth/reset-password', json={'email':'creator@lumio.demo','token':token,'new_password':'NewPassword123'})
    assert changed.status_code == 200, changed.text
    assert c.get('/api/auth/me', headers=old_auth).status_code == 401
    fresh = c.post('/api/auth/login', json={'email':'creator@lumio.demo','password':'NewPassword123'})
    assert fresh.status_code == 200, fresh.text
    CRE = {'Authorization': 'Bearer ' + fresh.json()['token']}
    assert c.post('/api/auth/reset-password', json={'email':'creator@lumio.demo','token':token,'new_password':'AnotherPassword123'}).status_code == 400

def test_pdf_text_upload_review():
    from reportlab.pdfgen import canvas
    b = io.BytesIO(); pdf = canvas.Canvas(b); pdf.drawString(72, 720, 'Plans start at Rs 499. Sign up now!!'); pdf.save()
    r = c.post('/api/reviews/upload-docx', headers={'Authorization': 'Bearer ' + c.post('/api/auth/login', json={'email':'creator@lumio.demo','password':'NewPassword123'}).json()['token']},
               files={'file': ('campaign.pdf', b.getvalue(), 'application/pdf')},
               data={'title': 'PDF campaign', 'intent': 'get sign-ups', 'protected': 'Sign up'})
    assert r.status_code == 200, r.text
    assert r.json()['title'] == 'PDF campaign'
    assert 'Rs 499' in r.json()['draft']


def test_pdf_without_selectable_text_returns_clear_error():
    from reportlab.pdfgen import canvas
    b = io.BytesIO(); pdf = canvas.Canvas(b); pdf.showPage(); pdf.save()
    r = c.post('/api/reviews/upload-docx', headers={'Authorization': 'Bearer ' + c.post('/api/auth/login', json={'email':'creator@lumio.demo','password':'NewPassword123'}).json()['token']},
               files={'file': ('scan.pdf', b.getvalue(), 'application/pdf')}, data={})
    assert r.status_code == 422
    assert 'OCR' in r.json()['detail']


def test_gemini_provider_config_and_disclosure(monkeypatch):
    monkeypatch.setenv('AI_PROVIDER', 'gemini')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-only-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-test-model')
    monkeypatch.delenv('OPENAI_API_KEY', raising=False)
    cfg = bm._ai_config()
    assert cfg['provider'] == 'gemini'
    assert cfg['model'] == 'gemini-test-model'
    assert cfg['base'] == 'https://generativelanguage.googleapis.com/v1beta/openai'
    disclosure = c.get('/api/ai-disclosure').json()
    assert disclosure['external_ai_enabled'] is True
    assert disclosure['provider'] == 'gemini'
    assert disclosure['model'] == 'gemini-test-model'


def test_gemini_call_uses_openai_compatible_endpoint(monkeypatch):
    import json
    from unittest.mock import patch
    monkeypatch.setenv('AI_PROVIDER', 'gemini')
    monkeypatch.setenv('GEMINI_API_KEY', 'test-only-key')
    monkeypatch.setenv('GEMINI_MODEL', 'gemini-test-model')
    class FakeResponse:
        def __enter__(self): return self
        def __exit__(self, *args): return False
        def read(self): return json.dumps({'choices': [{'message': {'content': '{"findings": []}'}}]}).encode()
    with patch.object(bm.urllib.request, 'urlopen', return_value=FakeResponse()) as mocked:
        answer = bm._llm()('system prompt', 'user prompt')
    assert answer == '{"findings": []}'
    request = mocked.call_args.args[0]
    assert request.full_url.endswith('/chat/completions')
    assert request.get_header('Authorization') == 'Bearer test-only-key'
    payload = json.loads(request.data.decode())
    assert payload['model'] == 'gemini-test-model'

def test_p1_vector_knowledge_retrieval():
    global ADM
    ADM = login('admin@lumio.demo')
    r=c.post('/api/knowledge/reindex',headers=ADM); assert r.status_code==200 and r.json()['chunks'] >= 1
    r=c.get('/api/knowledge/search',params={'q':'Starter plan price'},headers=CRE); assert r.status_code==200 and r.json()['results']
    assert r.json()['results'][0]['kind'] in ('fact','rule')

def test_p1_export_data():
    make(h=CRE)
    r=c.get('/api/account/export',headers=CRE); assert r.status_code==200
    assert r.json()['user']['email']=='creator@lumio.demo' and r.json()['owned_reviews']

def test_p1_version_conflict_prevents_overwrite():
    r=make(h=CRE); rid=r['id']
    changed=c.post(f'/api/reviews/{rid}/revise',json={'draft':'A completely new draft without risky claims.','expected_version':1},headers=CRE)
    assert changed.status_code==200, changed.text
    stale=c.post(f'/api/reviews/{rid}/revise',json={'draft':'Stale edit that should not overwrite.','expected_version':1},headers=CRE)
    assert stale.status_code==409

def test_p1_oauth_config_is_reported_and_state_is_one_time():
    assert c.get('/api/oauth/providers').status_code==200
    assert c.get('/api/auth/oauth/google/start').status_code in (302,503)
    state=bm._oauth_state('google'); bm._consume_oauth_state(state,'google')
    try: bm._consume_oauth_state(state,'google'); assert False, 'state should be single use'
    except bm.HTTPException as exc: assert exc.status_code==400

def test_p1_billing_is_fail_closed_without_keys():
    assert c.get('/api/billing/plans').status_code==200
    r=c.post('/api/billing/checkout',json={'plan':'team','success_url':'https://example.com/success','cancel_url':'https://example.com/cancel'},headers=CRE)
    assert r.status_code==503

def test_p1_visual_review_requires_provider_key(monkeypatch):
    monkeypatch.delenv('GEMINI_API_KEY',raising=False)
    r=c.post('/api/reviews/upload-image',files={'file':('logo.png',b'not-real-image','image/png')},headers=CRE)
    assert r.status_code==503

def test_p1_features_and_pwa_assets():
    f=c.get('/api/features/p1').json(); assert f['pwa'] and f['account_export_delete']
    assert c.get('/manifest.webmanifest').status_code==200
    assert c.get('/sw.js').status_code==200

def test_p1_scanned_pdf_ocr_path_is_wired(monkeypatch):
    global CRE
    from reportlab.pdfgen import canvas
    import io
    CRE=login('creator@lumio.demo') if False else CRE
    b=io.BytesIO(); pdf=canvas.Canvas(b); pdf.showPage(); pdf.save()
    monkeypatch.setattr(bm,'_gemini_ocr_pdf',lambda data,max_pages=8:'Plans start at Rs 499. Sign up now!!')
    r=c.post('/api/reviews/upload-docx',headers=CRE,files={'file':('scan.pdf',b.getvalue(),'application/pdf')},data={'title':'OCR test'})
    assert r.status_code==200 and 'Rs 499' in r.json()['draft']

def test_p1_account_delete_requires_confirmation_and_password():
    # Use the demo creator's current test password after the password-reset test.
    bad=c.request('DELETE','/api/account',headers=CRE,json={'password':'wrong-password','confirmation':'DELETE MY ACCOUNT'})
    assert bad.status_code in (400,403)

def test_p1_pg_adapter_helpers_compile():
    row=bm._HybridRow({'id':7,'name':'test'})
    assert row[0]==7 and row['name']=='test'
    assert 'BIGSERIAL PRIMARY KEY' in bm.SCHEMA.replace('INTEGER PRIMARY KEY','BIGSERIAL PRIMARY KEY')

def test_p1_accessibility_contrast_calculation():
    r=c.post('/api/accessibility/contrast',headers=CRE,json={'foreground':'#FFFFFF','background':'#000000'})
    assert r.status_code==200 and r.json()['contrast_ratio']==21.0 and r.json()['wcag_aa_normal_text']
    assert c.post('/api/accessibility/contrast',headers=CRE,json={'foreground':'#FFF','background':'black'}).status_code==400

def test_p1_review_event_feed():
    r=make(h=CRE); rid=r['id']
    comment=c.post(f'/api/reviews/{rid}/comments',headers=CRE,json={'body':'Live collaboration comment'}); assert comment.status_code==200
    events=c.get(f'/api/reviews/{rid}/events',headers=CRE).json()['events']
    assert any(e['event_type']=='comment' for e in events)

def test_p1_google_oauth_callback_with_mocked_provider(monkeypatch):
    import json
    monkeypatch.setenv('GOOGLE_CLIENT_ID','test-client'); monkeypatch.setenv('GOOGLE_CLIENT_SECRET','test-secret')
    monkeypatch.setenv('GOOGLE_REDIRECT_URI','https://example.test/api/auth/oauth/google/callback')
    state=bm._oauth_state('google')
    class Resp:
        def __init__(self,p): self.p=p
        def __enter__(self): return self
        def __exit__(self,*a): return False
        def read(self): return json.dumps(self.p).encode()
    responses=iter([{'access_token':'fake-token'},{'email':'oauth-p1@example.test','email_verified':True,'name':'OAuth P1'}])
    monkeypatch.setattr(bm.urllib.request,'urlopen',lambda *a,**k:Resp(next(responses)))
    r=c.get('/api/auth/oauth/google/callback',params={'code':'fake','state':state},follow_redirects=False)
    assert r.status_code==303 and 'oauth_token=' in r.headers['location']
    assert c.get('/api/oauth/providers').json()['google']

def test_p1_stripe_webhook_signature_and_idempotency(monkeypatch):
    import json,hmac,hashlib,time
    monkeypatch.setenv('STRIPE_WEBHOOK_SECRET','whsec-test')
    event={'id':'evt_p1_test','type':'checkout.session.completed','data':{'object':{'mode':'subscription','payment_status':'paid','client_reference_id':'1','customer':'cus_test','subscription':'sub_p1_test','metadata':{'plan':'team'}}}}
    raw=json.dumps(event,separators=(',',':')).encode(); ts=str(int(time.time()))
    sig=hmac.new(b'whsec-test',ts.encode()+b'.'+raw,hashlib.sha256).hexdigest()
    headers={**CRE,'stripe-signature':f't={ts},v1={sig}'}
    r=c.post('/api/billing/webhook',content=raw,headers=headers); assert r.status_code==200,r.text
    duplicate=c.post('/api/billing/webhook',content=raw,headers=headers); assert duplicate.status_code==200 and duplicate.json().get('duplicate') is True
    assert c.get('/api/billing/status',headers=CRE).json()['active']

def test_p2_readiness_request_id_and_no_store():
    r = c.get('/api/ready', headers={'X-Request-ID':'p2-test-123'})
    assert r.status_code == 200 and r.json()['checks']['database'] and r.json()['checks']['storage']
    assert r.headers['x-request-id'] == 'p2-test-123'
    auth = c.get('/api/auth/me', headers={**CRE, 'X-Request-ID':'auth-check'})
    assert auth.headers['cache-control'] == 'no-store'

def test_p2_brand_kit_export_import_and_validation():
    exported = c.get('/api/brand-kit/export', headers=ADM)
    assert exported.status_code == 200 and exported.json()['schema_version'] == 1
    payload = {'schema_version':1,'facts':[{'name':'Imported fact','value':'A real fact','source':'Source doc','category':'claim'}],
               'rules':[{'rule':'No wow','kind':'tone','severity':'Low','pattern':r'\bwow\b','source':'Voice guide','suggestion':'Use precise language'}]}
    r = c.post('/api/brand-kit/import', json=payload, headers=ADM)
    assert r.status_code == 200 and r.json()['facts_imported'] == 1 and r.json()['rules_imported'] == 1
    assert any(f['name']=='Imported fact' for f in c.get('/api/facts',headers=ADM).json())
    bad = {'schema_version':1,'facts':[],'rules':[{'rule':'bad','kind':'tone','severity':'Low','pattern':'(','source':'x'}]}
    assert c.post('/api/brand-kit/import',json=bad,headers=ADM).status_code == 400

def test_p2_review_html_report_escapes_untrusted_content():
    r = make(h=REV, title='<script>alert(1)</script>', draft='Hello <script>alert(2)</script>!!')
    report = c.get(f"/api/reviews/{r['id']}/report.html", headers=REV)
    assert report.status_code == 200 and 'attachment;' in report.headers['content-disposition']
    assert '<script>alert(1)</script>' not in report.text
    assert '&lt;script&gt;alert(2)&lt;/script&gt;' in report.text
    assert c.get(f"/api/reviews/{r['id']}/report.html",headers=REV).status_code == 200

def test_p2_audit_csv_role_and_headers():
    c.get('/api/audit',headers=ADM)
    r = c.get('/api/audit/export.csv',headers=ADM)
    assert r.status_code == 200 and 'text/csv' in r.headers['content-type']
    assert 'actor,action' in r.text or 'id,actor,action' in r.text
    assert c.get('/api/audit/export.csv',headers=CRE).status_code == 403

def test_p2_rule_simulation_does_not_persist_rule():
    before = len(c.get('/api/rules',headers=ADM).json())
    r = c.post('/api/rules/simulate',headers=ADM,json={'rule':'no wow','kind':'tone','severity':'Low','pattern':r'\bwow\b','source':'test','replacement':'great','draft':'wow wow'})
    assert r.status_code == 200 and r.json()['match_count'] == 2 and r.json()['saved'] is False
    assert len(c.get('/api/rules',headers=ADM).json()) == before
    bad = c.post('/api/rules/simulate',headers=ADM,json={'rule':'bad','pattern':'(','draft':'x'})
    assert bad.status_code == 400

def test_p2_cleanup_expired_auth_records():
    with bm.db() as db:
        db.execute('INSERT INTO sessions(token_hash,user_id,expires) VALUES(?,?,?)', ('expired-test-session', 1, int(__import__('time').time())-10))
        db.execute('INSERT INTO oauth_states(state_hash,provider,expires) VALUES(?,?,?)', ('expired-test-state','google',int(__import__('time').time())-10))
    r = c.post('/api/maintenance/cleanup',headers=ADM)
    assert r.status_code == 200 and r.json()['counts']['expired_sessions_deleted'] >= 1
    with bm.db() as db:
        assert not db.execute('SELECT 1 FROM sessions WHERE token_hash=?',('expired-test-session',)).fetchone()
        assert not db.execute('SELECT 1 FROM oauth_states WHERE state_hash=?',('expired-test-state',)).fetchone()
