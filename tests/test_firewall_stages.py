import json
import os
from pathlib import Path
import subprocess
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine import overlay
from engine.descriptor import build_parser
from engine.lib.ufw import Ufw
from engine.lib.sys import Sys


def action(name):
    path = Path(__file__).resolve().parents[1] / "actions" / "config" / f"{name}.py"
    return overlay.load_module(str(path), f"test_{name}")


class FirewallStages(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = Path(temp.name)
        self.host = self.cfg / "ufw/host"
        self.host.mkdir(parents=True)
        (self.host / "host.rules").write_text("default deny incoming\nallow 2222/tcp\n")
        self.docker = self.cfg / "ufw/docker"
        self.docker.mkdir()
        (self.docker / "web.rules").write_text("allow web 443/tcp direct\n")
        self.ctx = Mock()
        self.ctx.paths.configs = str(self.cfg)
        self.ctx.ufw = Ufw(self.ctx.log, self.ctx.sys, self.ctx.host)
        self.commands = []
        self.active = False

        def sudo(cmd, **kwargs):
            self.commands.append(cmd)
            if cmd == ["ufw", "--force", "enable"]:
                self.active = True
            return SimpleNamespace(stdout="Status: active\n" if self.active else "Status: inactive\n")
        self.ctx.sys.sudo.side_effect = sudo

    def apply(self, *flags):
        module = action("ufw")
        return module.run(self.ctx, build_parser(module.METADATA, "ufw").parse_args(flags))

    def test_host_activation_preserves_current_ssh_before_defaults_and_enable(self):
        with patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.9 49424 192.0.2.1 22"}):
            self.apply("--host-only")
        self.assertLess(self.commands.index(["ufw", "prepend", "allow", "proto", "tcp",
                                             "from", "192.0.2.9", "to", "192.0.2.1", "port", "22",
                                             "comment", "APEX SSH handover"]),
                        self.commands.index(["ufw", "default", "deny", "incoming"]))
        self.assertLess(self.commands.index(["ufw", "allow", "2222/tcp"]),
                        self.commands.index(["ufw", "--force", "enable"]))
        self.assertTrue(self.active)

    def test_declared_ssh_rule_is_staged_first_and_active_firewall_reloaded(self):
        self.active = True
        with patch.dict(os.environ, {"SSH_CONNECTION": "2001:db8::1 49424 2001:db8::2 2222"}):
            self.apply("--host-only")
        self.assertEqual(self.commands[0], ["ufw", "allow", "2222/tcp"])
        self.assertIn(["ufw", "reload"], self.commands)
        self.assertNotIn(["ufw", "--force", "enable"], self.commands)
        self.assertFalse(any("APEX SSH handover" in command for command in self.commands))

    def test_console_without_allow_for_listening_ssh_port_fails_before_changes(self):
        self.ctx.sys.sudo.side_effect = lambda *args, **kwargs: SimpleNamespace(stdout="Port 22\n")
        with patch.dict(os.environ, {}, clear=True), self.assertRaisesRegex(RuntimeError, "listening SSH port"):
            self.apply("--host-only")
        self.ctx.sys.sudo.assert_called_once_with(["/usr/sbin/sshd", "-T"], capture=True)

    def test_dry_run_previews_both_stages_without_applying(self):
        with patch.dict(os.environ, {"SSH_CONNECTION": "192.0.2.9 49424 192.0.2.1 22"}):
            self.apply("--host-only", "--dry-run")
            self.apply("--docker-only", "--dry-run")
        self.assertFalse(self.commands)
        self.assertFalse(self.active)

    def test_missing_host_rules_cannot_activate(self):
        (self.host / "host.rules").unlink()
        with self.assertRaisesRegex(RuntimeError, "host.rules"):
            self.apply("--host-only")
        self.assertFalse(self.commands)

    def docker_stage(self, before, after=None, *, helper_failure=False):
        self.commands.clear()
        endpoints = {"IPAddress": "192.0.2.2", "GlobalIPv6Address": ""}
        container = {"Name": "/web", "State": {"Running": True},
                     "NetworkSettings": {"Networks": {"direct": endpoints}, "Ports": {"443/tcp": [{}]}}}
        statuses = iter([before, after if after is not None else before])
        def sudo(cmd, **kwargs):
            self.commands.append(cmd)
            if cmd[-2:] == ["status", "numbered"]:
                return SimpleNamespace(stdout=next(statuses))
            if "inspect" in cmd:
                return SimpleNamespace(stdout=json.dumps([container]))
            if helper_failure:
                raise subprocess.CalledProcessError(1, cmd)
            return SimpleNamespace(stdout="")
        self.ctx.sys.sudo.side_effect = sudo
        with patch("engine.lib.ufw.PINNED_HELPER", Path("/bin/sh")):
            self.apply("--docker-only")

    def test_inactive_container_stage_fails_without_calling_helper(self):
        with self.assertRaisesRegex(RuntimeError, "inactive"):
            self.docker_stage("Status: inactive\n")
        self.assertFalse(any(cmd[0] == "/bin/sh" for cmd in self.commands))

    def test_explicit_container_rule_failure_reaches_caller(self):
        with self.assertRaises(subprocess.CalledProcessError):
            self.docker_stage("Status: active\n", helper_failure=True)
        self.assertIn(["/bin/sh", "allow", "web", "443/tcp", "direct"], self.commands)

    def test_stale_helper_allow_for_declared_target_fails_before_mutation(self):
        for stale in ("192.0.2.2 80/tcp ALLOW FWD Anywhere # allow web 80/tcp direct",
                      "192.0.2.3 443/tcp ALLOW FWD Anywhere # allow web 443/tcp direct",
                      "2001:db8::3 443/tcp ALLOW FWD Anywhere (v6) # allow web/v6 443/tcp direct"):
            with self.subTest(stale=stale), self.assertRaisesRegex(RuntimeError, "stale"):
                self.docker_stage("Status: active\n[ 2] " + stale)
            self.assertFalse(any(cmd[0] == "/bin/sh" or "delete" in cmd for cmd in self.commands))

    def test_reconciles_required_rule_and_preserves_unrelated_and_user_permissions(self):
        before = ("Status: active\n"
                  "[ 1] 192.0.2.6 80/tcp ALLOW FWD Anywhere # allow web-admin 80/tcp direct\n"
                  "[ 2] 192.0.2.4 80/tcp ALLOW FWD Anywhere # allow web 80/tcp other\n"
                  "[ 3] 192.0.2.2 80/tcp ALLOW FWD Anywhere # operator exception\n")
        wanted = "[ 4] 192.0.2.2 443/tcp ALLOW FWD Anywhere # allow web 443/tcp direct\n"
        self.docker_stage(before, before + wanted)
        self.assertIn(["/bin/sh", "allow", "web", "443/tcp", "direct"], self.commands)
        self.assertFalse(any("delete" in cmd for cmd in self.commands))

    def test_missing_live_rule_after_helper_success_fails(self):
        with self.assertRaisesRegex(RuntimeError, "missing"):
            self.docker_stage("Status: active\n")

    def test_inactive_firewall_stops_helper_before_download_or_install(self):
        module = action("ufw-docker")
        ctx = Mock()
        ctx.ufw.is_active.return_value = False
        with (patch.object(module, "_check_helper_path"), patch.object(module, "_install_helper") as install,
              self.assertRaisesRegex(RuntimeError, "Activate UFW")):
            module.run(ctx, None)
        install.assert_not_called()
        ctx.sys.sudo.assert_not_called()

    def test_helper_finds_privileged_commands_with_an_empty_operator_path(self):
        module = action("ufw-docker")
        ctx = Mock()
        ctx.sys = Sys(ctx.log)
        ctx.ufw.is_active.return_value = True
        commands = []

        admin = self.cfg / "admin-bin"
        admin.mkdir()
        for binary in ("ufw", "docker"):
            executable = admin / binary
            executable.touch(mode=0o755)
        def privileged(cmd, **kwargs):
            if cmd[0] == "/bin/sh":
                return subprocess.run(cmd, env={"PATH": str(admin)}, capture_output=True, check=False)
            commands.append(cmd)
            return SimpleNamespace(stdout="")
        ctx.node = None
        with (patch.dict(os.environ, PATH=""), patch.object(ctx.sys, "sudo", side_effect=privileged),
              patch.object(module, "_install_helper") as install, patch.object(module, "_check_helper_path")):
            module.run(ctx, None)
        install.assert_called_once()
        self.assertIn(["ufw", "reload"], commands)

    def test_global_helper_with_no_node_identity_keeps_private_ranges(self):
        module = action("ufw-docker")
        ctx = Mock()
        ctx.node = None
        ctx.sys.sudo.return_value = SimpleNamespace(stdout="")
        self.assertEqual(set(module._subnets(ctx)), {"10.0.0.0/8", "172.16.0.0/12",
                                                    "192.168.0.0/16", "fd00::/8"})

    def test_helper_uses_baseline_direct_and_current_networks_then_reloads(self):
        module = action("ufw-docker")
        ctx = Mock()
        ctx.node.subnet = "203.0.113.0/24"
        ctx.ufw.is_active.return_value = True
        ctx.sys.sudo.return_value = SimpleNamespace(stdout="")
        ctx.sys.sudo.side_effect = [
            SimpleNamespace(stdout="first\nsecond\n"),
            SimpleNamespace(stdout='[{"IPAM":{"Config":[{"Subnet":"198.51.100.0/24"}]}},{"IPAM":{"Config":null}}]'),
            None,
        ]
        with patch.object(module, "_check_helper_path"), patch.object(module, "_install_helper") as install:
            module.run(ctx, None)
        self.assertEqual(set(install.call_args.args[1]), {"10.0.0.0/8", "172.16.0.0/12", "192.168.0.0/16",
                                           "fd00::/8", "203.0.113.0/24", "198.51.100.0/24"})
        ctx.sys.sudo.assert_called_with(["ufw", "reload"])
