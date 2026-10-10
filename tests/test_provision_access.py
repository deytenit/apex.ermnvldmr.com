import json
from pathlib import Path
import pwd
import shutil
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning.access import AccessError, Host, Transition, atomic, main
from provision_fakes import SecurityBoundary


TOKEN = 'a' * 32


class FakeHost(Host):
    """Replace system service effects; keep snapshots and transition files real."""
    def __init__(self, root):
        super().__init__(root)
        self.security = SecurityBoundary()
        self.boot = 'first-boot'
        self.armed = False
        self.restored = 0
        self.ports = [22]
        self.firewall = False
        self.bad_sudo = False
        self.fail_final = False
        self.existing_final = False
        atomic(self.path('/etc/ssh/sshd_config'), b'Port 22\n')

    def boot_id(self):
        return self.boot

    def preflight(self):
        if self.bad_sudo:
            raise AccessError('sudo failed')

    def arm(self):
        self.armed = True

    def disarm(self):
        self.armed = False

    def firewall_needed(self):
        return True

    def open_firewall(self):
        self.firewall = True

    def policy(self, ports, final, address):
        if final and self.fail_final:
            raise AccessError('policy failed')

    def current_final(self, address):
        return self.existing_final

    def verify_active(self, ports, final, address):
        self.policy(ports, final, address)
        if self.ports != sorted(set(ports)):
            raise AccessError('Live listener ports changed')

    def install_policy(self, ports, final, address):
        self.policy(ports, final, address)
        self.ports = sorted(set(ports))
        atomic(self.path('/etc/systemd/system/ssh.service.d/apex-init.conf'),
               json.dumps({'ports': self.ports, 'final': final}).encode())

    def restore(self, state):
        self.restored += 1
        self.ports = [state['original_port']]
        self.firewall = False
        path = self.path('/etc/systemd/system/ssh.service.d/apex-init.conf')
        if state['previous'] is None:
            path.unlink(missing_ok=True)
        else:
            import base64
            atomic(path, base64.b64decode(state['previous']))


class AccessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = FakeHost(Path(self.temp.name))
        self.clock = 1000
        self.transition = Transition(self.host, now=lambda: self.clock, security=self.host.security)

    def begin(self):
        return self.transition.execute('begin', TOKEN, 22, '192.0.2.10')

    def test_transition_keeps_original_until_final_and_commit_stops_rollback(self):
        self.begin()
        self.assertEqual(self.host.ports, [22, 2222])
        self.assertTrue(self.host.armed)
        self.transition.execute('finalize', TOKEN)
        self.assertEqual(self.host.ports, [2222])
        self.assertTrue(self.host.armed)
        self.transition.execute('commit', TOKEN)
        self.clock += 400
        self.host.boot = 'new-boot'
        self.transition.execute('tick')
        self.assertEqual(self.host.ports, [2222])
        self.assertEqual(self.host.restored, 0)
        self.assertFalse(self.host.armed)

    def test_expiry_restores_original_even_after_final_policy(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.clock += 301
        self.transition.execute('tick')
        self.assertEqual(self.host.ports, [22])
        self.assertFalse(self.host.firewall)
        self.assertEqual(self.transition.load()['status'], 'rolled_back')

    def test_reboot_recovers_persisted_transition_with_fresh_runner(self):
        self.begin()
        self.host.boot = 'second-boot'
        fresh = Transition(self.host, now=lambda: 0, security=self.host.security)
        fresh.execute('tick')
        self.assertEqual(self.host.ports, [22])
        self.assertEqual(self.host.restored, 1)

    def test_watchdog_restores_access_while_enrollment_lock_is_held(self):
        from engine.provisioning.state import exclusive_lock
        self.begin()
        self.clock += 301
        with exclusive_lock(self.host.path('/var/lib/apex-init/init.lock')):
            self.transition.execute('tick')
        self.assertEqual(self.host.ports, [22])
        self.assertEqual(self.host.restored, 1)

    def test_external_edit_prevents_finalize_and_watchdog_preserves_edit(self):
        self.begin()
        path = self.host.path('/etc/ssh/sshd_config')
        path.write_text('Port 22\nBanner /etc/banner\n')
        with self.assertRaises(AccessError):
            self.transition.execute('finalize', TOKEN)
        self.assertTrue(self.host.armed)
        self.clock += 301
        self.transition.execute('tick')
        self.assertIn('Banner', path.read_text())

    def test_invalid_sudo_never_changes_listeners(self):
        self.host.bad_sudo = True
        with self.assertRaises(AccessError):
            self.begin()
        self.assertEqual(self.host.ports, [22])
        self.assertFalse(self.host.armed)

    def test_failed_final_policy_restores_original(self):
        self.begin()
        self.host.fail_final = True
        with self.assertRaises(AccessError):
            self.transition.execute('finalize', TOKEN)
        self.assertEqual(self.host.ports, [22])
        self.assertFalse(self.host.armed)

    def test_other_coordinator_cannot_commit(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        with self.assertRaises(AccessError):
            self.transition.execute('commit', 'b' * 32)
        self.assertTrue(self.host.armed)

    def test_committed_retry_does_not_reopen_root_listener(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.transition.execute('commit', TOKEN)
        result = self.transition.execute('begin', 'b' * 32, 2222, '192.0.2.10')
        self.assertEqual(result['status'], 'committed')
        self.assertEqual(self.host.ports, [2222])
        self.assertFalse(self.host.armed)

    def test_committed_retry_rejects_live_listener_drift(self):
        self.begin()
        self.transition.execute('finalize', TOKEN)
        self.transition.execute('commit', TOKEN)
        self.host.ports = [22, 2222]
        with self.assertRaises(AccessError):
            self.transition.execute('begin', 'b' * 32, 2222, '192.0.2.10')

    def test_preexisting_final_policy_is_preserved_during_security_activation(self):
        self.host.existing_final = True
        self.host.ports = [2222]
        result = self.transition.execute('begin', TOKEN, 2222, '192.0.2.10')
        self.assertEqual(result['status'], 'transition')
        self.assertTrue(self.host.armed)
        self.assertFalse(self.host.firewall)
        self.transition.execute('finalize', TOKEN)
        self.transition.execute('commit', TOKEN)
        self.assertEqual(self.transition.load()['status'], 'committed')
        self.assertFalse(self.host.path('/etc/systemd/system/ssh.service.d/apex-init.conf').exists())

    def test_reject_symlink_state_ancestor(self):
        outside = self.host.path('/outside')
        outside.mkdir()
        self.host.path('/var').mkdir()
        self.host.path('/var/lib').symlink_to(outside)
        with self.assertRaises(AccessError):
            self.begin()
        self.assertEqual(list(outside.iterdir()), [])

    def test_policy_rejects_additive_ports(self):
        def runner(args, **kwargs):
            text = 'port 22\nport 2222\n' if '-T' in args else ''
            return subprocess.CompletedProcess(args, 0, text, '')
        with self.assertRaises(AccessError):
            Host(self.host.root, runner).policy([2222], True, '192.0.2.10')

    def test_atomic_snapshot_does_not_follow_predictable_temporary_link(self):
        destination = self.host.root / 'snapshot'
        victim = self.host.root / 'victim'
        victim.write_bytes(b'preserved')
        destination.with_name('snapshot.new').symlink_to(victim)
        atomic(destination, b'new snapshot')
        self.assertEqual(destination.read_bytes(), b'new snapshot')
        self.assertEqual(victim.read_bytes(), b'preserved')


class LivePolicyTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.arguments = ['/usr/sbin/sshd', '-D', '-p', '2222']
        self.running_arguments = self.arguments[:]
        self.effective = ('port 2222\npermitrootlogin no\npasswordauthentication no\n'
                          'kbdinteractiveauthentication no\npubkeyauthentication yes\n'
                          'authenticationmethods publickey\nallowusers adam\n'
                          'x11forwarding no\npermitemptypasswords no\n')
        self.listeners = 'LISTEN 0 128 0.0.0.0:2222 0.0.0.0:* users:(("sshd",pid=755,fd=6))\n'
        self.calls = []
        self.host = Host(self.root, self.runner)
        atomic(self.host.path('/etc/ssh/sshd_config'), b'# fixture\n')
        process = self.root / 'proc/755'
        process.mkdir(parents=True)
        (process / 'exe').symlink_to('/usr/sbin/sshd')
        self.cmdline()

    def cmdline(self, title=False):
        data = ('sshd: ' + ' '.join(self.running_arguments) + ' [listener] 0 of 10-100 startups\0'
                if title else '\0'.join(self.running_arguments) + '\0')
        (self.root / 'proc/755/cmdline').write_bytes(data.encode())

    def runner(self, args, **kwargs):
        self.calls.append(args)
        if args[:2] in (['systemctl', 'is-active'], ['systemctl', 'is-enabled']) and args[-1] == 'ssh.socket':
            return subprocess.CompletedProcess(args, 3, '', '')
        if 'ExecStart' in args:
            output = '{ path=/usr/sbin/sshd ; argv[]=' + ' '.join(self.arguments) + ' ; ignore_errors=no ; }'
        elif 'MainPID' in args:
            output = '755\n'
        elif 'KillMode' in args:
            output = 'process\n'
        elif args[0] == 'ss':
            output = self.listeners
        elif '-T' in args:
            output = self.effective
        else:
            output = ''
        return subprocess.CompletedProcess(args, 0, output, '')

    def test_verifies_exact_live_arguments_without_inventing_auth_options(self):
        self.cmdline(title=True)
        self.host.verify_active([2222], True, '192.0.2.1')
        effective_call = next(args for args in self.calls if '-T' in args)
        self.assertEqual(effective_call[:3], ['/usr/sbin/sshd', '-p', '2222'])
        self.assertNotIn('PermitRootLogin=no', effective_call)

    def test_password_or_root_enabled_on_live_daemon_is_rejected(self):
        for setting in ('passwordauthentication', 'permitrootlogin'):
            with self.subTest(setting=setting):
                previous = self.effective
                self.effective = previous.replace(setting + ' no', setting + ' yes')
                with self.assertRaises(AccessError):
                    self.host.verify_active([2222], True, '192.0.2.1')
                self.effective = previous

    def test_inherited_final_restrictions_cannot_silently_allow_other_accounts(self):
        for previous, conflicting in (('allowusers adam', 'allowusers adam\nallowusers provider'),
                                      ('x11forwarding no', 'x11forwarding yes'),
                                      ('permitemptypasswords no', 'permitemptypasswords yes')):
            with self.subTest(conflicting=conflicting):
                original = self.effective
                self.effective = original.replace(previous, conflicting)
                with self.assertRaises(AccessError):
                    self.host.verify_active([2222], True, '192.0.2.1')
                self.effective = original

    def test_bootstrap_prerequisites_do_not_require_adam_or_sudo_or_write_state(self):
        self.arguments = self.running_arguments = ['/usr/sbin/sshd', '-D']
        self.cmdline()
        def unseeded(args, **kwargs):
            if args[0] in ('visudo', 'runuser', 'sudo'):
                raise AssertionError('administrator tools do not exist before baseline')
            return self.runner(args, **kwargs)
        self.host.runner = unseeded
        before = sorted(self.root.rglob('*'))
        with patch('engine.provisioning.access.Host', return_value=self.host), \
                patch('engine.provisioning.access.os.geteuid', return_value=0):
            self.assertEqual(main(['preflight']), 0)
        self.assertEqual(sorted(self.root.rglob('*')), before)
        self.assertIn(['/usr/sbin/sshd', '-t'], self.calls)
        self.assertTrue(any('MainPID' in args for args in self.calls))

    def test_bootstrap_prerequisites_reject_socket_and_live_invocation_drift(self):
        self.arguments = self.running_arguments = ['/usr/sbin/sshd', '-D']
        self.cmdline()
        for failure in ('socket', 'invocation'):
            with self.subTest(failure=failure):
                def incompatible(args, **kwargs):
                    if failure == 'socket' and args == ['systemctl', 'is-enabled', '--quiet', 'ssh.socket']:
                        return subprocess.CompletedProcess(args, 0, '', '')
                    return self.runner(args, **kwargs)
                self.host.runner = incompatible
                self.running_arguments = ['/usr/sbin/sshd', '-D', '-p', '22'] if failure == 'invocation' else self.arguments
                self.cmdline()
                with self.assertRaises(AccessError):
                    self.host.bootstrap_preflight()

    def test_later_preflight_still_checks_administrator_sudo(self):
        self.arguments = self.running_arguments = ['/usr/sbin/sshd', '-D']
        self.cmdline()
        def bad_sudo(args, **kwargs):
            if args[:3] == ['runuser', '-u', 'adam']:
                return subprocess.CompletedProcess(args, 1, '', 'sudo denied')
            return self.runner(args, **kwargs)
        self.host.runner = bad_sudo
        with self.assertRaises(AccessError):
            self.host.preflight()

    def test_bootstrap_follows_recursive_relative_and_quoted_include_paths(self):
        self.arguments = self.running_arguments = ['/usr/sbin/sshd', '-D']
        self.cmdline()
        main_config = self.host.path('/etc/ssh/sshd_config')
        first = self.host.path('/etc/ssh/sshd_config.d/10-first.conf')
        second = self.host.path('/etc/ssh/provider # policy.conf')
        atomic(main_config, b'Include sshd_config.*/[0-9]*.conf /etc/ssh/missing/*.conf\n')
        atomic(first, b'iNcLuDe = "provider # policy.conf" # relative to /etc/ssh\n')
        atomic(second, b'# Match Address 203.0.113.0/24\nBanner "/etc/Match banner"\n')
        self.host.bootstrap_preflight()
        second.write_text('"mAtCh" Address 203.0.113.0/24\n PasswordAuthentication yes\n')
        before = {path: path.read_bytes() for path in (main_config, first, second)}
        with self.assertRaisesRegex(AccessError, 'Match.*manual reconciliation'):
            self.host.bootstrap_preflight()
        self.assertEqual({path: path.read_bytes() for path in before}, before)

    def test_bootstrap_rejects_recursive_or_oversized_config_without_mutation(self):
        self.arguments = self.running_arguments = ['/usr/sbin/sshd', '-D']
        self.cmdline()
        configuration = self.host.path('/etc/ssh/sshd_config')
        for content in ('Include sshd_config\n', '#' + 'x' * (1024 * 1024 + 1)):
            with self.subTest(recursive=content.startswith('Include')):
                configuration.write_text(content)
                with self.assertRaisesRegex(AccessError, 'manual reconciliation'):
                    self.host.bootstrap_preflight()
                self.assertEqual(configuration.read_text(), content)

    def test_extended_include_glob_tokens_are_rejected_even_without_matches(self):
        self.arguments = self.running_arguments = ['/usr/sbin/sshd', '-D']
        self.cmdline()
        configuration = self.host.path('/etc/ssh/sshd_config')
        for pattern in ('provider[a[:digit:]].conf', 'provider[a[.x.]].conf',
                        'provider[a[=x=]].conf', 'provider[:digit:].conf'):
            with self.subTest(pattern=pattern):
                content = 'Include ' + pattern + '\n'
                configuration.write_text(content)
                with self.assertRaisesRegex(AccessError, 'glob syntax.*manual reconciliation'):
                    self.host.bootstrap_preflight()
                self.assertEqual(configuration.read_text(), content)

    def test_unrestarted_daemon_with_old_arguments_is_rejected(self):
        self.running_arguments = ['/usr/sbin/sshd', '-D', '-p', '22']
        self.cmdline()
        with self.assertRaises(AccessError):
            self.host.verify_active([2222], True, '192.0.2.1')

    def test_extra_live_listener_port_is_rejected(self):
        self.listeners += 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=755,fd=7))\n'
        with self.assertRaises(AccessError):
            self.host.verify_active([2222], True, '192.0.2.1')

    def test_additional_sshd_service_listener_is_rejected(self):
        self.listeners += 'LISTEN 0 128 0.0.0.0:22 0.0.0.0:* users:(("sshd",pid=999,fd=7))\n'
        with self.assertRaises(AccessError):
            self.host.verify_active([2222], True, '192.0.2.1')

    def test_runtime_override_changes_fingerprint(self):
        before = self.host.fingerprint()
        atomic(self.host.path('/run/systemd/system/ssh.service.d/override.conf'), b'[Service]\nExecStart=/usr/sbin/sshd -D\n')
        self.assertNotEqual(before, self.host.fingerprint())

    def test_managed_file_does_not_exempt_drifted_service_from_preflight(self):
        atomic(self.host.path('/etc/systemd/system/ssh.service.d/apex-init.conf'),
               b'[Service]\nExecStart=\nExecStart=/usr/sbin/sshd -p 2222 -o PermitRootLogin=no -D\n')
        with self.assertRaisesRegex(AccessError, 'differs from its managed invocation'):
            self.host.preflight()


@unittest.skipUnless(shutil.which('sshd') and shutil.which('ssh-keygen'), 'OpenSSH tools unavailable')
class OpenSSHPolicyTests(unittest.TestCase):
    """Evaluate the managed options with a real daemon without opening sockets."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.key = self.root / 'host-key'
        self.config = self.root / 'etc/ssh/sshd_config'
        self.config.parent.mkdir(parents=True)
        self.config.write_text('PasswordAuthentication yes\nPermitRootLogin yes\n'
                               'X11Forwarding yes\nPermitEmptyPasswords yes\n')
        subprocess.run(['ssh-keygen', '-q', '-t', 'ed25519', '-N', '', '-f', str(self.key)],
                       check=True, capture_output=True, timeout=30)
        self.host = Host(self.root, self.runner)
        original_path = self.host.path
        self.host.path = lambda path: Path(path) if Path(path).is_relative_to(self.root) else original_path(path)

    def runner(self, args, **kwargs):
        if args[0] != '/usr/sbin/sshd':
            code = 3 if args[-1] == 'ssh.socket' else 0
            output = 'process\n' if 'KillMode' in args else ''
            return subprocess.CompletedProcess(args, code, output, '')
        return subprocess.run([shutil.which('sshd'), '-f', str(self.config), '-h', str(self.key), *args[1:]],
                              **kwargs)

    def effective(self, arguments, user):
        result = self.runner(arguments + ['-T', '-C', 'user=' + user + ',host=localhost,addr=192.0.2.1'],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.splitlines()

    def test_transition_preserves_provider_access_and_only_final_policy_excludes_it(self):
        transition = self.host.policy([22, 2222], False, '192.0.2.1')
        transitional = self.effective(transition, 'provider')
        self.assertFalse(any(line.startswith('allowusers ') for line in transitional))
        self.assertIn('passwordauthentication yes', transitional)
        final = self.host.policy([2222], True, '192.0.2.1')
        for user in ('adam', 'provider'):
            with self.subTest(user=user):
                effective = self.effective(final, user)
                allowed = [line.removeprefix('allowusers ') for line in effective if line.startswith('allowusers ')]
                self.assertEqual(allowed, ['adam'])
                self.assertNotIn('provider', allowed)
                self.assertIn('x11forwarding no', effective)
                self.assertIn('permitemptypasswords no', effective)

    def test_additive_inherited_allowusers_stops_finalization(self):
        with self.config.open('a') as stream:
            stream.write('AllowUsers provider\n')
        self.host.policy([22, 2222], False, '192.0.2.1')
        with self.assertRaises(AccessError):
            self.host.policy([2222], True, '192.0.2.1')

    def test_provider_account_match_block_cannot_bypass_final_restrictions(self):
        provider = pwd.struct_passwd(('provider', 'x', 1001, 1001, '', '/home/provider', '/bin/bash'))
        for setting in ('AllowUsers provider', 'X11Forwarding yes', 'PermitEmptyPasswords yes'):
            with self.subTest(setting=setting):
                self.config.write_text('Match User provider\n ' + setting + '\n')
                with patch('pwd.getpwall', return_value=[provider]), self.assertRaises(AccessError):
                    self.host.policy([2222], True, '192.0.2.1')

    def test_bootstrap_rejects_match_address_in_main_or_recursively_included_file(self):
        conditional = ('\n AllowUsers root provider\n PasswordAuthentication yes\n'
                       ' PermitRootLogin yes\n AuthenticationMethods any\n'
                       ' X11Forwarding yes\n PermitEmptyPasswords yes\n')
        first = self.root / 'first # include.conf'
        second = self.root / 'second include.conf'
        for directive, included in (('Match Address', False), ('mAtCh=Address', False),
                                    ('"Match" Address', False), ('"mAtCh" Address', True)):
            with self.subTest(directive=directive, included=included):
                payload = directive + ' 203.0.113.0/24' + conditional
                if included:
                    second.write_text(payload)
                    first.write_text('iNcLuDe "' + str(second) + '"\n')
                    self.config.write_text('"Include" "' + str(first) + '" # provider policy\n')
                else:
                    self.config.write_text(payload)
                arguments = ['/usr/sbin/sshd', '-T', '-p', '2222', '-o', 'AllowUsers=adam',
                             '-o', 'PermitRootLogin=no', '-o', 'PasswordAuthentication=no',
                             '-o', 'AuthenticationMethods=publickey', '-o', 'X11Forwarding=no',
                             '-o', 'PermitEmptyPasswords=no']
                other_address = self.runner(arguments + ['-C', 'user=root,host=localhost,addr=203.0.113.25'],
                                            capture_output=True, text=True, timeout=30)
                self.assertEqual(other_address.returncode, 0, other_address.stderr)
                self.assertIn('permitrootlogin yes', other_address.stdout.splitlines())
                self.assertIn('passwordauthentication yes', other_address.stdout.splitlines())
                before = self.config.read_bytes()
                with patch.object(self.host, 'service_arguments', return_value=['/usr/sbin/sshd', '-D']), \
                        patch.object(self.host, 'live_arguments'), \
                        self.assertRaisesRegex(AccessError, 'Match.*manual reconciliation'):
                    self.host.bootstrap_preflight()
                self.assertEqual(self.config.read_bytes(), before)

    def test_posix_class_include_cannot_hide_a_match_from_preflight(self):
        included = self.root / 'provider1.conf'
        included.write_text('Match Address 203.0.113.0/24\n PermitRootLogin yes\n')
        for pattern in ('provider[[:digit:]].conf', 'provider[a[:digit:]].conf'):
            with self.subTest(pattern=pattern):
                self.config.write_text('Include ' + str(self.root / pattern) + '\n')
                effective = self.runner(['/usr/sbin/sshd', '-T', '-o', 'PermitRootLogin=no',
                                         '-C', 'user=root,host=localhost,addr=203.0.113.25'],
                                        capture_output=True, text=True, timeout=30)
                self.assertEqual(effective.returncode, 0, effective.stderr)
                self.assertIn('permitrootlogin yes', effective.stdout.splitlines())
                with patch.object(self.host, 'service_arguments', return_value=['/usr/sbin/sshd', '-D']), \
                        patch.object(self.host, 'live_arguments'), \
                        self.assertRaisesRegex(AccessError, 'manual reconciliation'):
                    self.host.bootstrap_preflight()


if __name__ == '__main__':
    unittest.main()
