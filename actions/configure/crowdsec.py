# actions/configure/crowdsec.py
"""Port of configure/crowdsec — install, deploy /etc/crowdsec, collections, bouncer."""
import os
from engine.descriptor import Meta, Flag

METADATA = Meta(summary="Install + configure CrowdSec from configs/crowdsec.",
                args=[Flag("--dry-run", "Render only; do not write/reload.")])

def run(ctx, args):
    cfg = os.path.join(ctx.paths.configs, "crowdsec")
    if not os.path.isdir(cfg):
        ctx.log.error(f"Config directory not found: {cfg}"); raise SystemExit(1)
    ctx.log.info("Configuring CrowdSec...")
    if not args.dry_run:
        ctx.crowdsec.ensure_installed()
    ctx.crowdsec.deploy("crowdsec", ctx.vars(), dry_run=args.dry_run)
    if not args.dry_run:
        ctx.crowdsec.install_collections(["crowdsecurity/linux", "crowdsecurity/traefik"])
        bouncer = os.path.join(cfg, "bouncers", "crowdsec-firewall-bouncer.yaml.local")
        if os.path.isfile(bouncer) and not ctx.sys.ok(
                ["bash", "-c", "dpkg -l | grep -q crowdsec-firewall-bouncer-iptables"]):
            ctx.log.info("Installing crowdsec-firewall-bouncer-iptables...")
            ctx.sys.sudo(["apt-get", "install", "-y", "crowdsec-firewall-bouncer-iptables"])
    ctx.log.success("Configured crowdsec.")
