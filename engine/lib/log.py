# engine/lib/log.py
"""ctx.log — [node] [action] [level] message (port of root_log_*)."""
from __future__ import annotations
import sys


class Log:
    def __init__(self, node: str = "global", action: str = "core"):
        self.node = node
        self.action = action

    def _emit(self, level: str, msg: str, stream=None) -> None:
        print(f"[{self.node}] [{self.action}] [{level}] {msg}",
              file=stream or sys.stdout, flush=True)

    def info(self, msg): self._emit("INFO", msg)
    def warn(self, msg): self._emit("WARN", msg)
    def error(self, msg): self._emit("ERROR", msg, sys.stderr)
    def success(self, msg): self._emit("SUCCESS", msg)
