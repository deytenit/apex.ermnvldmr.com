from pathlib import Path
from tempfile import TemporaryDirectory
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

from engine import overlay
from engine.lib.crowdsec import Crowdsec
from engine.lib.tpl import Tpl

action = overlay.load_module(str(Path(__file__).resolve().parents[1] / "actions/config/crowdsec.py"),
                             "test_configure_crowdsec")


class CrowdsecConfiguration(unittest.TestCase):
    def setUp(self):
        temp = TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.cfg = Path(temp.name) / "crowdsec"
        self.cfg.mkdir()
        self.ctx = Mock()
        self.ctx.paths.configs = self.cfg.parent
        self.ctx.vars.return_value = {}
        self.ctx.crowdsec = Crowdsec(self.ctx.log, self.ctx.sys, Tpl(self.cfg.parent, self.ctx.log), self.ctx.host)
        self.ctx.sys.restart.side_effect = self.restart
        self.args = SimpleNamespace(dry_run=False)

    def restart(self, service):
        self.ctx.sys.sudo(["systemctl", "restart", service])
        return True

    def configure(self):
        action.run(self.ctx, self.args)

    def test_node_collections_and_bouncer_activated_after_engine_validation(self):
        (self.cfg / "collections.txt").write_text("# Services on this node\nvendor/mail\n\nvendor/http\n")
        (self.cfg / "acquis.yaml").write_text("source: journald\n")
        (self.cfg / "bouncers").mkdir()
        (self.cfg / "bouncers/crowdsec-firewall-bouncer.yaml.local").write_text("iptables_chains: [INPUT]\n")
        self.configure()
        targets = [call.args[0] for call in self.ctx.host.write_file.call_args_list]
        self.assertCountEqual(targets, ["/etc/crowdsec/acquis.yaml",
                              "/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml.local"])
        commands = [call.args[0] for call in self.ctx.sys.sudo.call_args_list]
        self.assertEqual([cmd[-1] for cmd in commands if cmd[:3] == ["cscli", "collections", "install"]],
                         ["vendor/mail", "vendor/http"])
        self.assertLess(commands.index(["crowdsec", "-t"]), commands.index(["systemctl", "restart", "crowdsec"]))
        self.assertLess(commands.index(["systemctl", "restart", "crowdsec"]),
                        commands.index(["systemctl", "restart", "crowdsec-firewall-bouncer"]))

    def test_absent_manifest_installs_baseline_and_skips_bouncer(self):
        self.configure()
        commands = [call.args[0] for call in self.ctx.sys.sudo.call_args_list]
        self.assertEqual([cmd[-1] for cmd in commands if cmd[:3] == ["cscli", "collections", "install"]],
                         ["crowdsecurity/linux", "crowdsecurity/traefik"])
        self.assertNotIn(["systemctl", "restart", "crowdsec-firewall-bouncer"], commands)

    def test_bad_manifest_fails_before_install_or_deploy_even_in_preview(self):
        (self.cfg / "collections.txt").write_text("legit/linux\n--force\n")
        self.args.dry_run = True
        with self.assertRaisesRegex(ValueError, "collections.txt:2"):
            self.configure()
        self.assertFalse(self.ctx.sys.mock_calls)
        self.ctx.host.write_file.assert_not_called()

    def test_failed_engine_validation_does_not_restart_running_services(self):
        def sudo(command):
            if command == ["crowdsec", "-t"]:
                raise RuntimeError("invalid engine configuration")
        self.ctx.sys.sudo.side_effect = sudo
        with self.assertRaisesRegex(RuntimeError, "invalid engine"):
            self.configure()
        self.assertFalse(any(call.args[0][:2] == ["systemctl", "restart"]
                             for call in self.ctx.sys.sudo.call_args_list))
