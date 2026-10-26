import io
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from provisioning import access, image
from provisioning.image import cloud_enroll, sanitize_files
from provisioning.transport import build_bundle
from provisioning.state import atomic_write, safe_path
from provisioning.state import ProvisionError


class ImageTests(unittest.TestCase):
    def test_uploaded_package_installs_runnable_wrappers_and_watchdog(self):
        commons = Path(__file__).resolve().parents[4]
        data, digest, _ = build_bundle(commons)
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            upload = root / 'tmp'
            upload.mkdir()
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                archive.extractall(upload, filter='data')

            def mapped(path):
                return safe_path(root / str(path).lstrip('/'))

            def write(path, content, mode=0o600):
                return atomic_write(mapped(path), content, mode)

            with patch.object(image, '__file__', str(upload / 'provisioning/image.py')), \
                    patch.object(image, 'safe_path', side_effect=mapped), \
                    patch.object(image, 'atomic_write', side_effect=write):
                metadata = image.install()
            installed = root / 'usr/local/lib/apex-provisioner' / digest
            self.assertEqual(metadata['path'], str(installed))
            self.assertTrue((installed / 'provisioning/seed.sh').is_file())
            environment = dict(os.environ)
            environment.pop('PYTHONPATH', None)
            for executable in ('apex-provision-image', 'apex-bootstrap'):
                wrapper = root / 'usr/local/bin' / executable
                self.assertEqual(wrapper.stat().st_mode & 0o777, 0o755)
                script = wrapper.read_text().replace('/usr/bin/python3', shlex.quote(sys.executable) + ' -S')
                result = subprocess.run(['/bin/sh', '-c', script, executable, '--help'],
                                        cwd=root, env=environment, capture_output=True,
                                        text=True, timeout=30)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertIn('usage:', result.stdout)

            commands = []
            def runner(args, **kwargs):
                commands.append(args)
                return subprocess.CompletedProcess(args, 0, '', '')

            canonical = '/usr/local/lib/apex-provisioner/' + digest
            with patch.object(access, '__file__', canonical + '/provisioning/access.py'):
                access.Host(root, runner=runner).arm()
            service = (root / 'etc/systemd/system/apex-access-rollback.service').read_text()
            fields = dict(line.split('=', 1) for line in service.splitlines() if '=' in line)
            self.assertEqual(fields['WorkingDirectory'], canonical)
            argv = shlex.split(fields['ExecStart'])
            self.assertEqual(argv[-1], 'tick')
            result = subprocess.run([sys.executable, '-S', *argv[1:-1], '--help'],
                                    cwd=installed, env=environment, capture_output=True,
                                    text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn(['systemctl', 'enable', '--now', 'apex-access-rollback.timer'], commands)
            sanitize_files(root)
            self.assertFalse((upload / 'provisioning').exists())
            self.assertTrue((installed / 'provisioning/image.py').is_file())

    def test_watchdog_rejects_uninstalled_package_before_writing_units(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            with self.assertRaisesRegex(access.AccessError, 'installed root-owned bundle'):
                access.Host(root).arm()
            self.assertEqual(list(root.iterdir()), [])

    def test_incompatible_image_stops_before_any_enrollment(self):
        for metadata in ('{}', '{"enrollment_interface":999}'):
            with self.subTest(metadata=metadata), patch.object(Path, 'read_text', return_value=metadata), \
                    patch('provisioning.target.enroll') as enroll:
                with self.assertRaisesRegex(ProvisionError, 'image enrollment interface'):
                    cloud_enroll()
                enroll.assert_not_called()

    def test_sanitization_removes_cloned_identity_and_enrollment_but_preserves_runner(self):
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary).resolve()
            for relative in ('etc/ssh/ssh_host_ed25519_key', 'etc/ssh/ssh_host_ed25519_key.pub',
                             'root/.ssh/authorized_keys', 'home/debian/.ssh/authorized_keys',
                             'etc/machine-id', 'var/lib/dbus/machine-id',
                             'var/lib/apex-init/enrollment.json', 'var/lib/cloud/instance/user-data.txt',
                             'usr/local/lib/apex-provisioner/digest/provisioning/target.py',
                             'tmp/provisioning/image.py'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('build-specific')
            sanitize_files(root)
            self.assertEqual(list((root / 'etc/ssh').glob('ssh_host_*')), [])
            self.assertEqual((root / 'etc/machine-id').read_text(), '')
            self.assertFalse((root / 'root/.ssh/authorized_keys').exists())
            self.assertFalse((root / 'var/lib/apex-init').exists())
            self.assertFalse((root / 'var/lib/cloud').exists())
            self.assertFalse((root / 'tmp/provisioning').exists())
            self.assertTrue((root / 'usr/local/lib/apex-provisioner/digest/provisioning/target.py').is_file())
            self.assertTrue((root / 'etc/systemd/system/ssh.service.d/10-generate-host-keys.conf').is_file())
