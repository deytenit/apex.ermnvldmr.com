import os
import tempfile
import unittest
from types import SimpleNamespace

from engine.context import Context, Paths
import actions.tier.link as tier_link
import actions.tier.chown as tier_chown


class TestTier0(unittest.TestCase):
    def test_paths_tier0_and_vars(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = os.path.join(tmpdir, "repo")
            commons = os.path.join(tmpdir, "commons")
            comp_dir = os.path.join(repo_root, "compositions")
            apex_dir = os.path.join(comp_dir, "apex")
            os.makedirs(apex_dir, exist_ok=True)

            paths = Paths(repo_root, commons)
            self.assertEqual(paths.root_tier0, os.path.realpath(os.path.join(comp_dir, "@tier0")))
            self.assertEqual(paths.tier0, os.path.realpath(os.path.join(apex_dir, "@tier0")))

            node = SimpleNamespace(name="test-node", host="test-host", fqdn="test.example.com", subnet="10.0.0.0/24")
            ctx = Context(node, paths, "test")
            v = ctx.vars()
            self.assertEqual(v["APEX_TIER0"], paths.tier0)
            self.assertEqual(v["APEX_TIER_ROOT0"], paths.root_tier0)

    def test_tier_link_creates_tier0_structure(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = os.path.join(tmpdir, "repo")
            commons = os.path.join(tmpdir, "commons")
            comp_dir = os.path.join(repo_root, "compositions")
            proj_dir = os.path.join(comp_dir, "proj1")
            os.makedirs(proj_dir, exist_ok=True)

            t1 = os.path.join(tmpdir, "t1")
            t2 = os.path.join(tmpdir, "t2")
            t3 = os.path.join(tmpdir, "t3")

            paths = Paths(repo_root, commons)
            commands = []

            class FakeSys:
                def sudo(self, cmd, **kwargs):
                    commands.append(cmd)
                    if cmd[0] == "mkdir":
                        os.makedirs(cmd[-1], exist_ok=True)
                    elif cmd[0] == "ln":
                        target = cmd[-2]
                        link = cmd[-1]
                        if os.path.islink(link) or os.path.exists(link):
                            os.remove(link)
                        os.symlink(target, link)

            ctx = SimpleNamespace(
                paths=paths,
                log=SimpleNamespace(info=lambda m: None, success=lambda m: None, warn=lambda m: None),
                sys=FakeSys()
            )

            tier_link.run(ctx, SimpleNamespace(tier1_path=t1, tier2_path=t2, tier3_path=t3))

            self.assertTrue(os.path.isdir(paths.root_tier0))
            self.assertTrue(os.path.isdir(os.path.join(proj_dir, "@tier0")))
            proj_tier0_link = os.path.join(paths.root_tier0, "proj1")
            self.assertTrue(os.path.islink(proj_tier0_link))
            self.assertEqual(os.path.realpath(proj_tier0_link), os.path.realpath(os.path.join(proj_dir, "@tier0")))

    def test_tier_chown_handles_tier0(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = os.path.join(tmpdir, "repo")
            commons = os.path.join(tmpdir, "commons")
            comp_dir = os.path.join(repo_root, "compositions")
            proj_dir = os.path.join(comp_dir, "proj1")
            proj_tier0 = os.path.join(proj_dir, "@tier0")
            os.makedirs(proj_tier0, exist_ok=True)

            paths = Paths(repo_root, commons)
            os.makedirs(paths.root_tier0, exist_ok=True)

            commands = []

            class FakeSys:
                def run(self, cmd, **kwargs):
                    if cmd[:2] == ["getent", "group"]:
                        return SimpleNamespace(returncode=0, stdout="noroot-shared:x:1000:")
                    return SimpleNamespace(returncode=0, stdout="")

                def sudo(self, cmd, **kwargs):
                    commands.append(cmd)
                    return SimpleNamespace(returncode=0, stdout="")

            # Write .env with APEX_UID/GID/SHARED_GID
            with open(os.path.join(proj_dir, ".env"), "w", encoding="utf-8") as f:
                f.write("APEX_UID=1001\nAPEX_GID=1001\nAPEX_SHARED_GID=1000\n")

            ctx = SimpleNamespace(
                paths=paths,
                log=SimpleNamespace(info=lambda m: None, success=lambda m: None, warn=lambda m: None, error=lambda m: None),
                sys=FakeSys()
            )

            tier_chown.run(ctx, SimpleNamespace(tier1_path=None, tier2_path=None, tier3_path=None))

            chown_cmds = [c for c in commands if c[0] == "chown"]
            # root_tier0 owned by adam:adam
            self.assertTrue(any(c == ["chown", "adam:adam", paths.root_tier0] for c in chown_cmds))
            # proj_tier0 owned by adam:1001
            self.assertTrue(any(c == ["chown", "-R", "adam:1001", proj_tier0] for c in chown_cmds))
            # non-root project must NOT be chowned to adam:root
            self.assertFalse(any(c == ["chown", "-R", "adam:root", proj_tier0] for c in chown_cmds))

    def test_tier_chown_handles_root_owned_tier0(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            repo_root = os.path.join(tmpdir, "repo")
            commons = os.path.join(tmpdir, "commons")
            comp_dir = os.path.join(repo_root, "compositions")
            proj_dir = os.path.join(comp_dir, "rootproj")
            proj_tier0 = os.path.join(proj_dir, "@tier0")
            os.makedirs(proj_tier0, exist_ok=True)
            with open(os.path.join(proj_dir, ".apex-root-owned"), "w", encoding="utf-8") as f:
                f.write("")

            paths = Paths(repo_root, commons)
            commands = []

            class FakeSys:
                def run(self, cmd, **kwargs):
                    if cmd[:2] == ["getent", "group"]:
                        return SimpleNamespace(returncode=0, stdout="noroot-shared:x:1000:")
                    return SimpleNamespace(returncode=0, stdout="")

                def sudo(self, cmd, **kwargs):
                    commands.append(cmd)
                    return SimpleNamespace(returncode=0, stdout="")

            ctx = SimpleNamespace(
                paths=paths,
                log=SimpleNamespace(info=lambda m: None, success=lambda m: None, warn=lambda m: None, error=lambda m: None),
                sys=FakeSys()
            )

            tier_chown.run(ctx, SimpleNamespace(tier1_path=None, tier2_path=None, tier3_path=None))

            chown_cmds = [c for c in commands if c[0] == "chown"]
            # root-owned proj_tier0 must be adam:root
            self.assertTrue(any(c == ["chown", "-R", "adam:root", proj_tier0] for c in chown_cmds))


if __name__ == "__main__":
    unittest.main()
