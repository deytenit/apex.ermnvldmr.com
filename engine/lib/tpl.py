# engine/lib/tpl.py
"""ctx.tpl — stdlib string.Template rendering (envsubst successor, SP2 decision #3).

safe_substitute matches envsubst's forgiving behavior: known $VAR/${VAR} replaced,
unknown left literal, and shell $(...) command-substitution survives untouched.
Loops/conditionals live in the python action, never in the template file.
"""
from __future__ import annotations
import os
from string import Template
from typing import Dict


class Tpl:
    def __init__(self, configs_dir: str, log):
        self.configs_dir = configs_dir
        self.log = log

    def _resolve(self, path: str) -> str:
        return path if os.path.isabs(path) else os.path.join(self.configs_dir, path)

    def render(self, path: str, vars: Dict[str, str]) -> str:
        full = self._resolve(path)
        with open(full) as f:
            return Template(f.read()).safe_substitute(vars)

    def render_dir(self, rel_dir: str, vars: Dict[str, str]) -> Dict[str, str]:
        """Render every file under configs/<rel_dir>. Returns {relpath: content}."""
        base = self._resolve(rel_dir)
        out: Dict[str, str] = {}
        for dirpath, _dirs, files in os.walk(base):
            for fn in sorted(files):
                full = os.path.join(dirpath, fn)
                rel = os.path.relpath(full, base).replace(os.sep, "/")
                with open(full) as f:
                    out[rel] = Template(f.read()).safe_substitute(vars)
        return out
