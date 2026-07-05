# actions/backup/run.py
"""Port of sync/tiers restic-backup (SP4: behavior-identical). Runs the restic core
service one-shot: init-if-needed -> backup tiers -> forget --prune -> Telegram."""
import os
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Run restic backup of the backed-up tiers (init-if-needed, backup, forget --prune).",
                args=[Arg("telegram_bot_url", "Telegram Bot URL for notifications")])
TITLE = "Restic Host Backup"

def run(ctx, args):
    log, node, url = ctx.log, ctx.node.name, args.telegram_bot_url
    core = ctx.paths.core
    tag = f"com-ermnvldmr-root-{node}"          # KEPT unchanged for backup continuity (SP4/Decision D)
    if not os.path.isfile(os.path.join(core, "docker-compose.yml")):
        log.error(f"Core compose not found in {core}"); raise SystemExit(1)
    env = dict(os.environ, COMPOSE_PROFILES="manual")
    env.update({k: v for k, v in ctx.vars().items() if k.startswith("APEX_")})  # tier paths, identity
    envfiles = []
    for ef in (".env", "apex.env"):          # secrets (.env) + committed scalars (apex.env)
        if os.path.isfile(os.path.join(core, ef)):
            envfiles += ["--env-file", ef]

    def restic(*cmd, check=True):
        return ctx.sys.run(["docker", "compose", *envfiles, "--profile", "manual",
                            "run", "--rm", "restic", *cmd], cwd=core, env=env, check=check)

    try:
        log.info("Checking restic repository availability...")
        if restic("snapshots", check=False).returncode == 0:
            log.info("Restic repository reachable.")
        elif restic("init", check=False).returncode != 0:
            ctx.notify.error(TITLE, url, "restic init failed", node); raise SystemExit(1)
        log.info("Backing up /data/@tier1 and /data/@tier2 ...")
        if restic("backup", "/data/@tier1", "/data/@tier2", "--host", node, "--tag", tag,
                  "--compression", "auto", check=False).returncode != 0:
            ctx.notify.error(TITLE, url, "restic backup failed", node); raise SystemExit(1)
        log.info("Applying retention (forget --prune)...")
        if restic("forget", "--prune", "--keep-daily", "7", "--keep-weekly", "4",
                  "--keep-monthly", "12", check=False).returncode != 0:
            ctx.notify.error(TITLE, url, "restic forget/prune failed", node); raise SystemExit(1)
        log.success("Restic backup completed.")
        if not ctx.notify.success(TITLE, url, f"Restic backup + retention succeeded for {node}", node):
            raise SystemExit(1)   # bash exited 1 when the success telegram failed to send
    except SystemExit:
        raise
    except Exception as e:
        log.error(f"{type(e).__name__}: {e}")
        ctx.notify.error(TITLE, url, f"backup failed: {e}", node)
        raise SystemExit(1)
