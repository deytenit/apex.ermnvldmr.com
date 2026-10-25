# actions/compose.py
"""Orchestrate docker compose across the node.

Supports targeting all compositions in sequence (default: apex core first, services next;
reverse on down) or targeting a specific composition (e.g. `apex compose restart apex`
or `apex compose up -d sso`). Exports derived APEX_* vars and layers --env-file (.env secrets + apex.env).
"""
import os
from engine.descriptor import Arg, Flag, Meta, Opt, Rest

METADATA = Meta(
    summary="Orchestrate docker compose across the node or on a specific composition.",
    args=[
        Opt("-p", "Target composition name or 'all'.", default=None),
        Flag("--dry-run", "Print commands only."),
        Arg("action", "up | down | restart | ps | logs | ..."),
        Rest("extra", "Extra args (target composition name, service names, or docker compose flags)."),
    ],
)


def _clean_xray_sockets(ctx):
    """Unlink stale host-side Xray sockets in /dev/shm to prevent REALITY panic crash loops."""
    shm = os.path.join(ctx.paths.stier3, "apex", "xray", "shm")
    if os.path.isdir(shm):
        for f in os.listdir(shm):
            if f.endswith(".sock"):
                try:
                    os.unlink(os.path.join(shm, f))
                    ctx.log.info(f"Cleaned stale Xray socket: {f}")
                except OSError as e:
                    ctx.log.warn(f"Failed to unlink {f}: {e}")


def run(ctx, args):
    log, comp, action = ctx.log, ctx.paths.compositions, args.action
    extra = [a for a in (args.extra or []) if a != "--dry-run"]
    dry_run = args.dry_run or len(extra) != len(args.extra or [])
    env = dict(os.environ)
    env.update({k: v for k, v in ctx.vars().items() if k.startswith("APEX_")})

    available = []
    if os.path.isdir(comp):
        available = [
            d for d in os.listdir(comp)
            if os.path.isdir(os.path.join(comp, d))
            and not d.startswith((".", "@"))
            and os.path.isfile(os.path.join(comp, d, "docker-compose.yml"))
        ]

    target = getattr(args, "p", None)
    if not target and extra:
        first = extra[0]
        if first == "all":
            target = "all"
            extra = extra[1:]
        elif first in available:
            target = first
            extra = extra[1:]
        elif not first.startswith("-"):
            target = first
            extra = extra[1:]
        elif extra[-1] in available and len(extra) > 1:
            target = extra[-1]
            extra = extra[:-1]

    if not target:
        target = "all"

    if target != "all" and target not in available:
        log.error(f"Unknown composition '{target}'. Available: {', '.join(sorted(available)) if available else 'none'}")
        raise SystemExit(1)

    if action in ("up", "restart", "start") and target in ("all", "apex") and not dry_run:
        _clean_xray_sockets(ctx)

    def sequence():
        if target != "all":
            return [os.path.join(comp, target)]
        core = os.path.join(comp, "apex")
        others = sorted(os.path.join(comp, d) for d in available if d != "apex")
        seq = ([core] if "apex" in available else []) + others
        return list(reversed(seq)) if action == "down" else seq

    def run_one(dirpath):
        name = os.path.basename(dirpath)
        base = ["docker", "compose"]
        for ef in (".env", "apex.env"):
            if os.path.isfile(os.path.join(dirpath, ef)):
                base += ["--env-file", ef]
        if action == "up":
            up_flags = [] if "-d" in extra else ["-d"]
            cmd = base + ["up"] + up_flags + extra
        else:
            cmd = base + [action] + extra

        if dry_run:
            log.info(f"[DRY-RUN] (cd {dirpath} && {' '.join(cmd)})")
        else:
            log.info(f"{action} {name}...")
            ctx.sys.run(cmd, cwd=dirpath, env=env, check=True)

    for d in sequence():
        run_one(d)
    log.success(f"Orchestrated {action} ({target}).")
