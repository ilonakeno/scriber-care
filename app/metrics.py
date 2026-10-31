"""Support effectiveness, computed only from the cases, actions and audit tables."""
import json
import sqlite3
import statistics
from datetime import datetime

from . import audit
from .db import get_setting, rows


def _minutes(start: str | None, end: str | None) -> float | None:
    if not start or not end:
        return None
    return (datetime.fromisoformat(end) - datetime.fromisoformat(start)).total_seconds() / 60


def summary(conn: sqlite3.Connection) -> dict:
    cases = rows(conn, "SELECT * FROM cases WHERE channel IN ('chat', 'review') AND status != 'new'")
    approved = {r["case_id"] for r in rows(conn, "SELECT DISTINCT case_id FROM actions WHERE approved_by IS NOT NULL")}
    agent_msgs = {r["case_id"] for r in rows(conn, "SELECT DISTINCT case_id FROM messages WHERE sender = 'agent'")}
    total = len(cases)
    ai_only = [c for c in cases if c["status"] == "resolved" and c["id"] not in approved
               and c["id"] not in agent_msgs and c["priority"] != "P0"]
    support = [c for c in cases if c["id"] not in {x["id"] for x in ai_only}]
    dev = [c for c in cases if c["incident_id"]]
    analyses = [json.loads(c["analysis"] or "{}") for c in cases]
    no_question = sum(1 for a in analyses if not a.get("clarifying_question"))
    retained = sum(1 for c, a in zip(cases, analyses)
                   if "скасу" in (a.get("stated_request") or "").lower() + (c["text"] or "").lower()
                   and not rows(conn, "SELECT id FROM doctors WHERE id = ? AND status = 'cancelled'", (c["doctor_id"],)))
    times = [m for m in (_minutes(c["processed_at"], c["resolved_at"]) for c in cases) if m is not None]
    manual_minutes = get_setting(conn, "manual_minutes_per_case", 8)

    reviews = rows(conn, "SELECT * FROM reviews WHERE processed = 1")
    by_product: dict[str, list] = {}
    for r in reviews:
        by_product.setdefault(r["product"], []).append(json.loads(r["analysis"] or "{}") | {"rating": r["rating"]})
    products = {p: {"reviews": len(items),
                    "avg_rating": round(statistics.mean(i["rating"] for i in items), 1),
                    "avg_sentiment": round(statistics.mean(i.get("sentiment", 0) for i in items), 2),
                    "sarcasm": sum(1 for i in items if i.get("sarcasm")),
                    "complaints": [i.get("summary") for i in items if i.get("sentiment", 0) < 0][:3]}
                for p, items in by_product.items()}

    def pct(n: int) -> int:
        return round(100 * n / total) if total else 0

    return {
        "cases": total,
        "ai_only_pct": pct(len(ai_only)), "support_pct": pct(len(support)), "dev_pct": pct(len(dev)),
        "ai_only": len(ai_only), "needed_support": len(support), "needed_dev": len(dev),
        "p0": sum(1 for c in cases if c["priority"] == "P0"),
        "median_minutes_to_resolve": round(statistics.median(times), 1) if times else None,
        "no_clarifying_question_pct": pct(no_question),
        "minutes_saved": len(ai_only) * manual_minutes, "manual_minutes_assumption": manual_minutes,
        "retained_after_cancel_request": retained,
        "repeat_contacts": sum(1 for a in analyses if a.get("repeat_contact")),
        "reviews": {"processed": len(reviews),
                    "support_cases": len(rows(conn, "SELECT id FROM cases WHERE channel = 'review'")),
                    "replied": len(rows(conn, "SELECT id FROM reviews WHERE reply_status = 'published'"))},
        "products": products,
        "incidents": rows(conn, "SELECT id, title, doctors, status, issue_url FROM incidents"),
        "engines": sorted({a.get("engine") for a in analyses if a.get("engine")}),
        "audit": audit.verify(conn),
    }


def digest_text(s: dict) -> str:
    lines = [
        "📊 Scriber Care · щоденний звіт (синтетичні демо-дані)",
        f"Звернень: {s['cases']} · AI сам: {s['ai_only_pct']}% · підтримка: {s['support_pct']}% · "
        f"розробники: {s['dev_pct']}%",
        f"Медіанний час до вирішення: {s['median_minutes_to_resolve']} хв · заощаджено ~{s['minutes_saved']} хв агентів "
        f"(припущення: {s['manual_minutes_assumption']} хв на ручне звернення)",
        f"Без уточнювальних питань: {s['no_clarifying_question_pct']}% · утримано після «скасуйте»: "
        f"{s['retained_after_cancel_request']} · критичних (P0): {s['p0']}",
    ]
    for i in s["incidents"]:
        lines.append(f"🔴 Інцидент: {i['title']} ({i['doctors']} лікарів) · {i['status']} · {i['issue_url'] or 'чернетка'}")
    r = s["reviews"]
    lines.append(f"⭐ Відгуки: {r['processed']} оброблено · {r['support_cases']} стали зверненнями · {r['replied']} з відповіддю")
    for product, p in s["products"].items():
        label = "наш продукт" if product == "Scriber" else "вигаданий конкурент"
        lines.append(f"   {product} ({label}): ★{p['avg_rating']} · настрій тексту {p['avg_sentiment']:+} · "
                     f"сарказм: {p['sarcasm']}" + (f" · скарги: {'; '.join(p['complaints'][:2])}" if p["complaints"] else ""))
    lines.append(f"🔐 Журнал: {'цілісний' if s['audit']['ok'] else 'ПОРУШЕНО'} · {s['audit']['entries']} записів")
    return "\n".join(lines)
