# actions/configure/base.py
"""Port of configure/base — base packages + ufw + ufw-docker helper."""
import os, shutil, stat, urllib.request
from engine.descriptor import Meta

METADATA = Meta(summary="Configure base system packages (curl, wget, git, rsyslog, ufw, ufw-docker).")
UFW_DOCKER_URL = "https://github.com/chaifeng/ufw-docker/raw/master/ufw-docker"

def run(ctx, args):
    log, s = ctx.log, ctx.sys
    log.info("Starting base system initialization...")
    if not os.path.isfile("/etc/debian_version"):
        log.error("Requires a Debian-based system (Debian/Ubuntu)."); raise SystemExit(1)
    needed = [p for p in ["curl", "wget", "git", "rsyslog"] if not shutil.which(p)]
    if needed:
        log.info("Updating package lists..."); s.sudo(["apt-get", "update", "-y"])
        log.info(f"Installing missing essential packages: {', '.join(needed)}...")
        s.sudo(["apt-get", "install", "-y", "-o", "DPkg::Lock::Timeout=60", *needed])
    else:
        log.info("Essential packages already installed.")
    s.ensure_running("rsyslog")

    # Configure APT to discard downloaded archives
    apt_clean_path = "/etc/apt/apt.conf.d/99-apex"
    apt_clean_content = 'APT::Keep-Downloaded-Packages "0";\n'
    if os.path.exists("/etc/apt/apt.conf.d/01clean"):
        s.sudo(["rm", "-f", "/etc/apt/apt.conf.d/01clean"])
    try:
        current_apt = open(apt_clean_path).read() if os.path.exists(apt_clean_path) else None
    except Exception:
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
    try:
        current_journal = open(journal_conf).read() if os.path.exists(journal_conf) else None
    except Exception:
        current_journal = None
    if current_journal != journal_content:
        log.info("Configuring systemd-journald SystemMaxUse=200M...")
        s.sudo(["mkdir", "-p", journal_d])
        s.sudo(["tee", journal_conf], input=journal_content)
        s.sudo(["systemctl", "restart", "systemd-journald"])

    if not shutil.which("ufw"):
        log.info("Installing UFW..."); s.sudo(["apt-get", "install", "-y", "-o", "DPkg::Lock::Timeout=60", "ufw"])
    else:
        log.info("UFW already installed.")
    from engine.lib.ufw import resolve_ufw_docker_bin
    binp = resolve_ufw_docker_bin()
    if not (os.path.isfile(binp) and os.access(binp, os.X_OK)):
        log.info("Downloading ufw-docker helper...")
        target_bin = os.path.expanduser("~/.local/bin/ufw-docker")
        os.makedirs(os.path.dirname(target_bin), exist_ok=True)
        urllib.request.urlretrieve(UFW_DOCKER_URL, target_bin)
        os.chmod(target_bin, os.stat(target_bin).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        log.success(f"ufw-docker downloaded to {target_bin}.")
    else:
        log.info(f"ufw-docker already exists at {binp}.")
    log.info("Ensuring ufw-docker routing rules are installed in UFW...")
    s.sudo([binp, "install"])
    log.success("Base system initialization completed.")
