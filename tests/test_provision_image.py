from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning.image import cloud_enroll, sanitize_files
from engine.provisioning.state import ProvisionError


class ImageTests(unittest.TestCase):
    def test_incompatible_image_stops_before_any_enrollment(self):
        for metadata in ('{}', '{"enrollment_interface":999}'):
            with self.subTest(metadata=metadata), patch.object(Path, 'read_text', return_value=metadata), \
                    patch('engine.provisioning.target.enroll') as enroll:
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
                             'usr/local/lib/apex-provisioner/digest/engine/provisioning/target.py'):
                path = root / relative
                path.parent.mkdir(parents=True, exist_ok=True)
                path.write_text('build-specific')
            sanitize_files(root)
            self.assertEqual(list((root / 'etc/ssh').glob('ssh_host_*')), [])
            self.assertEqual((root / 'etc/machine-id').read_text(), '')
            self.assertFalse((root / 'root/.ssh/authorized_keys').exists())
            self.assertFalse((root / 'var/lib/apex-init').exists())
            self.assertFalse((root / 'var/lib/cloud').exists())
            self.assertTrue((root / 'usr/local/lib/apex-provisioner/digest/engine/provisioning/target.py').is_file())
            self.assertTrue((root / 'etc/systemd/system/ssh.service.d/10-generate-host-keys.conf').is_file())
