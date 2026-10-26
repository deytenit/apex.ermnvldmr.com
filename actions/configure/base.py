# actions/configure/base.py
"""Port of configure/base — base packages + ufw + ufw-docker helper."""
import hashlib
import os
from pathlib import Path
import re
import stat
import urllib.request
from engine.descriptor import Meta

METADATA = Meta(summary="Configure base system packages (curl, wget, git, rsyslog, ufw, ufw-docker).")
UFW_DOCKER_REVISION = "020a8699f95592561f254d8d4ad1bb40d401dfc7"
UFW_DOCKER_SHA256 = "643e56b080567c567b4aa28650196849b2a2da5dd0473fd3e5216b0886035ab0"
UFW_DOCKER_URL = (
    "https://raw.githubusercontent.com/chaifeng/ufw-docker/"
    + UFW_DOCKER_REVISION + "/ufw-docker"
)
UFW_DOCKER_PATH = Path("/usr/local/bin/ufw-docker")
BASE_PACKAGES = ("curl", "wget", "git", "rsyslog", "ufw")


def _install_missing(s, packages):
    missing = []
    for package in packages:
        result = s.run(["dpkg-query", "-W", "-f=${Status}", package],
                       check=False, capture=True)
        if result.returncode or result.stdout.split()[1:] != ["ok", "installed"]:
            missing.append(package)
    if not missing:
        return
    apt = ["env", "LC_ALL=C", "DEBIAN_FRONTEND=noninteractive", "apt-get",
           "-o", "DPkg::Lock::Timeout=60", "-o", "Acquire::Retries=2",
           "-o", "Acquire::http::Timeout=30", "-o", "Acquire::https::Timeout=30"]
    s.sudo([*apt, "update", "--error-on=any"])
    command = [*apt, "install", "-y", "--no-install-recommends", "--no-upgrade",
               "--no-remove", *missing]
    plan = s.sudo([*command, "--simulate"], capture=True)
    if any(line.startswith("Remv ") or re.match(r"^Inst \S+ \[", line)
           for line in plan.stdout.splitlines()):
        raise RuntimeError("Missing packages require upgrades/removals; reconcile packages first.")
    s.sudo(command)


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
    log, s = ctx.log, ctx.sys
    log.info("Starting base system initialization...")
    if not os.path.isfile("/etc/debian_version"):
        log.error("Requires a Debian-based system (Debian/Ubuntu).")
        raise SystemExit(1)
    _check_helper_path(UFW_DOCKER_PATH)
    _install_missing(s, BASE_PACKAGES)
    s.ensure_running("rsyslog")

    # Configure APT to discard downloaded archives
    apt_clean_path = "/etc/apt/apt.conf.d/99-apex"
    apt_clean_content = 'APT::Keep-Downloaded-Packages "0";\n'
    if os.path.exists("/etc/apt/apt.conf.d/01clean"):
        s.sudo(["rm", "-f", "/etc/apt/apt.conf.d/01clean"])
    current_apt = None
    if os.path.exists(apt_clean_path):
        try:
            with open(apt_clean_path, encoding="utf-8") as f:
                current_apt = f.read()
        except OSError:
            current_apt = None
    if current_apt != apt_clean_content:
        log.info("Configuring APT to discard downloaded archives...")
        s.sudo(["tee", apt_clean_path], input=apt_clean_content)

    # Limit systemd-journald max disk usage
    journal_d = "/etc/systemd/journald.conf.d"
    journal_conf = os.path.join(journal_d, "99-apex.conf")
    journal_content = "[Journal]\nSystemMaxUse=200M\n"
    if os.path.exists(os.path.join(journal_d, "maxuse.conf")):
        s.sudo(["rm", "-f", os.path.join(journal_d, "maxuse.conf")])
    current_journal = None
    if os.path.exists(journal_conf):
        try:
            with open(journal_conf, encoding="utf-8") as f:
                current_journal = f.read()
        except OSError:
            current_journal = None
    if current_journal != journal_content:
        log.info("Configuring systemd-journald SystemMaxUse=200M...")
        s.sudo(["mkdir", "-p", journal_d])
        s.sudo(["tee", journal_conf], input=journal_content)
        s.sudo(["systemctl", "restart", "systemd-journald"])

    _install_helper(ctx)
    log.success("Base system initialization completed.")
