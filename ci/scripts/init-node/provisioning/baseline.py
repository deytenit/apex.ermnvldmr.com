"""Shared Debian baseline. Callers own the exclusive initialization lock."""
from __future__ import annotations

from contextlib import contextmanager
import fcntl
from functools import wraps
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import stat
import subprocess
import tempfile
import urllib.request
import uuid


class BaselineError(RuntimeError):
    """A required baseline postcondition could not be established."""


def expected_errors(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError) as exc:
            raise BaselineError("Baseline filesystem or saved state is invalid; inspect the target before retrying.") from exc
    return guarded


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
DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io",
                   "docker-buildx-plugin", "docker-compose-plugin")
SECURITY_PACKAGES = ("crowdsec", "crowdsec-firewall-bouncer-iptables")
DOCKER_CONFLICTS = ("docker.io", "docker-doc", "docker-compose", "docker-buildx",
                    "podman-docker", "containerd", "runc")
DOCKER_CONFIG = {"data-root": "/srv/docker", "log-driver": "json-file",
                 "log-opts": {"max-size": "20m", "max-file": "3"}}
HELPER_REVISION = "020a8699f95592561f254d8d4ad1bb40d401dfc7"
HELPER_URL = f"https://raw.githubusercontent.com/chaifeng/ufw-docker/{HELPER_REVISION}/ufw-docker"
HELPER_SHA256 = "643e56b080567c567b4aa28650196849b2a2da5dd0473fd3e5216b0886035ab0"
POLICY = Path("/usr/sbin/policy-rc.d")
POLICY_STATE = Path("/usr/sbin/.apex-policy-rc.d")
SECURITY_STATE = Path("/var/lib/apex/baseline-security.json")
SECURITY_SERVICES = {"ufw": "ufw", "crowdsec": "crowdsec",
                     "crowdsec-firewall-bouncer-iptables": "crowdsec-firewall-bouncer"}
SECURITY_CONDITION = "[Unit]\nConditionPathExists=/dev/null/apex-security-start-allowed\n"
APT = ["apt-get", "-o", "DPkg::Lock::Timeout=60", "-o",
       "Acquire::Retries=2", "-o", "Acquire::http::Timeout=30", "-o",
       "Acquire::https::Timeout=30"]


def run(args, *, check=True, timeout=300):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                                env={**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise BaselineError(f"Cannot complete {args[0]}: {exc}") from exc
    if check and result.returncode:
        raise BaselineError(f"{args[0]} failed with exit status {result.returncode}; inspect target system logs.")
    return result


def preflight(*, strict=True):
    if os.geteuid() != 0:
        raise BaselineError("Baseline requires root.")
    if not strict:
        if not Path("/etc/debian_version").is_file():
            raise BaselineError("Base policy requires a Debian-based system.")
        return
    if not Path("/etc/os-release").is_file():
        raise BaselineError("Supported target is Debian 13 amd64 only.")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line and not line.startswith("#"))
    if (release.get("ID", "").strip('"') != "debian" or
            release.get("VERSION_ID", "").strip('"') != "13" or
            run(["dpkg", "--print-architecture"]).stdout.strip() != "amd64"):
        raise BaselineError("Supported target is Debian 13 amd64 only.")


def package_versions():
    result = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Status}\t${Version}\n"])
    installed = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[1].split()[1:] == ["ok", "installed"]:
            installed[fields[0].removesuffix(":amd64")] = fields[2]
    return installed


def write_managed(path, content, mode=0o644):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise BaselineError(f"Refusing managed symlink: {parent}")
    payload = content.encode() if isinstance(content, str) else content
    if path.exists() and path.read_bytes() == payload:
        if path.stat().st_mode & 0o777 != mode:
            path.chmod(mode)
        return False
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix=".apex-", dir=path.parent)
    try:
        with os.fdopen(fd, "wb") as stream:
            stream.write(payload)
            stream.flush()
            os.fsync(stream.fileno())
        os.chmod(temporary, mode)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    return True


def restore_service_policy():
    if not POLICY_STATE.exists():
        return
    backup = POLICY_STATE / "original"
    if (POLICY_STATE.is_symlink() or POLICY_STATE.stat().st_uid != os.geteuid()):
        raise BaselineError("Refusing untrusted service policy recovery state.")
    if (POLICY_STATE / "ready").exists() and POLICY.exists():
        original_matches = (backup.exists() and not POLICY.is_symlink() and
                            POLICY.read_bytes() == backup.read_bytes())
        symlink_matches = (backup.is_symlink() and POLICY.is_symlink() and
                           os.readlink(backup) == os.readlink(POLICY))
        guard_matches = not POLICY.is_symlink() and POLICY.read_bytes() == b"#!/bin/sh\nexit 101\n"
        if not (original_matches or symlink_matches or guard_matches):
            raise BaselineError("Service policy changed outside APEX; recovery requires explicit reconciliation.")
    if backup.exists() or backup.is_symlink():
        # Copy first: an interrupted restoration must retain its recovery copy.
        temporary = POLICY_STATE / "restoring"
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        shutil.copy2(backup, temporary, follow_symlinks=False)
        os.replace(temporary, POLICY)
    elif (POLICY_STATE / "absent").exists():
        POLICY.unlink(missing_ok=True)
    elif (POLICY_STATE / "ready").exists():
        raise BaselineError("Service policy recovery is incomplete; inspect /usr/sbin/.apex-policy-rc.d.")
    shutil.rmtree(POLICY_STATE)


@contextmanager
def service_start_guard():
    lock_path = Path("/run/lock/apex-package-policy.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise BaselineError("Another APEX package operation is running.") from None
        with _service_start_guard():
            yield


@contextmanager
def _service_start_guard():
    restore_service_policy()
    POLICY_STATE.mkdir(mode=0o700)
    try:
        if POLICY.exists() or POLICY.is_symlink():
            shutil.copy2(POLICY, POLICY_STATE / "original", follow_symlinks=False)
        else:
            (POLICY_STATE / "absent").touch()
        (POLICY_STATE / "ready").touch()
        # Replace the path itself, preserving any original symlink in the backup.
        temporary = POLICY_STATE / "guard"
        temporary.write_text("#!/bin/sh\nexit 101\n")
        temporary.chmod(0o755)
        os.replace(temporary, POLICY)
        yield
    finally:
        restore_service_policy()


@contextmanager
def security_service_guard(packages):
    """Guard new security units without occupying package-owned unit paths."""
    record = json.loads(SECURITY_STATE.read_text()) if SECURITY_STATE.exists() else {"version": 2, "services": {}}
    if not isinstance(record, dict) or record.get("version") != 2:
        raise BaselineError("Legacy security service guard state requires explicit reconciliation.")
    managed = record.get("services")
    if not isinstance(managed, dict) or any(SECURITY_SERVICES.get(key) != value for key, value in managed.items()):
        raise BaselineError("Invalid baseline security service recovery state.")
    installed = package_versions()
    for package, service in SECURITY_SERVICES.items():
        if package in packages and package not in installed and package not in managed:
            if run(["systemctl", "is-enabled", service], check=False).stdout.strip() == "masked":
                continue
            managed[package] = service
            write_managed(SECURITY_STATE, json.dumps({"version": 2, "services": managed}), 0o600)
    for service in managed.values():
        dropin = Path(f"/etc/systemd/system/{service}.service.d/99-apex-bootstrap-guard.conf")
        if dropin.exists() and dropin.read_text() != SECURITY_CONDITION:
            raise BaselineError("Security service guard changed outside APEX; reconcile it explicitly.")
        # /dev/null cannot have children: this condition stays false after reboot.
        write_managed(dropin, SECURITY_CONDITION)
    if managed:
        run(["systemctl", "daemon-reload"])
    try:
        yield
    finally:
        installed = package_versions()
        for package, service in managed.items():
            unit_exists = any(Path(f"{directory}/{service}.service").exists() for directory in
                              ("/etc/systemd/system", "/usr/lib/systemd/system", "/lib/systemd/system"))
            run(["systemctl", "disable", service], check=package in installed or unit_exists)
            dropin = Path(f"/etc/systemd/system/{service}.service.d/99-apex-bootstrap-guard.conf")
            if dropin.exists():
                if dropin.is_symlink() or dropin.read_text() != SECURITY_CONDITION:
                    raise BaselineError("Security service guard changed outside APEX; reconcile it explicitly.")
                dropin.unlink()
        if managed:
            run(["systemctl", "daemon-reload"])
        SECURITY_STATE.unlink(missing_ok=True)


def install_missing(packages):
    installed = package_versions()
    missing = [package for package in packages if package not in installed]
    if not missing:
        return []
    run([*APT, "update", "--error-on=any"])
    command = [*APT, "install", "-y", "--no-install-recommends", "--no-upgrade", "--no-remove", *missing]
    plan = run([*command, "--simulate"]).stdout
    if any(line.startswith("Remv ") or re.match(r"^Inst \S+ \[", line)
           for line in plan.splitlines()):
        raise BaselineError("Missing packages require an upgrade/removal; reconcile packages explicitly before retrying.")
    # Package writes must settle even after coordinator loss; stages gate later work.
    run(command, timeout=None)
    remaining = set(missing) - package_versions().keys()
    if remaining:
        raise BaselineError("Required packages not installed: " + ", ".join(sorted(remaining)))
    return missing


def base_policy():
    return {"packages": list(BASE_PACKAGES), "files": {
        "/etc/apt/apt.conf.d/99-apex": 'APT::Keep-Downloaded-Packages "0";\n',
        "/etc/systemd/journald.conf.d/99-apex.conf": "[Journal]\nSystemMaxUse=200M\n",
    }, "helper": {"url": HELPER_URL, "sha256": HELPER_SHA256}}


def configure_common():
    policy = base_policy()
    for legacy in ("/etc/apt/apt.conf.d/01clean", "/etc/systemd/journald.conf.d/maxuse.conf"):
        path = Path(legacy)
        if path.exists() or path.is_symlink():
            path.unlink()
    for path, content in policy["files"].items():
        changed = write_managed(path, content)
        if changed and path.endswith("99-apex.conf"):
            run(["systemctl", "restart", "systemd-journald"])
    target = Path("/usr/local/bin/ufw-docker")
    if (not target.is_file() or hashlib.sha256(target.read_bytes()).hexdigest() != HELPER_SHA256):
        try:
            with urllib.request.urlopen(HELPER_URL, timeout=30) as response:
                data = response.read()
        except OSError as exc:
            raise BaselineError("Unable to download pinned ufw-docker helper.") from exc
        if hashlib.sha256(data).hexdigest() != HELPER_SHA256:
            raise BaselineError("Pinned ufw-docker checksum mismatch.")
        write_managed(target, data, 0o755)
    elif target.is_symlink():
        raise BaselineError("Refusing a symlinked ufw-docker helper.")
    else:
        target.chmod(0o755)


def docker_preflight(installed):
    conflicts = sorted(set(DOCKER_CONFLICTS) & installed.keys())
    if conflicts:
        raise BaselineError("Conflicting Docker packages require explicit reconciliation: " + ", ".join(conflicts))
    path = Path("/etc/docker/daemon.json")
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise BaselineError("Cannot validate existing Docker daemon configuration.") from exc
    if not isinstance(config, dict):
        raise BaselineError("Docker daemon configuration must be an object.")
    for name, value in DOCKER_CONFIG.items():
        if name in config and config[name] != value:
            raise BaselineError(f"Conflicting Docker {name}; reconcile explicitly before initialization.")
    old_data = Path("/var/lib/docker")
    if old_data.exists() and any(old_data.iterdir()):
        raise BaselineError("Existing /var/lib/docker data must be reconciled explicitly; no data migration is automatic.")
    data = Path("/srv/docker")
    if data.is_symlink() or data.parent.is_symlink():
        raise BaselineError("Refusing a redirected Docker data-root.")
    if data.exists() and any(data.iterdir()) and config.get("data-root") != "/srv/docker":
        raise BaselineError("Unmanaged /srv/docker data requires explicit reconciliation.")
    if "docker-ce" in installed:
        active = run(["systemctl", "is-active", "--quiet", "docker"], check=False).returncode == 0
        if active:
            root = run(["docker", "info", "--format", "{{.DockerRootDir}}"] ).stdout.strip()
            if root != "/srv/docker" or any(config.get(key) != value for key, value in DOCKER_CONFIG.items()):
                raise BaselineError("Running Docker has incompatible data-root/log policy; reconcile it before retrying.")
    return {**config, **DOCKER_CONFIG}


def configure_sources():
    write_managed("/etc/apt/sources.list.d/apex-zfs.sources",
                  "Types: deb\nURIs: https://deb.debian.org/debian\nSuites: trixie\n"
                  "Components: contrib\nArchitectures: amd64\n"
                  "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg\n")
    for name, key_url, uri, suite, component in (
        ("docker", "https://download.docker.com/linux/debian/gpg",
         "https://download.docker.com/linux/debian", "trixie", "stable"),
        ("crowdsec", "https://packagecloud.io/crowdsec/crowdsec/gpgkey",
         "https://packagecloud.io/crowdsec/crowdsec/any", "any", "main"),
    ):
        key = Path(f"/etc/apt/keyrings/apex-{name}.asc")
        if not key.exists():
            try:
                with urllib.request.urlopen(key_url, timeout=30) as response:
                    data = response.read()
            except OSError as exc:
                raise BaselineError(f"Cannot download {name} repository signing key.") from exc
            if not data.startswith(b"-----BEGIN PGP PUBLIC KEY BLOCK-----"):
                raise BaselineError(f"Invalid {name} repository signing key.")
            write_managed(key, data)
        write_managed(f"/etc/apt/sources.list.d/apex-{name}.sources",
                      f"Types: deb\nURIs: {uri}\nSuites: {suite}\nComponents: {component}\n"
                      f"Architectures: amd64\nSigned-By: {key}\n")


def ensure_service(name):
    if run(["systemctl", "is-enabled", "--quiet", name], check=False).returncode:
        run(["systemctl", "enable", name])
    if run(["systemctl", "is-active", "--quiet", name], check=False).returncode:
        run(["systemctl", "start", name])


def repair_bouncer_credentials():
    """Repair package placeholders without rewriting administrator YAML settings."""
    config = Path("/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml")
    identity = config.with_suffix(".yaml.id")
    for path in (config, identity):
        if any(parent.is_symlink() for parent in (path, *path.parents)):
            raise BaselineError("Refusing redirected CrowdSec bouncer credential paths.")
        if path.exists():
            metadata = path.stat()
            if (not stat.S_ISREG(metadata.st_mode) or metadata.st_nlink != 1 or
                    metadata.st_uid != os.geteuid()):
                raise BaselineError("Refusing untrusted CrowdSec bouncer credential files.")
    if not config.exists():
        raise BaselineError("CrowdSec bouncer configuration is missing; reconcile the package installation.")
    content = config.read_text()
    entries = list(re.finditer(r"^(?:api_key|'api_key'|\"api_key\"):[^\r\n]*", content, re.MULTILINE))
    if len(entries) != 1:
        raise BaselineError("CrowdSec bouncer requires one explicit api_key; reconcile its configuration.")
    entry = entries[0]
    for following in content[entry.end():].splitlines():
        if not following.strip() or following.lstrip().startswith("#"):
            continue
        if following[:1].isspace():
            raise BaselineError("Multiline CrowdSec bouncer api_key requires explicit reconciliation.")
        break
    scalar = re.fullmatch(r"(?:api_key|'api_key'|\"api_key\"):[ \t]*(?P<value>\"[^\"]*\"|'[^']*'|[^ \t#'\"]*)(?:[ \t]+(?:#.*)?)?", entry.group())
    if scalar is None:
        raise BaselineError("Unsupported CrowdSec bouncer api_key; reconcile its configuration.")
    value = scalar.group("value")
    uninitialized = value in ("null", "Null", "NULL", "~")
    if value[:1] in ("'", '"'):
        value = value[1:-1]
    if not uninitialized and value not in ("", "<API_KEY>", "$API_KEY", "${API_KEY}"):
        return
    metadata = config.stat()
    registration = "cs-firewall-bouncer-apex-" + uuid.uuid4().hex
    key = run(["cscli", "-oraw", "bouncers", "add", registration]).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9_+/=-]{24,256}", key):
        raise BaselineError("CrowdSec bouncer registration did not return a usable API key.")
    start, end = scalar.span("value")
    separator = " " if entry.group()[start - 1] == ":" else ""
    updated = content[:entry.start() + start] + separator + key + content[entry.start() + end:]

    def publish(path, payload, mode, owner):
        fd, temporary = tempfile.mkstemp(prefix=".apex-", dir=path.parent)
        try:
            with os.fdopen(fd, "w") as stream:
                os.fchown(stream.fileno(), *owner)
                os.fchmod(stream.fileno(), mode)
                stream.write(payload)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(temporary, path)
            directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(directory)
            finally:
                os.close(directory)
        finally:
            if os.path.exists(temporary):
                os.unlink(temporary)

    # Publish the key last: interruption can orphan a registration, but retries
    # replace the placeholder and its package bookkeeping together in this order.
    owner = (metadata.st_uid, metadata.st_gid)
    publish(identity, registration + "\n", 0o600, owner)
    publish(config, updated, (stat.S_IMODE(metadata.st_mode) & 0o640) | 0o600, owner)
    if config.read_text() != updated or identity.read_text().strip() != registration:
        raise BaselineError("CrowdSec bouncer credential publication did not converge.")


@expected_errors
def apply_base_policy():
    """The bounded configure/base policy: no Docker, storage or enrollment."""
    preflight(strict=False)
    with service_start_guard(), security_service_guard(BASE_PACKAGES):
        install_missing(BASE_PACKAGES)
    configure_common()
    ensure_service("rsyslog")
    run(["/usr/local/bin/ufw-docker", "install"])
    return {"packages": {key: value for key, value in package_versions().items() if key in BASE_PACKAGES}}


@expected_errors
def apply_baseline():
    """Converge baseline and return verified package versions and capabilities."""
    preflight()
    installed = package_versions()
    docker_config = docker_preflight(installed)
    kernel = run(["uname", "-r"]).stdout.strip()
    if not re.fullmatch(r"[A-Za-z0-9.+_-]+", kernel):
        raise BaselineError("Unsupported running kernel release.")
    packages = (*BASE_PACKAGES, *ADMIN_PACKAGES, *DOCKER_PACKAGES,
                *SECURITY_PACKAGES, "linux-headers-amd64", f"linux-headers-{kernel}",
                "zfsutils-linux", "zfs-dkms")
    with service_start_guard(), security_service_guard(packages):
        install_missing((*BASE_PACKAGES, *ADMIN_PACKAGES))
        configure_sources()
        install_missing(("crowdsec",))
        install_missing(packages)
        repair_bouncer_credentials()
    configure_common()
    write_managed("/etc/docker/daemon.json", json.dumps(docker_config, indent=2) + "\n")
    Path("/srv/docker").mkdir(parents=True, exist_ok=True)
    ensure_service("containerd")
    ensure_service("docker")
    ensure_service("rsyslog")
    ensure_service("cron")
    root = run(["docker", "info", "--format", "{{.DockerRootDir}}"] ).stdout.strip()
    if root != "/srv/docker":
        raise BaselineError("Docker did not activate the required /srv/docker data-root.")
    compose = run(["docker", "compose", "version", "--short"]).stdout.strip()
    if not compose:
        raise BaselineError("Docker Compose capability is unavailable.")
    module = run(["modprobe", "zfs"], check=False)
    if module.returncode:
        raise BaselineError(f"ZFS unavailable for running kernel {kernel}; install matching headers/module or reboot into the supported kernel, then retry. No automatic reboot was performed.")
    zfs = run(["zfs", "version"]).stdout.strip()
    versions = package_versions()
    if set(packages) - versions.keys():
        raise BaselineError("Baseline package verification failed.")
    return {"packages": {name: versions[name] for name in packages},
            "capabilities": {"docker_data_root": root, "compose": compose, "zfs": zfs,
                             "kernel": kernel, "ufw_docker_sha256": HELPER_SHA256},
            "sources": {"docker": "docker.com/debian trixie stable",
                        "crowdsec": "packagecloud.io/crowdsec/crowdsec/any any main",
                        "zfs": "Debian trixie contrib"}}


if __name__ == "__main__":
    import argparse
    from .state import ProvisionError, exclusive_lock
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base-policy", action="store_true")
    args = parser.parse_args()
    try:
        preflight(strict=not args.base_policy)
        with exclusive_lock("/var/lib/apex-init/init.lock"):
            print(json.dumps(apply_base_policy() if args.base_policy else apply_baseline(), sort_keys=True))
    except (BaselineError, OSError, ProvisionError) as exc:
        parser.exit(1, f"Baseline failed: {exc}\n")
