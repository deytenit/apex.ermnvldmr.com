# engine/lib/crowdsec.py
"""ctx.crowdsec — install + config deploy + collections (port of configure/crowdsec)."""
from __future__ import annotations
import os
import shutil


class Crowdsec:
    def __init__(self, log, sys_, tpl, host):
        self.log, self.sys, self.tpl, self.host = log, sys_, tpl, host

    def ensure_installed(self) -> None:
        if shutil.which("cscli"):
            return
        self.log.info("Installing CrowdSec via official script...")
        self.sys.run(["bash", "-c", "set -o pipefail; curl -s https://install.crowdsec.net | sudo sh"],
                     check=True)
        self.sys.sudo(["apt-get", "install", "-y", "crowdsec"])

    def deploy(self, configs_subdir: str, vars: dict, dry_run: bool = False) -> None:
        for rel, content in self.tpl.render_dir(configs_subdir, vars).items():
            self.host.write_file(os.path.join("/etc/crowdsec", rel), content,
                                 backup=True, dry_run=dry_run, sudo=True)

    def install_collections(self, collections) -> None:
        self.sys.sudo(["cscli", "hub", "update"])
        for c in collections:
            self.sys.sudo(["cscli", "collections", "install", c])
