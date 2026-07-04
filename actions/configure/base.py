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
    log.info("Updating package lists..."); s.sudo(["apt-get", "update", "-y"])
    log.info("Installing essential packages...")
    s.sudo(["apt-get", "install", "-y", "curl", "wget", "git", "rsyslog"])
    s.ensure_running("rsyslog")
    if not shutil.which("ufw"):
        log.info("Installing UFW..."); s.sudo(["apt-get", "install", "-y", "ufw"])
    else:
        log.info("UFW already installed.")
    binp = os.path.expanduser("~/.local/bin/ufw-docker")
    if not (os.path.isfile(binp) and os.access(binp, os.X_OK)):
        log.info("Downloading ufw-docker helper...")
        os.makedirs(os.path.dirname(binp), exist_ok=True)
        urllib.request.urlretrieve(UFW_DOCKER_URL, binp)       # stdlib; replaces wget/curl
        os.chmod(binp, os.stat(binp).st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
        log.success(f"ufw-docker downloaded to {binp}.")
    else:
        log.info(f"ufw-docker already exists at {binp}.")
    log.success("Base system initialization completed.")
