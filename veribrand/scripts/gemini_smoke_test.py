"""Live Gemini API smoke test. Run only after setting GEMINI_API_KEY.
Uses synthetic, non-sensitive text and prints no API key.
"""
import json
import os
import sys

os.environ.setdefault('AI_PROVIDER', 'gemini')
if not os.getenv('GEMINI_API_KEY', '').strip():
    print('ERROR: set GEMINI_API_KEY in environment or .env first.', file=sys.stderr)
    raise SystemExit(2)

import backend_main as app
cfg = app._ai_config()
if cfg['provider'] != 'gemini':
    print('ERROR: AI_PROVIDER must be gemini for this smoke test.', file=sys.stderr)
    raise SystemExit(2)

try:
    call = app._llm()
    if call is None:
        raise RuntimeError('Gemini client is not configured')
    raw = call(
        'Return only a valid JSON object with keys "provider_test" and "ok". No markdown.',
        'Synthetic connectivity test only. Return {"provider_test":"gemini","ok":true}.',
    )
    parsed = json.loads(raw)
    if parsed.get('provider_test') != 'gemini' or parsed.get('ok') is not True:
        raise RuntimeError('Gemini responded, but the output did not match the smoke-test schema')
    print(f"PASS: Gemini API responded. model={cfg['model']} endpoint={cfg['base']}")
except Exception as exc:
    print(f'FAIL: Gemini smoke test failed: {type(exc).__name__}: {exc}', file=sys.stderr)
    raise SystemExit(1)
