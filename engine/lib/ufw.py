# engine/lib/ufw.py
"""ctx.ufw — ufw + ufw-docker rule application with self-healing marker migration.

Injects the APEX marker block into before/after.rules, deleting BOTH the old ROOT
and the new APEX blocks first (so re-running after cutover migrates the marker in
place — SP2 §6). Docker rules go through ufw-docker; host rules through plain ufw.
"""
from __future__ import annotations
import os

APEX_START = "# START APEX.ERMNVLDMR.COM RULES"
APEX_END = "# END APEX.ERMNVLDMR.COM RULES"
OLD_PAIRS = [("# START ROOT.ERMNVLDMR.COM RULES", "# END ROOT.ERMNVLDMR.COM RULES")]
UFW_DOCKER_BIN = os.path.expanduser("~/.local/bin/ufw-docker")


def clean_rules(text: str):
    """Strip whitespace, drop blank + comment lines (matches the bash awk filter)."""
    return [s for s in (ln.strip() for ln in text.splitlines()) if s and not s.startswith("#")]


class Ufw:
    def __init__(self, log, sys_, host):
        self.log, self.sys, self.host = log, sys_, host

    def _inject_file(self, target: str, source_file: str, dry_run: bool):
        if not os.path.isfile(source_file):
            return
        if not self.sys.ok(["sudo", "test", "-f", target]):
            self.log.error(f"Target file does not exist: {target}")
            raise SystemExit(1)
        content = "\n".join(clean_rules(open(source_file).read()))
        self.host.inject_block(target, content, APEX_START, APEX_END,
                               anchor="# End required lines", old_pairs=OLD_PAIRS,
                               dry_run=dry_run, sudo=True)

    def apply(self, rules_dir: str, config_dir=None, dry_run: bool = False) -> None:
        # EX_NOINPUT guards first, tools second — same order/codes as root_template_ufw.
        if not os.path.isdir(rules_dir):
            self.log.error(f"Rules directory does not exist: {rules_dir}")
            raise SystemExit(66)
        if config_dir and not os.path.isdir(config_dir):
            self.log.error(f"Config directory does not exist: {config_dir}")
            raise SystemExit(66)
        if not self.sys.ok(["sudo", "ufw", "--version"]):
            self.log.error("ufw not installed/working via sudo. Run configure/base first.")
            raise SystemExit(1)
        if not (os.path.isfile(UFW_DOCKER_BIN) and os.access(UFW_DOCKER_BIN, os.X_OK)):
            self.log.error(f"ufw-docker not found at {UFW_DOCKER_BIN}. Run configure/base first.")
            raise SystemExit(1)

        if config_dir:
            host_rules = os.path.join(config_dir, "host.rules")
            if os.path.isfile(host_rules):
                for rule in clean_rules(open(host_rules).read()):
                    self.log.info(f"Applying host ufw rule: {rule}")
                    if dry_run:
                        self.log.info(f"[DRY-RUN] sudo ufw {rule}")
                    else:
                        self.sys.run(["bash", "-c", f"sudo ufw {rule}"], check=True)
            self._inject_file("/etc/ufw/before.rules", os.path.join(config_dir, "before.rules"), dry_run)
            self._inject_file("/etc/ufw/after.rules", os.path.join(config_dir, "after.rules"), dry_run)

        rule_files = [fn for fn in sorted(os.listdir(rules_dir))
                      if fn.endswith(".rules") and not fn.startswith(".")]
        if not rule_files:
            self.log.warn(f"No .rules files found in {rules_dir}, nothing to apply.")
        for fn in rule_files:
            for rule in clean_rules(open(os.path.join(rules_dir, fn)).read()):
                self.log.info(f"Applying ufw-docker rule ({fn}): {rule}")
                if dry_run:
                    self.log.info(f"[DRY-RUN] sudo {UFW_DOCKER_BIN} {rule}")
                else:
                    self.sys.run(["sudo", UFW_DOCKER_BIN, *rule.split()], check=True)

        self.log.success("All ufw rules applied.")
        self.log.warn("Run `sudo systemctl restart ufw` to apply. Ensure ssh is allowed via plain ufw!")
