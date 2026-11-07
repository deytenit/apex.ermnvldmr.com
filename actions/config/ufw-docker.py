"""Install the pinned ufw-docker integration on Docker hosts."""
import hashlib
import os
from pathlib import Path
import stat
import urllib.request
from engine.descriptor import Meta

METADATA = Meta(summary="Install the pinned Docker integration for UFW.")
UFW_DOCKER_REVISION = "020a8699f95592561f254d8d4ad1bb40d401dfc7"
UFW_DOCKER_SHA256 = "643e56b080567c567b4aa28650196849b2a2da5dd0473fd3e5216b0886035ab0"
UFW_DOCKER_URL = (
    "https://raw.githubusercontent.com/chaifeng/ufw-docker/"
    + UFW_DOCKER_REVISION + "/ufw-docker"
)
UFW_DOCKER_PATH = Path("/usr/local/bin/ufw-docker")
def _check_helper_path(path):
    for parent in reversed(path.parents):
        info = parent.lstat()
        if (not stat.S_ISDIR(info.st_mode) or info.st_uid != 0 or
                info.st_mode & 0o022):
            raise RuntimeError(f"Unsafe ufw-docker ancestor: {parent}")
    try:
        info = path.lstat()
    except FileNotFoundError:
        return False
    if (not stat.S_ISREG(info.st_mode) or info.st_uid != 0 or info.st_gid != 0 or
            info.st_mode & 0o022 or info.st_nlink != 1):
        raise RuntimeError(f"Unsafe ufw-docker target: {path}")
    return True


def _install_helper(ctx):
    path = UFW_DOCKER_PATH
    exists = _check_helper_path(path)
    payload = path.read_bytes() if exists else None
    if payload is None or hashlib.sha256(payload).hexdigest() != UFW_DOCKER_SHA256:
        ctx.log.info(f"Downloading pinned ufw-docker helper to {path}...")
        # The immutable HTTPS URL is authenticated by the digest below.
        with urllib.request.urlopen(UFW_DOCKER_URL, timeout=30) as response:  # noqa: S310
            payload = response.read(1024 * 1024 + 1)
        if len(payload) > 1024 * 1024 or hashlib.sha256(payload).hexdigest() != UFW_DOCKER_SHA256:
            raise RuntimeError("ufw-docker checksum mismatch; refusing installation.")
        _check_helper_path(path)
        ctx.host.write_file(str(path), payload.decode("utf-8"), mode="0755",
                            backup=False, sudo=True)
    elif stat.S_IMODE(path.stat().st_mode) != 0o755:
        ctx.host.write_file(str(path), payload.decode("utf-8"), mode="0755",
                            backup=False, sudo=True)
    if (not _check_helper_path(path) or
            hashlib.sha256(path.read_bytes()).hexdigest() != UFW_DOCKER_SHA256 or
            stat.S_IMODE(path.stat().st_mode) != 0o755):
        raise RuntimeError("ufw-docker verification failed; refusing execution.")
    ctx.log.info("Ensuring ufw-docker routing rules are installed in UFW...")
    ctx.sys.sudo([str(path), "install"])


def run(ctx, args):
    _check_helper_path(UFW_DOCKER_PATH)
    if os.path.exists("/usr/bin/pveversion"):
        raise RuntimeError("Use the Proxmox firewall on a PVE host.")
    for binary in ("ufw", "docker"):
        if not ctx.sys.ok(["which", binary]):
            raise RuntimeError(f"Install {binary} before configuring ufw-docker.")
    _install_helper(ctx)
