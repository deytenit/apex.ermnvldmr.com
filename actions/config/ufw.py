"""Apply the host firewall and the published-container rules from configs/ufw."""
from pathlib import Path

from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Apply this node's host and Docker firewall rules.", args=[
    Flag("--dry-run", "Preview the selected rules."),
    Flag("--host-only", "Apply host rules and activate UFW before installing ufw-docker."),
    Flag("--docker-only", "Apply required container rules after the workloads are running."),
])


def run(ctx, args):
    if args.host_only and args.docker_only:
        raise ValueError("Choose one firewall stage.")
    cfg = Path(ctx.paths.configs) / "ufw"
    if not cfg.is_dir():
        raise RuntimeError(f"Config directory not found: {cfg}")
    host = cfg / "host"
    docker = cfg / "docker"
    if args.host_only and not host.is_dir():
        raise RuntimeError(f"Host rules directory not found: {host}")
    if args.docker_only and not docker.is_dir():
        raise RuntimeError(f"Docker rules directory not found: {docker}")
    if args.host_only and Path("/usr/bin/pveversion").exists():
        raise RuntimeError("Use the Proxmox firewall on a PVE host.")
    if args.docker_only:
        return ctx.ufw.reconcile(docker, dry_run=args.dry_run)
    ctx.ufw.apply(str(docker) if docker.is_dir() and not args.host_only else None,
                  str(host) if host.is_dir() else None,
                  dry_run=args.dry_run, activate=args.host_only)
