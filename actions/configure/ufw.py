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
    # Mirrors the bash branch structure: host dir is passed unconditionally (a missing
    # one exits 66 inside apply); both dirs missing = warn + success, like bash.
    if os.path.isdir(docker_dir):
        ctx.ufw.apply(docker_dir, host_dir, dry_run=args.dry_run)
    else:
        ctx.log.warn(f"Docker rules dir not found at {docker_dir}. Skipping ufw-docker rules.")
        if os.path.isdir(host_dir):
            ctx.ufw.apply(docker_dir, host_dir, dry_run=args.dry_run)
    ctx.log.success("Configured ufw.")
