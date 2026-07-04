# engine/cli.py
"""apex CLI: list / dispatch / help. Acts on the local checkout (no <node> arg)."""
from __future__ import annotations
import os
import socket
import sys

from engine import overlay
from engine.identity import resolve
from engine.context import Context, Paths
from engine.descriptor import build_parser, Meta


def _resolve_layout():
    commons_dir = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))
    parent = os.path.dirname(commons_dir)
    is_node = (os.path.basename(commons_dir) == "commons"
               and os.path.isfile(os.path.join(parent, "node.env")))
    if is_node:
        repo_root = parent
        prop_actions = os.path.join(repo_root, "proprietaries", "actions")
    else:
        repo_root = commons_dir          # standalone commons repo is its own root
        prop_actions = None
    return commons_dir, repo_root, prop_actions, is_node


def _print_list(commons_actions, prop_actions):
    print("Usage: apex <group>/<action> [args...]\n\nAvailable actions:")
    tag = {"commons": "", "local": "  (local)", "override": "  (local override)"}
    for i in overlay.discover(commons_actions, prop_actions):
        mark = f"  [DISABLED: {i.disabled}]" if i.disabled else ""
        print(f"  {i.name:<34} {i.summary}{tag[i.source]}{mark}")


def main(argv) -> int:
    commons_dir, repo_root, prop_actions, is_node = _resolve_layout()
    commons_actions = os.path.join(commons_dir, "actions")

    if not argv or argv[0] in ("--help", "-h"):
        _print_list(commons_actions, prop_actions)
        return 0

    name, rest = argv[0], argv[1:]
    r = overlay.resolve(name, commons_actions, prop_actions)
    if r is None:
        sys.stderr.write(f"[global] [core] [ERROR] Unknown action: {name}\n")
        return 1

    node, warns = (resolve(socket.getfqdn(), repo_root) if is_node else (None, []))
    ctx = Context(node, Paths(repo_root, commons_dir), name, shadowed=r.shadowed)
    for w in warns:
        ctx.log.warn(w)

    modname = "apex_action_" + name.replace("/", "_").replace("-", "_")
    mod = overlay.load_module(r.path, modname)

    disabled = getattr(mod, "DISABLED", None)
    if disabled:
        reason = disabled if isinstance(disabled, str) else "disabled"
        ctx.log.error(f"Action {name} is disabled: {reason}")
        return 1

    meta = getattr(mod, "METADATA", Meta(summary=""))
    args = build_parser(meta, f"apex {name}").parse_args(rest)  # --help & EX_USAGE handled here

    try:
        rc = mod.run(ctx, args)
        return int(rc) if isinstance(rc, int) else 0
    except SystemExit as e:
        return e.code if isinstance(e.code, int) else 1
    except Exception as e:  # map to general error, log with context prefix
        ctx.log.error(f"{type(e).__name__}: {e}")
        return 1
