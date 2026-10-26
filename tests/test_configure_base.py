"""Local baseline policy: fake only package, network, and root privilege boundaries."""
import hashlib
import io
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock, patch

from engine import overlay
from engine.lib.host import Host
from engine.lib.sys import Sys

base = overlay.load_module(
    str(Path(__file__).resolve().parents[1] / "actions/configure/base.py"),
    "apex_test_configure_base",
)


class TestBasePackages(unittest.TestCase):
    def setUp(self):
        self.system = Mock()
        self.installed = set(base.BASE_PACKAGES)
        self.system.run.side_effect = self.query
        self.system.sudo.return_value = subprocess.CompletedProcess([], 0, stdout="")

    def query(self, command, **kwargs):
        self.assertEqual(command[:3], ["dpkg-query", "-W", "-f=${Status}"])
        status = "install ok installed" if command[-1] in self.installed else "unknown ok not-installed"
        return subprocess.CompletedProcess(command, 0, stdout=status)

    def test_installed_rsyslog_and_ufw_do_not_require_executable_or_apt_update(self):
        base._install_missing(self.system, base.BASE_PACKAGES)
        self.assertEqual({call.args[0][-1] for call in self.system.run.call_args_list},
                         {"curl", "wget", "git", "rsyslog", "ufw"})
        self.system.sudo.assert_not_called()

    def test_only_missing_packages_are_installed_after_safe_simulation(self):
        self.installed.remove("ufw")
        self.system.sudo.return_value = subprocess.CompletedProcess(
            [], 0, stdout="Inst ufw (0.36.2 Debian:13 [all])\nConf ufw (0.36.2 Debian:13 [all])\n",
        )
        base._install_missing(self.system, base.BASE_PACKAGES)
        commands = [call.args[0] for call in self.system.sudo.call_args_list]
        self.assertEqual(len(commands), 3)
        self.assertIn("update", commands[0])
        self.assertEqual(commands[1], [*commands[2], "--simulate"])
        install = commands[2]
        self.assertEqual(install[install.index("install"):], [
            "install", "-y", "--no-install-recommends", "--no-upgrade", "--no-remove", "ufw",
        ])

    def test_privileged_apt_environment_survives_sudo_filtering(self):
        execute = subprocess.run
        operations = []

        def filtered_sudo(command, **kwargs):
            if command[0] == "dpkg-query":
                return subprocess.CompletedProcess(command, 1, stdout="")
            self.assertEqual(command[0], "sudo")
            incoming = kwargs.get("env") or os.environ
            filtered = {"PATH": os.defpath}
            if "LC_ALL" in incoming:
                filtered["LC_ALL"] = incoming["LC_ALL"]
            apt_index = command.index("apt-get")
            probe = execute([
                *command[1:apt_index], sys.executable, "-I", "-c",
                "import json, os; print(json.dumps([os.getenv('LC_ALL'), "
                "os.getenv('DEBIAN_FRONTEND')]))",
            ], env=filtered, check=True, text=True, capture_output=True)
            self.assertEqual(json.loads(probe.stdout), ["C", "noninteractive"])
            operations.append(command[apt_index:])
            plan = "Inst ufw (0.36.2 Debian:13 [all])\n" if "--simulate" in command else ""
            return subprocess.CompletedProcess(command, 0, stdout=plan)

        with patch("engine.lib.sys.subprocess.run", side_effect=filtered_sudo):
            base._install_missing(Sys(Mock()), ["ufw"])
        self.assertEqual(len(operations), 3)
        self.assertIn("update", operations[0])
        self.assertIn("--simulate", operations[1])
        self.assertIn("install", operations[2])
        self.assertNotIn("--simulate", operations[2])

    def test_dependency_upgrades_and_removals_abort_before_installation(self):
        self.installed.remove("curl")
        for plan in ("Inst libcurl4 [7.0] (8.0 Debian:13 [amd64])\n",
                     "Remv libcurl4 [7.0]\n"):
            with self.subTest(plan=plan):
                self.system.sudo.reset_mock()
                self.system.sudo.return_value = subprocess.CompletedProcess([], 0, stdout=plan)
                with self.assertRaisesRegex(RuntimeError, "upgrades/removals"):
                    base._install_missing(self.system, base.BASE_PACKAGES)
                self.assertEqual(self.system.sudo.call_count, 2)

    def test_failed_package_query_is_not_treated_as_installed(self):
        self.system.run.return_value = subprocess.CompletedProcess([], 1, stdout="")
        self.system.run.side_effect = None
        base._install_missing(self.system, ["rsyslog"])
        self.assertEqual(self.system.sudo.call_args.args[0][-1], "rsyslog")


class TestPinnedHelper(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.path = self.root / "bin" / "ufw-docker"
        self.path.parent.mkdir()
        self.payload = b"#!/bin/sh\nprintf 'verified fixture\\n'\n"
        self.wrong_owner = None
        self.wrong_group = None
        self.system = Mock()
        self.system.sudo.side_effect = self.sudo
        self.ctx = SimpleNamespace(log=Mock(), sys=self.system)
        self.ctx.host = Host(self.ctx.log, self.system)
        real_lstat = Path.lstat

        def root_lstat(path, *args, **kwargs):
            info = list(real_lstat(path, *args, **kwargs))
            info[4] = 1001 if path == self.wrong_owner else 0
            info[5] = 1001 if path == self.wrong_group else 0
            if path not in (self.path, self.path.parent, self.root):
                info[0] &= ~0o022
            return os.stat_result(info)

        # Model root-owned ancestry without sudo/chown; content, modes and links stay real.
        self.addCleanup(patch.stopall)
        patch.object(Path, "lstat", autospec=True, side_effect=root_lstat).start()
        patch.object(base, "UFW_DOCKER_PATH", self.path).start()
        patch.object(base, "UFW_DOCKER_SHA256", hashlib.sha256(self.payload).hexdigest()).start()
        self.download = patch.object(base.urllib.request, "urlopen").start()
        self.download.return_value = io.BytesIO(self.payload)

    def sudo(self, command, **kwargs):
        if command[:3] == ["python3", "-I", "-c"]:
            return subprocess.run([sys.executable, *command[1:]], check=True, text=True, **kwargs)
        self.assertEqual(command, [str(self.path), "install"])
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o755)
        return subprocess.CompletedProcess(command, 0)

    def write_helper(self, payload=None, mode=0o755):
        self.path.write_bytes(self.payload if payload is None else payload)
        self.path.chmod(mode)

    def test_verified_existing_helper_executes_without_download(self):
        self.write_helper()
        base._install_helper(self.ctx)
        self.download.assert_not_called()
        self.assertEqual(self.system.sudo.call_args.args[0], [str(self.path), "install"])
        self.assertEqual(self.system.sudo.call_count, 1)

    def test_wrong_executable_content_is_replaced_only_with_verified_download(self):
        self.write_helper(b"#!/bin/sh\necho wrong\n")
        base._install_helper(self.ctx)
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(self.system.sudo.call_count, 2)
        self.download.assert_called_once_with(base.UFW_DOCKER_URL, timeout=30)

    def test_missing_helper_is_published_with_executable_mode(self):
        base._install_helper(self.ctx)
        self.assertEqual(self.path.read_bytes(), self.payload)
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o755)
        self.assertEqual(list(self.path.parent.iterdir()), [self.path])

    def test_verified_nonexecutable_content_is_reused_and_mode_repaired(self):
        self.write_helper(mode=0o600)
        base._install_helper(self.ctx)
        self.download.assert_not_called()
        self.assertEqual(stat.S_IMODE(self.path.stat().st_mode), 0o755)
        self.assertEqual(self.system.sudo.call_count, 2)

    def test_bad_checksum_never_publishes_or_executes(self):
        self.download.return_value = io.BytesIO(b"not the pinned helper")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            base._install_helper(self.ctx)
        self.system.sudo.assert_not_called()
        self.assertFalse(self.path.exists())

    def test_bad_download_preserves_existing_wrong_helper_without_executing_it(self):
        self.write_helper(b"old unknown executable")
        self.download.return_value = io.BytesIO(b"bad download")
        with self.assertRaisesRegex(RuntimeError, "checksum mismatch"):
            base._install_helper(self.ctx)
        self.system.sudo.assert_not_called()
        self.assertEqual(self.path.read_bytes(), b"old unknown executable")

    def assert_rejected_before_network_or_sudo(self):
        with self.assertRaisesRegex(RuntimeError, "Unsafe ufw-docker"):
            base._install_helper(self.ctx)
        self.download.assert_not_called()
        self.system.sudo.assert_not_called()

    def test_symlink_target_is_rejected(self):
        target = self.root / "untrusted"
        target.write_bytes(self.payload)
        self.path.symlink_to(target)
        self.assert_rejected_before_network_or_sudo()

    def test_dangling_symlink_target_is_rejected(self):
        self.path.symlink_to(self.root / "missing")
        self.assert_rejected_before_network_or_sudo()

    def test_symlink_ancestor_is_rejected(self):
        self.path.parent.rmdir()
        target = self.root / "real-bin"
        target.mkdir()
        self.path.parent.symlink_to(target)
        self.assert_rejected_before_network_or_sudo()

    def test_writable_ancestor_is_rejected(self):
        self.path.parent.chmod(0o777)
        self.assert_rejected_before_network_or_sudo()

    def test_unsafe_helper_preflight_precedes_package_network_or_mutation(self):
        self.path.parent.chmod(0o777)
        with patch.object(base.os.path, "isfile", return_value=True):
            with self.assertRaisesRegex(RuntimeError, "Unsafe ufw-docker"):
                base.run(self.ctx, None)
        self.download.assert_not_called()
        self.system.run.assert_not_called()
        self.system.sudo.assert_not_called()

    def test_untrusted_owner_ancestor_is_rejected(self):
        self.wrong_owner = self.path.parent
        self.assert_rejected_before_network_or_sudo()

    def test_untrusted_file_owner_is_rejected(self):
        self.write_helper()
        self.wrong_owner = self.path
        self.assert_rejected_before_network_or_sudo()

    def test_untrusted_file_group_is_rejected(self):
        self.write_helper()
        self.wrong_group = self.path
        self.assert_rejected_before_network_or_sudo()

    def test_writable_helper_is_rejected(self):
        self.write_helper(mode=0o775)
        self.assert_rejected_before_network_or_sudo()

    def test_hardlinked_helper_is_rejected(self):
        self.write_helper()
        os.link(self.path, self.root / "other-link")
        self.assert_rejected_before_network_or_sudo()

    def test_nonregular_helper_is_rejected(self):
        self.path.mkdir()
        self.assert_rejected_before_network_or_sudo()

    def test_path_changed_during_download_is_rejected_before_publication(self):
        def download(*args, **kwargs):
            self.path.symlink_to(self.root / "untrusted")
            return io.BytesIO(self.payload)

        self.download.side_effect = download
        with self.assertRaisesRegex(RuntimeError, "Unsafe ufw-docker"):
            base._install_helper(self.ctx)
        self.system.sudo.assert_not_called()

    def test_changed_content_after_publication_is_not_executed(self):
        real_sudo = self.sudo

        def tamper(command, **kwargs):
            result = real_sudo(command, **kwargs)
            self.path.write_bytes(b"changed after publication")
            return result

        self.system.sudo.side_effect = tamper
        with self.assertRaisesRegex(RuntimeError, "verification failed"):
            base._install_helper(self.ctx)
        self.assertEqual(self.system.sudo.call_count, 1)


if __name__ == "__main__":
    unittest.main()
