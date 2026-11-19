"""Independent additive commerce storage. Monetary values are integer toman."""
from contextlib import contextmanager, closing
from pathlib import Path
import os
import sqlite3


SCHEMA = '''
CREATE TABLE IF NOT EXISTS customers (
 id INTEGER PRIMARY KEY, telegram_id INTEGER NOT NULL UNIQUE, first_name TEXT NOT NULL DEFAULT '',
 username TEXT NOT NULL DEFAULT '', suspended INTEGER NOT NULL DEFAULT 0,
 referral_code TEXT NOT NULL UNIQUE, referrer_id INTEGER REFERENCES customers(id), created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS products (
 id INTEGER PRIMARY KEY, name TEXT NOT NULL, category TEXT NOT NULL,
 traffic_gb INTEGER, days INTEGER NOT NULL, devices INTEGER NOT NULL, price_toman INTEGER NOT NULL CHECK(price_toman>=0),
 active INTEGER NOT NULL DEFAULT 0, target TEXT NOT NULL, pricing TEXT NOT NULL DEFAULT '{}', created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS quotes (
 id TEXT PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), snapshot TEXT NOT NULL,
 total_toman INTEGER NOT NULL CHECK(total_toman>=0), expires_at INTEGER NOT NULL, consumed_order INTEGER);
CREATE TABLE IF NOT EXISTS payment_destinations (
 id INTEGER PRIMARY KEY, card_number TEXT NOT NULL, holder_name TEXT NOT NULL, bank_name TEXT NOT NULL DEFAULT '',
 instructions TEXT NOT NULL DEFAULT '', active INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS destination_audit (
 id INTEGER PRIMARY KEY, destination_id INTEGER NOT NULL REFERENCES payment_destinations(id), actor TEXT NOT NULL,
 snapshot TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS orders (
 id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), quote_id TEXT REFERENCES quotes(id),
 status TEXT NOT NULL, snapshot TEXT NOT NULL, total_toman INTEGER NOT NULL CHECK(total_toman>=0),
 payment_method TEXT NOT NULL, payment_id INTEGER, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS payments (
 id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), order_id INTEGER REFERENCES orders(id),
 kind TEXT NOT NULL CHECK(kind IN ('order','topup')), amount_toman INTEGER NOT NULL CHECK(amount_toman>=0),
 status TEXT NOT NULL, destination_snapshot TEXT NOT NULL, receipt_reference TEXT, transaction_text TEXT,
 reviewer TEXT, reviewed_at INTEGER, reason TEXT, created_at INTEGER NOT NULL, expires_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS wallet_ledger (
 id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), amount_toman INTEGER NOT NULL,
 kind TEXT NOT NULL, reference TEXT NOT NULL UNIQUE, reason TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TRIGGER IF NOT EXISTS wallet_no_update BEFORE UPDATE ON wallet_ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TRIGGER IF NOT EXISTS wallet_no_delete BEFORE DELETE ON wallet_ledger BEGIN SELECT RAISE(ABORT,'immutable ledger'); END;
CREATE TABLE IF NOT EXISTS subscriptions (
 id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), order_id INTEGER UNIQUE REFERENCES orders(id),
 native_id INTEGER UNIQUE, status TEXT NOT NULL, snapshot TEXT NOT NULL, native_snapshot TEXT,
 trial INTEGER NOT NULL DEFAULT 0, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS provisioning_jobs (
 id INTEGER PRIMARY KEY, operation_id TEXT NOT NULL UNIQUE, order_id INTEGER UNIQUE REFERENCES orders(id),
 subscription_id INTEGER NOT NULL REFERENCES subscriptions(id), status TEXT NOT NULL, spec TEXT NOT NULL,
 attempts INTEGER NOT NULL DEFAULT 0, error TEXT, created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS trial_claims (
 customer_id INTEGER PRIMARY KEY REFERENCES customers(id), subscription_id INTEGER NOT NULL UNIQUE REFERENCES subscriptions(id), created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS discounts (
 id INTEGER PRIMARY KEY, code TEXT NOT NULL UNIQUE COLLATE NOCASE, kind TEXT NOT NULL CHECK(kind IN ('percent','fixed')),
 value INTEGER NOT NULL CHECK(value>=0), active INTEGER NOT NULL DEFAULT 0, expires_at INTEGER,
 max_uses INTEGER, per_customer INTEGER NOT NULL DEFAULT 1, product_ids TEXT NOT NULL DEFAULT '[]', first_purchase INTEGER NOT NULL DEFAULT 0);
CREATE TABLE IF NOT EXISTS discount_redemptions (
 id INTEGER PRIMARY KEY, discount_id INTEGER NOT NULL REFERENCES discounts(id), customer_id INTEGER NOT NULL REFERENCES customers(id),
 order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS referral_rewards (
 referred_id INTEGER PRIMARY KEY REFERENCES customers(id), referrer_id INTEGER NOT NULL REFERENCES customers(id),
 order_id INTEGER NOT NULL UNIQUE REFERENCES orders(id), amount_toman INTEGER NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS support_tickets (
 id INTEGER PRIMARY KEY, customer_id INTEGER NOT NULL REFERENCES customers(id), category TEXT NOT NULL, status TEXT NOT NULL,
 created_at INTEGER NOT NULL, updated_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS support_messages (
 id INTEGER PRIMARY KEY, ticket_id INTEGER NOT NULL REFERENCES support_tickets(id), actor TEXT NOT NULL,
 body TEXT NOT NULL, attachment_reference TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS notification_jobs (
 id INTEGER PRIMARY KEY, customer_id INTEGER REFERENCES customers(id), kind TEXT NOT NULL, payload TEXT NOT NULL,
 unique_key TEXT NOT NULL UNIQUE, status TEXT NOT NULL DEFAULT 'pending', attempts INTEGER NOT NULL DEFAULT 0,
 available_at INTEGER NOT NULL, lease_until INTEGER NOT NULL DEFAULT 0, last_error TEXT, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS commerce_audit (
 id INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL, target TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS commerce_settings (name TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS customer_announcements (
 id INTEGER PRIMARY KEY, title TEXT NOT NULL, body TEXT NOT NULL, active INTEGER NOT NULL DEFAULT 1,
 actor TEXT NOT NULL, created_at INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS commerce_receipts (
 actor TEXT NOT NULL, request_id TEXT NOT NULL, fingerprint TEXT NOT NULL, response TEXT NOT NULL,
 created_at INTEGER NOT NULL, PRIMARY KEY(actor,request_id));
CREATE INDEX IF NOT EXISTS orders_customer ON orders(customer_id,id);
CREATE INDEX IF NOT EXISTS payments_pending ON payments(status,id);
CREATE INDEX IF NOT EXISTS subscriptions_customer ON subscriptions(customer_id,id);
CREATE INDEX IF NOT EXISTS ledger_customer ON wallet_ledger(customer_id,id);
CREATE INDEX IF NOT EXISTS support_customer ON support_tickets(customer_id,id);
CREATE INDEX IF NOT EXISTS jobs_pending ON provisioning_jobs(status,id);
CREATE INDEX IF NOT EXISTS notifications_pending ON notification_jobs(status,available_at);
'''


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.initialize()

    def _connect(self):
        db = sqlite3.connect(self.path, timeout=30, isolation_level=None)
        db.row_factory = sqlite3.Row
        db.execute('PRAGMA foreign_keys=ON')
        db.execute('PRAGMA busy_timeout=30000')
        return db

    def initialize(self):
        self.path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
        with closing(self._connect()) as db:
            db.executescript(SCHEMA)
            db.execute('PRAGMA user_version=1')
        if os.name != 'nt':
            os.chmod(self.path, 0o600)

    @contextmanager
    def db(self, write=False):
        db = self._connect()
        try:
            db.execute('BEGIN IMMEDIATE' if write else 'BEGIN')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()


CommerceStore = Store
