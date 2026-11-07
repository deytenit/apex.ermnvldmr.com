"""Install common Debian administrator tools without replacing host packages."""
import re
from pathlib import Path
from engine.descriptor import Meta

METADATA = Meta(summary="Install Debian administrator packages and enable rsyslog.")
BASE_PACKAGES = ("curl", "wget", "git", "rsyslog", "ufw")


ADMIN_PACKAGES = (
    "tcpdump", "socat", "rsync", "bind9-dnsutils", "iproute2", "net-tools",
    "traceroute", "ca-certificates", "jq", "sed", "gawk", "tar", "gzip",
    "bzip2", "xz-utils", "unzip", "htop", "iotop", "strace", "lsof",
    "sysstat", "parted", "fdisk", "pciutils", "usbutils", "smartmontools",
    "tmux", "vim", "less", "tree", "bash-completion", "python3",
    "python3-minimal", "python3-yaml", "sudo", "cron", "iptables", "ipset",
    "debian-archive-keyring", "kmod",
)

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


def run(ctx, args):
    if not Path("/etc/debian_version").is_file():
        raise RuntimeError("Debian required.")
    packages = (*BASE_PACKAGES, *ADMIN_PACKAGES)
    if Path("/usr/bin/pveversion").exists():
        packages = tuple(package for package in packages if package != "ufw")
    _install_missing(ctx.sys, packages)
    if not ctx.sys.start_and_enable("rsyslog"):
        raise RuntimeError("rsyslog failed to start.")
