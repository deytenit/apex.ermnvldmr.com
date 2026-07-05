# actions/compose.py
"""Port of compose — orchestrate docker compose across the node. apex core first
(owns networks), then services alpha; reverse on down. Exports derived APEX_* vars and
layers --env-file (.env secrets + apex.env committed scalars)."""
import os
from engine.descriptor import Meta, Arg, Flag, Rest

METADATA = Meta(summary="Orchestrate docker compose across the node (apex core first, services next).",
                args=[Arg("action", "up | down | restart | ..."),
                      Flag("--dry-run", "Print commands only."),
                      Rest("extra", "Extra args passed through to docker compose.")])

def run(ctx, args):
    log, comp, action = ctx.log, ctx.paths.compositions, args.action
    # argparse.REMAINDER swallows flags placed after the action, so strip --dry-run
    # out of the passthrough args like bash did ("apex compose up --dry-run" must
    # preview, not hand docker compose its own --dry-run).
    extra = [a for a in (args.extra or []) if a != "--dry-run"]
    dry_run = args.dry_run or len(extra) != len(args.extra or [])
    env = dict(os.environ)
    env.update({k: v for k, v in ctx.vars().items() if k.startswith("APEX_")})

    def sequence():
        core = os.path.join(comp, "apex")
        others = sorted(
            os.path.join(comp, d) for d in os.listdir(comp)
            if os.path.isdir(os.path.join(comp, d)) and not d.startswith((".", "@")) and d != "apex"
            and os.path.isfile(os.path.join(comp, d, "docker-compose.yml")))
        seq = ([core] if os.path.isfile(os.path.join(core, "docker-compose.yml")) else []) + others
        return list(reversed(seq)) if action == "down" else seq

    def run_one(dirpath):
        name = os.path.basename(dirpath)
        base = ["docker", "compose"]
        for ef in (".env", "apex.env"):
            if os.path.isfile(os.path.join(dirpath, ef)):
                base += ["--env-file", ef]
        cmd = base + (["up", "-d"] if action == "up" else [action]) + extra
        if dry_run:
            log.info(f"[DRY-RUN] (cd {dirpath} && {' '.join(cmd)})")
        else:
            log.info(f"{action} {name}...")
            ctx.sys.run(cmd, cwd=dirpath, env=env, check=True)

    for d in sequence():
        run_one(d)
    log.success(f"Orchestrated {action}.")
