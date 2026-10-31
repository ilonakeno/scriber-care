"""Policy engine: the model only proposes actions, this module decides what may run.

Rules mirror the solution spec: an allowlist of actions, priority levels P0-P3, hard caps,
and an autonomy slider the head of support controls. Risk detection here is deterministic,
so the model can raise a case to P0 but can never lower it.
"""
import re
from dataclasses import dataclass, field

LEVELS = ["P3", "P2", "P1", "P0"]
MAX_SEATS_PER_CHANGE = 20


@dataclass(frozen=True)
class ActionSpec:
    level: str
    title: str
    approver: str | None = None  # who confirms when approval is needed: "agent" or "dev"
    tunable: bool = False  # may the autonomy slider make it automatic


CATALOG: dict[str, ActionSpec] = {
    "acknowledge": ActionSpec("P3", "Взяли в роботу"),
    "read_feature_docs": ActionSpec("P3", "Прочитати базу знань фічі"),
    "explain_how_it_works": ActionSpec("P3", "Пояснити, як це працює"),
    "lookup_account": ActionSpec("P3", "Переглянути акаунт"),
    "get_term_edits": ActionSpec("P3", "Переглянути правки термінів"),
    "add_term_personal_dictionary": ActionSpec("P3", "Додати термін в особистий словник"),
    "regenerate_protocol": ActionSpec("P3", "Перегенерувати протокол"),
    "send_help_article": ActionSpec("P3", "Надіслати інструкцію"),
    "resend_export": ActionSpec("P3", "Повторно надіслати експорт"),
    "restore_recording": ActionSpec("P3", "Відновити обірваний запис"),
    "propose_kb_update": ActionSpec("P2", "Доповнити базу знань", approver="agent"),
    "issue_credit": ActionSpec("P2", "Нарахувати кредит", approver="agent", tunable=True),
    "waive_overage": ActionSpec("P2", "Списати оплату понад ліміт", approver="agent", tunable=True),
    "add_seats": ActionSpec("P2", "Додати місця клініці", approver="agent"),
    "cancel_subscription": ActionSpec("P2", "Скасувати підписку", approver="agent"),
    "post_review_reply": ActionSpec("P2", "Опублікувати відповідь на відгук", approver="agent"),
    "notify_affected": ActionSpec("P2", "Повідомити всіх постраждалих", approver="agent"),
    "create_bug_ticket": ActionSpec("P1", "Створити баг-тікет"),
    "add_term_global_dictionary": ActionSpec("P1", "Додати термін у загальний словник", approver="dev"),
    "escalate_to_human": ActionSpec("P0", "Передати людині"),
}

ALWAYS_ALLOWED_IN_P0 = {"acknowledge", "escalate_to_human", "lookup_account", "read_feature_docs"}

RISK_PATTERNS: dict[str, list[str]] = {
    "legal_threat": [
        r"\bсуд\w*", r"юрист", r"адвокат", r"позов", r"регулятор", r"чарджбек", r"chargeback",
        r"оскарж\w*\s+(платіж|списання|оплату)", r"через\s+банк", r"\bв\s+банк\b", r"поліці",
    ],
    "patient_data": [
        r"(видал|стер|знищ|передай|надішл|покаж|вивантаж)\w*.{0,60}(дан\w*|запис\w*|протокол\w*).{0,40}пацієнт",
        r"пацієнт\w*.{0,60}(видал|стер|знищ)\w*",
        r"(персональн|медичн)\w*\s+дан\w*\s+пацієнт",
    ],
    "clinical_risk": [
        r"(вже|уже)\s+(віддал|видал|роздрукува|надісла|переда)\w*.{0,40}пацієнт",
        r"пацієнт\w*.{0,40}(вже|уже)\s+(отрима|забра|має)\w*.{0,40}(помилк|неправильн)",
        r"помилк\w*.{0,30}(діагноз|висновк)",
    ],
}


@dataclass
class Proposed:
    action: str
    params: dict = field(default_factory=dict)
    why: str = ""


@dataclass
class Context:
    confidence: float
    ai_priority: str
    risk_flags: list[str]
    account_monthly_usd: float
    overage_usd: float
    recent_credit: bool


@dataclass
class Settings:
    auto_credit_limit_usd: float = 0.0  # 0 = slider off, every credit needs approval
    auto_waive_limit_usd: float = 0.0
    auto_min_confidence: float = 0.9
    low_confidence: float = 0.6


@dataclass
class Decision:
    action: str
    params: dict
    why: str
    outcome: str  # auto | approval | blocked | rejected
    rule: str
    level: str
    approver: str | None


def detect_risk_flags(text: str) -> list[str]:
    lowered = text.lower()
    return [flag for flag, patterns in RISK_PATTERNS.items()
            if any(re.search(p, lowered) for p in patterns)]


def _validate(action: str, params: dict, ctx: Context) -> str | None:
    """Returns a rejection reason, or None when the parameters are within hard caps."""
    if action == "issue_credit":
        amount = _number(params.get("amount_usd"))
        if amount is None or amount <= 0:
            return "сума кредиту має бути додатною"
        if amount > ctx.account_monthly_usd:
            return f"кредит ${amount:g} більший за місячну оплату акаунта ${ctx.account_monthly_usd:g}"
    if action == "waive_overage":
        amount = _number(params.get("amount_usd"))
        if amount is None or amount <= 0 or amount > ctx.overage_usd:
            return f"можна списати не більше фактичної переплати ${ctx.overage_usd:g}"
    if action == "add_seats":
        count = _number(params.get("count"))
        if count is None or not 1 <= count <= MAX_SEATS_PER_CHANGE:
            return f"кількість місць має бути від 1 до {MAX_SEATS_PER_CHANGE}"
    return None


def _number(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _tunable_limit(action: str, settings: Settings) -> float:
    return settings.auto_credit_limit_usd if action == "issue_credit" else settings.auto_waive_limit_usd


def evaluate(plan: list[Proposed], ctx: Context, settings: Settings) -> tuple[str, list[Decision]]:
    flags = list(ctx.risk_flags)
    if ctx.confidence < settings.low_confidence:
        flags.append("low_confidence")
    p0 = bool(flags) or ctx.ai_priority == "P0"

    steps = list(plan)
    if p0 and not any(p.action == "escalate_to_human" for p in steps):
        steps.append(Proposed("escalate_to_human", {"reason": ", ".join(flags) or "AI позначив P0"},
                              "P0: рішення ухвалює людина"))

    decisions: list[Decision] = []
    for p in steps:
        spec = CATALOG.get(p.action)
        if spec is None:
            decisions.append(Decision(p.action, p.params, p.why, "rejected",
                                      "дії немає в дозволеному списку", "P0", None))
            continue
        reason = _validate(p.action, p.params, ctx)
        if reason:
            decisions.append(Decision(p.action, p.params, p.why, "rejected", reason, spec.level, None))
            continue
        if p0 and p.action not in ALWAYS_ALLOWED_IN_P0:
            decisions.append(Decision(p.action, p.params, p.why, "blocked",
                                      "P0: автоматичні дії заблоковано, вирішує людина", spec.level, None))
            continue
        decisions.append(_decide(p, spec, ctx, settings))

    priority = "P0" if p0 else _max_level(
        [ctx.ai_priority if ctx.ai_priority in LEVELS else "P3"]
        + [d.level for d in decisions if d.outcome != "rejected" and not
           (d.level == "P2" and d.outcome == "auto")]
    )
    return priority, decisions


def _decide(p: Proposed, spec: ActionSpec, ctx: Context, settings: Settings) -> Decision:
    def make(outcome: str, rule: str) -> Decision:
        return Decision(p.action, p.params, p.why, outcome, rule, spec.level,
                        spec.approver if outcome == "approval" else None)

    if spec.level == "P3":
        return make("auto", "P3: безпечна дія, яку можна відкотити")
    if spec.level == "P0":
        return make("auto", "P0: передати людині можна завжди")
    if spec.level == "P1":
        if spec.approver:
            return make("approval", "P1: зміна для всіх клієнтів, підтверджує розробник")
        return make("auto", "P1: баг-тікет створюється автоматично")
    if spec.tunable:
        if p.action == "issue_credit" and ctx.recent_credit:
            return make("approval", "кредит уже нараховували за останні 30 днів")
        limit = _tunable_limit(p.action, settings)
        amount = _number(p.params.get("amount_usd")) or 0
        if limit > 0 and amount <= limit and ctx.confidence >= settings.auto_min_confidence:
            return make("auto", f"повзунок автономності: до ${limit:g} при впевненості ≥ "
                                f"{settings.auto_min_confidence:.0%}")
        return make("approval", "гроші: потрібне підтвердження агента")
    return make("approval", "P2: потрібне підтвердження агента")


def _max_level(levels: list[str]) -> str:
    return max(levels, key=LEVELS.index) if levels else "P3"
