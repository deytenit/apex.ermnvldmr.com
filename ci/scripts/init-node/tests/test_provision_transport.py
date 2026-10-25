import io
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import tarfile
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning.transport import (
    SSH, TransportError, HandoverUncertain, build_bundle, destination, local_file, protected_handover,
    initialize,
    REMOTE_PATH_GUARD, remote_atomic_input,
)


class TransportTests(unittest.TestCase):
    def test_durable_runner_checks_access_after_seed_before_enrollment(self):
        for compatible in (False, True):
            with self.subTest(compatible=compatible), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'user-data'
                source.write_bytes(b'opaque input')
                stage = root / 'stage'
                stage.mkdir()
                installed = root / 'bundle'
                seed = installed / 'engine/provisioning/seed.sh'
                seed.parent.mkdir(parents=True)
                seed.write_text('printf "seed\\n" >> "$APEX_TEST_EVENTS"\n')
                binaries = root / 'bin'
                binaries.mkdir()
                python = binaries / 'python3'
                python.write_text('#!' + sys.executable + '\n' + '''
import os, sys
module, operation = sys.argv[2:4]
event = 'access-' + operation if module.endswith('.access') else operation
with open(os.environ['APEX_TEST_EVENTS'], 'a') as stream:
    stream.write(event + '\\n')
if event == 'access-preflight' and os.environ['APEX_TEST_COMPATIBLE'] == 'False':
    sys.exit(42)
if event == 'enroll':
    sys.exit(43)
''')
                python.chmod(0o755)
                events = root / 'events'
                environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'],
                                   APEX_TEST_EVENTS=str(events), APEX_TEST_COMPATIBLE=str(compatible))
                class Remote:
                    user, host, identity = 'root', 'example.com', None
                    resolved_port = 22
                    def __init__(self, *args):
                        pass
                    def resolve(self):
                        pass
                    def run(self, script, data=None, **kwargs):
                        value = b''
                        if 'ssh_host_' in script:
                            value = b'192.0.2.1 50000 192.0.2.2 22\nssh-ed25519 AAAA fixture\n'
                        elif 'mktemp -d /var/tmp/apex-init.' in script:
                            value = b'/var/tmp/apex-init.0123456789\n'
                        elif 'systemd-run --quiet' in script:
                            arguments = shlex.split(script)
                            wrapper = arguments[-1].replace('/usr/local/lib/apex-provisioner/' + 'a' * 64, str(installed))
                            wrapper = wrapper.replace('/var/tmp/apex-init.0123456789', str(stage))
                            wrapper = wrapper.replace('/etc/tmpfiles.d/', str(root) + '/')
                            subprocess.run(['/bin/sh', '-c', wrapper], env=environment,
                                           capture_output=True, timeout=30)
                        elif script.startswith('if test -f /var/tmp'):
                            value = (stage / 'result').read_bytes()
                        return subprocess.CompletedProcess([], 0, value, b'')
                with patch('engine.provisioning.transport.SSH', Remote), \
                        patch('engine.provisioning.transport.build_bundle', return_value=(b'bundle', 'a' * 64, 'b' * 40)), \
                        patch('engine.provisioning.transport.shutil.which', return_value='/usr/bin/ssh'):
                    with self.assertRaises(TransportError):
                        initialize(root, 'root@example.com', source, progress=lambda message: None)
                expected = ['seed', 'validate', 'access-preflight']
                if compatible:
                    expected.append('enroll')
                self.assertEqual(events.read_text().splitlines(), expected)
                self.assertEqual((stage / 'result').read_text().strip(), '43' if compatible else '42')

    def test_rejects_options_shell_fragments_and_invalid_ports(self):
        for value in ['root@-oProxyCommand=id', 'root@host;id', '-oProxyCommand=id@host',
                      'bob;id@host', 'bob name@host', 'bob\n@host', 'bob/other@host',
                      'bob$(id)@host', '@host', '9bob@host', 'a' * 33 + '@host',
                      'root@host\nfoo', 'host', 'debian@host -oProxyCommand=id']:
            with self.subTest(value=value), self.assertRaises(TransportError):
                destination(value)
        for port in ['0', '65536', '-1', '22;id', '2.5', '']:
            with self.subTest(port=port), self.assertRaises(TransportError):
                destination('root@example.com', port)
        self.assertEqual(destination('adam@node', 2222), ('adam', 'node', 2222))

    def test_existing_bootstrap_accounts_are_accepted(self):
        for user in ['root', 'adam', 'debian', 'bob', 'cloud-admin', '_admin', 'Admin.1']:
            with self.subTest(user=user):
                self.assertEqual(destination(user + '@example.com'), (user, 'example.com', None))
        self.assertEqual(destination('debian@[2001:db8::1]', 22), ('debian', '2001:db8::1', 22))

    def test_nonroot_bootstrap_uses_noninteractive_sudo_and_preserves_stdin(self):
        calls = []
        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return subprocess.CompletedProcess(args, 0, b'root verified', b'')
        ssh = SSH('debian@host', runner=runner)
        self.assertEqual(ssh.run('id -u', b'opaque input').stdout, b'root verified')
        args, kwargs = calls[0]
        self.assertEqual(args[args.index('-l') + 1], 'debian')
        self.assertEqual(shlex.split(args[-1]), ['sudo', '-n', '--', 'sh', '-c', 'id -u'])
        self.assertEqual(kwargs['input'], b'opaque input')
        ssh.run('printf %s "$SSH_CONNECTION"', privileged=False)
        self.assertEqual(shlex.split(calls[1][0][-1])[:2], ['sh', '-c'])

    def test_sudo_cleared_connection_environment_uses_unprivileged_fallback(self):
        class PreflightComplete(Exception):
            pass
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'user-data'
            source.write_bytes(b'opaque input')
            release = root / 'os-release'
            release.write_text('ID=debian\nVERSION_ID=13\n')
            systemd_run = root / 'systemd-run'
            systemd_run.write_text('#!/bin/sh\nexit 0\n')
            systemd_run.chmod(0o755)
            environment = dict(os.environ, PATH=str(root) + os.pathsep + os.environ['PATH'])
            environment.pop('SSH_CONNECTION', None)
            fallbacks = []
            def runner(args, **kwargs):
                if '-G' in args:
                    return subprocess.CompletedProcess(args, 0, 'hostname 192.0.2.2\nport 2222\n', '')
                command = shlex.split(args[-1])
                script = command[-1]
                if 'ssh_host_' in script:
                    self.assertEqual(command[:3], ['sudo', '-n', '--'])
                    tools = ('id() { printf 0; }; dpkg() { printf amd64; }; '
                             "cat() { printf 'ssh-ed25519 AAAA fixture\\n'; }; ")
                    script = script.replace('/etc/os-release', shlex.quote(str(release)))
                    result = subprocess.run(['sh', '-c', tools + script], capture_output=True, env=environment)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    return result
                if 'SSH_CONNECTION' in script:
                    self.assertEqual(command[:2], ['sh', '-c'])
                    fallbacks.append(script)
                    return subprocess.run(command, capture_output=True,
                                          env=dict(environment, SSH_CONNECTION='192.0.2.1 50000 192.0.2.2 2222'))
                self.assertIn('/var/lib/apex-init/remote.json', script)
                self.assertIn('ssh-ed25519 AAAA', transport.trust_file.read_text())
                raise PreflightComplete
            transport = SSH('adam@example.com', runner=runner)
            with patch('engine.provisioning.transport.SSH', return_value=transport), \
                    patch('engine.provisioning.transport.build_bundle', return_value=(b'bundle', 'a' * 64, 'b' * 40)), \
                    patch('engine.provisioning.transport.shutil.which', return_value='/usr/bin/ssh'):
                with self.assertRaises(PreflightComplete):
                    initialize(root, 'adam@example.com', source, progress=lambda message: None)
            self.assertEqual(len(fallbacks), 1)

    def test_remote_script_quoting_transports_literal_values(self):
        received = []
        def runner(args, **kwargs):
            command = shlex.split(args[-1])
            self.assertEqual(command[:2], ['sh', '-c'])
            result = subprocess.run(command, input=kwargs.get('input'), capture_output=True)
            received.append(result.stdout)
            return result
        value = "spaces '$HOME' `echo unsafe`; $(echo unsafe)"
        SSH('root@host', runner=runner).run('printf %s ' + shlex.quote(value))
        self.assertEqual(received, [value.encode()])

    def test_secret_bytes_go_over_stdin_and_diagnostics_are_redacted(self):
        secret = b'private key material'
        def runner(args, **kwargs):
            self.assertEqual(kwargs['input'], secret)
            self.assertNotIn(secret.decode(), repr(args))
            return subprocess.CompletedProcess(args, 1, b'', secret)
        with self.assertRaises(TransportError) as caught:
            SSH('root@host', runner=runner).run('cat >/dev/null', secret)
        self.assertNotIn(secret.decode(), str(caught.exception))

    def test_fresh_login_disables_mux_password_and_pins_verified_identity(self):
        calls = []
        def runner(args, **kwargs):
            calls.append(args)
            return subprocess.CompletedProcess(args, 0, b'', b'')
        ssh = SSH('root@original-alias', runner=runner)
        ssh.resolved_host = '192.0.2.42'
        ssh.trust_file = Path('/tmp/trusted')
        ssh.fresh_login()
        args = calls[0]
        self.assertIn('-T', args)
        for option in ('ControlMaster=no', 'ControlPath=none', 'BatchMode=yes',
                       'PreferredAuthentications=publickey', 'StrictHostKeyChecking=yes',
                       'Hostname=192.0.2.42', 'HostKeyAlias=apex-init-verified-target'):
            self.assertIn(option, args)
        self.assertEqual(args[args.index('-l') + 1], 'adam')
        self.assertEqual(args[args.index('-p') + 1], '2222')

    def test_failed_first_fresh_login_rolls_back_without_finalizing(self):
        calls = []
        class FailedSSH:
            def fresh_login(self):
                raise TransportError('key rejected')
        def invoke(operation, *args):
            calls.append(operation)
            return {'status': 'transition'}
        with self.assertRaises(TransportError):
            protected_handover(FailedSSH(), invoke, 'a' * 32, 22, '192.0.2.1')
        self.assertEqual(calls, ['begin', 'rollback'])

    def test_failed_second_fresh_login_never_commits(self):
        calls = []
        class SecondFailure:
            count = 0
            def fresh_login(self):
                self.count += 1
                if self.count == 2:
                    raise TransportError('final policy rejected login')
        def invoke(operation, *args):
            calls.append(operation)
            return {'status': 'transition'}
        with self.assertRaises(TransportError):
            protected_handover(SecondFailure(), invoke, 'a' * 32, 22, '192.0.2.1')
        self.assertEqual(calls, ['begin', 'finalize', 'rollback'])

    def test_two_successful_fresh_logins_precede_commit(self):
        calls = []
        class VerifiedSSH:
            def fresh_login(self):
                calls.append('fresh')
        def invoke(operation, *args):
            calls.append(operation)
            return {'status': 'transition'}
        protected_handover(VerifiedSSH(), invoke, 'a' * 32, 22, '192.0.2.1')
        self.assertEqual(calls, ['begin', 'fresh', 'finalize', 'fresh', 'commit'])

    def test_lost_commit_reply_is_reconciled_without_rolling_back_committed_access(self):
        from engine.provisioning.access import Transition
        from tests.test_provision_access import FakeHost
        with tempfile.TemporaryDirectory() as directory:
            host = FakeHost(Path(directory))
            transition = Transition(host, now=lambda: 1000, security=host.security)
            calls = []
            class VerifiedSSH:
                def fresh_login(self):
                    pass
            def invoke(operation, token, port, address, fresh):
                calls.append(operation)
                if operation == 'status':
                    self.assertTrue(fresh)
                    return transition.load()
                result = transition.execute(operation, token, port, address)
                if operation == 'commit':
                    raise TransportError('SSH reply lost')
                return result
            protected_handover(VerifiedSSH(), invoke, 'a' * 32, 22, '192.0.2.1')
            self.assertEqual(transition.load()['status'], 'committed')
            self.assertEqual(host.ports, [2222])
            self.assertFalse(host.armed)
            self.assertEqual(host.restored, 0)
            self.assertEqual(calls, ['begin', 'finalize', 'commit', 'status'])

    def test_known_uncommitted_lost_reply_still_rolls_back(self):
        calls = []
        class VerifiedSSH:
            def fresh_login(self):
                pass
        def invoke(operation, *args):
            calls.append(operation)
            if operation == 'commit':
                raise TransportError('SSH failed before commit')
            return {'status': 'final' if operation == 'status' else 'transition', 'token': 'a' * 32}
        with self.assertRaises(TransportError):
            protected_handover(VerifiedSSH(), invoke, 'a' * 32, 22, '192.0.2.1')
        self.assertEqual(calls, ['begin', 'finalize', 'commit', 'status', 'rollback'])

    def test_lost_commit_reply_reports_verified_or_uncertain_resume_endpoints(self):
        from engine.provisioning.access import Transition
        from tests.test_provision_access import FakeHost
        for unreachable in (False, True):
            with self.subTest(unreachable=unreachable), tempfile.TemporaryDirectory() as directory:
                root = Path(directory)
                source = root / 'user-data'
                source.write_bytes(b'opaque input')
                host = FakeHost(root)
                transition = Transition(host, now=lambda: 1000, security=host.security)
                messages, commands = [], []
                def runner(args, **kwargs):
                    if '-G' in args:
                        return subprocess.CompletedProcess(args, 0, 'hostname 192.0.2.2\nport 22\n', '')
                    command = shlex.split(args[-1])
                    script = command[-1]
                    commands.append(script)
                    value = b''
                    if 'ssh_host_' in script:
                        value = b'192.0.2.1 50000 192.0.2.2 22\nssh-ed25519 AAAA fixture\n'
                    elif 'mktemp -d /var/tmp/apex-init.' in script:
                        value = b'/var/tmp/apex-init.0123456789\n'
                    elif script.startswith('if test -f /var/tmp'):
                        value = b'0'
                    elif script.startswith('cd ') and 'engine.provisioning.access' in script:
                        access_args = shlex.split(script)[3:]
                        operation = access_args[3]
                        token = access_args[access_args.index('--token') + 1]
                        port = int(access_args[access_args.index('--original-port') + 1]) if '--original-port' in access_args else None
                        address = access_args[access_args.index('--client-address') + 1] if port else None
                        value = json.dumps(transition.execute(operation, token, port, address)).encode()
                        if operation == 'commit':
                            return subprocess.CompletedProcess(args, 255, b'', b'connection lost')
                    elif 'cat /var/lib/apex-init/access/state.json' in script:
                        self.assertEqual(args[args.index('-l') + 1], 'adam')
                        self.assertEqual(args[args.index('-p') + 1], '2222')
                        self.assertEqual(command[:3], ['sudo', '-n', '--'])
                        if unreachable:
                            return subprocess.CompletedProcess(args, 255, b'', b'connection lost')
                        value = json.dumps(transition.load()).encode()
                    elif 'engine.provisioning.target complete' in script:
                        value = b'{"status": "complete"}'
                    return subprocess.CompletedProcess(args, 0, value, b'')
                def transport(*args):
                    return SSH(*args, runner=runner)
                with patch('engine.provisioning.transport.SSH', side_effect=transport), \
                        patch('engine.provisioning.transport.build_bundle', return_value=(b'bundle', 'a' * 64, 'b' * 40)), \
                        patch('engine.provisioning.transport.shutil.which', return_value='/usr/bin/ssh'):
                    if unreachable:
                        with self.assertRaises(HandoverUncertain):
                            initialize(root, 'debian@example.com', source, progress=messages.append)
                        self.assertTrue(any('outcome is unknown' in message for message in messages))
                        self.assertIn('adam@example.com --port 2222', messages[-2])
                        self.assertIn('debian@example.com --port 22', messages[-1])
                        self.assertFalse(any('Safe resume' in message or 'pending rollback' in message for message in messages))
                    else:
                        self.assertEqual(initialize(root, 'debian@example.com', source, progress=messages.append), {'status': 'complete'})
                        self.assertIn('Safe resume: ./apex init adam@example.com --port 2222', messages[-1])
                self.assertEqual(transition.load()['status'], 'committed')
                self.assertFalse(host.armed)
                self.assertFalse(any('access rollback' in script for script in commands))

    def test_bundle_excludes_repository_and_environment_secrets(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / 'engine/provisioning').mkdir(parents=True)
            (root / 'engine/__init__.py').write_text('')
            (root / 'engine/provisioning/seed.sh').write_text('#!/bin/sh\n')
            (root / '.env').write_text('private secret')
            (root / 'engine/private.env').write_text('private secret')
            data, digest, revision = build_bundle(root)
            with tarfile.open(fileobj=io.BytesIO(data)) as archive:
                self.assertEqual(set(archive.getnames()), {'engine/__init__.py', 'engine/provisioning/seed.sh'})
            self.assertNotIn(b'private secret', data)
            self.assertEqual(build_bundle(root)[1], digest)

    def test_missing_input_is_a_safe_validation_error(self):
        with self.assertRaises(TransportError):
            local_file('/nonexistent/apex-user-data', 'User-data')

    def test_init_from_node_checkout_does_not_resolve_local_identity(self):
        from engine import cli
        root = Path(__file__).resolve().parents[1]
        with patch.object(cli, '_resolve_layout', return_value=(str(root), str(root), None, True)), \
                patch.object(cli, 'resolve', side_effect=AssertionError('local identity used')):
            with self.assertRaises(SystemExit) as caught:
                cli.main(['init', '--help'])
        self.assertEqual(caught.exception.code, 0)

    def test_dead_enrollment_unit_stops_polling_and_removes_input(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'user-data'
            source.write_bytes(b'opaque input')
            calls = []
            class Remote:
                user, host, identity = 'root', 'example.com', None
                def __init__(self, *args):
                    self.resolved_port = 22
                def resolve(self):
                    pass
                def run(self, script, data=None, **kwargs):
                    calls.append(script)
                    if 'ssh_host_' in script:
                        value = b'192.0.2.1 50000 192.0.2.2 22\nssh-ed25519 AAAA fixture\n'
                    elif 'mktemp -d /var/tmp/apex-init.' in script:
                        value = b'/var/tmp/apex-init.0123456789\n'
                    elif script.startswith('if test -f /var/tmp'):
                        value = b'stopped'
                    else:
                        value = b''
                    return subprocess.CompletedProcess([], 0, value, b'')
            messages = []
            with patch('engine.provisioning.transport.SSH', Remote), \
                    patch('engine.provisioning.transport.build_bundle', return_value=(b'bundle', 'a' * 64, 'b' * 40)), \
                    patch('engine.provisioning.transport.shutil.which', return_value='/usr/bin/ssh'):
                with self.assertRaises(TransportError) as caught:
                    initialize(root, 'root@example.com', source, progress=messages.append)
            self.assertIn('/runner.log', str(caught.exception))
            self.assertTrue(any(script.startswith('rm -f ') and '/user-data ' in script for script in calls))
            self.assertIn('root@example.com --port 22', messages[-1])

    def test_completion_failure_keeps_administrator_resume_endpoint(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / 'user-data'
            source.write_bytes(b'opaque input')
            class Remote:
                user, host, identity = 'root', 'example.com', None
                def __init__(self, *args):
                    self.resolved_port = 22
                def resolve(self):
                    pass
                def run(self, script, data=None, **kwargs):
                    if 'ssh_host_' in script:
                        value = b'192.0.2.1 50000 192.0.2.2 22\nssh-ed25519 AAAA fixture\n'
                    elif 'mktemp -d /var/tmp/apex-init.' in script:
                        value = b'/var/tmp/apex-init.0123456789\n'
                    elif script.startswith('if test -f /var/tmp'):
                        value = b'0'
                    elif 'engine.provisioning.target complete' in script:
                        raise TransportError('record failed')
                    else:
                        value = b''
                    return subprocess.CompletedProcess([], 0, value, b'')
            messages = []
            with patch('engine.provisioning.transport.SSH', Remote), \
                    patch('engine.provisioning.transport.build_bundle', return_value=(b'bundle', 'a' * 64, 'b' * 40)), \
                    patch('engine.provisioning.transport.protected_handover'), \
                    patch('engine.provisioning.transport.shutil.which', return_value='/usr/bin/ssh'):
                with self.assertRaises(TransportError):
                    initialize(root, 'root@example.com', source, progress=messages.append)
            self.assertIn('adam@example.com --port 2222', messages[-1])


class TargetPathTests(unittest.TestCase):
    """Run the real shell guards with only Debian stat/mv translated on macOS."""
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name).resolve()
        self.bin = self.root / 'bin'
        self.bin.mkdir()
        (self.bin / 'stat').write_text('#!' + sys.executable + '\n' + '''
import os, stat, sys
path = sys.argv[-1]
info = os.lstat(path)
field = sys.argv[sys.argv.index('-c') + 1]
values = {'%a': format(stat.S_IMODE(info.st_mode), 'o'), '%h': str(info.st_nlink),
          '%u': '1000' if path == os.environ.get('TEST_NONROOT_PATH') else '0'}
print(values[field])
''')
        (self.bin / 'mv').write_text('#!' + sys.executable + '\nimport os, sys\nos.replace(sys.argv[-2], sys.argv[-1])\n')
        for command in self.bin.iterdir():
            command.chmod(0o755)
        self.environment = dict(os.environ, PATH=str(self.bin) + os.pathsep + os.environ['PATH'])

    def write(self, path, data=b'new metadata'):
        return subprocess.run(['sh', '-c', remote_atomic_input(str(path))], input=data,
                              capture_output=True, env=self.environment)

    def test_atomic_write_ignores_predictable_old_temporary_symlink(self):
        destination = self.root / 'remote.json'
        victim = self.root / 'victim'
        victim.write_bytes(b'preserve me')
        destination.with_name('remote.json.new').symlink_to(victim)
        result = self.write(destination)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(destination.read_bytes(), b'new metadata')
        self.assertEqual(victim.read_bytes(), b'preserve me')
        self.assertEqual(destination.stat().st_mode & 0o777, 0o600)
        self.assertFalse(list(self.root.glob('.apex-write.*')))

    def test_destination_symlink_is_rejected_without_touching_victim(self):
        victim = self.root / 'victim'
        victim.write_bytes(b'preserve me')
        destination = self.root / 'remote.json'
        destination.symlink_to(victim)
        result = self.write(destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(victim.read_bytes(), b'preserve me')

    def test_redirected_parent_is_rejected_before_creating_files(self):
        victim = self.root / 'victim'
        victim.mkdir()
        redirected = self.root / 'apex-init'
        redirected.symlink_to(victim, target_is_directory=True)
        result = self.write(redirected / 'remote.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(victim.iterdir()), [])

    def test_unowned_parent_is_rejected_before_creating_files(self):
        state = self.root / 'apex-init'
        state.mkdir()
        self.environment['TEST_NONROOT_PATH'] = str(state)
        result = self.write(state / 'remote.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(state.iterdir()), [])

    def test_writable_ancestor_is_rejected_before_creating_files(self):
        state = self.root / 'apex-init'
        state.mkdir(mode=0o777)
        state.chmod(0o777)
        result = self.write(state / 'remote.json')
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(list(state.iterdir()), [])

    def test_hardlinked_destination_is_rejected_without_touching_victim(self):
        victim = self.root / 'victim'
        victim.write_bytes(b'preserve me')
        destination = self.root / 'remote.json'
        os.link(victim, destination)
        result = self.write(destination)
        self.assertNotEqual(result.returncode, 0)
        self.assertEqual(victim.read_bytes(), b'preserve me')


if __name__ == '__main__':
    unittest.main()
