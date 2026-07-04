# engine/overlay.py
"""Resolve action names across commons + proprietaries.

proprietaries first (override), commons second. Listing uses static ast scanning
(no import side effects). Dispatch loads exactly one module by path.
"""
from __future__ import annotations

import ast
import importlib.util
import os
from dataclasses import dataclass
from typing import List, Optional

ACTION_EXT = ".py"


@dataclass
class Resolved:
    name: str
    path: str
    shadowed: Optional[str]   # commons path when a proprietaries action overrides it


@dataclass
class ActionInfo:
    name: str
    source: str               # "commons" | "local" | "override"
    summary: str
    disabled: object          # None | True | "reason"


def _rel_names(root: Optional[str]):
    if not root or not os.path.isdir(root):
        return {}
    out = {}
    for dirpath, _dirs, files in os.walk(root):
        for fn in files:
            if not fn.endswith(ACTION_EXT):
                continue
            full = os.path.join(dirpath, fn)
            rel = os.path.relpath(full, root)[: -len(ACTION_EXT)]
            out[rel.replace(os.sep, "/")] = full
    return out


def resolve(name: str, commons_root: str, prop_root: Optional[str]) -> Optional[Resolved]:
    rel = name + ACTION_EXT
    commons_path = os.path.join(commons_root, rel)
    c = os.path.isfile(commons_path)
    prop_path = os.path.join(prop_root, rel) if prop_root else None
    p = bool(prop_path) and os.path.isfile(prop_path)
    if p and c:
        return Resolved(name, prop_path, commons_path)
    if p:
        return Resolved(name, prop_path, None)
    if c:
        return Resolved(name, commons_path, None)
    return None


def discover(commons_root: str, prop_root: Optional[str]) -> List[ActionInfo]:
    commons = _rel_names(commons_root)
    prop = _rel_names(prop_root)
    infos = []
    for name in sorted(set(commons) | set(prop)):
        if name in prop and name in commons:
            source, path = "override", prop[name]
        elif name in prop:
            source, path = "local", prop[name]
        else:
            source, path = "commons", commons[name]
        summary, disabled = _scan(path)
        infos.append(ActionInfo(name, source, summary, disabled))
    return infos


def _scan(path: str):
    """Statically extract (summary, DISABLED) without importing the module."""
    try:
        with open(path) as f:
            tree = ast.parse(f.read(), path)
    except (SyntaxError, OSError):
        return "(unparseable)", None
    summary, disabled = "", None
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        targets = [t.id for t in node.targets if isinstance(t, ast.Name)]
        if "METADATA" in targets and isinstance(node.value, ast.Call):
            summary = _summary_of(node.value)
        if "DISABLED" in targets and isinstance(node.value, ast.Constant):
            disabled = node.value.value if node.value.value else None
    return summary, disabled


def _summary_of(call: ast.Call) -> str:
    for kw in call.keywords:
        if kw.arg == "summary" and isinstance(kw.value, ast.Constant):
            return kw.value.value
    if call.args and isinstance(call.args[0], ast.Constant):
        return call.args[0].value
    return ""


def load_module(path: str, modname: str):
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod
