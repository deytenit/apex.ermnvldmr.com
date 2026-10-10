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
        self.runtime_chains = {name: {'DOCKER-USER': ['-N DOCKER-USER'],
                                      'ADMIN-SENTINEL': ['-N ADMIN-SENTINEL', '-A ADMIN-SENTINEL -j RETURN']}
                               for name in ('iptables', 'ip6tables')}
        self.forward_hook = True
        self.fail = None
        self.after_command = None
        self.pending_starts = set()
        contents = {
            '/etc/default/ufw': 'IPV6=yes\nDEFAULT_INPUT_POLICY="ACCEPT"\nDEFAULT_OUTPUT_POLICY="ACCEPT"\nDEFAULT_FORWARD_POLICY="DROP"\n',
            '/etc/ufw/ufw.conf': 'ENABLED=no\n',
            '/etc/ufw/user.rules': '*filter\n-A ufw-after-logging-forward -j LOG --log-prefix "[UFW BLOCK] " -m limit --limit 3/min --limit-burst 10\nCOMMIT\n',
            '/etc/ufw/user6.rules': '*filter\n-A ufw6-after-logging-forward -j LOG --log-prefix "[UFW BLOCK] " -m limit --limit 3/min --limit-burst 10\nCOMMIT\n',
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
        self.bouncer_conffiles = {}
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

    def load_helper_runtime(self):
        for executable, filename, fixture in (('iptables', 'after.rules', HELPER_V4),
                                              ('ip6tables', 'after6.rules', HELPER_V6)):
            if executable == 'ip6tables' and 'IPV6=yes' not in self.path('/etc/default/ufw').read_text():
                continue
            if '# BEGIN UFW AND DOCKER' not in self.path('/etc/ufw/' + filename).read_text():
                continue
            logging_chain = 'ufw6-docker-logging-deny' if executable == 'ip6tables' else 'ufw-docker-logging-deny'
            for chain in ('DOCKER-USER', logging_chain):
                self.runtime_chains[executable][chain] = ['-N ' + chain] + [
                    line for line in fixture.splitlines() if line.startswith('-A ' + chain + ' ')]

    def assert_restore_scope(self, args):
        if '--noflush' not in args:
            raise AssertionError('whole-table restore is forbidden')
        if Path(args[-1]).stat().st_mode & 0o777 != 0o600:
            raise AssertionError('restore input must be private')


    def command(self, args, check=True):
        self.commands.append(args)
        output, code = '', 0
        if args[:2] == ['systemctl', 'show']:
            output = '\n'.join(k + '=' + v for k, v in self.services[args[2]].items())
        elif args[:2] == ['ufw', 'status']:
            output = 'Status: ' + ('active' if self.active else 'inactive')
        elif args[0] == 'dpkg-query':
            conffiles = self.bouncer_conffiles if args[-1] == 'crowdsec-firewall-bouncer-iptables' else self.conffiles
            output = '\n'.join(' ' + name + ' ' + digest for name, digest in conffiles.items())
        elif args[0] in ('iptables', 'ip6tables'):
            if args[1] == '-C':
                code = 0 if self.forward_hook else 1
            elif args[1] == '-S':
                chains = self.runtime_chains[args[0]]
                if len(args) == 3:
                    chain = args[2]
                    code = 0 if chain in chains else 1
                    output = self.runtime_overrides.get((args[0], chain), '\n'.join(chains.get(chain, [])) + '\n')
                else:
                    output = '-P FORWARD ACCEPT\n' + ('-A FORWARD -j DOCKER-USER\n' if self.forward_hook else '')
                    output += ''.join(self.runtime_overrides.get((args[0], chain), '\n'.join(lines) + '\n')
                                      for chain, lines in chains.items())
            else:
                raise AssertionError(args)
        elif args[0] in ('iptables-restore', 'ip6tables-restore'):
            self.assert_restore_scope(args)
            executable = args[0].removesuffix('-restore')
            chains = copy.deepcopy(self.runtime_chains[executable])
            for line in Path(args[-1]).read_text().splitlines():
                if line in ('*filter', 'COMMIT'):
                    continue
                operation, chain, *rest = line.split()
                if operation == '-F':
                    chains[chain] = ['-N ' + chain]
                elif operation == '-A':
                    chains[chain].append(line)
                elif operation == '-X':
                    del chains[chain]
                else:
                    raise AssertionError(line)
            self.runtime_chains[executable] = chains
        elif args[0] == '/usr/local/bin/ufw-docker':
            if args[1] == 'check':
                code = 0 if '# BEGIN UFW AND DOCKER' in self.path('/etc/ufw/after.rules').read_text() else 1
            elif args[1] == 'install':
                if not self.active:
                    raise RuntimeError('UFW is disabled')
                for name in ('after.rules', 'after6.rules'):
                    path = self.path('/etc/ufw/' + name)
                    path.write_text(path.read_text() + (HELPER_V6 if name == 'after6.rules' else HELPER_V4))
        elif args[0] == 'ufw':
            if 'allow' in args and 'proto' in args:
                if args[1:3] == ['insert', '1'] and not any(
                        '### tuple ###' in self.path('/etc/ufw/' + name).read_text()
                        for name in ('user.rules', 'user6.rules')):
                    if check:
                        raise RuntimeError("ERROR: Invalid position '1'")
                    return subprocess.CompletedProcess(args, 1, '', "ERROR: Invalid position '1'")
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
                if self.active and 'DEFAULT_FORWARD_POLICY="ACCEPT"' in self.path('/etc/default/ufw').read_text():
                    for name, prefix in (('user.rules', 'ufw'), ('user6.rules', 'ufw6')):
                        path = self.path('/etc/ufw/' + name)
                        path.write_text(''.join(line for line in path.read_text().splitlines(keepends=True)
                                                if not line.startswith('-A ' + prefix + '-after-logging-forward -j LOG ')))
            elif args[-1] != 'reload':
                raise AssertionError(args)
            if self.active and args[-1] in ('enable', 'reload'):
                self.load_helper_runtime()
        elif args[0] == 'systemctl':
            field, value = {'enable': ('UnitFileState', 'enabled'), 'disable': ('UnitFileState', 'disabled'),
                            'start': ('ActiveState', 'active'), 'stop': ('ActiveState', 'inactive'),
                            'reset-failed': ('ActiveState', 'inactive')}[args[1]]
            if args[1] == 'reset-failed' and self.services[args[2]]['ActiveState'] != 'failed':
                raise AssertionError('reset-failed must only follow an observed failure')
            self.services[args[2]][field] = value
            if args[1] == 'stop':
                self.pending_starts.discard(args[2])
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

    def test_packaged_bouncer_unit_in_etc_can_activate_and_restore(self):
        name = '/etc/systemd/system/crowdsec-firewall-bouncer.service'
        path = self.host.path(name)
        path.parent.mkdir(parents=True)
        path.write_text('[Service]\nExecStart=/usr/bin/crowdsec-firewall-bouncer\n')
        self.host.bouncer_conffiles[name] = hashlib.md5(path.read_bytes()).hexdigest()
        self.host.services['crowdsec-firewall-bouncer']['FragmentPath'] = name
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'active')
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'inactive')
        self.assertEqual(state['cleanup'], 'complete')

    def test_modified_or_unmanaged_bouncer_unit_in_etc_refuses_before_activation(self):
        name = '/etc/systemd/system/crowdsec-firewall-bouncer.service'
        path = self.host.path(name)
        path.parent.mkdir(parents=True)
        path.write_text('[Service]\nExecStart=/usr/bin/crowdsec-firewall-bouncer\n')
        self.host.services['crowdsec-firewall-bouncer']['FragmentPath'] = name
        for conffiles in ({}, {name: '0' * 32}):
            with self.subTest(conffiles=conffiles):
                self.host.bouncer_conffiles = conffiles
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

    def test_bootstrap_allowance_exists_when_pristine_firewall_first_activates(self):
        enabled = []
        def inspect_enable(args):
            if args == ['ufw', '--force', 'enable']:
                rules = self.host.path('/etc/ufw/user.rules').read_text()
                self.assertIn('192.0.2.10', rules)
                self.assertIn('2222', rules)
                self.assertIn('2222', self.host.path('/etc/ufw/user6.rules').read_text())
                enabled.append(True)
        self.host.after_command = inspect_enable
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.assertEqual(enabled, [True])

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

    def test_rollback_restores_live_helper_chains_and_allows_next_preflight(self):
        for original in (['-N DOCKER-USER'], ['-N DOCKER-USER', '-A DOCKER-USER -j RETURN']):
            with self.subTest(original=original):
                for executable in ('iptables', 'ip6tables'):
                    self.host.runtime_chains[executable]['DOCKER-USER'] = original[:]
                before = copy.deepcopy(self.host.runtime_chains)
                state = self.prepared()
                self.adapter.activate(state, self.persist)
                self.adapter.restore(state, self.persist)
                self.assertEqual(self.host.runtime_chains, before)
                self.assertTrue(self.host.forward_hook)
                self.assertTrue(self.adapter.preflight()['pristine'])

    def test_preexisting_helper_specific_chain_is_rejected_before_mutation(self):
        self.host.runtime_chains['ip6tables']['ufw6-docker-logging-deny'] = ['-N ufw6-docker-logging-deny']
        before = copy.deepcopy(self.host.runtime_chains)
        with self.assertRaises(security.SecurityError):
            self.adapter.preflight()
        self.assertEqual(self.host.runtime_chains, before)
        self.assertFalse(self.host.active)

    def test_docker_runtime_must_match_preflight_at_capture(self):
        plan = self.adapter.preflight()
        self.host.runtime_chains['iptables']['DOCKER-USER'].append('-A DOCKER-USER -j RETURN')
        with self.assertRaises(security.SecurityError):
            self.adapter.snapshot(plan, 22, '192.0.2.10')
        self.assertFalse(self.host.active)

    def test_changed_helper_runtime_is_preserved_while_access_recovers(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.runtime_chains['iptables']['DOCKER-USER'].append('-A DOCKER-USER -j ACCEPT')
        before = copy.deepcopy(self.host.runtime_chains)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.runtime_chains, before)
        self.assertTrue(state['access_recovered'])
        self.assertEqual(state['cleanup'], 'pending')
        self.assertIn('docker-firewall-runtime', state['diagnostics'])

    def test_interrupted_helper_runtime_cleanup_resumes_each_family(self):
        state = self.prepared()
        before = copy.deepcopy(self.host.runtime_chains)
        self.adapter.activate(state, self.persist)
        def interrupt(args):
            if args[0] == 'iptables-restore':
                raise KeyboardInterrupt()
        self.host.after_command = interrupt
        with self.assertRaises(KeyboardInterrupt):
            self.adapter.restore(state, self.persist)
        self.host.after_command = None
        self.assertEqual(self.host.runtime_chains['iptables'], before['iptables'])
        self.assertNotEqual(self.host.runtime_chains['ip6tables'], before['ip6tables'])
        resumed = copy.deepcopy(self.saved[-1])
        self.adapter.restore(resumed, self.persist)
        self.assertEqual(self.host.runtime_chains, before)
        self.assertEqual(resumed['cleanup'], 'complete')
        self.assertEqual(sum(args[0] == 'iptables-restore' for args in self.host.commands), 1)

    def test_successful_restore_command_requires_runtime_postcondition(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        native = self.host.command
        def no_change(args, check=True):
            if args[0].endswith('tables-restore'):
                return subprocess.CompletedProcess(args, 0, '', '')
            return native(args, check)
        with patch.object(self.host, 'command', side_effect=no_change):
            with self.assertRaises(security.SecurityError):
                self.adapter.restore(state, self.persist)
        self.assertTrue(state['access_recovered'])
        self.assertEqual(state['cleanup'], 'pending')

    def test_runtime_drift_after_cleanup_persistence_is_not_overwritten(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        changed = []
        def change_during_persist(value):
            self.persist(value)
            if value['helper_runtime'].get('restoring') == 'iptables' and not changed:
                self.host.runtime_chains['iptables']['DOCKER-USER'].append('-A DOCKER-USER -j ACCEPT')
                changed.append(True)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, change_during_persist)
        self.assertEqual(self.host.runtime_chains['iptables']['DOCKER-USER'][-1], '-A DOCKER-USER -j ACCEPT')
        self.assertTrue(state['access_recovered'])
        self.assertFalse(any(args[0].endswith('tables-restore') for args in self.host.commands))

    def test_runtime_drift_during_final_reload_prevents_false_complete_cleanup(self):
        self.host.active = True
        self.host.services['ufw'].update(ActiveState='active', UnitFileState='enabled')
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        def change_after_reload(args):
            if args == ['ufw', 'reload']:
                self.host.runtime_chains['iptables']['DOCKER-USER'].append('-A DOCKER-USER -j ACCEPT')
        self.host.after_command = change_after_reload
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.runtime_chains['iptables']['DOCKER-USER'][-1], '-A DOCKER-USER -j ACCEPT')
        self.assertEqual(state['cleanup'], 'pending')
        self.assertTrue(state['access_recovered'])

    def test_legacy_runtime_snapshot_requires_reconciliation_but_recovers_access(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        del state['plan']['helper_runtime_before']
        del state['helper_runtime']
        before = copy.deepcopy(self.host.runtime_chains)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.runtime_chains, before)
        self.assertTrue(state['access_recovered'])
        self.assertEqual(state['cleanup'], 'pending')

    def test_disabled_ipv6_runtime_is_preserved_during_helper_cleanup(self):
        path = self.host.path('/etc/default/ufw')
        path.write_text(path.read_text().replace('IPV6=yes', 'IPV6=no'))
        self.host.conffiles['/etc/default/ufw'] = hashlib.md5(path.read_bytes()).hexdigest()
        before = copy.deepcopy(self.host.runtime_chains)
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.runtime_chains, before)
        self.assertFalse(any(args[0] == 'ip6tables-restore' for args in self.host.commands))

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

    def test_rollback_stops_owned_services_that_are_restarting_or_failed(self):
        for activity in ('activating', 'deactivating', 'failed', 'reloading'):
            with self.subTest(activity=activity):
                self.host = SecurityHost(Path(self.temp.name) / activity)
                self.adapter = security.Security(self.host)
                state = self.prepared()
                self.adapter.activate(state, self.persist)
                self.host.services['crowdsec-firewall-bouncer']['ActiveState'] = activity
                self.adapter.restore(state, self.persist)
                self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'inactive')
                self.assertEqual(state['cleanup'], 'complete')

    def test_owned_inactive_service_stop_cancels_queued_start(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec-firewall-bouncer']['ActiveState'] = 'inactive'
        self.host.pending_starts.add('crowdsec-firewall-bouncer')
        self.adapter.restore(state, self.persist)
        self.assertFalse(self.host.pending_starts)
        self.assertEqual(state['cleanup'], 'complete')

    def test_failed_owned_stop_is_reset_and_verified_inactive(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        def failed_stop(args):
            if args == ['systemctl', 'stop', 'crowdsec']:
                self.host.services['crowdsec']['ActiveState'] = 'failed'
        self.host.after_command = failed_stop
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec']['ActiveState'], 'inactive')
        self.assertIn(['systemctl', 'reset-failed', 'crowdsec'], self.host.commands)
        self.assertNotIn(['systemctl', 'reset-failed', 'crowdsec-firewall-bouncer'], self.host.commands)
        self.assertEqual(state['cleanup'], 'complete')

    def test_previously_restored_owned_service_is_rechecked_and_stopped_again(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.restore(state, self.persist)
        self.host.services['crowdsec-firewall-bouncer']['ActiveState'] = 'activating'
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'inactive')
        self.assertEqual(state['cleanup'], 'complete')

    def test_late_owned_service_start_prevents_false_complete_cleanup(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        def late_start(args):
            if args == ['ufw', 'disable']:
                self.host.services['crowdsec-firewall-bouncer']['ActiveState'] = 'activating'
        self.host.after_command = late_start
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(state['cleanup'], 'pending')
        self.assertTrue(state['access_recovered'])
        self.host.after_command = None
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'inactive')
        self.assertEqual(state['cleanup'], 'complete')

    def test_failed_preexisting_service_is_not_stopped_or_reset(self):
        self.host.services['crowdsec'].update(ActiveState='active', UnitFileState='enabled')
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec']['ActiveState'] = 'failed'
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec']['ActiveState'], 'failed')
        self.assertNotIn(['systemctl', 'stop', 'crowdsec'], self.host.commands)
        self.assertNotIn(['systemctl', 'reset-failed', 'crowdsec'], self.host.commands)

    def test_failed_reset_does_not_claim_service_recovery(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        def remains_failed(args):
            if args in (['systemctl', 'stop', 'crowdsec'], ['systemctl', 'reset-failed', 'crowdsec']):
                self.host.services['crowdsec']['ActiveState'] = 'failed'
        self.host.after_command = remains_failed
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(state['cleanup'], 'pending')
        self.assertTrue(state['access_recovered'])

    def test_changed_previously_restored_service_unit_is_preserved(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.adapter.restore(state, self.persist)
        self.host.services['crowdsec-firewall-bouncer'].update(
            ActiveState='activating', DropInPaths='/etc/systemd/system/bouncer.service.d/custom.conf')
        count = len(self.host.commands)
        with self.assertRaises(security.SecurityError):
            self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.services['crowdsec-firewall-bouncer']['ActiveState'], 'activating')
        self.assertNotIn(['systemctl', 'stop', 'crowdsec-firewall-bouncer'], self.host.commands[count:])
        self.assertTrue(state['access_recovered'])

    def test_changed_service_unit_is_preserved_and_reported_pending(self):
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.host.services['crowdsec']['ActiveState'] = 'failed'
        self.host.services['crowdsec']['DropInPaths'] = '/etc/systemd/system/crowdsec.service.d/administrator.conf'
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
        self.host.load_helper_runtime()
        before = copy.deepcopy(self.host.runtime_chains)
        state = self.prepared()
        self.adapter.activate(state, self.persist)
        self.assertNotIn([security.HELPER, 'install'], self.host.commands)
        self.adapter.restore(state, self.persist)
        self.assertEqual(self.host.runtime_chains, before)
        self.assertFalse(any(args[0].endswith('tables-restore') for args in self.host.commands))

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
