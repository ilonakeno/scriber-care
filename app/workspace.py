"""Business template: who the business is, who owns what, where each kind of request goes,
and how the assistant must sound. Switching the template adapts the same agent to another business."""
import json
import re
import sqlite3

from . import config
from .db import get_setting, set_setting

PRESETS_DIR = config.ROOT / "presets"
MODES = {"ai": "AI вирішує сам", "ai_with_approval": "AI готує, людина підтверджує",
         "human_only": "Тільки людина (100%)"}


def presets() -> list[dict]:
    items = []
    for path in sorted(PRESETS_DIR.glob("*.json")):
        if path.name.startswith("_"):
            continue  # _template.json is a blank to copy, not a preset
        data = json.loads(path.read_text(encoding="utf-8"))
        items.append({"id": data["preset"], "name": data["business"]["name"]})
    return items


def load_preset(preset_id: str) -> dict:
    path = PRESETS_DIR / f"{preset_id}.json"
    if not path.exists() or not re.fullmatch(r"[a-z0-9-]+", preset_id):
        raise ValueError("невідомий пресет")
    return json.loads(path.read_text(encoding="utf-8"))


def get(conn: sqlite3.Connection) -> dict:
    return get_setting(conn, "workspace", None) or load_preset("scriber")


def save(conn: sqlite3.Connection, workspace: dict) -> dict:
    validate(workspace)
    set_setting(conn, "workspace", workspace)
    return workspace


def validate(ws: dict) -> None:
    team = {m["id"] for m in ws.get("team", [])}
    for r in ws.get("routes", []):
        if r.get("mode") not in MODES:
            raise ValueError(f"маршрут «{r.get('title')}»: невідомий режим")
        if r.get("owner") not in team:
            raise ValueError(f"маршрут «{r.get('title')}»: відповідального нема в команді")


def member(ws: dict, member_id: str | None) -> dict | None:
    return next((m for m in ws.get("team", []) if m["id"] == member_id), None)


def match_route(ws: dict, text: str, model_route: str | None) -> tuple[dict | None, str]:
    """Keyword triggers are checked first, so a human-only rule holds even if the model
    picked another category. Returns (route, why)."""
    low = text.lower()
    for r in ws.get("routes", []):
        hit = next((k for k in r.get("keywords", []) if k and k.lower() in low), None)
        if hit:
            return r, f"слово-тригер «{hit}»"
    for r in ws.get("routes", []):
        if r["id"] == model_route:
            return r, "тип звернення визначив AI"
    return None, ""


def tone_issues(ws: dict, reply: str) -> list[str]:
    low = reply.lower()
    return [phrase for phrase in ws.get("tone", {}).get("forbidden", []) if phrase.lower() in low]


def prompt_block(ws: dict) -> dict:
    """The part of the template the model sees (trusted, written by the business)."""
    return {
        "business": ws["business"],
        "routes": [{"id": r["id"], "title": r["title"], "mode": MODES[r["mode"]]} for r in ws["routes"]],
        "tone": ws.get("tone", {}),
        "instructions": ws.get("instructions", ""),
    }
