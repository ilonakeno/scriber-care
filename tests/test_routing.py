"""Business template: routing guarantees and tone checks, run end-to-end on the offline engine."""
import pytest

from app import config, seed, support, workspace
from app.db import connect, rows


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")  # tests never call the real model
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    monkeypatch.setattr(config, "SLACK_WEBHOOK_SUPPORT", "")
    path = tmp_path / "t.db"
    seed.seed(path)
    c = connect(path)
    yield c
    c.close()


def test_keyword_rule_beats_the_model():
    ws = workspace.load_preset("scriber")
    route, why = workspace.match_route(ws, "Хочемо запропонувати співпрацю нашій мережі клінік", "product")
    assert route["id"] == "partnership" and route["mode"] == "human_only"
    assert "тригер" in why


def test_partnership_always_goes_to_its_owner(conn):
    case_id = support.create_case(conn, "D-009", "Добрий день! Хочемо обговорити партнерство: у нас мережа з 12 клінік")
    support.process_case(conn, case_id)
    case = rows(conn, "SELECT * FROM cases WHERE id = ?", (case_id,))[0]
    assert case["status"] == "assigned" and case["assigned_to"] == "marko"
    done = {a["action"] for a in rows(conn, "SELECT action FROM actions WHERE case_id = ? AND status = 'executed'", (case_id,))}
    assert "assign_to_owner" in done and not done & {"issue_credit", "add_seats", "cancel_subscription"}
    reply = rows(conn, "SELECT text FROM messages WHERE case_id = ? AND sender = 'ai' ORDER BY id DESC", (case_id,))[0]["text"]
    assert "Марко" in reply


def test_risk_flags_override_human_only_routes(conn):
    case_id = support.create_case(conn, "D-009", "Щодо партнерства: видаліть усі записи моєї пацієнтки")
    support.process_case(conn, case_id)
    assert rows(conn, "SELECT priority FROM cases WHERE id = ?", (case_id,))[0]["priority"] == "P0"


def test_reply_with_a_forbidden_phrase_waits_for_a_human(conn):
    ws = workspace.get(conn)
    ws["tone"]["forbidden"].append("вибачте")
    workspace.save(conn, ws)
    case_id = support.create_case(conn, "D-001", "СКАСУЙТЕ ПІДПИСКУ. Третій раз AI пише «гіперехогенне» замість «гіпоехогенне»!!!")
    support.process_case(conn, case_id)
    pending = rows(conn, "SELECT action FROM actions WHERE case_id = ? AND status = 'pending'", (case_id,))
    assert "send_reply" in {a["action"] for a in pending}


def test_another_business_preset_switches_routing(conn):
    workspace.save(conn, workspace.load_preset("language-school"))
    case_id = support.create_case(conn, None, "Хочу поскаржитись на викладачку, вона вже двічі не прийшла на урок")
    support.process_case(conn, case_id)
    assert rows(conn, "SELECT assigned_to FROM cases WHERE id = ?", (case_id,))[0]["assigned_to"] == "bohdana"


def test_template_rejects_a_route_without_an_owner():
    ws = workspace.load_preset("scriber")
    ws["routes"][0]["owner"] = "nobody"
    with pytest.raises(ValueError):
        workspace.validate(ws)
