"""Verify or install a local Docker Engine and CLI plugins."""
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
import subprocess
import tempfile
import urllib.request


class DockerError(RuntimeError):
    """A required Docker postcondition could not be established."""


def expected_errors(operation):
    @wraps(operation)
    def guarded(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (OSError, ValueError) as exc:
            raise DockerError("Docker filesystem or saved state is invalid; inspect the target before retrying.") from exc
    return guarded


DOCKER_PACKAGES = ("docker-ce", "docker-ce-cli", "containerd.io",
                   "docker-buildx-plugin", "docker-compose-plugin")
DOCKER_CONFLICTS = ("docker.io", "docker-doc", "docker-compose", "docker-buildx",
                    "podman-docker", "containerd", "runc")
DOCKER_CONFIG = {"data-root": "/srv/docker", "log-driver": "json-file",
                 "log-opts": {"max-size": "20m", "max-file": "3"}}
POLICY = Path("/usr/sbin/policy-rc.d")
POLICY_STATE = Path("/usr/sbin/.apex-policy-rc.d")
APT = ["apt-get", "-o", "DPkg::Lock::Timeout=60", "-o",
       "Acquire::Retries=2", "-o", "Acquire::http::Timeout=30", "-o",
       "Acquire::https::Timeout=30"]


def run(args, *, check=True, timeout=300):
    try:
        result = subprocess.run(args, text=True, capture_output=True, timeout=timeout,
                                env={**os.environ, "LC_ALL": "C", "DEBIAN_FRONTEND": "noninteractive"})
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise DockerError(f"Cannot complete {args[0]}: {exc}") from exc
    if check and result.returncode:
        raise DockerError(f"{args[0]} failed with exit status {result.returncode}; inspect target system logs.")
    return result


def package_versions():
    architecture = run(["dpkg", "--print-architecture"]).stdout.strip()
    result = run(["dpkg-query", "-W", "-f=${binary:Package}\t${Status}\t${Version}\n"])
    installed = {}
    for line in result.stdout.splitlines():
        fields = line.split("\t")
        if len(fields) == 3 and fields[1].split()[1:] == ["ok", "installed"]:
            installed[fields[0].removesuffix(":" + architecture)] = fields[2]
    return installed


def write_managed(path, content, mode=0o644):
    path = Path(path)
    for parent in (path, *path.parents):
        if parent.is_symlink():
            raise DockerError(f"Refusing managed symlink: {parent}")
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
        raise DockerError("Refusing untrusted service policy recovery state.")
    ready = POLICY_STATE / "ready"
    if not ready.exists():
        # The live policy is untouched until the backup is complete.
        shutil.rmtree(POLICY_STATE)
        return
    if POLICY.exists() or POLICY.is_symlink():
        original_matches = (backup.is_file() and not backup.is_symlink() and not POLICY.is_symlink() and
                            POLICY.read_bytes() == backup.read_bytes())
        symlink_matches = (backup.is_symlink() and POLICY.is_symlink() and
                           os.readlink(backup) == os.readlink(POLICY))
        guard_matches = not POLICY.is_symlink() and POLICY.read_bytes() == b"#!/bin/sh\nexit 101\n"
        if not (original_matches or symlink_matches or guard_matches):
            raise DockerError("Service policy changed outside APEX; recovery requires explicit reconciliation.")
    if backup.exists() or backup.is_symlink():
        # Copy first: an interrupted restoration must retain its recovery copy.
        temporary = POLICY_STATE / "restoring"
        if temporary.exists() or temporary.is_symlink():
            temporary.unlink()
        shutil.copy2(backup, temporary, follow_symlinks=False)
        os.replace(temporary, POLICY)
    elif (POLICY_STATE / "absent").exists():
        POLICY.unlink(missing_ok=True)
    else:
        raise DockerError("Service policy recovery is incomplete; inspect /usr/sbin/.apex-policy-rc.d.")
    ready.unlink()
    shutil.rmtree(POLICY_STATE)


@contextmanager
def package_policy_lock():
    lock_path = Path("/run/lock/apex-package-policy.lock")
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with lock_path.open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise DockerError("Another APEX package operation is running.") from None
        restore_service_policy()
        yield


@contextmanager
def service_start_guard():
    """Temporarily inhibit service starts while the caller holds the package lock."""
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
        raise DockerError("Missing packages require an upgrade/removal; reconcile packages explicitly before retrying.")
    # Let dpkg finish before restoring the service-start policy.
    run(command, timeout=None)
    remaining = set(missing) - package_versions().keys()
    if remaining:
        raise DockerError("Required packages not installed: " + ", ".join(sorted(remaining)))
    return missing


def docker_preflight(installed):
    conflicts = sorted(set(DOCKER_CONFLICTS) & installed.keys())
    if conflicts:
        raise DockerError("Conflicting Docker packages require explicit reconciliation: " + ", ".join(conflicts))
    path = Path("/etc/docker/daemon.json")
    try:
        config = json.loads(path.read_text()) if path.exists() else {}
    except (OSError, ValueError) as exc:
        raise DockerError("Cannot validate existing Docker daemon configuration.") from exc
    if not isinstance(config, dict):
        raise DockerError("Docker daemon configuration must be an object.")
    for name, value in DOCKER_CONFIG.items():
        if name in config and config[name] != value:
            raise DockerError(f"Conflicting Docker {name}; reconcile explicitly before initialization.")
    old_data = Path("/var/lib/docker")
    if old_data.exists() and any(old_data.iterdir()):
        raise DockerError("Existing /var/lib/docker data must be reconciled explicitly; no data migration is automatic.")
    data = Path("/srv/docker")
    if data.is_symlink() or data.parent.is_symlink():
        raise DockerError("Refusing a redirected Docker data-root.")
    if data.exists() and any(data.iterdir()) and config.get("data-root") != "/srv/docker":
        raise DockerError("Unmanaged /srv/docker data requires explicit reconciliation.")
    if "docker-ce" in installed:
        active = run(["systemctl", "is-active", "--quiet", "docker"], check=False).returncode == 0
        if active:
            root = run(["docker", "info", "--format", "{{.DockerRootDir}}"] ).stdout.strip()
            if root != "/srv/docker" or any(config.get(key) != value for key, value in DOCKER_CONFIG.items()):
                raise DockerError("Running Docker has incompatible data-root/log policy; reconcile it before retrying.")
    return {**config, **DOCKER_CONFIG}


DOCKER_KEY_SHA256 = "1500c1f56fa9e26b9b8f42452a553675796ade0807cdce11975eb98170b3a570"
LOCAL_DOCKER = ["docker", "--host", "unix:///var/run/docker.sock"]


def _docker_source(arch):
    key = Path("/etc/apt/keyrings/apex-docker.asc")
    source = Path("/etc/apt/sources.list.d/apex-docker.sources")
    content = ("Types: deb\nURIs: https://download.docker.com/linux/debian\nSuites: trixie\n"
               f"Components: stable\nArchitectures: {arch}\nSigned-By: {key}\n")
    for path in (key, source):
        if any(part.is_symlink() for part in (path, *path.parents)):
            raise DockerError(f"Refusing redirected APT path: {path}")
    if source.exists() and source.read_text() != content:
        raise DockerError(f"Reconcile existing Docker repository configuration: {source}")
    if key.exists():
        data = key.read_bytes()
    else:
        url = "https://download.docker.com/linux/debian/gpg"
        with urllib.request.urlopen(url, timeout=30) as response:  # noqa: S310
            data = response.read(1024 * 1024)
    if hashlib.sha256(data).hexdigest() != DOCKER_KEY_SHA256:
        raise DockerError("Docker repository signing key differs from the pinned release key.")
    write_managed(key, data)
    write_managed(source, content)


def _docker_capabilities(root, *, complete_plugins=False):
    capabilities = {"docker_data_root": root}
    plugins = {"docker-compose-plugin": ("compose", ["compose", "version", "--short"]),
               "docker-buildx-plugin": ("buildx", ["buildx", "version"])}
    missing = []
    for package, (name, args) in plugins.items():
        result = run([*LOCAL_DOCKER, *args], check=False)
        if result.returncode or not result.stdout.strip():
            missing.append(package)
        else:
            capabilities[name] = result.stdout.strip()
    if missing:
        if complete_plugins and "docker-ce" in package_versions():
            with service_start_guard():
                install_missing(missing)
            return _docker_capabilities(root)
        raise DockerError("Existing Docker needs functional plugins: " + ", ".join(missing))
    if not root:
        raise DockerError("Unable to verify the local Docker data-root.")
    return capabilities


@expected_errors
def apply_docker_policy():
    if Path("/usr/bin/pveversion").exists():
        raise DockerError("Docker installation on a Proxmox host requires separate administration.")
    release = dict(line.split("=", 1) for line in Path("/etc/os-release").read_text().splitlines()
                   if "=" in line and not line.startswith("#"))
    arch = run(["dpkg", "--print-architecture"]).stdout.strip()
    if (release.get("ID", "").strip('"') != "debian" or
            release.get("VERSION_ID", "").strip('"') != "13" or arch not in ("amd64", "arm64")):
        raise DockerError("Docker installation supports stock Debian 13 amd64 or arm64.")
    with package_policy_lock():
        return _apply_docker_policy(arch)


def _apply_docker_policy(arch):
    info = [*LOCAL_DOCKER, "info", "--format", "{{.DockerRootDir}}"]
    if shutil.which("docker"):
        current = run(info, check=False)
        if current.returncode == 0:
            return _docker_capabilities(current.stdout.strip(), complete_plugins=True)
    installed = package_versions()
    source = Path("/etc/apt/sources.list.d/apex-docker.sources")
    if (not source.exists() and
            (shutil.which("docker") or set(DOCKER_PACKAGES).intersection(installed))):
        raise DockerError("Existing Docker installation is inactive; reconcile it before retrying.")
    config = docker_preflight(installed)
    with service_start_guard():
        install_missing(("ca-certificates",))
        _docker_source(arch)
        write_managed("/etc/docker/daemon.json", json.dumps(config, indent=2) + "\n")
        install_missing(DOCKER_PACKAGES)
    ensure_service("containerd")
    ensure_service("docker")
    root = run(info).stdout.strip()
    if root != DOCKER_CONFIG["data-root"]:
        raise DockerError("Docker started with an unexpected data-root; inspect its configuration.")
    return _docker_capabilities(root)


def ensure_service(name):
    if run(["systemctl", "is-enabled", "--quiet", name], check=False).returncode:
        run(["systemctl", "enable", name])
    if run(["systemctl", "is-active", "--quiet", name], check=False).returncode:
        run(["systemctl", "start", name])


if __name__ == "__main__":
    from .state import ProvisionError, exclusive_lock

    try:
        if os.geteuid() != 0 or not Path("/etc/debian_version").is_file():
            raise DockerError("Docker setup requires root on Debian.")
        with exclusive_lock("/var/lib/apex-init/init.lock"):
            print(json.dumps(apply_docker_policy(), sort_keys=True))
    except (DockerError, OSError, ProvisionError) as exc:
        raise SystemExit(f"Docker setup failed: {exc}") from None
