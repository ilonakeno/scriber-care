"""Daily pass over public reviews (G2, Capterra): sentiment by text rather than stars,
sarcasm, support cases for our product, a reviewer match proposal and a reply draft."""
import json
import sqlite3

from . import ai, audit, config
from .db import now_iso, row, rows
from .integrations import notify
from .masking import find_leaks, mask
from .policy import Context, Proposed, Settings, evaluate
from .support import _record, case_link

TRANSLIT = str.maketrans({"а": "a", "б": "b", "в": "v", "г": "h", "ґ": "g", "д": "d", "е": "e", "є": "ie",
                          "ж": "zh", "з": "z", "и": "y", "і": "i", "ї": "i", "й": "i", "к": "k", "л": "l",
                          "м": "m", "н": "n", "о": "o", "п": "p", "р": "r", "с": "s", "т": "t", "у": "u",
                          "ф": "f", "х": "kh", "ц": "ts", "ч": "ch", "ш": "sh", "щ": "shch", "ь": "",
                          "ю": "iu", "я": "ia", "'": "", "’": ""})
SIZE_BUCKETS = {"Self-employed": (0, 0), "1–10 employees": (1, 10), "11–50 employees": (11, 50),
                "51–200 employees": (51, 200)}


def match_reviewer(conn: sqlite3.Connection, review: dict) -> dict | None:
    """B2B reviews are signed with a first name, surname initial, role and company size.
    Returns the best candidate with the reasons; a human confirms it."""
    first, _, rest = review["author"].partition(" ")
    initial = rest[:1].lower()
    low, high = SIZE_BUCKETS.get(review["company_size"], (None, None))
    best = None
    for d in rows(conn, "SELECT d.*, c.seats FROM doctors d LEFT JOIN clinics c ON c.id = d.clinic_id"):
        name, surname = d["name"].split(" ", 1)
        reasons, score = [], 0.0
        if name.lower().translate(TRANSLIT) == first.lower():
            reasons.append("ім'я збігається")
            score += 0.45
        else:
            continue
        if surname.lower().translate(TRANSLIT)[:1] == initial:
            reasons.append("перша літера прізвища збігається")
            score += 0.25
        seats = d["seats"] or 0
        if low is not None and low <= seats <= high:
            reasons.append(f"розмір компанії відповідає клініці ({seats} місць)")
            score += 0.15
        if best is None or score > best["confidence"]:
            best = {"doctor_id": d["id"], "name": d["name"], "confidence": round(score, 2), "reasons": reasons}
    return best


def daily_pass(conn: sqlite3.Connection) -> dict:
    pending = rows(conn, "SELECT * FROM reviews WHERE processed = 0 ORDER BY date")
    if not pending:
        return {"processed": 0}
    context = {"product": "Scriber", "open_incidents": rows(conn, "SELECT title FROM incidents WHERE status = 'open'")}
    results, engine = ai.analyze_reviews(conn, pending, context)
    incident = row(conn, "SELECT * FROM incidents WHERE status = 'open' ORDER BY id DESC LIMIT 1")
    new_cases = []
    for review, res in zip(pending, results):
        conn.execute("UPDATE reviews SET processed = 1, analysis = ? WHERE id = ?",
                     (json.dumps({**res, "engine": engine}, ensure_ascii=False), review["id"]))
        if not res["is_support_case"]:
            continue
        candidate = match_reviewer(conn, review)
        cur = conn.execute("INSERT INTO cases (channel, doctor_id, review_id, text, masked_text, created_at, status) "
                           "VALUES ('review', NULL, ?, ?, ?, ?, 'new')",
                           (review["id"], review["text"], mask(review["text"]).text, now_iso()))
        case_id = cur.lastrowid
        invite = f"{config.PUBLIC_BASE_URL}/chat?case={case_id}"
        reply = (res["reply_draft"] or "Дякуємо за відгук! Напишіть нам у чат підтримки, і ми допоможемо.")
        reply = f"{reply}\nЧат підтримки: {invite}"
        leaks = find_leaks(reply)
        linked = incident["id"] if incident and res["feature"] == "transcription" else None
        analysis = {
            "stated_request": f"{review['source']} · {review['rating']}★ · {review['author']}",
            "real_goal": res["summary"], "category": "review", "feature": res["feature"],
            "verdict": "bug" if res["feature"] in ("transcription", "export") else "account_billing",
            "emotion": {"type": "frustration" if res["sentiment"] < 0 else "satisfaction",
                        "intensity": abs(res["sentiment"])},
            "sarcasm": res["sarcasm"], "sentiment": res["sentiment"], "churn_risk": "high" if res["sentiment"] < -0.5 else "medium",
            "candidate": candidate, "engine": engine,
            "evidence": [f"оцінка {review['rating']}★, а настрій тексту {res['sentiment']:+.1f}"
                         + (" (сарказм)" if res["sarcasm"] else "")]
                        + ([f"пов'язано з інцидентом «{incident['title']}»"] if linked else []),
            "reply_now": "", "reply_after_approval": "", "confidence": candidate["confidence"] if candidate else 0.5,
            "leaks": leaks,
        }
        _, decisions = evaluate([Proposed("post_review_reply", {"text": reply}, "публічна скарга на наш продукт")],
                                Context(confidence=1.0, ai_priority="P2", risk_flags=[], account_monthly_usd=0,
                                        overage_usd=0, recent_credit=False), Settings())
        for d in decisions:
            if leaks:
                _record(conn, case_id, d.action, d.params, d.why, "blocked",
                        f"фільтр персональних даних: {', '.join(leaks)}", "blocked")
            else:
                _record(conn, case_id, d.action, d.params, d.why, d.outcome, d.rule, "pending")
        conn.execute("UPDATE cases SET analysis = ?, priority = 'P2', verdict = ?, status = 'waiting_approval', "
                     "processed_at = ?, incident_id = ? WHERE id = ?",
                     (json.dumps(analysis, ensure_ascii=False), analysis["verdict"], now_iso(), linked, case_id))
        conn.commit()
        notify(conn, "support", f"🟠 P2 · Відгук {review['source']} {review['rating']}★"
                                f"{' · сарказм' if res['sarcasm'] else ''} · {res['summary'][:80]} · "
                                f"підтвердити публічну відповідь", case_link(case_id))
        new_cases.append(case_id)
    audit.log(conn, "daily_review_pass", {"reviews": len(pending), "support_cases": len(new_cases), "engine": engine})
    conn.commit()
    return {"processed": len(pending), "support_cases": new_cases, "engine": engine}


def confirm_reviewer(conn: sqlite3.Connection, case_id: int, doctor_id: str, who: str) -> dict:
    conn.execute("UPDATE cases SET doctor_id = ? WHERE id = ?", (doctor_id, case_id))
    audit.log(conn, "reviewer_confirmed", {"doctor_id": doctor_id, "by": who}, case_id)
    conn.commit()
    return {"done": "автора відгуку підтверджено"}
