# engine/lib/systemd.py
"""ctx.systemd — render + install unit files, daemon-reload, enable+start."""
from __future__ import annotations
import os


class Systemd:
    def __init__(self, log, sys_, tpl, host):
        self.log, self.sys, self.tpl, self.host = log, sys_, tpl, host

    def deploy_units(self, configs_subdir: str, vars: dict, dry_run: bool = False) -> None:
        rendered = self.tpl.render_dir(configs_subdir, vars)
        if not rendered:
            self.log.warn(f"No systemd units found under configs/{configs_subdir}.")
            return
        for rel, content in sorted(rendered.items()):
            name = os.path.basename(rel)
            self.host.write_file(f"/etc/systemd/system/{name}", content,
                                 backup=True, dry_run=dry_run, sudo=True)
            if name.endswith(".service") and not dry_run:
                self.sys.daemon_reload()
                self.sys.ensure_running(name[: -len(".service")])
                self.log.success(f"Service {name[:-8]} active and enabled.")
