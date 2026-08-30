import unittest
import os
import shutil
from engine.lib.ufw import resolve_ufw_docker_bin

class TestUfwDockerResolution(unittest.TestCase):
    def test_prefers_path_or_usr_local_bin_when_present(self):
        bin_path = resolve_ufw_docker_bin()
        # Should return a valid string path
        self.assertIsInstance(bin_path, str)
        self.assertTrue(len(bin_path) > 0)

    def test_fallback_path(self):
        # If not in PATH, should fall back to user local bin
        original_which = shutil.which
        original_isfile = os.path.isfile
        try:
            shutil.which = lambda cmd: None
            os.path.isfile = lambda p: False if p == "/usr/local/bin/ufw-docker" else original_isfile(p)
            expected = os.path.expanduser("~/.local/bin/ufw-docker")
            self.assertEqual(resolve_ufw_docker_bin(), expected)
        finally:
            shutil.which = original_which
            os.path.isfile = original_isfile

if __name__ == "__main__":
    unittest.main()
