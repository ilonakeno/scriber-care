"""What the customer is told must match what actually happened: a declined or refused action is never
reported as done, money moved by the slider is reported, and one approval runs an action once."""
from datetime import datetime, timedelta, timezone

import pytest

from app import actions, config, seed, support, workspace
from app.db import connect, row, rows, set_setting

MARTA = "СКАСУЙТЕ ПІДПИСКУ. Третій раз за тиждень AI пише «гіперехогенне» замість «гіпоехогенне», я так працювати не можу!!!"


@pytest.fixture()
def conn(tmp_path, monkeypatch):
    monkeypatch.setattr(config, "GEMINI_API_KEY", "")  # tests never call the real model
    monkeypatch.setattr(config, "GITHUB_REPO", "")
    monkeypatch.setattr(config, "SLACK_WEBHOOK_SUPPORT", "")
    monkeypatch.setattr(config, "SLACK_WEBHOOK_DEV", "")
    path = tmp_path / "t.db"
    seed.seed(path)
    c = connect(path)
    yield c
    c.close()


def ai_messages(conn, case_id):
    return [m["text"] for m in rows(conn, "SELECT text FROM messages WHERE case_id = ? AND sender = 'ai'", (case_id,))]


def pending(conn, case_id, action):
    return row(conn, "SELECT id FROM actions WHERE case_id = ? AND action = ? AND status = 'pending'", (case_id, action))


def test_declined_credit_is_never_reported_as_paid(conn):
    case_id = support.create_case(conn, "D-001", MARTA)
    support.process_case(conn, case_id)
    support.reject(conn, pending(conn, case_id, "issue_credit")["id"], "Оля")
    assert not any("нарахували" in m for m in ai_messages(conn, case_id))
    assert row(conn, "SELECT status FROM cases WHERE id = ?", (case_id,))["status"] == "needs_reply"
    support.agent_reply(conn, case_id, "Марто, кредит не нараховуємо, але словник уже виправлено.", "Оля")
    assert row(conn, "SELECT status FROM cases WHERE id = ?", (case_id,))["status"] == "resolved"


def test_approved_credit_is_reported_after_it_ran(conn):
    case_id = support.create_case(conn, "D-001", MARTA)
    support.process_case(conn, case_id)
    support.approve(conn, pending(conn, case_id, "issue_credit")["id"], "Оля")
    assert any("нарахували" in m for m in ai_messages(conn, case_id))
    assert row(conn, "SELECT status FROM cases WHERE id = ?", (case_id,))["status"] == "resolved"


def test_action_refused_by_the_rules_is_not_resolved_by_ai(conn):
    case_id = support.create_case(conn, "D-009", "Додайте нам, будь ласка, ще 30 місць, з понеділка виходять нові лікарі")
    support.process_case(conn, case_id)
    assert row(conn, "SELECT status FROM actions WHERE case_id = ? AND action = 'add_seats'", (case_id,))["status"] == "rejected"
    assert row(conn, "SELECT status FROM cases WHERE id = ?", (case_id,))["status"] == "waiting_approval"
    assert ai_messages(conn, case_id) == ["Взяли в роботу."]  # the promise "додамо місця" waits for a human
    support.reject(conn, pending(conn, case_id, "send_reply")["id"], "Оля")
    assert row(conn, "SELECT status FROM cases WHERE id = ?", (case_id,))["status"] == "needs_reply"
    assert ai_messages(conn, case_id) == ["Взяли в роботу."]


def test_credit_moved_by_the_slider_is_reported_at_once(conn):
    set_setting(conn, "autonomy", {"auto_credit_limit_usd": 100, "auto_waive_limit_usd": 0,
                                   "auto_min_confidence": 0.7, "low_confidence": 0.6})
    case_id = support.create_case(conn, "D-001", MARTA)
    support.process_case(conn, case_id)
    assert row(conn, "SELECT status FROM actions WHERE case_id = ? AND action = 'issue_credit'", (case_id,))["status"] == "executed"
    assert any("нарахували" in m for m in ai_messages(conn, case_id))


def test_double_click_runs_an_action_once(conn):
    case_id = support.create_case(conn, "D-001", MARTA)
    support.process_case(conn, case_id)
    action_id = pending(conn, case_id, "issue_credit")["id"]
    support.approve(conn, action_id, "Оля")
    assert "error" in support.approve(conn, action_id, "Катерина")
    assert "error" in support.reject(conn, action_id, "Катерина")
    assert len(rows(conn, "SELECT id FROM credits WHERE case_id = ?", (case_id,))) == 1


def test_broken_integration_is_a_product_problem_not_a_partnership():
    route, _ = workspace.match_route(workspace.load_preset("scriber"),
                                     "Інтеграція з нашою МІС злетіла, протоколи не передаються", None)
    assert route is None or route["id"] != "partnership"


def test_time_windows_compare_full_timestamps(conn):
    # Stored timestamps look like 2026-10-01T10:00:00+00:00; a window must not stretch to the whole boundary day.
    old = (datetime.now(timezone.utc) - timedelta(days=30, minutes=5)).replace(microsecond=0).isoformat()
    conn.execute("INSERT INTO credits (account_id, amount_usd, reason, case_id, ts) VALUES ('D-003', 10, 'x', NULL, ?)",
                 (old,))
    assert actions.account_snapshot(conn, "D-003")["recent_credit"] is False
