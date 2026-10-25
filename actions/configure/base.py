# actions/configure/base.py
"""Port of configure/base — base packages + ufw + ufw-docker helper."""
import os
import shutil
import tempfile
import urllib.request
from engine.descriptor import Meta

METADATA = Meta(summary="Configure base system packages (curl, wget, git, rsyslog, ufw, ufw-docker).")
UFW_DOCKER_URL = "https://github.com/chaifeng/ufw-docker/raw/master/ufw-docker"


def run(ctx, args):
    log, s = ctx.log, ctx.sys
    log.info("Starting base system initialization...")
    if not os.path.isfile("/etc/debian_version"):
        log.error("Requires a Debian-based system (Debian/Ubuntu).")
        raise SystemExit(1)
    needed = [p for p in ["curl", "wget", "git", "rsyslog"] if not shutil.which(p)]
    if needed:
        log.info("Updating package lists...")
        s.sudo(["apt-get", "update", "-y"])
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

    if not shutil.which("ufw"):
        log.info("Installing UFW...")
        s.sudo(["apt-get", "install", "-y", "-o", "DPkg::Lock::Timeout=60", "ufw"])
    else:
        log.info("UFW already installed.")

    target_bin = "/usr/local/bin/ufw-docker"
    if not (os.path.isfile(target_bin) and os.access(target_bin, os.X_OK)):
        log.info("Downloading ufw-docker helper to /usr/local/bin/ufw-docker...")
        with tempfile.NamedTemporaryFile(delete=False) as tf:
            tmp_name = tf.name
        try:
            urllib.request.urlretrieve(UFW_DOCKER_URL, tmp_name)
            s.sudo(["cp", tmp_name, target_bin])
            s.sudo(["chown", "root:root", target_bin])
            s.sudo(["chmod", "0755", target_bin])
            log.success(f"ufw-docker installed to {target_bin}.")
        finally:
            if os.path.exists(tmp_name):
                os.unlink(tmp_name)
    else:
        log.info(f"ufw-docker already exists at {target_bin}.")

    log.info("Ensuring ufw-docker routing rules are installed in UFW...")
    s.sudo([target_bin, "install"])
    log.success("Base system initialization completed.")
