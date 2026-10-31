"""Append-only audit log. Each entry stores the hash of the previous one, so any edit
made after the fact breaks the chain and shows up in verify()."""
import hashlib
import json
import sqlite3

from .db import now_iso

GENESIS = "0" * 64


def _digest(prev_hash: str, ts: str, case_id, event: str, data_json: str) -> str:
    payload = json.dumps([prev_hash, ts, case_id, event, data_json], ensure_ascii=False)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def log(conn: sqlite3.Connection, event: str, data: dict, case_id: int | None = None) -> str:
    last = conn.execute("SELECT hash FROM audit ORDER BY id DESC LIMIT 1").fetchone()
    prev = last["hash"] if last else GENESIS
    ts = now_iso()
    data_json = json.dumps(data, ensure_ascii=False, sort_keys=True)
    digest = _digest(prev, ts, case_id, event, data_json)
    conn.execute(
        "INSERT INTO audit (ts, case_id, event, data, prev_hash, hash) VALUES (?, ?, ?, ?, ?, ?)",
        (ts, case_id, event, data_json, prev, digest),
    )
    conn.commit()
    return digest


def verify(conn: sqlite3.Connection) -> dict:
    prev = GENESIS
    count = 0
    for r in conn.execute("SELECT * FROM audit ORDER BY id"):
        expected = _digest(prev, r["ts"], r["case_id"], r["event"], r["data"])
        if r["prev_hash"] != prev or r["hash"] != expected:
            return {"ok": False, "broken_at": r["id"], "entries": count}
        prev = r["hash"]
        count += 1
    return {"ok": True, "broken_at": None, "entries": count}
