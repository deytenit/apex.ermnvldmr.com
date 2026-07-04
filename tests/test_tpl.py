# tests/test_tpl.py
import os, tempfile, unittest
from engine.lib.tpl import Tpl
from engine.lib.log import Log

class TestTpl(unittest.TestCase):
    def setUp(self):
        self.cfg = tempfile.mkdtemp()
        self.tpl = Tpl(self.cfg, Log())

    def _w(self, rel, body):
        p = os.path.join(self.cfg, rel)
        os.makedirs(os.path.dirname(p), exist_ok=True)
        open(p, "w").write(body)

    def test_render_substitutes_known_vars(self):
        self._w("cron/crontab", "root=${APEX_TIER1} node=$APEX_NODE\n")
        out = self.tpl.render("cron/crontab", {"APEX_TIER1": "/mnt/t1", "APEX_NODE": "daedalus"})
        self.assertEqual(out, "root=/mnt/t1 node=daedalus\n")

    def test_render_leaves_shell_command_substitution(self):
        self._w("c", "x $(cat /f) ${APEX_NODE}\n")
        out = self.tpl.render("c", {"APEX_NODE": "d"})
        self.assertEqual(out, "x $(cat /f) d\n")   # $(...) survives; ${APEX_NODE} replaced

    def test_render_dir_returns_relpath_map(self):
        self._w("fail2ban/jail.local", "a=${X}\n")
        self._w("fail2ban/sub/extra.conf", "b=${Y}\n")
        out = self.tpl.render_dir("fail2ban", {"X": "1", "Y": "2"})
        self.assertEqual(out, {"jail.local": "a=1\n", "sub/extra.conf": "b=2\n"})

if __name__ == "__main__":
    unittest.main()
