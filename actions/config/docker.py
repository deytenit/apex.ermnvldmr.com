"""Install a local Docker Engine, Compose and Buildx on Debian 13."""
from pathlib import Path

from engine.descriptor import Meta

METADATA = Meta(summary="Verify a working Docker toolchain or install it on stock Debian 13.")


def run(ctx, args):
    provisioning = Path(ctx.paths.commons) / "ci/scripts/init-node"
    ctx.sys.sudo(["python3", "-E", "-s", "-m", "provisioning.baseline", "--docker-policy"],
                 cwd=str(provisioning))
