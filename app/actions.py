"""Executors for allowlisted actions against the simulated Scriber backend.
Only the support layer calls these, and only after the policy engine allowed it."""
import sqlite3

from .db import now_iso, row, rows


def _doctor(conn, case) -> dict | None:
    return row(conn, "SELECT * FROM doctors WHERE id = ?", (case.get("doctor_id"),))


def _last_protocol(conn, doctor_id) -> dict | None:
    return row(conn, "SELECT * FROM protocols WHERE doctor_id = ? ORDER BY created_at DESC LIMIT 1", (doctor_id,))


def execute(conn: sqlite3.Connection, case: dict, action: str, params: dict) -> dict:
    doctor = _doctor(conn, case)
    did = doctor["id"] if doctor else None
    ts = now_iso()

    if action == "add_term_personal_dictionary":
        conn.execute("INSERT INTO dictionary (doctor_id, term, added_at, added_by) VALUES (?, ?, ?, 'ai')",
                     (did, params.get("term"), ts))
        return {"done": f"«{params.get('term')}» додано в особистий словник"}
    if action == "add_term_global_dictionary":
        conn.execute("INSERT INTO dictionary (doctor_id, term, added_at, added_by) VALUES (NULL, ?, ?, 'dev')",
                     (params.get("term"), ts))
        return {"done": f"«{params.get('term')}» додано в загальний словник"}
    if action in ("regenerate_protocol", "restore_recording"):
        protocol = _last_protocol(conn, did)
        if not protocol:
            return {"done": "протокол не знайдено"}
        status = "regenerated" if action == "regenerate_protocol" else "restored"
        conn.execute("UPDATE protocols SET version = version + 1, status = ? WHERE id = ?", (status, protocol["id"]))
        return {"done": f"протокол {protocol['id']}: версія {protocol['version'] + 1}, попередня збережена"}
    if action in ("explain_how_it_works", "send_help_article"):
        return {"done": f"надіслано пояснення {params.get('gotcha') or ''}".strip()}
    if action == "resend_export":
        protocol = _last_protocol(conn, did)
        return {"done": f"протокол {protocol['id'] if protocol else ''} надіслано як {params.get('format', 'docx')}"}
    if action == "issue_credit":
        account = doctor["clinic_id"] or did if doctor else None
        conn.execute("INSERT INTO credits (account_id, amount_usd, reason, case_id, ts) VALUES (?, ?, ?, ?, ?)",
                     (did or account, float(params["amount_usd"]), "компенсація за незручності", case["id"], ts))
        return {"done": f"нараховано кредит ${float(params['amount_usd']):g}"}
    if action == "waive_overage":
        conn.execute("UPDATE invoices SET overage_usd = MAX(overage_usd - ?, 0), amount_usd = amount_usd - ? "
                     "WHERE account_id = ? AND overage_usd > 0", (float(params["amount_usd"]),
                                                                 float(params["amount_usd"]), did))
        return {"done": f"списано доплату ${float(params['amount_usd']):g}"}
    if action == "add_seats":
        clinic = doctor["clinic_id"] if doctor else None
        conn.execute("UPDATE clinics SET seats = seats + ? WHERE id = ?", (int(params["count"]), clinic))
        return {"done": f"клініці додано {int(params['count'])} місця"}
    if action == "cancel_subscription":
        conn.execute("UPDATE doctors SET status = 'cancelled' WHERE id = ?", (did,))
        return {"done": "підписку скасовано"}
    if action == "post_review_reply":
        conn.execute("UPDATE reviews SET reply = ?, reply_status = 'published' WHERE id = ?",
                     (params.get("text"), case.get("review_id")))
        return {"done": "відповідь опубліковано (симуляція майданчика)"}
    if action == "escalate_to_human":
        return {"done": "передано людині"}
    if action == "send_reply":
        conn.execute("INSERT INTO messages (case_id, sender, text, ts) VALUES (?, 'ai', ?, ?)",
                     (case["id"], params.get("text"), ts))
        return {"done": "відповідь надіслано після перевірки людиною"}
    if action == "notify_affected":
        doctors = params.get("doctors") or []
        for doctor_id in doctors:
            conn.execute("INSERT INTO cases (channel, doctor_id, text, created_at, status, priority, verdict) "
                         "VALUES ('proactive', ?, ?, ?, 'resolved', 'P3', 'bug')",
                         (doctor_id, "Проактивне повідомлення про виправлення", ts))
            new_id = conn.execute("SELECT last_insert_rowid()").fetchone()[0]
            conn.execute("INSERT INTO messages (case_id, sender, text, ts) VALUES (?, 'ai', ?, ?)",
                         (new_id, params.get("text"), ts))
        return {"done": f"повідомлено {len(doctors)} лікарів"}
    if action == "propose_kb_update":
        conn.execute("UPDATE kb_updates SET status = 'approved' WHERE case_id = ? AND status = 'proposed'",
                     (case["id"],))
        return {"done": "базу знань доповнено (у проді: PR у репозиторій документації)"}
    return {"done": "без змін"}


def account_snapshot(conn: sqlite3.Connection, doctor_id: str | None) -> dict:
    doctor = row(conn, "SELECT * FROM doctors WHERE id = ?", (doctor_id,))
    if not doctor:
        return {}
    clinic = row(conn, "SELECT * FROM clinics WHERE id = ?", (doctor["clinic_id"],)) if doctor["clinic_id"] else None
    invoice = row(conn, "SELECT * FROM invoices WHERE account_id = ? ORDER BY period DESC LIMIT 1",
                  (doctor["clinic_id"] or doctor_id,))
    own_invoice = row(conn, "SELECT * FROM invoices WHERE account_id = ? ORDER BY period DESC LIMIT 1", (doctor_id,))
    recent_credit = row(conn, "SELECT id FROM credits WHERE account_id = ? AND ts >= datetime('now', '-30 days')",
                        (doctor_id,))
    return {
        "doctor": {"first_name": doctor["name"].split()[0], "specialty": doctor["specialty"],
                   "plan": doctor["plan"], "monthly_usd": doctor["price"], "status": doctor["status"]},
        "clinic": {"name": clinic["name"], "seats": clinic["seats"]} if clinic else None,
        "invoice": {"period": (own_invoice or invoice or {}).get("period"),
                    "protocols": (own_invoice or invoice or {}).get("protocols"),
                    "overage_usd": (own_invoice or {}).get("overage_usd", 0.0)},
        "recent_credit": bool(recent_credit),
    }


def term_edits(conn: sqlite3.Connection, doctor_id: str | None) -> list[dict]:
    return rows(conn, "SELECT before, after, COUNT(*) AS count FROM term_edits WHERE doctor_id = ? "
                      "AND ts >= datetime('now', '-7 days') GROUP BY before, after ORDER BY count DESC",
                (doctor_id,))
