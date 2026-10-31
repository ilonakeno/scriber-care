"""HTTP API and static UI. Run: uvicorn app.main:app --reload"""
import json
from dataclasses import asdict

from fastapi import Depends, FastAPI, HTTPException
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import audit, config, metrics, reviews, seed, support
from .db import connect, get_setting, row, rows, set_setting
from .integrations import notify
from .policy import CATALOG

app = FastAPI(title="Scriber Care")
STATIC = config.ROOT / "static"
app.mount("/static", StaticFiles(directory=STATIC), name="static")


def db():
    conn = connect(config.DB_PATH)
    try:
        yield conn
    finally:
        conn.close()


@app.on_event("startup")
def ensure_seeded() -> None:
    if not config.DB_PATH.exists():
        seed.seed()


@app.get("/")
def agent_page():
    return FileResponse(STATIC / "agent.html")


@app.get("/chat")
def chat_page():
    return FileResponse(STATIC / "chat.html")


@app.get("/dashboard")
def dashboard_page():
    return FileResponse(STATIC / "dashboard.html")


class ChatIn(BaseModel):
    doctor_id: str
    text: str


class ReplyIn(BaseModel):
    text: str
    who: str = "Оля"


class WhoIn(BaseModel):
    who: str = "Оля"


class ReviewerIn(BaseModel):
    doctor_id: str
    who: str = "Оля"


class AutonomyIn(BaseModel):
    auto_credit_limit_usd: float = 0
    auto_waive_limit_usd: float = 0
    auto_min_confidence: float = 0.9


def _case_view(conn, case: dict) -> dict:
    doctor = row(conn, "SELECT id, name, specialty, clinic_id, plan FROM doctors WHERE id = ?", (case["doctor_id"],))
    acts = rows(conn, "SELECT * FROM actions WHERE case_id = ? ORDER BY id", (case["id"],))
    for a in acts:
        a["params"] = json.loads(a["params"] or "{}")
        a["result"] = json.loads(a["result"] or "{}")
        a["title"] = CATALOG[a["action"]].title if a["action"] in CATALOG else a["action"]
    return {**case, "analysis": json.loads(case["analysis"] or "{}"), "doctor": doctor, "actions": acts,
            "messages": rows(conn, "SELECT * FROM messages WHERE case_id = ? ORDER BY id", (case["id"],)),
            "review": row(conn, "SELECT * FROM reviews WHERE id = ?", (case["review_id"],)),
            "incident": row(conn, "SELECT * FROM incidents WHERE id = ?", (case["incident_id"],)),
            "kb_update": row(conn, "SELECT * FROM kb_updates WHERE case_id = ? ORDER BY id DESC LIMIT 1", (case["id"],))}


@app.get("/api/cases")
def list_cases(conn=Depends(db)):
    order = "CASE priority WHEN 'P0' THEN 0 WHEN 'P1' THEN 1 WHEN 'P2' THEN 2 WHEN 'P3' THEN 3 ELSE 4 END"
    items = rows(conn, f"SELECT c.id, c.channel, c.status, c.priority, c.verdict, c.text, c.created_at, c.analysis, "
                       f"d.name AS doctor FROM cases c LEFT JOIN doctors d ON d.id = c.doctor_id "
                       f"WHERE c.channel != 'proactive' ORDER BY CASE c.status WHEN 'new' THEN 1 "
                       f"WHEN 'resolved' THEN 2 ELSE 0 END, {order}, c.id DESC")
    for item in items:
        a = json.loads(item.pop("analysis") or "{}")
        item.update(real_goal=a.get("real_goal"), emotion=a.get("emotion"), sarcasm=a.get("sarcasm"),
                    churn_risk=a.get("churn_risk"))
    return items


@app.get("/api/cases/{case_id}")
def get_case(case_id: int, conn=Depends(db)):
    case = row(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise HTTPException(404)
    return _case_view(conn, case)


@app.post("/api/cases/{case_id}/process")
def process(case_id: int, conn=Depends(db)):
    support.process_case(conn, case_id)
    return get_case(case_id, conn)


@app.post("/api/queue/process")
def process_queue(conn=Depends(db)):
    ids = [r["id"] for r in rows(conn, "SELECT id FROM cases WHERE status = 'new' AND channel = 'chat' ORDER BY id")]
    for case_id in ids:
        support.process_case(conn, case_id)
    return {"processed": ids}


@app.post("/api/cases/{case_id}/reply")
def reply(case_id: int, body: ReplyIn, conn=Depends(db)):
    return support.agent_reply(conn, case_id, body.text, body.who)


@app.post("/api/cases/{case_id}/reviewer")
def confirm_reviewer(case_id: int, body: ReviewerIn, conn=Depends(db)):
    return reviews.confirm_reviewer(conn, case_id, body.doctor_id, body.who)


@app.post("/api/actions/{action_id}/approve")
def approve(action_id: int, body: WhoIn, conn=Depends(db)):
    return support.approve(conn, action_id, body.who)


@app.post("/api/actions/{action_id}/reject")
def reject(action_id: int, body: WhoIn, conn=Depends(db)):
    return support.reject(conn, action_id, body.who)


@app.get("/api/doctors")
def doctors(conn=Depends(db)):
    return rows(conn, "SELECT d.id, d.name, d.specialty, d.plan, c.name AS clinic FROM doctors d "
                      "LEFT JOIN clinics c ON c.id = d.clinic_id ORDER BY d.id")


@app.post("/api/chat")
def chat(body: ChatIn, conn=Depends(db)):
    case_id = support.create_case(conn, body.doctor_id, body.text)
    support.process_case(conn, case_id)
    return {"case_id": case_id}


@app.get("/api/chat/{case_id}")
def chat_messages(case_id: int, conn=Depends(db)):
    case = row(conn, "SELECT id, channel, doctor_id, review_id, status FROM cases WHERE id = ?", (case_id,))
    if not case:
        raise HTTPException(404)
    msgs = rows(conn, "SELECT sender, text, ts FROM messages WHERE case_id = ? ORDER BY id", (case_id,))
    if case["channel"] == "review" and not msgs:
        r = row(conn, "SELECT * FROM reviews WHERE id = ?", (case["review_id"],))
        analysis = json.loads(r["analysis"] or "{}") if r else {}
        greeting = ("Вітаємо! Ми бачили ваш відгук і вже знаємо, у чому проблема"
                    + (f": {analysis.get('summary')}" if analysis.get("summary") else "")
                    + ". Пояснювати нічого не потрібно, розкажіть лише, чим ще допомогти.")
        support.add_message(conn, case_id, "ai", greeting)
        msgs = rows(conn, "SELECT sender, text, ts FROM messages WHERE case_id = ? ORDER BY id", (case_id,))
    return {"case": case, "messages": msgs}


@app.post("/api/reviews/run")
def run_reviews(conn=Depends(db)):
    return reviews.daily_pass(conn)


@app.get("/api/reviews")
def list_reviews(conn=Depends(db)):
    items = rows(conn, "SELECT * FROM reviews ORDER BY date DESC")
    for r in items:
        r["analysis"] = json.loads(r["analysis"] or "{}")
    return items


@app.post("/api/incidents/{incident_id}/fixed")
def incident_fixed(incident_id: int, body: WhoIn, conn=Depends(db)):
    return support.mark_incident_fixed(conn, incident_id, body.who)


@app.get("/api/metrics")
def get_metrics(conn=Depends(db)):
    return metrics.summary(conn)


@app.post("/api/digest")
def send_digest(conn=Depends(db)):
    text = metrics.digest_text(metrics.summary(conn))
    delivered = notify(conn, "support", text, f"{config.PUBLIC_BASE_URL}/dashboard")
    return {"text": text, "delivered_to_slack": delivered}


@app.get("/api/settings/autonomy")
def get_autonomy(conn=Depends(db)):
    return asdict(support.autonomy(conn))


@app.post("/api/settings/autonomy")
def set_autonomy(body: AutonomyIn, conn=Depends(db)):
    value = body.model_dump()
    set_setting(conn, "autonomy", value)
    audit.log(conn, "autonomy_changed", value)
    return value


@app.get("/api/notifications")
def notifications(conn=Depends(db)):
    return rows(conn, "SELECT * FROM notifications ORDER BY id DESC LIMIT 40")


@app.get("/api/audit")
def audit_log(conn=Depends(db)):
    return {"status": audit.verify(conn), "entries": rows(conn, "SELECT * FROM audit ORDER BY id DESC LIMIT 60")}


@app.get("/api/status")
def status(conn=Depends(db)):
    return {"llm_model": config.LLM_MODEL.split(",")[0].strip() if config.GEMINI_API_KEY else None,
            "slack": bool(config.SLACK_WEBHOOK_SUPPORT), "github_repo": config.GITHUB_REPO or None,
            "manual_minutes_per_case": get_setting(conn, "manual_minutes_per_case", 8)}


@app.post("/api/reset")
def reset():
    seed.seed()
    return {"done": "демо-дані перестворено"}
