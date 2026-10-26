# tests/test_host.py
import inspect
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

from engine.lib.host import Host, apply_block


def _check_backup_chmod(chmod, expected_group):
    import os
    import stat

    def checked(path, mode, *args, **kwargs):
        result = chmod(path, mode, *args, **kwargs)
        info = os.stat(path)
        with open(path, "rb") as stream:
            content = stream.read()
        if content and stat.S_IMODE(info.st_mode) & 0o077 and info.st_gid != expected_group:
            raise AssertionError("Backup content exposed to the inherited staging group")
        return result

    return checked


class TestHostWrites(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name).resolve()
        self.log = Mock()
        self.host = Host(self.log, Mock())

    def test_existing_private_modes_and_owner_survive_replacement(self):
        for mode in (0o600, 0o640):
            with self.subTest(mode=mode):
                path = self.root / "secret"
                path.write_text("old")
                path.chmod(mode)
                previous = path.stat()
                self.host.write_file(str(path), "new", backup=False)
                self.assertEqual(path.read_text(), "new")
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), mode)
                self.assertEqual((path.stat().st_uid, path.stat().st_gid),
                                 (previous.st_uid, previous.st_gid))

    def test_new_basename_defaults_to_0644(self):
        previous = Path.cwd()
        try:
            os.chdir(self.root)
            self.host.write_file("settings", "value", backup=False)
        finally:
            os.chdir(previous)
        path = self.root / "settings"
        self.assertEqual(path.read_text(), "value")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o644)

    def test_existing_supplementary_group_survives_replacement(self):
        path = self.root / "group-owned"
        path.write_text("old")
        groups = set(os.getgroups()) - {path.stat().st_gid}
        if not groups:
            self.skipTest("No alternate supplementary group available")
        group = next(iter(groups))
        try:
            os.chown(path, -1, group)
        except PermissionError:
            self.skipTest("Filesystem does not permit changing to a supplementary group")
        self.host.write_file(str(path), "new", backup=False)
        self.assertEqual(path.read_text(), "new")
        self.assertEqual(path.stat().st_gid, group)

    def test_explicit_mode_is_applied_before_atomic_publication(self):
        path = self.root / "secret"
        path.write_text("old")
        replace = os.replace
        published = []

        def observe(source, destination):
            self.assertEqual(path.read_text(), "old")
            self.assertEqual(Path(source).parent, self.root)
            self.assertEqual(stat.S_IMODE(os.stat(source).st_mode), 0o600)
            self.assertEqual(Path(source).read_text(), "new secret")
            published.append(destination)
            replace(source, destination)

        with patch("os.replace", side_effect=observe):
            self.host.write_file(str(path), "new secret", mode="0600", backup=False)
        self.assertEqual(len(published), 1)
        self.assertEqual(path.read_text(), "new secret")

    def test_symlink_remains_and_target_receives_new_content(self):
        target = self.root / "tier" / "secret"
        target.parent.mkdir()
        target.write_text("old")
        target.chmod(0o600)
        link = self.root / ".env"
        link.symlink_to("tier/secret")
        self.host.write_file(str(link), "new", backup=False)
        self.assertTrue(link.is_symlink())
        self.assertEqual(os.readlink(link), "tier/secret")
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_dry_run_does_not_create_parents_or_backups(self):
        self.host.write_file(str(self.root / "missing" / "secret"), "new", dry_run=True)
        self.assertEqual(list(self.root.iterdir()), [])

    def test_backup_preserves_old_private_content(self):
        path = self.root / "secret"
        path.write_text("old")
        path.chmod(0o600)
        self.host.write_file(str(path), "new")
        backups = list(self.root.glob("secret.*.bak"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(), "old")
        self.assertEqual(stat.S_IMODE(backups[0].stat().st_mode), 0o600)
        self.assertEqual(path.read_text(), "new")

    def test_backup_sets_owner_before_exposing_group_readable_content(self):
        path = self.root / "group-secret"
        path.write_text("synthetic group-only secret")
        groups = set(os.getgroups()) - {path.stat().st_gid}
        if not groups:
            self.skipTest("No alternate supplementary group available")
        group = next(iter(groups))
        try:
            os.chown(path, -1, group)
        except PermissionError:
            self.skipTest("Filesystem does not permit changing to a supplementary group")
        path.chmod(0o640)
        os.utime(path, (1600000000, 1600000000))

        def local_sudo(command, **kwargs):
            self.assertEqual(command[:3], ["python3", "-I", "-c"])
            probe = ("import os\n" + inspect.getsource(_check_backup_chmod) +
                     f"\nos.chmod = _check_backup_chmod(os.chmod, {group})\n")
            result = subprocess.run([sys.executable, "-I", "-c", probe + command[3]],
                                    check=False, text=True, capture_output=True, **kwargs)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result

        self.host.sys.ok.return_value = True
        self.host.sys.sudo.side_effect = local_sudo
        for sudo in (False, True):
            with self.subTest(sudo=sudo):
                with patch("os.chmod", side_effect=_check_backup_chmod(os.chmod, group)):
                    backup = Path(self.host.backup(str(path), sudo=sudo))
                self.assertEqual(backup.read_bytes(), path.read_bytes())
                self.assertEqual(backup.stat().st_gid, group)
                self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o640)
                self.assertEqual(backup.stat().st_mtime_ns, path.stat().st_mtime_ns)
                backup.unlink()

    def test_publication_failure_keeps_original_and_cleans_staging(self):
        path = self.root / "secret"
        path.write_text("old")
        with patch("os.replace", side_effect=OSError("publication failed")):
            with self.assertRaisesRegex(OSError, "publication failed"):
                self.host.write_file(str(path), "new", backup=False)
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.root.iterdir()), [path])
        self.log.success.assert_not_called()

    def test_write_failure_keeps_original_and_cleans_staging(self):
        path = self.root / "secret"
        path.write_text("old")
        with patch("os.fsync", side_effect=OSError("disk full")):
            with self.assertRaisesRegex(OSError, "disk full"):
                self.host.write_file(str(path), "new", backup=False)
        self.assertEqual(path.read_text(), "old")
        self.assertEqual(list(self.root.iterdir()), [path])
        self.log.success.assert_not_called()

    def test_sudo_runs_same_writer_without_copying_over_live_target(self):
        target = self.root / "secret"
        target.write_text("old")
        target.chmod(0o640)
        link = self.root / ".env"
        link.symlink_to("secret")

        def local_sudo(command, **kwargs):
            self.assertEqual(command[:3], ["python3", "-I", "-c"])
            return subprocess.run([sys.executable, *command[1:]], check=True,
                                  text=True, **kwargs)

        self.host.sys.sudo.side_effect = local_sudo
        self.host.write_file(str(link), "new", sudo=True, backup=False)
        self.assertTrue(link.is_symlink())
        self.assertEqual(target.read_text(), "new")
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o640)
        self.assertEqual(set(self.root.iterdir()), {link, target})

class TestApplyBlock(unittest.TestCase):
    def test_insert_after_anchor(self):
        text = "line1\n# End required lines\nline2\n"
        out, injected = apply_block(text, "ruleA\nruleB",
                                    "# START APEX.ERMNVLDMR.COM RULES",
                                    "# END APEX.ERMNVLDMR.COM RULES",
                                    anchor="# End required lines")
        self.assertTrue(injected)
        self.assertIn("# End required lines\n# START APEX.ERMNVLDMR.COM RULES\nruleA\nruleB\n# END APEX.ERMNVLDMR.COM RULES", out)

    def test_self_heal_removes_old_and_new_blocks(self):
        text = ("a\n# End required lines\n"
                "# START ROOT.ERMNVLDMR.COM RULES\nold\n# END ROOT.ERMNVLDMR.COM RULES\n"
                "# START APEX.ERMNVLDMR.COM RULES\nprev\n# END APEX.ERMNVLDMR.COM RULES\n"
                "b\n")
        out, injected = apply_block(text, "new",
                                    "# START APEX.ERMNVLDMR.COM RULES",
                                    "# END APEX.ERMNVLDMR.COM RULES",
                                    anchor="# End required lines",
                                    old_pairs=[("# START ROOT.ERMNVLDMR.COM RULES",
                                                "# END ROOT.ERMNVLDMR.COM RULES")])
        self.assertTrue(injected)
        self.assertNotIn("old", out)
        self.assertNotIn("prev", out)
        self.assertEqual(out.count("# START APEX.ERMNVLDMR.COM RULES"), 1)
        self.assertIn("new", out)

    def test_anchor_missing_does_not_inject(self):
        text = "a\nb\n"
        out, injected = apply_block(text, "x", "S", "E", anchor="# End required lines")
        self.assertFalse(injected)
        self.assertEqual(out, text)

if __name__ == "__main__":
    unittest.main()
