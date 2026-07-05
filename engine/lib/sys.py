# engine/lib/sys.py
"""ctx.sys — command execution + systemd service management (port of root_sys.sh)."""
from __future__ import annotations
import shutil
import subprocess
import time
from typing import List, Optional, Sequence


class Sys:
    def __init__(self, log):
        self.log = log

    # --- command execution ---
    def run(self, cmd: Sequence[str], check: bool = True, capture: bool = False,
            cwd: Optional[str] = None, env: Optional[dict] = None,
            input: Optional[str] = None) -> subprocess.CompletedProcess:
        return subprocess.run(list(cmd), check=check, cwd=cwd, env=env, text=True,
                              input=input,
                              stdout=subprocess.PIPE if capture else None,
                              stderr=subprocess.PIPE if capture else None)

    def sudo(self, cmd: Sequence[str], **kw) -> subprocess.CompletedProcess:
        return self.run(["sudo", *cmd], **kw)

    def ok(self, cmd: Sequence[str], **kw) -> bool:
        """Run, return True on exit 0, never raising."""
        return self.run(cmd, check=False, **kw).returncode == 0

    # --- systemd (port of root_sys_*) ---
    def service_exists(self, name: str) -> bool:
        cp = self.run(["systemctl", "list-unit-files", "--type=service"],
                      check=False, capture=True)
        return any(line.startswith(f"{name}.service")
                   for line in (cp.stdout or "").splitlines())

    def service_is_active(self, name: str) -> bool:
        return self.ok(["systemctl", "is-active", "--quiet", name])

    def service_is_enabled(self, name: str) -> bool:
        return self.ok(["systemctl", "is-enabled", "--quiet", name])

    def start_and_enable(self, name: str) -> bool:
        if not self.service_exists(name):
            self.log.warn(f"Service {name} does not exist on this system")
            return False
        if not self.service_is_active(name):
            self.log.info(f"Starting {name} service...")
            self.sudo(["systemctl", "start", name])
        if not self.service_is_enabled(name):
            self.log.info(f"Enabling {name} service...")
            self.sudo(["systemctl", "enable", name])
        return self.service_is_active(name)

    def ensure_running(self, name: str) -> None:
        """Port of root_init_ensure_service_running: enable --now if not active."""
        if not self.service_is_active(name):
            self.log.info(f"Starting {name} service...")
            self.sudo(["systemctl", "enable", "--now", name])
        else:
            self.log.info(f"{name} service already running.")

    def restart(self, name: str) -> bool:
        self.log.info(f"Restarting {name} service...")
        self.sudo(["systemctl", "restart", name])
        return self.service_is_active(name)

    def reload(self, name: str, dry_run: bool = False) -> None:
        if dry_run:
            self.log.info(f"[DRY-RUN] systemctl reload {name}")
            return
        self.log.info(f"Reloading {name} configuration...")
        self.sudo(["systemctl", "reload", name])

    def daemon_reload(self) -> None:
        self.sudo(["systemctl", "daemon-reload"])

    def wait_for(self, name: str, timeout: int = 30, interval: int = 2) -> bool:
        elapsed = 0
        while elapsed < timeout:
            if self.service_is_active(name):
                return True
            time.sleep(interval)
            elapsed += interval
        return False

    def install_packages(self, packages: List[str]) -> None:
        """Port of root_init_install_packages (install if the binary is absent)."""
        for pkg in packages:
            if shutil.which(pkg):
                self.log.info(f"{pkg} already installed.")
                continue
            self.log.info(f"Installing {pkg}...")
            self.sudo(["apt-get", "update", "-y"])
            self.sudo(["apt-get", "install", "-y", pkg])
