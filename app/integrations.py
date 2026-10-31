"""Outbound channels: Slack (team notifications) and GitHub Issues (bug tickets).
Every Slack message also lands in the in-app feed, and passes the leak filter first."""
import sqlite3
import subprocess

import httpx

from . import audit, config
from .db import now_iso
from .masking import find_leaks


def notify(conn: sqlite3.Connection, channel: str, text: str, link: str | None = None) -> bool:
    leaks = find_leaks(text)
    if leaks:
        audit.log(conn, "notification_blocked", {"channel": channel, "leaks": leaks})
        text = f"⚠️ Повідомлення заблоковано фільтром персональних даних ({', '.join(leaks)})"
    webhook = config.SLACK_WEBHOOK_SUPPORT if channel == "support" else config.SLACK_WEBHOOK_DEV
    delivered = False
    if webhook:
        body = text + (f"\n<{link}|Відкрити в Scriber Care>" if link else "")
        try:
            delivered = httpx.post(webhook, json={"text": body}, timeout=6).status_code == 200
        except httpx.HTTPError:
            delivered = False
    conn.execute("INSERT INTO notifications (channel, text, link, ts, delivered) VALUES (?, ?, ?, ?, ?)",
                 (channel, text, link, now_iso(), int(delivered)))
    conn.commit()
    return delivered


def create_issue(title: str, body: str) -> dict:
    if not config.GITHUB_REPO:
        return {"url": None, "draft": True}
    try:
        done = subprocess.run(["gh", "issue", "create", "--repo", config.GITHUB_REPO, "--title", title,
                               "--body", body], capture_output=True, text=True, timeout=30)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"url": None, "draft": True, "error": str(exc)}
    if done.returncode != 0:
        return {"url": None, "draft": True, "error": done.stderr.strip()[:200]}
    return {"url": done.stdout.strip().splitlines()[-1], "draft": False}
