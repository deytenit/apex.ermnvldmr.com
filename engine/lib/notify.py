# engine/lib/notify.py
"""ctx.notify — Telegram notifications.

The alert `instance` is the node's public host (APEX_NODE_HOST), passed in from the
context. POSTs application/x-www-form-urlencoded via urllib (stdlib).
"""
from __future__ import annotations
import html
import urllib.parse
import urllib.request
from datetime import datetime


def build_text(title: str, message: str, node: str, level: str, ts: str) -> str:
    status, icon, severity = "firing", "🚨", "info"
    if level == "SUCCESS":
        status, icon = "resolved", "✅"
    elif level == "ERROR":
        severity = "critical"
    elif level == "WARN":
        severity = "warning"
    instance = node
    msg = message if len(message) <= 200 else message[:200] + "..."
    msg = html.escape(msg)
    safe_title = html.escape(title)
    return (f"{icon} <b>{safe_title}</b>\n"
            f"<b>Instance:</b> {instance}\n"
            f"<b>Timestamp:</b> {ts}\n"
            f"<b>Status:</b> {status.capitalize()}\n"
            f"<b>Escalation:</b> {severity}\n"
            f"<pre><code>{msg}</code></pre>\n"
            f"#status_{status} #escalation_{severity} #instance_{instance.replace('.', '_')}")


class Notify:
    def __init__(self, log, node: str = "global"):
        self.log = log
        self.node = node

    def telegram(self, title, bot_url, message, node=None, level="INFO") -> bool:
        node = node or self.node
        if not bot_url:
            self.log.warn("Telegram Bot URL not provided. Skipping notification.")
            return False
        ts = datetime.now().astimezone().strftime("%Y-%m-%d %H:%M:%S %Z")
        text = build_text(title, message, node, level, ts)
        data = urllib.parse.urlencode({"parse_mode": "HTML", "text": text}).encode()
        self.log.info(f"Sending Telegram notification ({level}): {title}")
        try:
            with urllib.request.urlopen(urllib.request.Request(bot_url, data=data), timeout=120) as r:
                r.read()
            return True
        except Exception as e:
            self.log.error(f"Failed to send Telegram notification: {e}")
            return False

    def success(self, title, url, msg, node=None): return self.telegram(title, url, msg, node, "SUCCESS")
    def error(self, title, url, msg, node=None): return self.telegram(title, url, msg, node, "ERROR")
    def info(self, title, url, msg, node=None): return self.telegram(title, url, msg, node, "INFO")
    def warn(self, title, url, msg, node=None): return self.telegram(title, url, msg, node, "WARN")
