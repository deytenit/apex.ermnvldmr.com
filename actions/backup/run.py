# actions/backup/run.py
"""Label-driven restic backup via resticontainer (github.com/deytenit/resticontainer).
Runs the resticontainer core service one-shot: init-if-needed -> backup (discovers
`restic.*`-labelled services, runs their pre-hooks/stops, snapshots the union of the
resolved host paths in a single restic run) -> forget --prune -> Telegram. Every
non-`backup` subcommand passes straight through to restic, so init/snapshots/forget
behave exactly as before."""
import json
import os
from engine.descriptor import Meta, Arg

METADATA = Meta(summary="Run the resticontainer label-driven backup (init-if-needed, backup, forget --prune).",
                args=[Arg("telegram_bot_url", "Telegram Bot URL for notifications")])
TITLE = "Restic Host Backup"

def run(ctx, args):
    log, node, url = ctx.log, ctx.node.name, args.telegram_bot_url
    core = ctx.paths.core
    host = f"{node}.apex.ermnvldmr.com"         # restic --host: the per-node public name (matches the bucket)
    tag = "biweekly"                            # restic --tag: snapshot cadence (this cron fires on the 5th & 20th)
    if not os.path.isfile(os.path.join(core, "docker-compose.yml")):
        log.error(f"Core compose not found in {core}"); raise SystemExit(1)
    env = dict(os.environ, COMPOSE_PROFILES="manual")
    env.update({k: v for k, v in ctx.vars().items() if k.startswith("APEX_")})  # tier paths, identity
    envfiles = []
    for ef in (".env", "apex.env"):          # secrets (.env) + committed scalars (apex.env)
        if os.path.isfile(os.path.join(core, ef)):
            envfiles += ["--env-file", ef]

    def restic(*cmd, check=True, capture=False):
        return ctx.sys.run(["docker", "compose", *envfiles, "--profile", "manual",
                            "run", "--rm", "-T", "resticontainer", *cmd],
                           cwd=core, env=env, check=check, capture=capture)

    def host_snapshot_ids():
        """This host's snapshot IDs, or None if it can't be determined (never fail on this)."""
        cp = restic("snapshots", "--host", host, "--json", check=False, capture=True)
        if cp.returncode != 0:
            return None
        try:
            return {s.get("id") for s in json.loads(cp.stdout or "[]")}
        except (ValueError, TypeError):
            return None

    try:
        log.info("Checking restic repository availability...")
        if restic("snapshots", check=False).returncode == 0:
            log.info("Restic repository reachable.")
        elif restic("init", check=False).returncode != 0:
            ctx.notify.error(TITLE, url, "restic init failed", node); raise SystemExit(1)
        before = host_snapshot_ids()
        log.info("Backing up `restic.*`-labelled services (resticontainer discovers paths)...")
        if restic("backup", "--host", host, "--tag", tag, "--compression", "max",
                  check=False).returncode != 0:
            ctx.notify.error(TITLE, url, "restic backup failed", node); raise SystemExit(1)
        # Guard: the label-driven backup is a silent no-op if no service carries the
        # `restic.enable` label (resticontainer finds zero paths and skips restic).
        # Prove a fresh snapshot for this host actually landed before we prune/notify.
        after = host_snapshot_ids()
        if before is not None and after is not None and not (after - before):
            log.error("No new snapshot produced — refusing to report success.")
            ctx.notify.error(TITLE, url,
                             "backup produced no new snapshot (no restic.* labels discovered?)", node)
            raise SystemExit(1)
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
