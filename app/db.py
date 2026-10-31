"""SQLite storage: the simulated Scriber backend plus the support layer (cases, actions, audit)."""
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS clinics (
    id TEXT PRIMARY KEY, name TEXT, seats INTEGER, price_per_seat REAL, status TEXT
);
CREATE TABLE IF NOT EXISTS doctors (
    id TEXT PRIMARY KEY, name TEXT, specialty TEXT, clinic_id TEXT, plan TEXT,
    price REAL, status TEXT, review_alias TEXT
);
CREATE TABLE IF NOT EXISTS invoices (
    id TEXT PRIMARY KEY, account_id TEXT, period TEXT, amount_usd REAL,
    protocols INTEGER, overage_usd REAL, status TEXT
);
CREATE TABLE IF NOT EXISTS protocols (
    id TEXT PRIMARY KEY, doctor_id TEXT, created_at TEXT, template TEXT, version INTEGER, status TEXT
);
CREATE TABLE IF NOT EXISTS term_edits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, doctor_id TEXT, protocol_id TEXT, ts TEXT,
    before TEXT, after TEXT
);
CREATE TABLE IF NOT EXISTS dictionary (
    id INTEGER PRIMARY KEY AUTOINCREMENT, doctor_id TEXT, term TEXT, added_at TEXT, added_by TEXT
);
CREATE TABLE IF NOT EXISTS credits (
    id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT, amount_usd REAL, reason TEXT,
    case_id INTEGER, ts TEXT
);
CREATE TABLE IF NOT EXISTS cases (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, doctor_id TEXT, review_id INTEGER,
    text TEXT, masked_text TEXT, created_at TEXT, status TEXT, priority TEXT, verdict TEXT,
    analysis TEXT, resolved_at TEXT, incident_id INTEGER
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, sender TEXT, text TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS actions (
    id INTEGER PRIMARY KEY AUTOINCREMENT, case_id INTEGER, action TEXT, params TEXT, why TEXT,
    decision TEXT, rule TEXT, status TEXT, result TEXT, approved_by TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT, source TEXT, product TEXT, author TEXT, role TEXT,
    company_size TEXT, rating INTEGER, text TEXT, date TEXT, processed INTEGER DEFAULT 0,
    analysis TEXT, case_id INTEGER, reply TEXT, reply_status TEXT
);
CREATE TABLE IF NOT EXISTS incidents (
    id INTEGER PRIMARY KEY AUTOINCREMENT, key TEXT UNIQUE, title TEXT, feature TEXT,
    doctors INTEGER, first_seen TEXT, status TEXT, issue_url TEXT, fixed_at TEXT
);
CREATE TABLE IF NOT EXISTS notifications (
    id INTEGER PRIMARY KEY AUTOINCREMENT, channel TEXT, text TEXT, link TEXT, ts TEXT, delivered INTEGER
);
CREATE TABLE IF NOT EXISTS kb_updates (
    id INTEGER PRIMARY KEY AUTOINCREMENT, feature TEXT, title TEXT, body TEXT, case_id INTEGER,
    status TEXT, ts TEXT
);
CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS llm_cache (key TEXT PRIMARY KEY, response TEXT, model TEXT, ts TEXT);
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT, ts TEXT, case_id INTEGER, event TEXT, data TEXT,
    prev_hash TEXT, hash TEXT
);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def connect(path: Path | str) -> sqlite3.Connection:
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def rows(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    return [dict(r) for r in conn.execute(sql, params).fetchall()]


def row(conn: sqlite3.Connection, sql: str, params: tuple = ()) -> dict | None:
    r = conn.execute(sql, params).fetchone()
    return dict(r) if r else None


def get_setting(conn: sqlite3.Connection, key: str, default):
    r = row(conn, "SELECT value FROM settings WHERE key = ?", (key,))
    return json.loads(r["value"]) if r else default


def set_setting(conn: sqlite3.Connection, key: str, value) -> None:
    conn.execute(
        "INSERT INTO settings (key, value) VALUES (?, ?) "
        "ON CONFLICT(key) DO UPDATE SET value = excluded.value",
        (key, json.dumps(value)),
    )
    conn.commit()
