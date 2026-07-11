# tests/test_identity.py
import os, tempfile, unittest
from engine.identity import resolve, Node

class TestIdentity(unittest.TestCase):
    def _envfile(self, text):
        d = tempfile.mkdtemp()
        with open(os.path.join(d, "node.env"), "w") as f:
            f.write(text)
        return d

    def test_explicit_host_and_fqdn(self):
        repo = self._envfile("APEX_NODE_FQDN=node1.dc1.example.com\n"
                             "APEX_NODE_HOST=node1.example.com\n"
                             "APEX_SUBNET=10.0.0.0/24\n")
        n, warns = resolve("build-laptop", repo)   # hostname ignored when node.env has FQDN
        self.assertEqual((n.name, n.host, n.fqdn, n.subnet),
                         ("node1", "node1.example.com", "node1.dc1.example.com", "10.0.0.0/24"))
        self.assertEqual(warns, [])

    def test_host_defaults_to_fqdn(self):
        repo = self._envfile("APEX_NODE_FQDN=n1.example.com\nAPEX_SUBNET=10.0.0.0/24\n")
        n, warns = resolve("ignored", repo)
        self.assertEqual((n.name, n.host, n.fqdn), ("n1", "n1.example.com", "n1.example.com"))
        self.assertEqual(warns, [])

    def test_fqdn_falls_back_to_hostname(self):
        repo = self._envfile("APEX_SUBNET=10.0.0.0/24\n")
        n, warns = resolve("box.local", repo)
        self.assertEqual((n.name, n.host, n.fqdn), ("box", "box.local", "box.local"))
        self.assertTrue(any("APEX_NODE_FQDN" in w for w in warns))

    def test_missing_subnet_is_error(self):
        repo = self._envfile("APEX_NODE_FQDN=n1.example.com\n")
        with self.assertRaises(SystemExit):
            resolve("n1.example.com", repo)

if __name__ == "__main__":
    unittest.main()
