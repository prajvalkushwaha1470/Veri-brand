"""Initial VeriBrand schema for PostgreSQL."""
from alembic import op
revision = '0001_initial'
down_revision = None
branch_labels = None
depends_on = None

def upgrade():
    op.execute("CREATE TABLE IF NOT EXISTS users(id BIGSERIAL PRIMARY KEY, email TEXT UNIQUE NOT NULL, name TEXT NOT NULL, password_hash TEXT NOT NULL,\n  role TEXT NOT NULL DEFAULT 'Creator', verified INTEGER NOT NULL DEFAULT 0, otp_hash TEXT, otp_expires INTEGER, otp_last_sent INTEGER,\n  otp_attempts INTEGER NOT NULL DEFAULT 0, reset_hash TEXT, reset_expires INTEGER, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))")
    op.execute('CREATE TABLE IF NOT EXISTS sessions(token_hash TEXT PRIMARY KEY, user_id INTEGER NOT NULL, expires INTEGER NOT NULL, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute("CREATE TABLE IF NOT EXISTS facts(id BIGSERIAL PRIMARY KEY, name TEXT NOT NULL, value TEXT NOT NULL, source TEXT NOT NULL,\n  category TEXT NOT NULL DEFAULT 'claim', review_by TEXT DEFAULT '', updated TEXT DEFAULT (CURRENT_TIMESTAMP::text))")
    op.execute('CREATE TABLE IF NOT EXISTS rules(id BIGSERIAL PRIMARY KEY, rule TEXT NOT NULL, kind TEXT NOT NULL, severity TEXT NOT NULL, pattern TEXT,\n  source TEXT NOT NULL, replacement TEXT, suggestion TEXT, enabled INTEGER NOT NULL DEFAULT 1)')
    op.execute("CREATE TABLE IF NOT EXISTS reviews(id BIGSERIAL PRIMARY KEY, title TEXT, owner_id INTEGER, owner_name TEXT, status TEXT DEFAULT 'In review',\n  intent TEXT DEFAULT '', protected TEXT DEFAULT '[]', version INTEGER DEFAULT 1, draft TEXT, findings TEXT, ledger TEXT, stats TEXT,\n  approved_by TEXT, approved_at TEXT, content_hash TEXT, created TEXT DEFAULT (CURRENT_TIMESTAMP::text), updated TEXT DEFAULT (CURRENT_TIMESTAMP::text))")
    op.execute('CREATE TABLE IF NOT EXISTS review_versions(id BIGSERIAL PRIMARY KEY, review_id INTEGER NOT NULL, version INTEGER NOT NULL, draft TEXT, findings TEXT,\n  ledger TEXT, note TEXT, created_by TEXT, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS decisions(id BIGSERIAL PRIMARY KEY, review_id INTEGER, version INTEGER, finding_id TEXT, rule_id TEXT, decision TEXT,\n  note TEXT, actor TEXT, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS audit(id BIGSERIAL PRIMARY KEY, actor TEXT, action TEXT NOT NULL, entity TEXT, entity_id TEXT, details TEXT, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS comments(id BIGSERIAL PRIMARY KEY, review_id INTEGER NOT NULL, author TEXT NOT NULL, role TEXT, body TEXT NOT NULL, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS feedback(id BIGSERIAL PRIMARY KEY, email TEXT, rating INTEGER, message TEXT, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS chatlog(id BIGSERIAL PRIMARY KEY, question TEXT, answer TEXT, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS knowledge_chunks(id BIGSERIAL PRIMARY KEY, kind TEXT NOT NULL, source_id TEXT NOT NULL, title TEXT NOT NULL, body TEXT NOT NULL, vector TEXT NOT NULL, updated TEXT DEFAULT (CURRENT_TIMESTAMP::text), UNIQUE(kind,source_id))')
    op.execute("CREATE TABLE IF NOT EXISTS subscriptions(id BIGSERIAL PRIMARY KEY, user_id INTEGER NOT NULL, provider TEXT NOT NULL, customer_id TEXT, subscription_id TEXT, status TEXT NOT NULL DEFAULT 'pending', plan TEXT NOT NULL, created TEXT DEFAULT (CURRENT_TIMESTAMP::text), updated TEXT DEFAULT (CURRENT_TIMESTAMP::text))")
    op.execute('CREATE TABLE IF NOT EXISTS oauth_states(state_hash TEXT PRIMARY KEY, provider TEXT NOT NULL, expires INTEGER NOT NULL, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute('CREATE TABLE IF NOT EXISTS stripe_events(event_id TEXT PRIMARY KEY, event_type TEXT NOT NULL, created TEXT DEFAULT (CURRENT_TIMESTAMP::text))')
    op.execute("CREATE TABLE IF NOT EXISTS review_events(id BIGSERIAL PRIMARY KEY, review_id INTEGER NOT NULL, actor TEXT NOT NULL, event_type TEXT NOT NULL, payload TEXT NOT NULL DEFAULT '{}', created TEXT DEFAULT (CURRENT_TIMESTAMP::text))")

def downgrade():
    op.execute('DROP TABLE IF EXISTS review_events CASCADE')
    op.execute('DROP TABLE IF EXISTS stripe_events CASCADE')
    op.execute('DROP TABLE IF EXISTS oauth_states CASCADE')
    op.execute('DROP TABLE IF EXISTS subscriptions CASCADE')
    op.execute('DROP TABLE IF EXISTS knowledge_chunks CASCADE')
    op.execute('DROP TABLE IF EXISTS chatlog CASCADE')
    op.execute('DROP TABLE IF EXISTS feedback CASCADE')
    op.execute('DROP TABLE IF EXISTS comments CASCADE')
    op.execute('DROP TABLE IF EXISTS audit CASCADE')
    op.execute('DROP TABLE IF EXISTS decisions CASCADE')
    op.execute('DROP TABLE IF EXISTS review_versions CASCADE')
    op.execute('DROP TABLE IF EXISTS reviews CASCADE')
    op.execute('DROP TABLE IF EXISTS rules CASCADE')
    op.execute('DROP TABLE IF EXISTS facts CASCADE')
    op.execute('DROP TABLE IF EXISTS sessions CASCADE')
    op.execute('DROP TABLE IF EXISTS users CASCADE')
