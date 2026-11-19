import json
import sqlite3
import time
from contextlib import contextmanager

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT UNIQUE NOT NULL,
    full_name TEXT DEFAULT '', email TEXT DEFAULT '', phone TEXT DEFAULT '', upi_id TEXT DEFAULT '',
    pw_hash TEXT NOT NULL,
    is_admin INTEGER NOT NULL DEFAULT 0,
    status TEXT NOT NULL DEFAULT 'pending',        -- pending | approved | disabled
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS plans (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    account_size REAL NOT NULL,
    fee_inr REAL NOT NULL DEFAULT 0,
    phases INTEGER NOT NULL DEFAULT 2,
    p1_target_pct REAL NOT NULL DEFAULT 8,
    p2_target_pct REAL NOT NULL DEFAULT 5,
    min_trading_days INTEGER NOT NULL DEFAULT 3,
    max_days INTEGER NOT NULL DEFAULT 0,
    profit_split_pct REAL NOT NULL DEFAULT 80,
    payout_interval_days INTEGER NOT NULL DEFAULT 14,
    min_payout_usd REAL NOT NULL DEFAULT 0,
    consistency_pct REAL NOT NULL DEFAULT 0,
    rules_json TEXT NOT NULL DEFAULT '{}',
    active INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS accounts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    user_id INTEGER NOT NULL,
    plan_id INTEGER NOT NULL,
    plan_json TEXT NOT NULL,           -- snapshot of plan at purchase (rules can't change under the trader)
    phase TEXT NOT NULL,               -- phase1 | phase2 | funded
    status TEXT NOT NULL,              -- pending_payment | payment_review | awaiting_setup | active | paused_day
                                       -- | passed | payout_pending | failed | closed
    status_reason TEXT DEFAULT '',
    fee_inr REAL DEFAULT 0, payment_ref TEXT DEFAULT '',
    api_key TEXT DEFAULT '', api_secret TEXT DEFAULT '', subaccount_label TEXT DEFAULT '',
    starting_balance REAL,
    phase_started_at REAL,
    day_date TEXT DEFAULT '', day_start_equity REAL, peak_equity REAL, last_equity REAL,
    last_check REAL, last_error TEXT DEFAULT '',
    funded_at REAL, last_payout_at REAL,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orders (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL, ts REAL NOT NULL, day TEXT NOT NULL, phase TEXT,
    symbol TEXT, side TEXT, size INTEGER, order_type TEXT,
    limit_price REAL, stop_loss REAL, take_profit REAL, leverage INTEGER,
    kind TEXT, status TEXT, exchange_order_id TEXT, message TEXT
);
CREATE TABLE IF NOT EXISTS violations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL, ts REAL NOT NULL, rule TEXT, detail TEXT, action TEXT
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL, ts REAL NOT NULL, event TEXT, detail TEXT
);
CREATE TABLE IF NOT EXISTS equity_snaps (account_id INTEGER NOT NULL, ts REAL NOT NULL, equity REAL NOT NULL);
CREATE TABLE IF NOT EXISTS daily_stats (
    account_id INTEGER NOT NULL, day TEXT NOT NULL, phase TEXT,
    start_equity REAL, end_equity REAL, pnl REAL, low_equity REAL, trades INTEGER, violations INTEGER,
    PRIMARY KEY (account_id, day)
);
CREATE TABLE IF NOT EXISTS payouts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    account_id INTEGER NOT NULL, user_id INTEGER NOT NULL,
    requested_at REAL NOT NULL,
    profit_usd REAL, split_pct REAL, trader_share_usd REAL, trader_share_inr REAL, usd_inr REAL,
    status TEXT NOT NULL,              -- requested | approved | paid | rejected
    admin_note TEXT DEFAULT '', utr TEXT DEFAULT '', paid_at REAL
);
CREATE INDEX IF NOT EXISTS idx_snaps ON equity_snaps(account_id, ts);
CREATE INDEX IF NOT EXISTS idx_orders ON orders(account_id, ts);
"""


@contextmanager
def conn():
    c = sqlite3.connect(config.DB_PATH, timeout=30)
    c.row_factory = sqlite3.Row
    try:
        yield c
        c.commit()
    finally:
        c.close()


MIGRATIONS = [
    ("users", "otp_hash", "TEXT DEFAULT ''"),
    ("users", "otp_expires", "REAL DEFAULT 0"),
    ("users", "otp_tries", "INTEGER DEFAULT 0"),
    ("users", "otp_sent_at", "REAL DEFAULT 0"),
    ("users", "last_login", "REAL"),
]


def init():
    with conn() as c:
        c.executescript(SCHEMA)
        for table, col, decl in MIGRATIONS:
            cols = [r[1] for r in c.execute(f"PRAGMA table_info({table})")]
            if col not in cols:
                c.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def q(sql, args=(), one=False):
    with conn() as c:
        rows = [dict(r) for r in c.execute(sql, args).fetchall()]
    return (rows[0] if rows else None) if one else rows


def x(sql, args=()):
    with conn() as c:
        return c.execute(sql, args).lastrowid


def plan_of(account):
    return json.loads(account["plan_json"])


def rules_of(account):
    r = dict(config.DEFAULT_RULES)
    r.update(plan_of(account).get("rules", {}))
    return r


def event(account_id, ev, detail=""):
    x("INSERT INTO events(account_id, ts, event, detail) VALUES (?,?,?,?)", (account_id, time.time(), ev, detail))


def violation(account_id, rule, detail, action):
    x("INSERT INTO violations(account_id, ts, rule, detail, action) VALUES (?,?,?,?,?)",
      (account_id, time.time(), rule, detail, action))


def account(aid):
    return q("SELECT * FROM accounts WHERE id=?", (aid,), one=True)
