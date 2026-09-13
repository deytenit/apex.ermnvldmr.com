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
    has_docker = os.path.isdir(docker_dir)
    has_host = os.path.isdir(host_dir)
    if not has_docker:
        ctx.log.warn(f"Docker rules dir not found at {docker_dir}. Skipping ufw-docker rules.")
    if not has_host:
        ctx.log.warn(f"Host rules dir not found at {host_dir}. Skipping host rules.")
    if has_docker or has_host:
        ctx.ufw.apply(docker_dir if has_docker else None, host_dir if has_host else None, dry_run=args.dry_run)
    ctx.log.success("Configured ufw.")
