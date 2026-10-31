"""Masks personal and medical identifiers before any text reaches the model,
and checks outgoing public text (review replies, Slack) for leaks."""
import re
from dataclasses import dataclass, field

NAME = r"[А-ЯІЇЄҐ][а-яіїєґ'’]+(?:\s+[А-ЯІЇЄҐ][а-яіїєґ'’]+){0,2}"

PATTERNS: list[tuple[str, str]] = [
    ("EMAIL", r"[\w.+-]+@[\w-]+\.[\w.-]+"),
    ("КАРТКА", r"\b(?:\d[ -]?){15,18}\d\b"),
    ("ТЕЛЕФОН", r"(?:\+?38\s?0|\b0)\s?\(?\d{2}\)?[\s-]?\d{3}[\s-]?\d{2}[\s-]?\d{2}\b"),
    ("НОМЕР", r"(?:№|#)\s?\d{3,}|\b\d{10}\b"),
    ("ДАТА", r"\b\d{1,2}[./]\d{1,2}[./](?:19|20)\d{2}\b"),
]
PATIENT = re.compile(rf"(пацієнт\w*|patient)\s+({NAME})", re.IGNORECASE)
# Any other full name (two or three capitalised words): patients are often named without the word "пацієнт".
PERSON = re.compile(r"\b[А-ЯІЇЄҐ][а-яіїєґ'’]+(?:\s+[А-ЯІЇЄҐ][а-яіїєґ'’]+){1,2}\b")
OLD_YEAR_CUTOFF = 2015  # full dates before this are most likely dates of birth


@dataclass
class MaskResult:
    text: str
    mapping: dict[str, str] = field(default_factory=dict)


def mask(text: str) -> MaskResult:
    mapping: dict[str, str] = {}
    counters: dict[str, int] = {}

    def placeholder(kind: str, original: str) -> str:
        for key, value in mapping.items():
            if value == original and key.startswith(f"[{kind}_"):
                return key
        counters[kind] = counters.get(kind, 0) + 1
        key = f"[{kind}_{counters[kind]}]"
        mapping[key] = original
        return key

    def patient(m: re.Match) -> str:
        return f"{m.group(1)} {placeholder('ПАЦІЄНТ', m.group(2))}"

    out = PATIENT.sub(patient, text)
    out = PERSON.sub(lambda m: placeholder("ОСОБА", m.group(0)), out)
    for kind, pattern in PATTERNS:
        def repl(m: re.Match, kind=kind) -> str:
            if kind == "ДАТА" and int(re.split(r"[./]", m.group(0))[-1]) >= OLD_YEAR_CUTOFF:
                return m.group(0)  # recent dates (billing, visits) keep their meaning
            return placeholder(kind, m.group(0))
        out = re.sub(pattern, repl, out)
    return MaskResult(out, mapping)


def find_leaks(text: str) -> list[str]:
    """Everything that must never appear in a public reply or a Slack message."""
    leaks = []
    if re.search(r"\[(ПАЦІЄНТ|ОСОБА|ТЕЛЕФОН|EMAIL|НОМЕР|ДАТА|КАРТКА)_\d+\]", text):
        leaks.append("службова мітка маскування")
    if PATIENT.search(text):
        leaks.append("ім'я пацієнта")
    masked = mask(text)
    leaks.extend(f"{key.strip('[]').rsplit('_', 1)[0].lower()}" for key in masked.mapping
                 if not key.startswith(("[ПАЦІЄНТ", "[ОСОБА")))
    if re.search(r"\b[DC]-\d{3}\b", text):
        leaks.append("внутрішній ідентифікатор акаунта")
    return sorted(set(leaks))
