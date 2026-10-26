# actions/compose.py
"""Orchestrate docker compose across the node.

Supports targeting all compositions in sequence (default: apex core first, services next;
reverse on down) or targeting one with `apex compose -p apex restart traefik`.
Wrapper options precede the action. Exports derived APEX_* vars and layers env files.
"""
import os
import shlex

from engine.descriptor import Flag, Meta, Opt, Rest

METADATA = Meta(
    summary="Orchestrate docker compose across the node or on a specific composition.",
    args=[
        Opt("-p", "Target composition name or 'all'.", default=None),
        Flag("--dry-run", "Print commands only."),
        Rest("command", "ACTION [COMPOSE_ARGS...]; wrapper options must precede ACTION."),
    ],
)


def run(ctx, args):
    log, comp = ctx.log, ctx.paths.compositions
    command = list(args.command)
    if command and command[0] == "--":
        command.pop(0)
    if not command:
        log.error("ACTION is required (up, down, restart, logs, ...).")
        raise SystemExit(64)
    action, extra = command[0], command[1:]
    dry_run = args.dry_run
    before_separator = extra[:extra.index("--")] if "--" in extra else extra
    if any(arg in ("-p", "--dry-run") or arg.startswith("-p=")
           or arg.startswith("--dry-run=") for arg in before_separator):
        log.error("Wrapper options -p and --dry-run must precede ACTION; use -- for passthrough.")
        raise SystemExit(64)
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
    if target is None and extra:
        if extra[0] in ["all", *available]:
            target, extra = extra[0], extra[1:]
        elif action == "up" and len(extra) >= 2 and extra[0] == "-d":
            # Preserve the documented `up -d apex` spelling without scanning flag values.
            if extra[1] in ["all", *available]:
                if len(extra) != 2:
                    log.error("Use -p COMPOSITION before up when adding service arguments.")
                    raise SystemExit(64)
                target, extra = extra[1], ["-d"]

    if target is None:
        target = "all"

    if target != "all" and target not in available:
        names = ", ".join(sorted(available)) if available else "none"
        log.error(f"Unknown composition '{target}'. Available: {names}")
        raise SystemExit(1)

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
            up_flags = [] if any(flag in extra for flag in ("-d", "--detach")) else ["-d"]
            cmd = base + ["up"] + up_flags + extra
        else:
            cmd = base + [action] + extra

        if dry_run:
            log.info(f"[DRY-RUN] (cd {shlex.quote(dirpath)} && {shlex.join(cmd)})")
        else:
            log.info(f"{action} {name}...")
            ctx.sys.run(cmd, cwd=dirpath, env=env, check=True)

    for d in sequence():
        run_one(d)
    log.success(f"Orchestrated {action} ({target}).")
