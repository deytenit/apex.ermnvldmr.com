from contextlib import nullcontext
import hashlib
import io
import json
from pathlib import Path
from subprocess import CompletedProcess
import tempfile
import unittest
from unittest.mock import patch

from provisioning import baseline


class ManualDockerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name).resolve()
        (self.root / "etc").mkdir()
        (self.root / "etc/os-release").write_text("ID=debian\nVERSION_ID=13\n")
        self.commands = []
        self.arch = "arm64"
        self.running = False
        self.data_root = "/custom/data"
        self.missing_buildx = False
        self.versions = {}
        self.installations = []

    def run_command(self, cmd, **kwargs):
        self.commands.append(cmd)
        if cmd == ["dpkg", "--print-architecture"]:
            return CompletedProcess(cmd, 0, self.arch + "\n", "")
        if cmd[-1] == "{{.DockerRootDir}}":
            return CompletedProcess(cmd, 0 if self.running else 1, self.data_root + "\n", "")
        if cmd[-2:] in (["version", "--short"], ["buildx", "version"]):
            if self.missing_buildx and cmd[-2:] == ["buildx", "version"]:
                if ["docker-buildx-plugin"] not in self.installations:
                    return CompletedProcess(cmd, 1, "", "plugin missing")
            return CompletedProcess(cmd, 0, "installed\n", "")
        raise AssertionError(cmd)

    def apply(self):
        def target(path):
            path = Path(path)
            return path if self.root in path.parents else self.root / str(path).lstrip("/")

        with (patch.object(baseline, "Path", side_effect=target),
              patch.object(baseline, "run", side_effect=self.run_command),
              patch.object(baseline, "package_versions", return_value=self.versions),
              patch.object(baseline.shutil, "which", return_value="/usr/bin/docker" if self.running else None),
              patch.object(baseline, "install_missing", side_effect=self.installations.append)):
            return baseline.apply_docker_policy()

    def test_working_engine_keeps_local_data_root_without_writes(self):
        self.running = True
        self.versions = {"docker.io": "1"}
        self.assertEqual(self.apply()["docker_data_root"], "/custom/data")
        self.assertEqual(self.installations, [])
        self.assertFalse((self.root / "etc/docker").exists())

    def test_pve_refused_before_packages_or_daemon(self):
        binary = self.root / "usr/bin/pveversion"
        binary.parent.mkdir(parents=True)
        binary.touch()
        with self.assertRaisesRegex(baseline.BaselineError, "Proxmox"):
            self.apply()
        self.assertEqual(self.commands, [])

    def test_unsupported_architecture_refused(self):
        self.arch = "i386"
        with self.assertRaisesRegex(baseline.BaselineError, "amd64|arm64"):
            self.apply()
        self.assertEqual(self.installations, [])

    def test_existing_runtime_packages_not_replaced(self):
        self.versions = {"containerd": "1.7"}
        with self.assertRaisesRegex(baseline.BaselineError, "containerd"):
            self.apply()
        self.assertEqual(self.installations, [])

    def test_fresh_arm64_install_verifies_the_pinned_key_and_starts_local_engine(self):
        key = b"test repository signing key"
        self.data_root = "/srv/docker"

        def start(service):
            if service == "docker":
                self.running = True

        with (patch.object(baseline, "service_start_guard", return_value=nullcontext()),
              patch.object(baseline.urllib.request, "urlopen", return_value=io.BytesIO(key)),
              patch.object(baseline, "DOCKER_KEY_SHA256", hashlib.sha256(key).hexdigest()),
              patch.object(baseline, "ensure_service", side_effect=start)):
            self.assertEqual(self.apply()["docker_data_root"], "/srv/docker")
        source = (self.root / "etc/apt/sources.list.d/apex-docker.sources").read_text()
        self.assertIn("Architectures: arm64", source)
        self.assertIn("Signed-By: ", source)
        self.assertEqual((self.root / "etc/apt/keyrings/apex-docker.asc").read_bytes(), key)
        self.assertEqual(json.loads((self.root / "etc/docker/daemon.json").read_text())["data-root"],
                         "/srv/docker")
        self.assertIn("docker-compose-plugin", self.installations[-1])
        self.assertIn("docker-buildx-plugin", self.installations[-1])
        self.assertNotIn("ufw", self.installations[-1])

    def test_untrusted_signing_key_cannot_publish_apt_source_or_install_docker(self):
        with (patch.object(baseline, "service_start_guard", return_value=nullcontext()),
              patch.object(baseline.urllib.request, "urlopen", return_value=io.BytesIO(b"wrong key"))):
            with self.assertRaisesRegex(baseline.BaselineError, "signing key"):
                self.apply()
        self.assertFalse((self.root / "etc/apt/sources.list.d/apex-docker.sources").exists())
        self.assertFalse((self.root / "etc/docker/daemon.json").exists())
        self.assertEqual(self.installations, [("ca-certificates",)])

    def test_working_ce_engine_only_installs_the_missing_plugin(self):
        self.running = True
        self.missing_buildx = True
        self.versions = {"docker-ce": "1"}
        with patch.object(baseline, "service_start_guard", return_value=nullcontext()):
            self.assertEqual(self.apply()["docker_data_root"], "/custom/data")
        self.assertEqual(self.installations, [["docker-buildx-plugin"]])
        self.assertFalse((self.root / "etc/docker").exists())
