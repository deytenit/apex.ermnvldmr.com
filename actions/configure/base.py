"""Apply the bounded shared base package, journal and UFW helper policy."""
from pathlib import Path

from engine.descriptor import Meta

METADATA = Meta(summary="Configure base system packages (curl, wget, git, rsyslog, ufw, ufw-docker).")


def run(ctx, args):
    ctx.log.info("Starting base system initialization...")
    commons = Path(__file__).resolve().parents[2]
    ctx.sys.sudo(["python3", "-m", "engine.provisioning.baseline", "--base-policy"], cwd=str(commons))
    ctx.log.success("Base system initialization completed.")
