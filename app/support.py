"""Case pipeline: mask → context → AI analysis → policy → execute → notify → audit.
Also incidents (the same problem across many doctors) and the knowledge-base loop."""
import json
import sqlite3

from . import actions, ai, audit, config, kb, workspace
from .db import get_setting, now_iso, row, rows
from .integrations import create_issue, notify
from .masking import mask
from .policy import CATALOG, Context, Proposed, Settings, detect_risk_flags, evaluate

HOLDING_REPLY = "Ми передали ваше звернення відповідальному спеціалісту, відповімо протягом 15 хвилин."
INCIDENT_MIN_DOCTORS = 5
STATUS_BY_OUTCOME = {"auto": "executed", "approval": "pending", "blocked": "blocked", "rejected": "rejected"}
FLAG_TITLES = {"legal_threat": "погрози юристом чи банком", "patient_data": "запит на дані пацієнта",
               "clinical_risk": "клінічний ризик", "prompt_injection": "спроба керувати асистентом",
               "low_confidence": "AI не впевнений"}
SYSTEM_READS = ["acknowledge", "lookup_account", "get_term_edits", "read_feature_docs"]


def autonomy(conn: sqlite3.Connection) -> Settings:
    return Settings(**get_setting(conn, "autonomy", {}))


def case_link(case_id: int) -> str:
    return f"{config.PUBLIC_BASE_URL}/?case={case_id}"


def add_message(conn: sqlite3.Connection, case_id: int, sender: str, text: str) -> None:
    conn.execute("INSERT INTO messages (case_id, sender, text, ts) VALUES (?, ?, ?, ?)",
                 (case_id, sender, text, now_iso()))
    conn.commit()


def create_case(conn: sqlite3.Connection, doctor_id: str | None, text: str, channel: str = "chat",
                review_id: int | None = None) -> int:
    cur = conn.execute("INSERT INTO cases (channel, doctor_id, review_id, text, created_at, status) "
                       "VALUES (?, ?, ?, ?, ?, 'new')", (channel, doctor_id, review_id, text, now_iso()))
    case_id = cur.lastrowid
    if channel == "chat":
        add_message(conn, case_id, "client", text)
    conn.commit()
    return case_id


def _record(conn, case_id, action, params, why, outcome, rule, status, result=None, approved_by=None):
    conn.execute("INSERT INTO actions (case_id, action, params, why, decision, rule, status, result, approved_by, ts) "
                 "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                 (case_id, action, json.dumps(params, ensure_ascii=False), why, outcome, rule, status,
                  json.dumps(result or {}, ensure_ascii=False), approved_by, now_iso()))
    audit.log(conn, f"action_{status}", {"action": action, "params": params, "rule": rule,
                                         "approved_by": approved_by}, case_id)


def build_context(conn: sqlite3.Connection, case: dict) -> dict:
    snapshot = actions.account_snapshot(conn, case["doctor_id"])
    incidents = rows(conn, "SELECT title, doctors FROM incidents WHERE status = 'open'")
    # No timestamps in the prompt: identical cases then hit the response cache after a demo reset.
    events = rows(conn, "SELECT level, source, message FROM events WHERE account_id = ? "
                        "ORDER BY ts DESC LIMIT 8", (case["doctor_id"],))
    for e in events:
        e["message"] = mask(e["message"]).text
    return {"channel": case["channel"], **snapshot,
            "term_edits_7d": actions.term_edits(conn, case["doctor_id"]),
            "recent_events": events, "open_incidents": incidents,
            "previous_cases": previous_cases(conn, case)}


def previous_cases(conn: sqlite3.Connection, case: dict) -> list[dict]:
    """This customer's earlier cases from the last 7 days, newest first, so a follow-up or an answer
    to our clarifying question continues the conversation instead of starting from scratch."""
    earlier = rows(conn, "SELECT id, masked_text, status, analysis FROM cases WHERE doctor_id = ? AND id < ? "
                         "AND channel = 'chat' AND status != 'new' AND created_at >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days') "
                         "ORDER BY id DESC LIMIT 3", (case["doctor_id"], case["id"]))
    history = []
    for c in earlier:
        a = json.loads(c["analysis"] or "{}")
        last = row(conn, "SELECT text FROM messages WHERE case_id = ? AND sender != 'client' ORDER BY id DESC LIMIT 1",
                   (c["id"],))
        history.append({"message": c["masked_text"], "real_goal": a.get("real_goal"), "status": c["status"],
                        "our_question": a.get("clarifying_question"),
                        "our_last_reply": mask(last["text"]).text if last else None})
    return history


def process_case(conn: sqlite3.Connection, case_id: int) -> dict:
    case = row(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    masked = mask(case["text"])
    flags = detect_risk_flags(case["text"])
    context = build_context(conn, case)
    audit.log(conn, "case_received", {"channel": case["channel"], "risk_flags": flags,
                                      "masked": sorted({k.strip("[]").rsplit("_", 1)[0] for k in masked.mapping})},
              case_id)
    if case["channel"] == "chat":
        add_message(conn, case_id, "ai", "Взяли в роботу.")
    for read in SYSTEM_READS:
        _record(conn, case_id, read, {}, "система збирає контекст", "auto", "P3: читання", "executed")

    ws = workspace.get(conn)
    scriber = ws.get("connector") == "scriber"
    knowledge = (kb.knowledge_text(conn) if scriber else "") + "\n" + ws.get("knowledge", "")
    analysis = ai.analyze(conn, masked.text, context, knowledge, workspace.prompt_block(ws), ws.get("connector", "none"))
    if analysis["injection_suspected"] and "prompt_injection" not in flags:
        flags.append("prompt_injection")
    if context["previous_cases"]:
        analysis["repeat_contact"] = True  # from the case history, not guessed from the wording
    route, route_why = workspace.match_route(ws, case["text"], analysis.get("route"))
    owner = workspace.member(ws, route["owner"]) if route else None
    analysis["route"] = {"id": route["id"], "title": route["title"], "mode": route["mode"], "why": route_why,
                         "owner": owner} if route else None
    analysis["risk_flags"] = flags
    analysis["context"] = context
    doctor = context.get("doctor") or {}
    first_name = doctor.get("first_name")

    wants_human = any(a["action"] == "escalate_to_human" for a in analysis["proposed_actions"])
    unsure = analysis["confidence"] < autonomy(conn).low_confidence
    # A P0 from the model, or an unsure model asking for a human, is an alarm, not a routine hand-over:
    # the model can raise a case to P0, and no route can lower it.
    model_p0 = analysis["priority"] == "P0" or (wants_human and unsure)
    if route and not flags and not model_p0 and (route["mode"] == "human_only" or wants_human):
        # No risk flags: "needs a human" means the owner from the template, not a P0 alarm.
        return _hand_to_owner(conn, case, masked.text, analysis, route, owner, first_name)

    # Only an open incident on the same feature already has a ticket; an unrelated bug still gets its own.
    open_incident = row(conn, "SELECT id FROM incidents WHERE status = 'open' AND feature = ? LIMIT 1",
                        (analysis["feature"],))
    plan = [Proposed(a["action"], a["params"], a["why"]) for a in analysis["proposed_actions"]
            if not (a["action"] == "create_bug_ticket" and open_incident)]  # the incident already has a ticket
    ai_priority = analysis["priority"]
    if ai_priority == "P1" and open_incident:
        ai_priority = "P3"  # the incident case carries P1; this customer's own case is routine
    policy_ctx = Context(confidence=analysis["confidence"], ai_priority=ai_priority, risk_flags=flags,
                         account_monthly_usd=float(doctor.get("monthly_usd") or 0),
                         overage_usd=float((context.get("invoice") or {}).get("overage_usd") or 0),
                         recent_credit=bool(context.get("recent_credit")))
    priority, decisions = evaluate(plan, policy_ctx, autonomy(conn))

    pending, rerouted = [], False
    for d in decisions:
        if route and route["mode"] == "ai_with_approval" and d.outcome == "auto" and d.action not in READ_ONLY:
            d.outcome, d.rule = "approval", f"маршрут «{route['title']}»: AI готує, людина підтверджує"
            d.approver = "agent"
            rerouted = True
        status = STATUS_BY_OUTCOME[d.outcome]
        result = _run(conn, case, d.action, d.params) if d.outcome == "auto" else None
        _record(conn, case_id, d.action, d.params, d.why, d.outcome, d.rule, status, result)
        if status == "pending":
            pending.append(CATALOG[d.action].title)

    # The model wrote its reply before the rules ran. If the rules refused an action, or a route turned an
    # automatic action into one that waits, the reply may promise what did not happen: a human checks it first.
    refused = [d.action for d in decisions if d.outcome == "rejected"]
    hold = (f"правила відхилили дію ({', '.join(refused)}), а відповідь могла її пообіцяти" if refused else
            f"маршрут «{route['title']}»: дії ще чекають підтвердження" if rerouted else None)
    reply = (f"{first_name}, {HOLDING_REPLY[0].lower()}{HOLDING_REPLY[1:]}" if first_name else HOLDING_REPLY) \
        if priority == "P0" else analysis["reply_now"]
    if case["channel"] == "chat" and reply:
        if _send_or_hold(conn, case_id, reply, ws, analysis, None if priority == "P0" else hold):
            pending.append(CATALOG["send_reply"].title)
    # Money the autonomy slider already moved: the "after approval" reply is true now, so it goes now.
    auto_money = any(d.level == "P2" and d.outcome == "auto" for d in decisions)
    if case["channel"] == "chat" and not pending and not refused and auto_money and analysis["reply_after_approval"]:
        if not _send_or_hold(conn, case_id, analysis["reply_after_approval"], ws, analysis):
            analysis["reply_after_approval"] = ""

    status = ("escalated" if priority == "P0" else "waiting_approval" if pending
              else "needs_reply" if refused
              else "waiting_client" if analysis["clarifying_question"] else "resolved")
    _save(conn, case_id, masked.text, analysis, priority, status, route, owner)

    lead = _lead(ws)
    if priority == "P0":
        reasons = ", ".join(FLAG_TITLES.get(f, f) for f in flags) or "AI позначив критичним"
        notify(conn, "support", f"🔴 P0 · Кейс {case_id} · {reasons} · {lead}, потрібна людина",
               case_link(case_id))
    elif pending:
        notify(conn, "support", f"🟠 P2 · Кейс {case_id} · {analysis['real_goal'][:90]} · "
                                f"ризик відтоку: {analysis['churn_risk']} · підтвердити: {', '.join(pending)}",
               case_link(case_id))
    elif status == "needs_reply":
        notify(conn, "support", f"✋ Кейс {case_id} · правила відхилили запропоновану дію · "
                                f"відповідь клієнту пише людина", case_link(case_id))
    if scriber and (analysis["category"] == "stt_term_error" or context.get("term_edits_7d")):
        check_term_incidents(conn, case_id)
    return analysis


READ_ONLY = {"acknowledge", "lookup_account", "get_term_edits", "read_feature_docs", "explain_how_it_works",
             "send_help_article", "escalate_to_human"}


def _lead(ws: dict) -> str:
    team = ws.get("team", [])
    lead = next((m for m in team if "ескалац" in m.get("owns", "") or "P0" in m.get("owns", "")), team[0] if team else None)
    return lead["handle"] if lead else "керівник підтримки"


def _save(conn, case_id, masked_text, analysis, priority, status, route, owner) -> None:
    ts = now_iso()
    conn.execute("UPDATE cases SET masked_text = ?, analysis = ?, priority = ?, verdict = ?, status = ?, "
                 "processed_at = ?, resolved_at = ?, route = ?, assigned_to = ? WHERE id = ?",
                 (masked_text, json.dumps(analysis, ensure_ascii=False), priority, analysis["verdict"], status,
                  ts, ts if status == "resolved" else None, route["id"] if route else None,
                  owner["id"] if owner else None, case_id))
    conn.commit()
    audit.log(conn, "case_analyzed", {"priority": priority, "verdict": analysis["verdict"],
                                      "route": route["id"] if route else None,
                                      "engine": analysis.get("engine"), "confidence": analysis["confidence"]},
              case_id)


def _send_or_hold(conn, case_id: int, reply: str, ws: dict, analysis: dict, hold: str | None = None) -> bool:
    """Sends the reply unless it breaks the business tone rules or there is another reason to hold it;
    then it waits for a human. Returns True when the reply was held back."""
    issues = workspace.tone_issues(ws, reply)
    analysis["tone_issues"] = issues
    if not issues and not hold:
        add_message(conn, case_id, "ai", reply)
        return False
    why = f"заборонені фрази: {', '.join(issues)}" if issues else hold
    rule = ("тон: відповідь не пройшла перевірку, потрібна людина" if issues
            else "відповідь не збігається з тим, що виконано: потрібна людина")
    _record(conn, case_id, "send_reply", {"text": reply}, why, "approval", rule, "pending")
    return True


def _hand_to_owner(conn, case: dict, masked_text: str, analysis: dict, route: dict, owner: dict | None,
                   first_name: str | None) -> dict:
    """Human-only routes: the assistant never solves these, it hands them over with a warm reply."""
    case_id = case["id"]
    who = f"{owner['name']} ({owner['role'].lower()})" if owner else "відповідальна людина"
    _record(conn, case_id, "assign_to_owner", {"owner": owner["name"] if owner else None, "route": route["title"]},
            route_reason(analysis), "auto", f"маршрут «{route['title']}»: тільки людина (100%)", "executed",
            {"done": f"передано: {who}"})
    hello = f"{first_name}, дякуємо" if first_name else "Дякуємо"
    reply = (f"{hello} за звернення! Питання «{route['title'].lower()}» веде {who}. "
             f"Ми вже передали все, що ви написали, тож повторювати нічого не потрібно: "
             f"{owner['name'] if owner else 'колега'} відповість вам особисто.")
    if case["channel"] == "chat":
        add_message(conn, case_id, "ai", reply)
    analysis["reply_now"] = reply
    analysis["tone_issues"] = []
    _save(conn, case_id, masked_text, analysis, route.get("priority", "P2"), "assigned", route, owner)
    notify(conn, "support", f"👤 Кейс {case_id} · {route['title']} → {owner['handle'] if owner else '—'} · "
                            f"тільки людина · {analysis['route']['why']}", case_link(case_id))
    return analysis


def route_reason(analysis: dict) -> str:
    r = analysis.get("route") or {}
    return f"правило маршрутизації ({r.get('why', '')})"


def _run(conn: sqlite3.Connection, case: dict, action: str, params: dict) -> dict:
    if action == "create_bug_ticket":
        return create_bug(conn, case, params)
    result = actions.execute(conn, case, action, params)
    conn.commit()
    return result


def _finish_if_done(conn: sqlite3.Connection, case_id: int) -> None:
    if row(conn, "SELECT id FROM actions WHERE case_id = ? AND status = 'pending'", (case_id,)):
        return
    case = row(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    analysis = json.loads(case["analysis"] or "{}")
    refused = row(conn, "SELECT id FROM actions WHERE case_id = ? AND status IN ('declined', 'rejected') LIMIT 1",
                  (case_id,))
    if case["channel"] == "chat" and refused:
        # A human or the rules said no, so the prepared "it's done" reply would be a false promise.
        analysis.pop("reply_after_approval", None)
        answered = row(conn, "SELECT id FROM actions WHERE case_id = ? AND action = 'send_reply' "
                             "AND status = 'executed' LIMIT 1", (case_id,))
        status = "resolved" if answered else "needs_reply"
        conn.execute("UPDATE cases SET analysis = ?, status = ?, resolved_at = ? WHERE id = ?",
                     (json.dumps(analysis, ensure_ascii=False), status, now_iso() if answered else None, case_id))
        conn.commit()
        if not answered:
            notify(conn, "support", f"✋ Кейс {case_id} · дію відхилено · відповідь клієнту пише людина",
                   case_link(case_id))
        return
    if case["channel"] == "chat" and analysis.get("reply_after_approval"):
        if _send_or_hold(conn, case_id, analysis.pop("reply_after_approval"), workspace.get(conn), analysis):
            return
        conn.execute("UPDATE cases SET analysis = ? WHERE id = ?", (json.dumps(analysis, ensure_ascii=False), case_id))
    if case["status"] == "waiting_approval":
        conn.execute("UPDATE cases SET status = 'resolved', resolved_at = ? WHERE id = ?", (now_iso(), case_id))
        conn.commit()


def approve(conn: sqlite3.Connection, action_id: int, who: str) -> dict:
    a = row(conn, "SELECT * FROM actions WHERE id = ? AND status = 'pending'", (action_id,))
    if not a or not _claim(conn, action_id, "running", who):
        return {"error": "дію не знайдено або вже оброблено"}
    case = row(conn, "SELECT * FROM cases WHERE id = ?", (a["case_id"],))
    params = json.loads(a["params"])
    try:
        result = create_bug(conn, case, params) if a["action"] == "create_bug_ticket" else _run(conn, case, a["action"], params)
    except Exception:
        conn.execute("UPDATE actions SET status = 'pending', approved_by = NULL WHERE id = ?", (action_id,))
        conn.commit()
        raise
    conn.execute("UPDATE actions SET status = 'executed', result = ? WHERE id = ?",
                 (json.dumps(result, ensure_ascii=False), action_id))
    audit.log(conn, "action_approved", {"action": a["action"], "params": params, "approved_by": who}, a["case_id"])
    conn.commit()
    _finish_if_done(conn, a["case_id"])
    return result


def _claim(conn: sqlite3.Connection, action_id: int, status: str, who: str) -> bool:
    """Moves a pending action on in one statement, so a double click or a second agent can't run it twice."""
    claimed = conn.execute("UPDATE actions SET status = ?, approved_by = ? WHERE id = ? AND status = 'pending'",
                           (status, who, action_id)).rowcount
    conn.commit()
    return bool(claimed)


def reject(conn: sqlite3.Connection, action_id: int, who: str) -> dict:
    a = row(conn, "SELECT * FROM actions WHERE id = ? AND status = 'pending'", (action_id,))
    if not a or not _claim(conn, action_id, "declined", who):
        return {"error": "дію не знайдено або вже оброблено"}
    audit.log(conn, "action_declined", {"action": a["action"], "declined_by": who}, a["case_id"])
    conn.commit()
    _finish_if_done(conn, a["case_id"])
    return {"done": "відхилено"}


def agent_reply(conn: sqlite3.Connection, case_id: int, text: str, who: str) -> dict:
    """A human answered a case. If the answer is new knowledge, propose a knowledge-base addition."""
    case = row(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))
    add_message(conn, case_id, "agent", text)
    conn.execute("UPDATE cases SET status = 'resolved', resolved_at = ? WHERE id = ?", (now_iso(), case_id))
    audit.log(conn, "agent_replied", {"by": who}, case_id)
    analysis = json.loads(case["analysis"] or "{}")
    proposal = None
    if analysis.get("verdict") != "feature_request" and not analysis.get("gotcha"):
        feature = analysis.get("feature") if analysis.get("feature") in kb.FEATURES else "export"
        title = case["masked_text"][:70] if case["masked_text"] else case["text"][:70]
        body = (f"- **Симптом:** {case['masked_text'] or case['text']}\n- **Вердикт:** not_bug\n"
                f"- **Що сказати клієнту:** {text}\n- **Дія агента:** explain_how_it_works")
        conn.execute("INSERT INTO kb_updates (feature, title, body, case_id, status, ts) "
                     "VALUES (?, ?, ?, ?, 'proposed', ?)", (feature, title, body, case_id, now_iso()))
        _record(conn, case_id, "propose_kb_update", {"feature": feature, "title": title}, "рішення нове, його нема в базі знань",
                "approval", "P2: зміна бази знань, підтверджує людина", "pending")
        notify(conn, "support", f"📚 Кейс {case_id} · пропозиція в базу знань фічі «{feature}» · підтвердити",
               case_link(case_id))
        proposal = {"feature": feature, "title": title}
    conn.commit()
    return {"done": "відповідь надіслано", "kb_proposal": proposal}


# ---------------------------------------------------------------- incidents

def create_bug(conn: sqlite3.Connection, case: dict, params: dict) -> dict:
    issue = create_issue(params.get("title", "Баг з підтримки"), params.get("body", ""))
    return {"done": "баг-тікет створено" if issue.get("url") else "баг-тікет підготовлено (чернетка)",
            "url": issue.get("url"), "error": issue.get("error")}


def check_term_incidents(conn: sqlite3.Connection, trigger_case: int | None = None) -> list[dict]:
    groups = rows(conn, "SELECT before, after, COUNT(DISTINCT doctor_id) AS doctors, MIN(ts) AS first_seen "
                        "FROM term_edits WHERE ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-48 hours') GROUP BY before, after "
                        "HAVING doctors >= ?", (INCIDENT_MIN_DOCTORS,))
    trigger = row(conn, "SELECT doctor_id FROM cases WHERE id = ?", (trigger_case,)) if trigger_case else None
    own_pairs = {(r["before"], r["after"]) for r in actions.term_edits(conn, trigger["doctor_id"])} if trigger else set()
    created = []
    for g in groups:
        key = f"term:{g['before']}->{g['after']}"
        related = (g["before"], g["after"]) in own_pairs
        existing = row(conn, "SELECT * FROM incidents WHERE key = ?", (key,))
        if existing:
            if related:
                conn.execute("UPDATE cases SET incident_id = ? WHERE id = ?", (existing["id"], trigger_case))
                conn.commit()
            continue
        meta = kb.feature_meta("transcription")
        title = f"Розпізнавання: «{g['after']}» записується як «{g['before']}»"
        body = (f"Знайдено автоматично Scriber Care.\n\n- Лікарів з однаковою правкою за 48 год: **{g['doctors']}**\n"
                f"- Правка (знеособлено): «{g['before']}» → «{g['after']}»\n- Перший сигнал: {g['first_seen']}\n"
                f"- Власник фічі: {meta['owner']}\n- Точки входу: {'; '.join(meta['entry_points'])}\n\n"
                f"Пропозиція: додати «{g['after']}» у загальний словник розпізнавання.")
        cur = conn.execute("INSERT INTO incidents (key, title, feature, doctors, first_seen, status) "
                           "VALUES (?, ?, 'transcription', ?, ?, 'open')", (key, title, g["doctors"], g["first_seen"]))
        incident_id = cur.lastrowid
        holder = conn.execute(
            "INSERT INTO cases (channel, text, masked_text, created_at, processed_at, status, priority, verdict, "
            "analysis, incident_id) VALUES ('incident', ?, ?, ?, ?, 'waiting_approval', 'P1', 'bug', ?, ?)",
            (title, title, now_iso(), now_iso(), json.dumps({"real_goal": title, "category": "incident",
                                                             "feature": "transcription", "verdict": "bug",
                                                             "evidence": [f"{g['doctors']} лікарів за 48 год"]},
                                                            ensure_ascii=False), incident_id)).lastrowid
        holder_case = row(conn, "SELECT * FROM cases WHERE id = ?", (holder,))
        result = create_bug(conn, holder_case, {"title": title, "body": body})
        conn.execute("UPDATE incidents SET issue_url = ? WHERE id = ?", (result.get("url"), incident_id))
        _record(conn, holder, "create_bug_ticket", {"title": title}, "та сама проблема у багатьох лікарів",
                "auto", "P1: баг-тікет створюється автоматично", "executed", result)
        _record(conn, holder, "add_term_global_dictionary", {"term": g["after"]}, "виправлення для всіх лікарів",
                "approval", "P1: зміна для всіх клієнтів, підтверджує розробник", "pending")
        if related:
            conn.execute("UPDATE cases SET incident_id = ? WHERE id = ?", (incident_id, trigger_case))
        conn.commit()
        notify(conn, "dev", f"🔴 P1 · Масова проблема · {title} · {g['doctors']} лікарів за 48 год · "
                            f"{result.get('url') or 'тікет-чернетка'} · {meta['slack']} · "
                            f"точка входу: {meta['entry_points'][0] if meta['entry_points'] else '—'}",
               case_link(holder))
        created.append({"id": incident_id, "title": title})
    return created


def mark_incident_fixed(conn: sqlite3.Connection, incident_id: int, who: str) -> dict:
    incident = row(conn, "SELECT * FROM incidents WHERE id = ?", (incident_id,))
    if not incident or incident["status"] != "open":
        return {"error": "інцидент не знайдено або вже закрито"}
    before, after = incident["key"].removeprefix("term:").split("->")
    doctors = [r["doctor_id"] for r in rows(conn, "SELECT DISTINCT doctor_id FROM term_edits WHERE before = ? "
                                                  "AND after = ? AND ts >= strftime('%Y-%m-%dT%H:%M:%S', 'now', '-7 days')", (before, after))]
    conn.execute("UPDATE incidents SET status = 'fixed', fixed_at = ? WHERE id = ?", (now_iso(), incident_id))
    holder = row(conn, "SELECT id FROM cases WHERE channel = 'incident' AND incident_id = ?", (incident_id,))
    text = (f"Вітаємо! Ми виправили розпізнавання терміна «{after}»: тепер він записується правильно для всіх. "
            "Дякуємо за терпіння і вибачте за незручності.")
    _record(conn, holder["id"], "notify_affected", {"doctors": doctors, "text": text},
            "інцидент виправлено, повідомляємо всіх, кого він зачепив", "approval",
            "P2: масова розсилка, одне підтвердження на всіх", "pending")
    conn.execute("UPDATE cases SET status = 'waiting_approval' WHERE id = ?", (holder["id"],))
    audit.log(conn, "incident_fixed", {"incident": incident_id, "by": who, "affected": len(doctors)}, holder["id"])
    conn.commit()
    notify(conn, "support", f"🟢 Інцидент «{incident['title']}» виправлено · підтвердити повідомлення "
                            f"для {len(doctors)} лікарів", case_link(holder["id"]))
    return {"done": f"очікує підтвердження розсилки для {len(doctors)} лікарів"}
