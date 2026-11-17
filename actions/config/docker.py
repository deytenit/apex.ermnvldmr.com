"""Install a local Docker Engine, Compose and Buildx on Debian 13."""
import grp
from pathlib import Path
import pwd

from engine.descriptor import Meta

METADATA = Meta(summary="Verify a working Docker toolchain or install it on stock Debian 13.")


def run(ctx, args):
    provisioning = Path(ctx.paths.commons) / "scripts"
    ctx.sys.sudo(["python3", "-E", "-s", "-m", "provisioning.docker"],
                 cwd=str(provisioning))
    try:
        account = pwd.getpwnam("adam")
        grp.getgrnam("docker")
    except KeyError:
        return
    if account.pw_uid >= 1000:
        ctx.sys.sudo(["usermod", "--append", "--groups", "docker", "adam"])
