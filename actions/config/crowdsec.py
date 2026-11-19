"""Install CrowdSec, apply the node's acquisition policy, and activate its bouncer."""
from pathlib import Path
import re

from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Install and configure CrowdSec from configs/crowdsec.", args=[
    Flag("--dry-run", "Preview configuration deployment and collection selection."),
])


def _collections(manifest):
    if not manifest.exists():
        return ["crowdsecurity/linux", "crowdsecurity/traefik"]
    collections = []
    for number, line in enumerate(manifest.read_text(encoding="utf-8").splitlines(), 1):
        name = line.strip()
        if not name or name.startswith("#"):
            continue
        if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*/[A-Za-z0-9][A-Za-z0-9_.-]*", name):
            raise ValueError(f"{manifest}:{number}: expected one publisher/collection name.")
        if name not in collections:
            collections.append(name)
    return collections


def run(ctx, args):
    cfg = Path(ctx.paths.configs) / "crowdsec"
    if not cfg.is_dir():
        raise RuntimeError(f"Config directory not found: {cfg}")
    collections = _collections(cfg / "collections.txt")
    ctx.log.info(f"CrowdSec collections: {', '.join(collections)}")
    if not args.dry_run:
        ctx.crowdsec.ensure_installed()
    ctx.crowdsec.deploy("crowdsec", ctx.vars(), dry_run=args.dry_run)
    if args.dry_run:
        return
    ctx.crowdsec.install_collections(collections)
    ctx.sys.sudo(["crowdsec", "-t"])
    services = ["crowdsec"]
    if (cfg / "bouncers/crowdsec-firewall-bouncer.yaml.local").is_file():
        package = "crowdsec-firewall-bouncer-iptables"
        status = ctx.sys.run(["dpkg-query", "-W", "-f=${Status}", package], check=False, capture=True)
        if status.returncode or status.stdout.strip() != "install ok installed":
            ctx.sys.install_packages([package])
        ctx.sys.sudo(["crowdsec-firewall-bouncer", "-c",
                      "/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml", "-t"])
        services.append("crowdsec-firewall-bouncer")
    for service in services:
        if not ctx.sys.restart(service):
            raise RuntimeError(f"{service} is inactive after restart.")
    ctx.log.success("Configured CrowdSec.")
