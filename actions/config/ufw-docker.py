"""Install the pinned ufw-docker integration on Docker hosts."""
import hashlib
import ipaddress
import json
import os
import stat
import urllib.request
from engine.descriptor import Meta
from engine.lib.ufw import PINNED_HELPER as UFW_DOCKER_PATH

METADATA = Meta(summary="Install the pinned Docker integration for UFW.")
UFW_DOCKER_REVISION = "020a8699f95592561f254d8d4ad1bb40d401dfc7"
UFW_DOCKER_SHA256 = "643e56b080567c567b4aa28650196849b2a2da5dd0473fd3e5216b0886035ab0"
UFW_DOCKER_URL = (
    "https://raw.githubusercontent.com/chaifeng/ufw-docker/"
    + UFW_DOCKER_REVISION + "/ufw-docker"
)


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


def _install_helper(ctx, subnets):
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
    ctx.sys.sudo([str(path), "install", "--docker-subnets", *subnets])


def _subnets(ctx):
    # Supplying any custom subnet replaces all of the helper's defaults, including IPv6.
    subnets = {ipaddress.ip_network(cidr) for cidr in
               ("10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16", "fd00::/8")}
    if ctx.node:
        subnets.add(ipaddress.ip_network(ctx.node.subnet))
    networks = ctx.sys.sudo(["docker", "network", "ls", "-q"], capture=True).stdout.split()
    if networks:
        result = ctx.sys.sudo(["docker", "network", "inspect", *networks], capture=True)
        for network in json.loads(result.stdout):
            for pool in network["IPAM"].get("Config") or []:
                if pool.get("Subnet"):
                    subnets.add(ipaddress.ip_network(pool["Subnet"]))
    return [str(net) for net in sorted(subnets, key=lambda n: (n.version, n))]


def run(ctx, args):
    _check_helper_path(UFW_DOCKER_PATH)
    if os.path.exists("/usr/bin/pveversion"):
        raise RuntimeError("Use the Proxmox firewall on a PVE host.")
    for binary in ("ufw", "docker"):
        if not ctx.sys.sudo_has_command(binary):
            raise RuntimeError(f"Install {binary} before configuring ufw-docker.")
    if not ctx.ufw.is_active():
        raise RuntimeError("Activate UFW with config/ufw --host-only before installing ufw-docker.")
    _install_helper(ctx, _subnets(ctx))
    ctx.sys.sudo(["ufw", "reload"])
    if not ctx.ufw.is_active():
        raise RuntimeError("UFW is inactive after loading the Docker rules.")
