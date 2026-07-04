# actions/configure/ufw.py
"""Port of configure/ufw — apply docker + host ufw rules from configs/ufw."""
import os
from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Render + apply this node's UFW rules (configs/ufw).",
                args=[Flag("--dry-run", "Preview; do not apply.")])

def run(ctx, args):
    cfg = os.path.join(ctx.paths.configs, "ufw")
    if not os.path.isdir(cfg):
        ctx.log.error(f"Config directory not found: {cfg}"); raise SystemExit(1)
    docker_dir = os.path.join(cfg, "docker")
    host_dir = os.path.join(cfg, "host")
    ctx.log.info("Configuring UFW rules...")
    if not os.path.isdir(docker_dir):
        ctx.log.warn(f"Docker rules dir not found at {docker_dir}. Skipping ufw-docker rules.")
    ctx.ufw.apply(docker_dir, host_dir if os.path.isdir(host_dir) else None, dry_run=args.dry_run)
    ctx.log.success("Configured ufw.")
