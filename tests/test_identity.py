# tests/test_identity.py
import os, tempfile, unittest
from engine.identity import resolve, Node

class TestIdentity(unittest.TestCase):
    def _envfile(self, text, dirname=None):
        d = tempfile.mkdtemp()
        if dirname:
            d = os.path.join(d, dirname)
            os.makedirs(d)
        with open(os.path.join(d, "node.env"), "w") as f:
            f.write(text)
        return d

    def test_fqdn_is_source_of_truth(self):
        repo = self._envfile("APEX_CLUSTER=a1\nAPEX_SUBNET=198.18.11.0/24\n")
        n, warns = resolve("daedalus.a1.apex.ermnvldmr.com", repo)
        self.assertEqual((n.name, n.cluster, n.subnet), ("daedalus", "a1", "198.18.11.0/24"))
        self.assertEqual(warns, [])

    def test_cluster_drift_warns_hostname_wins(self):
        repo = self._envfile("APEX_CLUSTER=a9\nAPEX_SUBNET=198.18.11.0/24\n")
        n, warns = resolve("daedalus.a1.apex.ermnvldmr.com", repo)
        self.assertEqual(n.cluster, "a1")           # hostname wins
        self.assertTrue(any("drift" in w.lower() for w in warns))

    def test_fallback_when_fqdn_not_apex_shaped(self):
        # repo dir basename carries the node name in fallback (real host layout:
        # $HOME/morpheus.apex.ermnvldmr.com)
        repo = self._envfile("APEX_CLUSTER=a2\nAPEX_SUBNET=198.18.13.0/24\n",
                             dirname="morpheus.apex.ermnvldmr.com")
        n, warns = resolve("build-laptop", repo)
        self.assertEqual((n.name, n.cluster, n.subnet), ("morpheus", "a2", "198.18.13.0/24"))
        self.assertTrue(any("fqdn" in w.lower() for w in warns))

    def test_missing_subnet_is_error(self):
        repo = self._envfile("APEX_CLUSTER=a1\n")
        with self.assertRaises(SystemExit):
            resolve("daedalus.a1.apex.ermnvldmr.com", repo)

if __name__ == "__main__":
    unittest.main()
