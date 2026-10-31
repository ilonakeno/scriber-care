"""Product knowledge base: docs/features/<feature>/ plus additions approved by the team."""
import re
import sqlite3

from . import config

FEATURES = ["transcription", "protocol-generation", "export", "billing"]


def _read(path) -> str:
    return path.read_text(encoding="utf-8") if path.exists() else ""


def feature_meta(feature: str) -> dict:
    text = _read(config.DOCS_DIR / "features" / feature / "FEATURE.md")
    meta = {"feature": feature, "title": feature, "owner": "", "slack": "", "entry_points": []}
    block = re.match(r"---\n(.*?)\n---", text, re.S)
    if not block:
        return meta
    for line in block.group(1).splitlines():
        stripped = line.strip()
        if stripped.startswith("- "):
            meta["entry_points"].append(stripped[2:].strip().strip('"'))
        elif ":" in stripped:
            key, value = stripped.split(":", 1)
            value = value.split("#")[0].strip().strip('"')
            if key in ("title", "owner", "slack") and value:
                meta[key] = value
    return meta


def knowledge_text(conn: sqlite3.Connection) -> str:
    parts = [_read(config.DOCS_DIR / "support-playbook.md")]
    for feature in FEATURES:
        meta = feature_meta(feature)
        parts.append(f"\n## Фіча `{feature}` ({meta['title']}), власник: {meta['owner']}\n")
        parts.append(_read(config.DOCS_DIR / "features" / feature / "known-gotchas.md"))
        extra = conn.execute("SELECT title, body FROM kb_updates WHERE feature = ? AND status = 'approved'",
                             (feature,)).fetchall()
        for r in extra:
            parts.append(f"\n### Доповнено командою · {r['title']}\n{r['body']}\n")
    return "\n".join(p for p in parts if p)


def gotcha_ids() -> set[str]:
    ids = set()
    for feature in FEATURES:
        ids.update(re.findall(r"###\s+([A-Z]{2}-G\d+)", _read(config.DOCS_DIR / "features" / feature / "known-gotchas.md")))
    return ids
