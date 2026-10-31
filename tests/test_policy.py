"""Policy rules, written from the solution doc (sections 6 and 7) before the implementation."""
from app.policy import Context, Proposed, Settings, detect_risk_flags, evaluate


def ctx(**kw):
    base = dict(confidence=0.92, ai_priority="P2", risk_flags=[], account_monthly_usd=29.0,
                overage_usd=0.0, recent_credit=False)
    base.update(kw)
    return Context(**base)


def outcome(decisions, action):
    return next(d.outcome for d in decisions if d.action == action)


def test_safe_actions_run_automatically():
    plan = [Proposed("add_term_personal_dictionary", {"term": "гіпоехогенне"}),
            Proposed("regenerate_protocol", {"protocol": "last"})]
    priority, decisions = evaluate(plan, ctx(ai_priority="P3"), Settings())
    assert priority == "P3"
    assert all(d.outcome == "auto" for d in decisions)


def test_credit_needs_approval_by_default():
    priority, decisions = evaluate([Proposed("issue_credit", {"amount_usd": 29})], ctx(), Settings())
    assert priority == "P2"
    assert outcome(decisions, "issue_credit") == "approval"


def test_credit_above_what_the_account_pays_is_rejected():
    _, decisions = evaluate([Proposed("issue_credit", {"amount_usd": 1000})], ctx(), Settings())
    assert outcome(decisions, "issue_credit") == "rejected"


def test_action_outside_allowlist_is_rejected():
    _, decisions = evaluate([Proposed("delete_patient_data", {"patient": "X"}),
                             Proposed("transfer_money", {"amount_usd": 5})], ctx(), Settings())
    assert {d.outcome for d in decisions if d.action in ("delete_patient_data", "transfer_money")} == {"rejected"}


def test_legal_threat_forces_p0_and_blocks_everything_but_escalation():
    flags = detect_risk_flags("Якщо не повернете гроші, піду до юриста і оскаржу платіж через банк")
    assert flags
    plan = [Proposed("issue_credit", {"amount_usd": 29}), Proposed("cancel_subscription", {})]
    priority, decisions = evaluate(plan, ctx(risk_flags=flags, ai_priority="P3"), Settings())
    assert priority == "P0"
    assert outcome(decisions, "issue_credit") == "blocked"
    assert outcome(decisions, "cancel_subscription") == "blocked"
    assert outcome(decisions, "escalate_to_human") == "auto"


def test_patient_data_request_is_p0():
    assert "patient_data" in detect_risk_flags("Видаліть, будь ласка, всі записи моєї пацієнтки")


def test_clinical_risk_is_p0():
    assert "clinical_risk" in detect_risk_flags("Протокол з помилкою вже роздрукували пацієнтці, що робити?")


def test_ordinary_complaint_has_no_risk_flags():
    assert detect_risk_flags("Третій раз AI пише не той термін, я так не можу!!!") == []


def test_low_confidence_goes_to_human():
    priority, decisions = evaluate([Proposed("send_help_article", {})], ctx(confidence=0.4), Settings())
    assert priority == "P0"
    assert outcome(decisions, "send_help_article") == "blocked"


def test_model_cannot_downgrade_p0():
    priority, _ = evaluate([], ctx(ai_priority="P3", risk_flags=["clinical_risk"]), Settings())
    assert priority == "P0"


def test_autonomy_slider_allows_small_credit_with_high_confidence():
    settings = Settings(auto_credit_limit_usd=35, auto_min_confidence=0.9)
    _, decisions = evaluate([Proposed("issue_credit", {"amount_usd": 29})], ctx(confidence=0.92), settings)
    assert outcome(decisions, "issue_credit") == "auto"


def test_autonomy_slider_respects_confidence():
    settings = Settings(auto_credit_limit_usd=35, auto_min_confidence=0.9)
    _, decisions = evaluate([Proposed("issue_credit", {"amount_usd": 29})], ctx(confidence=0.85), settings)
    assert outcome(decisions, "issue_credit") == "approval"


def test_second_credit_in_30_days_needs_a_human():
    settings = Settings(auto_credit_limit_usd=35)
    _, decisions = evaluate([Proposed("issue_credit", {"amount_usd": 29})], ctx(recent_credit=True), settings)
    assert outcome(decisions, "issue_credit") == "approval"


def test_cancellation_and_public_replies_are_never_automatic():
    settings = Settings(auto_credit_limit_usd=1000, auto_waive_limit_usd=1000, auto_min_confidence=0.0)
    plan = [Proposed("cancel_subscription", {}), Proposed("post_review_reply", {"text": "Дякуємо"}),
            Proposed("add_seats", {"count": 2})]
    _, decisions = evaluate(plan, ctx(confidence=0.99), settings)
    assert {d.outcome for d in decisions} == {"approval"}


def test_waive_overage_cannot_exceed_actual_overage():
    _, decisions = evaluate([Proposed("waive_overage", {"amount_usd": 50})], ctx(overage_usd=12.5), Settings())
    assert outcome(decisions, "waive_overage") == "rejected"


def test_seat_count_is_bounded():
    _, decisions = evaluate([Proposed("add_seats", {"count": 0}), Proposed("add_seats", {"count": 50})],
                            ctx(), Settings())
    assert {d.outcome for d in decisions} == {"rejected"}


def test_bug_ticket_is_automatic_global_dictionary_needs_a_developer():
    plan = [Proposed("create_bug_ticket", {"title": "x"}),
            Proposed("add_term_global_dictionary", {"term": "гіпоехогенне"})]
    priority, decisions = evaluate(plan, ctx(), Settings())
    assert priority == "P1"
    assert outcome(decisions, "create_bug_ticket") == "auto"
    dev = next(d for d in decisions if d.action == "add_term_global_dictionary")
    assert dev.outcome == "approval" and dev.approver == "dev"
