# actions/configure/systemd.py
"""Port of configure/systemd — deploy + enable units from configs/systemd."""
import os
from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Deploy + enable systemd units from configs/systemd.",
                args=[Flag("--dry-run", "Render only; do not write/enable.")])

def run(ctx, args):
    cfg = os.path.join(ctx.paths.configs, "systemd")
    if not os.path.isdir(cfg):
        ctx.log.error(f"Config directory not found: {cfg}"); raise SystemExit(1)
    ctx.log.info("Deploying systemd units...")
    ctx.systemd.deploy_units("systemd", ctx.vars(), dry_run=args.dry_run)
    ctx.log.success("Configured systemd.")
