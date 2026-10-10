import base64
import copy
import hashlib
import json
import ipaddress
import re
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning.access import Host
from engine.provisioning import security


# Pinned helper default output, kept independent of the adapter's validation code.
HELPER_V4 = """# BEGIN UFW AND DOCKER
*filter
:ufw-user-forward - [0:0]
:ufw-docker-logging-deny - [0:0]
:DOCKER-USER - [0:0]
-A DOCKER-USER -j ufw-user-forward
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A DOCKER-USER -m conntrack --ctstate INVALID -j DROP
-A DOCKER-USER -i docker0 -o docker0 -j ACCEPT
-A DOCKER-USER -j RETURN -s 10.0.0.0/8
-A DOCKER-USER -j RETURN -s 172.16.0.0/12
-A DOCKER-USER -j RETURN -s 192.168.0.0/16
-A DOCKER-USER -j ufw-docker-logging-deny -m conntrack --ctstate NEW -d 10.0.0.0/8
-A DOCKER-USER -j ufw-docker-logging-deny -m conntrack --ctstate NEW -d 172.16.0.0/12
-A DOCKER-USER -j ufw-docker-logging-deny -m conntrack --ctstate NEW -d 192.168.0.0/16
-A DOCKER-USER -j RETURN
-A ufw-docker-logging-deny -m limit --limit 3/min --limit-burst 10 -j LOG --log-prefix "[UFW DOCKER BLOCK] "
-A ufw-docker-logging-deny -j DROP
COMMIT
# END UFW AND DOCKER
"""
HELPER_V6 = """# BEGIN UFW AND DOCKER
*filter
:ufw6-user-forward - [0:0]
:ufw6-docker-logging-deny - [0:0]
:DOCKER-USER - [0:0]
-A DOCKER-USER -j ufw6-user-forward
-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN
-A DOCKER-USER -m conntrack --ctstate INVALID -j DROP
-A DOCKER-USER -i docker0 -o docker0 -j ACCEPT
-A DOCKER-USER -j RETURN -s fd00::/8
-A DOCKER-USER -j ufw6-docker-logging-deny -m conntrack --ctstate NEW -d fd00::/8
-A DOCKER-USER -j RETURN
-A ufw6-docker-logging-deny -m limit --limit 3/min --limit-burst 10 -j LOG --log-prefix "[UFW DOCKER BLOCK] "
-A ufw6-docker-logging-deny -j DROP
COMMIT
# END UFW AND DOCKER
"""


def native_alert(value, scope='Ip', origin='cscli', identifier=1):
    return {'id': identifier, 'created_at': '2026-10-10T12:00:00Z', 'events_count': 1,
            'events': [], 'message': 'manual decision', 'scenario': 'manual', 'simulated': False,
            'source': {'scope': scope, 'value': value},
            'decisions': [{'id': identifier, 'duration': '4h', 'origin': origin, 'scenario': 'manual',
                           'scope': scope, 'simulated': False, 'type': 'ban', 'value': value}]}


class SecurityHost(Host):
    """Keep filesystem effects real; substitute Debian commands and services."""
    def __init__(self, root):
        super().__init__(root)
        self.active = False
        self.services = {name: {'LoadState': 'loaded', 'ActiveState': 'inactive',
                               'UnitFileState': 'disabled', 'DropInPaths': '',
                               'FragmentPath': '/usr/lib/systemd/system/' + name + '.service'}
                         for name in ('ufw', 'crowdsec', 'crowdsec-firewall-bouncer')}
        self.commands = []
        self.decisions = []
        self.runtime_overrides = {}
        self.forward_hook = True
        self.fail = None
        self.after_command = None
        contents = {
            '/etc/default/ufw': 'IPV6=yes\nDEFAULT_INPUT_POLICY="ACCEPT"\nDEFAULT_OUTPUT_POLICY="ACCEPT"\nDEFAULT_FORWARD_POLICY="DROP"\n',
            '/etc/ufw/ufw.conf': 'ENABLED=no\n',
            '/etc/ufw/user.rules': '*filter\nCOMMIT\n',
            '/etc/ufw/user6.rules': '*filter\nCOMMIT\n',
            '/etc/ufw/before.rules': '*filter\nCOMMIT\n',
            '/etc/ufw/before6.rules': '*filter\nCOMMIT\n',
            '/etc/ufw/after.rules': '*filter\nCOMMIT\n',
            '/etc/ufw/after6.rules': '*filter\nCOMMIT\n',
            '/usr/local/bin/ufw-docker': '#!/bin/sh\n',
        }
        for name, content in contents.items():
            path = self.path(name)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
        self.conffiles = {'/etc/default/ufw': hashlib.md5(contents['/etc/default/ufw'].encode()).hexdigest()}
        checksums = []
        for name, content in contents.items():
            if not name.startswith('/etc/ufw/'):
                continue
            filename = name.rsplit('/', 1)[1]
            template = '/usr/share/ufw/' + ('' if filename == 'ufw.conf' else 'iptables/') + filename
            path = self.path(template)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content)
            checksums.append(hashlib.md5(content.encode()).hexdigest() + '  ' + template.lstrip('/'))
        manifest = self.path('/var/lib/dpkg/info/ufw.md5sums')
        manifest.parent.mkdir(parents=True, exist_ok=True)
        manifest.write_text('\n'.join(checksums) + '\n')

    def command(self, args, check=True):
        self.commands.append(args)
        output, code = '', 0
        if args[:2] == ['systemctl', 'show']:
            output = '\n'.join(k + '=' + v for k, v in self.services[args[2]].items())
        elif args[:2] == ['ufw', 'status']:
            output = 'Status: ' + ('active' if self.active else 'inactive')
        elif args[0] == 'dpkg-query':
            output = '\n'.join(' ' + name + ' ' + digest for name, digest in self.conffiles.items())
        elif args[0] in ('iptables', 'ip6tables'):
            if args[1] == '-C':
                code = 0 if self.forward_hook else 1
            elif self.active and '# BEGIN UFW AND DOCKER' in self.path('/etc/ufw/after.rules').read_text():
                fixture = HELPER_V6 if args[0] == 'ip6tables' else HELPER_V4
                chain = args[2]
                output = self.runtime_overrides.get((args[0], chain), '-N ' + chain + '\n' + '\n'.join(
                    line for line in fixture.splitlines() if line.startswith('-A ' + chain + ' ')) + '\n')
            else:
                output = '-N DOCKER-USER\n-A DOCKER-USER -j RETURN\n'
        elif args[0] == '/usr/local/bin/ufw-docker':
            if args[1] == 'check':
                code = 0 if '# BEGIN UFW AND DOCKER' in self.path('/etc/ufw/after.rules').read_text() else 1
            elif args[1] == 'install':
                for name in ('after.rules', 'after6.rules'):
                    path = self.path('/etc/ufw/' + name)
                    path.write_text(path.read_text() + (HELPER_V6 if name == 'after6.rules' else HELPER_V4))
        elif args[0] == 'ufw':
            if 'allow' in args and 'proto' in args:
                source, port = args[args.index('from') + 1], args[args.index('port') + 1]
                v6 = ipaddress.ip_network(source, strict=False).version == 6
                path = self.path('/etc/ufw/user6.rules' if v6 else '/etc/ufw/user.rules')
                line = '### tuple ### allow tcp ' + port + (' ::/0' if v6 else ' 0.0.0.0/0') + ' any ' + source + ' in comment=' + args[-1].encode().hex() + '\n'
                path.write_text(path.read_text().replace(line, '') if 'delete' in args else path.read_text() + line)
            elif args[1] == 'default':
                key = {'incoming': 'INPUT', 'outgoing': 'OUTPUT', 'routed': 'FORWARD'}[args[3]]
                path = self.path('/etc/default/ufw')
                path.write_text(re.sub('DEFAULT_' + key + '_POLICY="[A-Z]+"', 'DEFAULT_' + key + '_POLICY="' + ('DROP' if args[2] == 'deny' else 'ACCEPT') + '"', path.read_text()))
            elif args[-1] in ('enable', 'disable'):
                self.active = args[-1] == 'enable'
                self.path('/etc/ufw/ufw.conf').write_text('ENABLED=' + ('yes' if self.active else 'no') + '\n')
            elif args[-1] != 'reload':
                raise AssertionError(args)
        elif args[0] == 'systemctl':
            field, value = {'enable': ('UnitFileState', 'enabled'), 'disable': ('UnitFileState', 'disabled'),
                            'start': ('ActiveState', 'active'), 'stop': ('ActiveState', 'inactive')}[args[1]]
            self.services[args[2]][field] = value
            if args[1:3] == ['stop', 'ufw']:
                self.active = False
        elif args[0] == 'crowdsec':
            pass
        elif args[:2] == ['cscli', 'lapi']:
            code = 0 if self.services['crowdsec']['ActiveState'] == 'active' else 1
        elif args[:2] == ['cscli', 'decisions']:
            alerts = self.decisions
            if isinstance(alerts, list) and all(isinstance(a, dict) and isinstance(a.get('decisions'), list) for a in alerts):
                if '--all' not in args:
                    alerts = [a for a in alerts if all(d['origin'] != 'CAPI' for d in a['decisions'])]
                limit = int(args[args.index('--limit') + 1]) if '--limit' in args else 100
                if limit:
                    alerts = alerts[:limit]
            output = json.dumps(alerts)
        else:
            raise AssertionError('Unexpected command: ' + repr(args))
        result = subprocess.CompletedProcess(args, code, output, '')
        if self.after_command:
            self.after_command(args)
        if check and code:
            raise RuntimeError('simulated command failure')
        return result


class SecurityTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = SecurityHost(Path(self.temp.name))
        self.adapter = security.Security(self.host)
        self.addCleanup(patch.stopall)
        patch.object(security, 'HELPER_SHA256', hashlib.sha256(b'#!/bin/sh\n').hexdigest(), create=True).start()

    def test_masked_security_service_refuses_before_mutation(self):
        self.host.services['crowdsec']['UnitFileState'] = 'masked'
        before = self.host.path('/etc/default/ufw').read_bytes()
        with self.assertRaises(security.SecurityError):
            self.adapter.preflight()
        self.assertEqual(self.host.path('/etc/default/ufw').read_bytes(), before)
        self.assertFalse(self.host.active)

    def test_inactive_custom_policy_is_not_mistaken_for_pristine(self):
        path = self.host.path('/etc/default/ufw')
        path.write_text(path.read_text() + 'MANAGED_BY_ADMIN=yes\n')
        with self.assertRaises(security.SecurityError):
            self.adapter.preflight()
        self.assertFalse(self.host.active)

    def test_native_generated_ufw_files_are_verified_against_package_templates(self):
        self.assertEqual(set(self.host.conffiles), {'/etc/default/ufw'})
        self.assertTrue(self.adapter.preflight()['pristine'])

    def test_modified_generated_file_or_package_template_is_not_pristine(self):
        for name in ('/etc/ufw/user.rules', '/usr/share/ufw/iptables/after.rules'):
            with self.subTest(path=name):
                path = self.host.path(name)
                before = path.read_bytes()
                path.write_bytes(before + b'# modified\n')
                with self.assertRaises(security.SecurityError):
                    self.adapter.preflight()
                path.write_bytes(before)

    def test_active_custom_policy_and_ipv6_rules_are_preserved_in_plan(self):
        self.host.active = True
        self.host.path('/etc/default/ufw').write_text('IPV6=yes\nDEFAULT_FORWARD_POLICY="DROP"\n')
        self.host.path('/etc/ufw/user6.rules').write_text('# existing administrator sentinel\n')
        plan = self.adapter.preflight()
        self.assertFalse(plan['pristine'])
        self.assertTrue(plan['ipv6'])
        self.assertIn('sentinel', base64.b64decode(plan['files']['/etc/ufw/user6.rules']['data']).decode())

    def test_incomplete_helper_is_rejected(self):
        self.host.active = True
        self.host.path('/etc/ufw/after.rules').write_text('# BEGIN UFW AND DOCKER\ncustom\n# END UFW AND DOCKER\n')
        with self.assertRaises(security.SecurityError):
            self.adapter.preflight()

    def prepared(self):
        self.saved = []
        state = self.adapter.snapshot(self.adapter.preflight(), 22, '192.0.2.10')
        self.saved.append(copy.deepcopy(state))
        return state

    def persist(self, state):
        self.saved.append(copy.deepcopy(state))

    def test_pristine_activation_establishes_security_without_losing_bootstrap(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.assertTrue(self.host.active)
        self.assertIn('DEFAULT_INPUT_POLICY="DROP"', self.host.path('/etc/default/ufw').read_text())
        self.assertIn('DEFAULT_FORWARD_POLICY="ACCEPT"', self.host.path('/etc/default/ufw').read_text())
        rules = self.host.path('/etc/ufw/user.rules').read_text()
        self.assertIn('192.0.2.10', rules)
        self.assertIn('2222', rules)
        for service in self.host.services.values():
            self.assertEqual(service['ActiveState'], 'active')
            self.assertEqual(service['UnitFileState'], 'enabled')
        self.adapter.verify(state)

    def test_active_policy_survives_activation_and_rollback(self):
        self.host.active = True
        self.host.services['ufw'].update(ActiveState='active', UnitFileState='enabled')
        self.host.path('/etc/ufw/user6.rules').write_text('# IPv6 administrator sentinel\n')
        before = {name: self.host.path(name).read_bytes() for name in security.FILES}
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.assertEqual(self.host.path('/etc/default/ufw').read_bytes(), before['/etc/default/ufw'])
        self.adapter.restore(state, self.persist)
        self.assertTrue(self.host.active)
        for name, content in before.items():
            self.assertEqual(self.host.path(name).read_bytes(), content)

    def test_peer_range_ban_refuses_bouncer_and_can_restore_access(self):
        self.host.decisions = [native_alert('192.0.2.0/24', 'Range')]
        state = self.prepared()
        with self.assertRaises(security.SecurityError):
            self.adapter.activate(state, self.persist)
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'inactive')
        self.adapter.restore(state, self.persist)
        self.assertFalse(self.host.active)
        self.assertEqual(self.host.services['crowdsec']['ActiveState'], 'inactive')

    def test_concurrent_edit_is_preserved_and_inactive_access_recovers(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        path = self.host.path('/etc/default/ufw')
        path.write_text(path.read_text() + 'ADMIN_SENTINEL=yes\n')
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertFalse(self.host.active)
        self.assertIn('ADMIN_SENTINEL=yes', path.read_text())
        self.assertTrue(state['access_recovered'])
        self.assertEqual(state['cleanup'], 'pending')
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'inactive')

    def test_finalization_removes_only_temporary_bootstrap_allowance(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.finalize(state, self.persist)
        rules = self.host.path('/etc/ufw/user.rules').read_text()
        self.assertNotIn('192.0.2.10', rules)
        self.assertIn('2222', rules)
        self.adapter.verify(state)

    def test_clean_rollback_restores_inactive_service_states(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.restore(state, self.persist)
        for service in self.host.services.values():
            self.assertEqual(service['ActiveState'], 'inactive')
            self.assertEqual(service['UnitFileState'], 'disabled')
        self.assertEqual(state['cleanup'], 'complete')

    def test_rollback_does_not_restart_preexisting_service_stopped_by_administrator(self):
        self.host.services['crowdsec'].update(ActiveState='active', UnitFileState='enabled')
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec']['ActiveState'] = 'inactive'
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec']['ActiveState'], 'inactive')
        self.assertEqual(self.host.services['crowdsec']['UnitFileState'], 'enabled')
        self.assertNotIn(['systemctl', 'start', 'crowdsec'], self.host.commands)

    def test_owned_enable_does_not_claim_preexisting_activity_field(self):
        self.host.services['crowdsec']['ActiveState'] = 'active'
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec']['ActiveState'] = 'inactive'
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec']['ActiveState'], 'inactive')
        self.assertEqual(self.host.services['crowdsec']['UnitFileState'], 'disabled')

    def test_conflicting_service_state_is_preserved_and_reported_pending(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec']['ActiveState'] = 'failed'
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec']['ActiveState'], 'failed')
        self.assertTrue(state['access_recovered'])
        self.assertIn('service:crowdsec:ActiveState', state['diagnostics'])

    def test_redirected_file_does_not_prevent_runtime_access_recovery(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        target = self.host.path('/etc/ufw/after.rules')
        sentinel = self.host.path('/administrator-owned')
        sentinel.write_text('do not touch\n')
        target.unlink()
        target.symlink_to(sentinel)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertFalse(self.host.active)
        self.assertTrue(state['access_recovered'])
        self.assertEqual(sentinel.read_text(), 'do not touch\n')

    def test_metadata_change_is_not_silently_overwritten(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        path = self.host.path('/etc/default/ufw')
        path.chmod(0o600)
        with self.assertRaises(security.SecurityError):
            self.adapter.verify(state)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertFalse(self.host.active)

    def test_process_death_after_native_write_preserves_unknown_bytes(self):
        state = self.prepared()
        def die(args):
            if args[:2] == ['ufw', 'default']:
                raise KeyboardInterrupt()
        self.host.after_command = die
        with self.assertRaises(KeyboardInterrupt):
            self.adapter.activate(state, self.persist)
        self.host.after_command = None
        recovered = copy.deepcopy(self.saved[-1])
        with self.assertRaises(security.SecurityError):
            security.Security(self.host).restore(recovered, self.persist)
        self.assertFalse(self.host.active)
        self.assertTrue(recovered['access_recovered'])
        self.assertEqual(recovered['cleanup'], 'pending')
        self.assertIn('interrupted-mutation', recovered['diagnostics'])

    def test_active_firewall_keeps_source_limited_bootstrap_on_ambiguous_recovery(self):
        self.host.active = True
        self.host.services['ufw'].update(ActiveState='active', UnitFileState='enabled')
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.finalize(state, self.persist)
        path = self.host.path('/etc/ufw/user6.rules')
        path.write_text(path.read_text() + '# concurrent IPv6 sentinel\n')
        before_restore = len(self.host.commands)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertTrue(self.host.active)
        self.assertTrue(state['access_recovered'])
        self.assertIn('192.0.2.10', self.host.path('/etc/ufw/user.rules').read_text())
        self.assertIn('# concurrent IPv6 sentinel', path.read_text())
        self.assertNotIn(['ufw', 'disable'], self.host.commands)
        self.assertNotIn(['ufw', 'reload'], self.host.commands[before_restore:])

    def test_preexisting_ssh_rule_is_never_relabelled_or_deleted(self):
        self.host.active = True
        self.host.services['ufw'].update(ActiveState='active', UnitFileState='enabled')
        path = self.host.path('/etc/ufw/user.rules')
        sentinel = '### tuple ### allow tcp 2222 0.0.0.0/0 any 0.0.0.0/0 in comment=' + b'administrator'.hex() + '\n'
        path.write_text(path.read_text() + sentinel)
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.finalize(state, self.persist)
        self.assertIn(sentinel, path.read_text())
        self.adapter.restore(state, self.persist)
        self.assertIn(sentinel, path.read_text())

    def test_retry_verification_detects_stopped_bouncer(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec-firewall-bouncer']['ActiveState'] = 'inactive'
        with self.assertRaises(security.SecurityError):
            self.adapter.verify(state)

    def test_redirected_ufw_enable_file_is_not_written_during_recovery(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        path = self.host.path('/etc/ufw/ufw.conf')
        sentinel = self.host.path('/administrator-config')
        sentinel.write_text('do not change\n')
        path.unlink()
        path.symlink_to(sentinel)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(sentinel.read_text(), 'do not change\n')
        self.assertFalse(self.host.active)
        self.assertTrue(state['access_recovered'])

    def test_compatible_existing_helper_is_preserved(self):
        self.host.active = True
        for name in ('after.rules', 'after6.rules'):
            path = self.host.path('/etc/ufw/' + name)
            path.write_text(path.read_text() + (HELPER_V6 if name == 'after6.rules' else HELPER_V4))
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.assertNotIn([security.HELPER, 'install'], self.host.commands)

    def test_helper_check_success_does_not_accept_permissive_marker_block(self):
        self.host.active = True
        for name in ('after.rules', 'after6.rules'):
            self.host.path('/etc/ufw/' + name).write_text(
                '# BEGIN UFW AND DOCKER\n*filter\n-A DOCKER-USER -j ACCEPT\nCOMMIT\n# END UFW AND DOCKER\n')
        self.assertEqual(self.host.command([security.HELPER, 'check']).returncode, 0)
        with self.assertRaises(security.SecurityError):
            self.adapter.preflight()

    def test_valid_helper_files_require_live_rules_and_forward_hook(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        for executable, chain, contents in (
                ('iptables', 'DOCKER-USER', '-N DOCKER-USER\n-A DOCKER-USER -j RETURN\n'),
                ('ip6tables', 'DOCKER-USER', '-N DOCKER-USER\n-A DOCKER-USER -j ACCEPT\n'),
                ('iptables', 'DOCKER-USER', '-N DOCKER-USER\n-A DOCKER-USER -j ACCEPT\n' + '\n'.join(
                    line for line in HELPER_V4.splitlines() if line.startswith('-A DOCKER-USER ')) + '\n'),
                ('iptables', 'ufw-docker-logging-deny', '-N ufw-docker-logging-deny\n')):
            with self.subTest(executable=executable, chain=chain):
                self.host.runtime_overrides[(executable, chain)] = contents
                with self.assertRaises(security.SecurityError):
                    self.adapter.verify(state)
                self.host.runtime_overrides.clear()
        self.host.forward_hook = False
        with self.assertRaises(security.SecurityError):
            self.adapter.verify(state)

    def test_native_runtime_option_order_is_accepted_without_reordering_rules(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        output = self.host.command(['iptables', '-S', 'DOCKER-USER']).stdout
        output = output.replace('RELATED,ESTABLISHED', 'ESTABLISHED,RELATED').replace(
            '-j RETURN -s 10.0.0.0/8', '-s 10.0.0.0/8 -j RETURN')
        self.host.runtime_overrides[('iptables', 'DOCKER-USER')] = output
        self.adapter.verify(state)
        lines = output.splitlines()
        lines[1], lines[-1] = lines[-1], lines[1]
        self.host.runtime_overrides[('iptables', 'DOCKER-USER')] = '\n'.join(lines) + '\n'
        with self.assertRaises(security.SecurityError):
            self.adapter.verify(state)

    def test_existing_live_ban_is_rejected_before_firewall_changes(self):
        self.host.services['crowdsec'].update(ActiveState='active', UnitFileState='enabled')
        self.host.decisions = [native_alert('192.0.2.10')]
        with self.assertRaises(security.SecurityError):
            self.prepared()
        self.assertFalse(self.host.active)
        self.assertNotIn('2222', self.host.path('/etc/ufw/user.rules').read_text())

    def test_ipv6_peer_requires_ipv6_firewall_support(self):
        path = self.host.path('/etc/default/ufw')
        path.write_text(path.read_text().replace('IPV6=yes', 'IPV6=no'))
        self.host.conffiles['/etc/default/ufw'] = hashlib.md5(path.read_bytes()).hexdigest()
        with self.assertRaises(security.SecurityError):
            self.adapter.snapshot(self.adapter.preflight(), 22, '2001:db8::1')

    def test_decision_check_covers_alerts_after_default_limit_and_central_api(self):
        self.host.services['crowdsec'].update(ActiveState='active', UnitFileState='enabled')
        harmless = [native_alert('198.51.100.' + str(i), identifier=i) for i in range(1, 101)]
        for origin in ('cscli', 'CAPI'):
            with self.subTest(origin=origin):
                self.host.decisions = harmless + [native_alert('192.0.2.0/24', 'Range', origin, 101)]
                with self.assertRaises(security.SecurityError):
                    self.prepared()
        self.assertFalse(self.host.active)

    def test_non_native_decision_json_is_rejected_instead_of_skipped(self):
        self.host.services['crowdsec'].update(ActiveState='active', UnitFileState='enabled')
        for malformed in ([{'scope': 'Ip', 'value': '192.0.2.10'}], [{}], {'decisions': []}, None):
            with self.subTest(value=malformed):
                self.host.decisions = malformed
                with self.assertRaises(security.SecurityError):
                    self.prepared()

    def test_optional_simulated_field_is_not_required_for_native_decisions(self):
        self.host.services['crowdsec'].update(ActiveState='active', UnitFileState='enabled')
        alert = native_alert('198.51.100.3')
        del alert['decisions'][0]['simulated']
        self.host.decisions = [alert]
        self.prepared()
        alert['decisions'][0]['value'] = '192.0.2.0/24'
        alert['decisions'][0]['scope'] = 'Range'
        with self.assertRaises(security.SecurityError):
            self.prepared()

    def test_surgical_helper_cleanup_preserves_concurrent_metadata_and_text(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        path = self.host.path('/etc/ufw/after.rules')
        path.write_text(path.read_text() + '# administrator sentinel\n')
        path.chmod(0o600)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertIn('# administrator sentinel', path.read_text())
        self.assertNotIn('# BEGIN UFW AND DOCKER', path.read_text())
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)


if __name__ == '__main__':
    unittest.main()
