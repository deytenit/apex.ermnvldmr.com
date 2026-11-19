# scripts/engine/lib/ufw.py
"""Apply ufw and ufw-docker rules.

Replace existing supported marker blocks before inserting current rules.
Docker rules go through ufw-docker; host rules through plain ufw.
"""
from __future__ import annotations

import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil

APEX_START = "# START APEX.ERMNVLDMR.COM RULES"
APEX_END = "# END APEX.ERMNVLDMR.COM RULES"
OLD_PAIRS = [("# START ROOT.ERMNVLDMR.COM RULES", "# END ROOT.ERMNVLDMR.COM RULES")]


def resolve_ufw_docker_bin() -> str:
    """Find ufw-docker in PATH, /usr/local/bin, or fallback to ~/.local/bin/ufw-docker."""
    found = shutil.which("ufw-docker")
    if found:
        return found
    if os.path.isfile("/usr/local/bin/ufw-docker") and os.access("/usr/local/bin/ufw-docker", os.X_OK):
        return "/usr/local/bin/ufw-docker"
    return os.path.expanduser("~/.local/bin/ufw-docker")


UFW_DOCKER_BIN = resolve_ufw_docker_bin()
PINNED_HELPER = Path("/usr/local/bin/ufw-docker")


def clean_rules(text: str):
    """Strip whitespace, drop blank + comment lines (matches the bash awk filter)."""
    return [s for s in (ln.strip() for ln in text.splitlines()) if s and not s.startswith("#")]


def docker_rules(directory):
    for path in sorted(Path(directory).glob("*.rules")):
        if not path.name.startswith("."):
            for line in clean_rules(path.read_text(encoding="utf-8")):
                yield shlex.split(line)


def _expected_routes(containers, rules):
    expected = {}
    for _, name, *options in rules:
        container = containers[name]
        settings = container["NetworkSettings"]
        ports = {port for port, bindings in (settings["Ports"] or {}).items() if bindings}
        if options:
            port = options[0] if "/" in options[0] else options[0] + "/tcp"
            ports &= {port}
        networks = settings["Networks"]
        selected = [options[1]] if len(options) == 2 else networks
        if not container["State"]["Running"] or not selected or not ports:
            raise RuntimeError(f"Missing running container, network or published port for {' '.join([name, *options])}.")
        for network in selected:
            endpoint = networks.get(network) or {}
            addresses = {str(ipaddress.ip_address(endpoint[key])) for key in ("IPAddress", "GlobalIPv6Address")
                         if endpoint.get(key)}
            if not addresses:
                raise RuntimeError(f"No address for {name} on {network}.")
            target = (container["Name"].removeprefix("/"), network)
            expected.setdefault(target, set()).update((port, ip) for port in ports for ip in addresses)
    return expected


# The pinned helper writes a numbered, unrestricted forward rule with this exact comment format.
_HELPER_RULE = re.compile(
    r"^\[\s*\d+\]\s+(\S+)\s+(\d+/(?:tcp|udp))\s+ALLOW FWD\s+Anywhere(?: \(v6\))?\s+"
    r"# allow ([-\w.]+)(/v6)? (\d+/(?:tcp|udp)) ([-\w.]+)$"
)


def _check_routes(status, expected, require_all):
    remaining = {key: routes.copy() for key, routes in expected.items()}
    for line in status.splitlines():
        match = _HELPER_RULE.fullmatch(line.rstrip())
        if not match:
            continue
        address, live_port, container, v6, port, network = match.groups()
        target = (container, network)
        if target not in expected:
            continue
        address = ipaddress.ip_address(address)
        route = (port, str(address))
        if live_port != port or bool(v6) != (address.version == 6) or route not in expected[target]:
            raise RuntimeError(f"Review stale ufw-docker rule before reconciling: {line.strip()}")
        remaining[target].discard(route)
    if require_all:
        for (container, network), routes in remaining.items():
            if routes:
                raise RuntimeError(f"Required ufw-docker rules missing for {container} on {network}: {sorted(routes)}")


class Ufw:
    def __init__(self, log, sys_, host):
        self.log, self.sys, self.host = log, sys_, host

    def _inject_file(self, target: str, source_file: str, dry_run: bool):
        if not os.path.isfile(source_file):
            return
        if not self.sys.ok(["sudo", "test", "-f", target]):
            self.log.error(f"Target file does not exist: {target}")
            raise SystemExit(1)
        with open(source_file, encoding="utf-8") as f:
            content = "\n".join(clean_rules(f.read()))
        injected = self.host.inject_block(
            target,
            content,
            APEX_START,
            APEX_END,
            anchor="# End required lines",
            old_pairs=OLD_PAIRS,
            dry_run=dry_run,
            sudo=True,
        )
        if not injected:
            raise RuntimeError(f"Unable to install host firewall rules in {target}.")

    def is_active(self):
        result = self.sys.sudo(["env", "LC_ALL=C", "ufw", "status"], capture=True)
        status = result.stdout.partition("\n")[0].strip()
        if status not in ("Status: active", "Status: inactive"):
            raise RuntimeError("Cannot determine UFW status.")
        return status == "Status: active"

    def reconcile(self, directory, dry_run=False):
        rules = list(docker_rules(directory))
        for rule in rules:
            if not (2 <= len(rule) <= 4 and rule[0] == "allow" and
                    re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]*", rule[1]) and
                    (len(rule) < 3 or re.fullmatch(r"[0-9]+(/(tcp|udp))?", rule[2]))):
                raise ValueError(f"Expected 'allow CONTAINER [PORT[/PROTO] [NETWORK]]', found {shlex.join(rule)}.")
        if dry_run:
            for rule in rules:
                self.log.info(f"[DRY-RUN] sudo {PINNED_HELPER} {shlex.join(rule)}")
            return

        def numbered_status():
            output = self.sys.sudo(["env", "LC_ALL=C", "ufw", "status", "numbered"], capture=True).stdout
            if output.partition("\n")[0].strip() != "Status: active":
                raise RuntimeError("UFW is inactive or its status is unknown. Run config/ufw --host-only.")
            return output

        status = numbered_status()
        if not rules:
            self.log.warn(f"No container allow rules declared in {directory}.")
            return
        names = sorted({rule[1] for rule in rules})
        result = self.sys.sudo(["docker", "container", "inspect", *names], capture=True)
        containers = dict(zip(names, json.loads(result.stdout), strict=True))
        expected = _expected_routes(containers, rules)
        _check_routes(status, expected, require_all=False)
        for rule in rules:
            self.sys.sudo([str(PINNED_HELPER), *rule], check=True)
        _check_routes(numbered_status(), expected, require_all=True)
        self.log.success("Verified required container firewall rules.")

    def _ssh_rule(self, rules):
        """Allow reconnecting to the current SSH endpoint before imposing host defaults."""
        connection = os.environ.get("SSH_CONNECTION")
        if connection:
            peer, _, local, port = connection.split()
            peer, local = str(ipaddress.ip_address(peer)), str(ipaddress.ip_address(local))
            if not port.isdecimal() or not 1 <= int(port) <= 65535:
                raise ValueError("Invalid SSH_CONNECTION server port.")
            ports = [port]
        else:
            effective = self.sys.sudo(["/usr/sbin/sshd", "-T"], capture=True).stdout
            ports = [line.split()[1] for line in effective.splitlines() if line.lower().startswith("port ")]
        selectors = set(ports) | {f"{p}/tcp" for p in ports}
        if "22" in ports:
            selectors.add("OpenSSH")
        for rule in rules:
            if len(rule) >= 2 and rule[0] in ("allow", "limit") and rule[1] in selectors:
                return rule
        if not connection:
            raise RuntimeError("Allow a listening SSH port in host.rules or run from the original SSH session.")
        self.log.warn("Adding a source-specific APEX SSH handover rule. Remove it after verifying the new login.")
        return ["prepend", "allow", "proto", "tcp", "from", peer, "to", local, "port", port,
                "comment", "APEX SSH handover"]

    def apply(self, rules_dir: str | None = None, config_dir: str | None = None,
              dry_run: bool = False, activate: bool = False) -> None:
        if rules_dir and not os.path.isdir(rules_dir):
            self.log.error(f"Rules directory does not exist: {rules_dir}")
            raise SystemExit(66)
        if config_dir and not os.path.isdir(config_dir):
            self.log.error(f"Config directory does not exist: {config_dir}")
            raise SystemExit(66)
        if not rules_dir and not config_dir:
            self.log.warn("Neither docker nor host ufw rules directory found; nothing to apply.")
            return
        if not self.sys.ok(["sudo", "ufw", "--version"]):
            self.log.error("ufw not installed/working via sudo. Run config/packages first.")
            raise SystemExit(1)
        docker_bin = UFW_DOCKER_BIN
        if rules_dir and not dry_run and not (os.path.isfile(docker_bin) and os.access(docker_bin, os.X_OK)):
            self.log.error(f"ufw-docker not found at {docker_bin}. Run config/ufw-docker first.")
            raise SystemExit(1)

        if config_dir:
            host_rules = os.path.join(config_dir, "host.rules")
            rules = []
            if os.path.isfile(host_rules):
                with open(host_rules, encoding="utf-8") as f:
                    rules = [shlex.split(line) for line in clean_rules(f.read())]
            if activate:
                if not rules:
                    raise RuntimeError(f"Cannot activate UFW without rules in {host_rules}.")
                ssh_rule = self._ssh_rule(rules)
                rules = [ssh_rule, *(rule for rule in rules if rule != ssh_rule)]
            for rule in rules:
                self.log.info(f"Applying host ufw rule: {shlex.join(rule)}")
                if not dry_run:
                    self.sys.sudo(["ufw", *rule], check=True)
            self._inject_file("/etc/ufw/before.rules", os.path.join(config_dir, "before.rules"), dry_run)
            self._inject_file("/etc/ufw/after.rules", os.path.join(config_dir, "after.rules"), dry_run)

        if activate:
            if dry_run:
                self.log.info("[DRY-RUN] enable or reload UFW after applying host rules.")
            else:
                if self.is_active():
                    self.sys.sudo(["ufw", "reload"])
                else:
                    self.sys.sudo(["ufw", "--force", "enable"])
                if not self.is_active():
                    raise RuntimeError("UFW is inactive after applying host rules.")

        if rules_dir:
            for rule in docker_rules(rules_dir):
                self.log.info(f"Applying ufw-docker rule: {shlex.join(rule)}")
                if not dry_run:
                    self.sys.sudo([docker_bin, *rule], check=True)

        self.log.success("All ufw rules applied.")
        if config_dir and not activate:
            self.log.warn("Reload UFW to activate host rule-file changes after checking SSH access.")
