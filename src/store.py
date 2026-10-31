"""SQLite store (outside process memory) for reviews, events and the simulated receiving systems."""
import os
import sqlite3

DB_PATH = os.environ.get("TRIAGE_DB", os.path.join("db", "triage.db"))

SCHEMA = """
CREATE TABLE IF NOT EXISTS reviews (
    review_id     TEXT PRIMARY KEY,
    claim_id      TEXT NOT NULL,
    run_id        TEXT,
    status        TEXT NOT NULL,      -- PENDING | ESCALATED | APPROVED | REJECTED | EXPIRED | SUPERSEDED
    action        TEXT NOT NULL,      -- what an approval authorizes, e.g. PAYOUT
    amount        REAL,
    content_hash  TEXT NOT NULL,      -- version of the claim content the reviewer was shown
    assigned_to   TEXT,
    escalate_to   TEXT,
    fallback_score INTEGER DEFAULT 0, -- 1 = risk score came from the rule fallback, not the model
    created_at    TEXT,
    expires_at    TEXT,
    escalated_at  TEXT,
    decided_by    TEXT,
    decided_at    TEXT,
    decision_note TEXT
);
CREATE TABLE IF NOT EXISTS events (
    event_id    TEXT PRIMARY KEY,     -- the idempotency key of an incoming event
    event_type  TEXT,
    review_id   TEXT,
    claim_id    TEXT,
    payload     TEXT,
    received_at TEXT,
    outcome     TEXT                  -- NULL until the event is fully processed
);
CREATE TABLE IF NOT EXISTS payments (          -- simulated payment system (the receiver)
    payment_id      INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    claim_id        TEXT,
    amount          REAL,
    created_at      TEXT
);
CREATE TABLE IF NOT EXISTS notifications (     -- simulated customer notification service
    notification_id INTEGER PRIMARY KEY AUTOINCREMENT,
    idempotency_key TEXT UNIQUE,
    claim_id        TEXT,
    kind            TEXT,
    created_at      TEXT
);
"""


def connect():
    os.makedirs(os.path.dirname(DB_PATH) or ".", exist_ok=True)
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def reset():
    """Delete the database (used by the test script for a clean start)."""
    if os.path.exists(DB_PATH):
        os.remove(DB_PATH)