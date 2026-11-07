"""Protected SSH handover with a separately locked, persistent rollback timer."""
from __future__ import annotations

import argparse
import base64
import fcntl
import fnmatch
import hashlib
import ipaddress
import json
import os
from pathlib import Path
import pwd
import re
import shlex
import stat
import subprocess
import sys
import tempfile
import time


class AccessError(RuntimeError):
    pass


def include_arguments(value):
    arguments, word, quote, started = [], [], None, False
    for character in value:
        if character == '\\':
            raise AccessError('Escaped SSH Include paths require manual reconciliation')
        if quote:
            if character == quote:
                quote = None
            else:
                word.append(character)
        elif character in ('"', "'"):
            quote, started = character, True
        elif character in ' \t\r':
            if started:
                arguments.append(''.join(word))
                word, started = [], False
        elif character == '#' and not started:
            break
        else:
            word.append(character)
            started = True
    if quote:
        raise AccessError('Unclosed SSH Include quote requires manual reconciliation')
    if started:
        arguments.append(''.join(word))
    if not arguments or len(arguments) > 128 or any(not item or len(item) > 4096 for item in arguments):
        raise AccessError('Unsupported SSH Include paths require manual reconciliation')
    return arguments


MANUAL_PROFILES = ('debian', 'proxmox')
OPERATOR_MATCH = """Match User adam
AuthenticationMethods publickey
PubkeyAuthentication yes
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitEmptyPasswords no
X11Forwarding no""".splitlines()


class Host:
    def __init__(self, root=Path('/'), runner=subprocess.run, profile=None):
        if profile is not None and profile not in MANUAL_PROFILES:
            raise AccessError('Unknown SSH access profile')
        self.profile = profile
        self.root = Path(root).resolve()
        self.runner = runner

    def path(self, path):
        return self.root / path.lstrip('/')

    def command(self, args, check=True):
        result = self.runner(args, capture_output=True, text=True, timeout=60)
        if check and result.returncode:
            raise AccessError('Access command failed: ' + args[0])
        return result

    def boot_id(self):
        return self.path('/proc/sys/kernel/random/boot_id').read_text().strip()

    def fingerprint(self):
        digest = hashlib.sha256()
        paths = [self.path('/etc/ssh/sshd_config'), self.path('/etc/default/ssh')]
        for directory in ('/etc/ssh/sshd_config.d', '/etc/systemd/system/ssh.service.d',
                          '/run/systemd/system/ssh.service.d', '/usr/lib/systemd/system/ssh.service.d',
                          '/lib/systemd/system/ssh.service.d'):
            paths += sorted(self.path(directory).glob('*.conf'))
        for path in paths:
            digest.update(str(path.relative_to(self.root)).encode())
            digest.update(path.read_bytes() if path.exists() else b'<absent>')
        return digest.hexdigest()

    def unconditional_configuration(self):
        remaining, directory_entries, visited = 1024 * 1024, 0, set()

        def expand(pattern):
            nonlocal directory_entries
            if any(token in pattern for token in ('[:', '[.', '[=', '[^')):
                raise AccessError('Extended SSH Include glob syntax requires manual reconciliation')
            absolute = pattern if pattern.startswith('/') else '/etc/ssh/' + pattern
            mapped = self.path(absolute)
            if len(mapped.parts) > 64:
                raise AccessError('SSH Include path exceeds supported limits; manual reconciliation required')
            candidates = [Path(mapped.anchor)]
            for component in mapped.parts[1:]:
                expanded = []
                for parent in candidates:
                    if not any(character in component for character in '*?['):
                        candidate = parent / component
                        if os.path.lexists(candidate):
                            expanded.append(candidate)
                        continue
                    try:
                        with os.scandir(parent) as entries:
                            for entry in entries:
                                directory_entries += 1
                                if directory_entries > 4096:
                                    raise AccessError('SSH Include traversal exceeds supported limits; manual reconciliation required')
                                if entry.name.startswith('.') and not component.startswith('.'):
                                    continue
                                if fnmatch.fnmatchcase(entry.name, component):
                                    expanded.append(Path(entry.path))
                    except (FileNotFoundError, NotADirectoryError):
                        continue
                if len(expanded) > 128:
                    raise AccessError('SSH Include expansion exceeds supported limits; manual reconciliation required')
                candidates = expanded
            return sorted(candidates)

        def inspect(path, ancestors):
            nonlocal remaining
            information = path.stat()
            identity = (information.st_dev, information.st_ino)
            if identity in ancestors or len(ancestors) >= 16:
                raise AccessError('Recursive SSH Include configuration requires manual reconciliation')
            if identity in visited:
                return
            if len(visited) >= 128 or not stat.S_ISREG(information.st_mode) or information.st_size > remaining:
                raise AccessError('SSH configuration exceeds supported inspection limits; manual reconciliation required')
            visited.add(identity)
            with path.open('rb') as stream:
                content = stream.read(remaining + 1)
            remaining -= len(content)
            if remaining < 0:
                raise AccessError('SSH configuration exceeds supported inspection limits; manual reconciliation required')
            lines = content.decode().splitlines()
            if self.profile and path == self.path('/etc/ssh/sshd_config'):
                # The legacy action appended this exact terminal block. Any additional
                # directives would change its meaning and require manual inspection.
                marker = '# APEX operator SSH policy'
                if marker in lines:
                    position = lines.index(marker)
                    if [item.strip() for item in lines[position + 1:] if item.strip()] == OPERATOR_MATCH:
                        lines = lines[:position]
            for line in lines:
                line = line.lstrip()
                if not line or line.startswith('#'):
                    continue
                if line.startswith('"'):
                    keyword, separator, value = line[1:].partition('"')
                    if not separator:
                        raise AccessError('Unsupported SSH keyword quoting requires manual reconciliation')
                else:
                    parsed = re.match(r'([^\s=]+)(.*)', line)
                    if not parsed:
                        raise AccessError('Unsupported SSH configuration syntax requires manual reconciliation')
                    keyword, value = parsed.groups()
                    value = value.lstrip()
                    if value.startswith('='):
                        value = value[1:]
                if keyword.lower() == 'match':
                    raise AccessError('SSH Match directives require manual reconciliation before initialization')
                if keyword.lower() != 'include':
                    continue
                for pattern in include_arguments(value):
                    for included in expand(pattern):
                        inspect(included, ancestors | {identity})

        try:
            inspect(self.path('/etc/ssh/sshd_config'), set())
        except (OSError, UnicodeError) as exc:
            raise AccessError('Cannot inspect SSH configuration and includes; manual reconciliation required') from exc

    def bootstrap_preflight(self):
        for activity in ('is-active', 'is-enabled'):
            if self.command(['systemctl', activity, '--quiet', 'ssh.socket'], False).returncode == 0:
                raise AccessError('SSH socket activation requires manual handover; no access changes made')
        if self.command(['systemctl', 'is-active', '--quiet', 'ssh.service'], False).returncode:
            raise AccessError('An active ssh.service is required')
        mode = self.command(['systemctl', 'show', 'ssh.service', '-p', 'KillMode', '--value']).stdout.strip()
        if mode != 'process':
            raise AccessError('ssh.service must preserve established sessions with KillMode=process')
        arguments = self.service_arguments()
        managed = self.path('/etc/systemd/system/ssh.service.d/apex-init.conf')
        if managed.exists():
            commands = [line.removeprefix('ExecStart=') for line in managed.read_text().splitlines()
                        if line.startswith('ExecStart=') and line != 'ExecStart=']
            if len(commands) != 1 or arguments != shlex.split(commands[0]):
                raise AccessError('Active SSH service configuration differs from its managed invocation')
        elif arguments != ['/usr/sbin/sshd', '-D']:
            raise AccessError('Custom SSH service invocation requires manual access reconciliation')
        defaults = self.path('/etc/default/ssh')
        if defaults.exists():
            for line in defaults.read_text().splitlines():
                if re.match(r'^\s*SSHD_OPTS\s*=', line):
                    value = line.split('=', 1)[1].strip().strip('\"\'')
                    if value:
                        raise AccessError('Custom SSHD_OPTS require manual access reconciliation')
        self.unconditional_configuration()
        self.command(['/usr/sbin/sshd', '-t'])
        self.live_arguments()

    def preflight(self):
        self.bootstrap_preflight()
        self.command(['visudo', '-c'])
        self.command(['runuser', '-u', 'adam', '--', 'sudo', '-n', 'true'])

    def service_arguments(self):
        output = self.command(['systemctl', 'show', 'ssh.service', '-p', 'ExecStart', '--value']).stdout
        matches = re.findall(r'argv\[\]=([^;]+)', output)
        if len(matches) != 1:
            raise AccessError('Unable to verify the SSH service invocation')
        arguments = shlex.split(matches[0])
        # Debian's stock unit has this optional variable. Its actual expansion is
        # independently checked against /proc below, so a nonempty value fails.
        if arguments == ['/usr/sbin/sshd', '-D', '$SSHD_OPTS']:
            arguments.pop()
        if not arguments or arguments[0] != '/usr/sbin/sshd':
            raise AccessError('SSH service does not invoke the supported daemon')
        return arguments

    def live_arguments(self):
        expected = self.service_arguments()
        pid = self.command(['systemctl', 'show', 'ssh.service', '-p', 'MainPID', '--value']).stdout.strip()
        if not pid.isdecimal() or int(pid) <= 1:
            raise AccessError('SSH service has no verifiable active listener process')
        process = self.path('/proc/' + pid)
        try:
            if os.readlink(process / 'exe') != '/usr/sbin/sshd':
                raise AccessError('Active SSH process does not run the supported daemon')
            parts = [part for part in (process / 'cmdline').read_bytes().decode().split('\0') if part]
        except (OSError, UnicodeError):
            raise AccessError('Unable to inspect the active SSH listener invocation') from None
        if len(parts) == 1 and parts[0].startswith('sshd: ') and ' [listener]' in parts[0]:
            actual = shlex.split(parts[0][6:].split(' [listener]', 1)[0])
        else:
            actual = parts
        if actual != expected:
            raise AccessError('Running SSH listener differs from the active service invocation')
        return actual, pid

    def verify_active(self, ports, final, address):
        args, pid = self.live_arguments()
        self.verify_policy([arg for arg in args if arg != '-D'], ports, final, address)
        if self.listener_ports(pid) != set(ports):
            raise AccessError('Active SSH listening ports differ from the declared policy')

    def listener_ports(self, pid):
        output = self.command(['ss', '-H', '-ltnp']).stdout
        actual_ports = set()
        for line in output.splitlines():
            if '("sshd",' not in line:
                continue
            if set(re.findall(r'pid=(\d+)', line)) != {pid}:
                raise AccessError('An additional SSH listener exists outside the managed service')
            fields = line.split()
            if len(fields) < 5 or not fields[3].rsplit(':', 1)[-1].isdecimal():
                raise AccessError('Unable to verify the active SSH listening sockets')
            actual_ports.add(int(fields[3].rsplit(':', 1)[-1]))
        return actual_ports

    @property
    def final_ports(self):
        return [22, 2222] if self.profile == 'proxmox' else [2222]

    def authentication_options(self):
        proxmox = self.profile == 'proxmox'
        options = {'PermitRootLogin': 'prohibit-password' if proxmox else 'no',
                   'PasswordAuthentication': 'no', 'KbdInteractiveAuthentication': 'no',
                   'PubkeyAuthentication': 'yes', 'AuthenticationMethods': 'publickey'}
        if not proxmox:
            options['AllowUsers'] = 'adam'
        options.update(X11Forwarding='no', PermitEmptyPasswords='no')
        return options

    def policy(self, ports, final, address):
        args = ['/usr/sbin/sshd']
        for port in sorted(set(ports)):
            args += ['-p', str(port)]
        if final:
            for key, value in self.authentication_options().items():
                args += ['-o', f'{key}={value}']
        elif self.profile:
            # Keep existing managed authentication options when adding the probe port.
            current = iter(self.service_arguments()[1:])
            for flag in current:
                if flag == '-D':
                    continue
                value = next(current, '')
                if flag == '-p' and value.isdecimal():
                    continue
                if flag != '-o' or not re.fullmatch(r'[A-Za-z]+=[A-Za-z0-9,-]+', value):
                    raise AccessError('Cannot safely extend the existing SSH invocation')
                args += [flag, value]
        self.verify_policy(args, ports, final, address)
        return args

    def verify_policy(self, args, ports, final, address):
        self.unconditional_configuration()
        self.command(args + ['-t'])
        users = {'adam', 'root'}
        if final:
            users.update(account.pw_name for account in pwd.getpwall())
        for user in sorted(users):
            output = self.command(args + ['-T', '-C', f'user={user},host=localhost,addr={address}']).stdout
            values = {}
            for line in output.splitlines():
                key, _, value = line.partition(' ')
                values.setdefault(key, []).append(value)
            if set(values.get('port', [])) != {str(p) for p in ports}:
                raise AccessError('Effective SSH ports do not match the handover policy')
            if final:
                expected = {key.lower(): [value] for key, value in self.authentication_options().items()}
                if self.profile == 'proxmox':
                    if values.get('permitrootlogin') == ['without-password']:
                        values['permitrootlogin'] = ['prohibit-password']
                    if any(key in values for key in ('allowusers', 'denyusers', 'allowgroups', 'denygroups')):
                        raise AccessError('SSH account restrictions conflict with Proxmox root-key access')
                if any(values.get(key) != value for key, value in expected.items()):
                    raise AccessError('Effective SSH authentication policy is inconsistent')

    def install_policy(self, ports, final, address):
        args = self.policy(ports, final, address)
        path = self.path('/etc/systemd/system/ssh.service.d/apex-init.conf')
        atomic(path, ('[Service]\nExecStart=\nExecStart=' + ' '.join(args + ['-D']) + '\n').encode(), 0o644)
        self.command(['systemctl', 'daemon-reload'])
        actual = self.command(['systemctl', 'show', 'ssh.service', '-p', 'ExecStart', '--value']).stdout
        match = re.search(r'argv\[\]=([^;]+)', actual)
        if not match or shlex.split(match[1]) != args + ['-D']:
            raise AccessError('Another service override prevents the managed SSH policy')
        self.command(['systemctl', 'restart', 'ssh.service'])
        self.verify_active(ports, final, address)

    def arm(self):
        bundle = Path(__file__).resolve().parents[1]
        if not re.fullmatch(r'/usr/local/lib/apex-provisioner/[a-f0-9]{64}', str(bundle)):
            raise AccessError('Access runner must execute from the installed root-owned bundle')
        service = ('[Unit]\nDescription=APEX pending access rollback\nAfter=local-fs.target\n'
                   '[Service]\nType=oneshot\nWorkingDirectory=' + str(bundle) + '\n'
                   'ExecStart=/usr/bin/python3 -m provisioning.access tick\n')
        timer = ('[Unit]\nDescription=APEX access rollback deadline and boot recovery\n'
                 '[Timer]\nOnBootSec=5s\nOnUnitActiveSec=10s\nAccuracySec=1s\n'
                 '[Install]\nWantedBy=timers.target\n')
        atomic(self.path('/etc/systemd/system/apex-access-rollback.service'), service.encode(), 0o644)
        atomic(self.path('/etc/systemd/system/apex-access-rollback.timer'), timer.encode(), 0o644)
        self.command(['systemctl', 'daemon-reload'])
        self.command(['systemctl', 'enable', '--now', 'apex-access-rollback.timer'])

    def disarm(self):
        self.command(['systemctl', 'disable', '--now', 'apex-access-rollback.timer'])

    def restore(self, state):
        path = self.path('/etc/systemd/system/ssh.service.d/apex-init.conf')
        if state['previous'] is None:
            path.unlink(missing_ok=True)
        else:
            atomic(path, base64.b64decode(state['previous']), 0o644)
        self.command(['/usr/sbin/sshd', '-t'])
        self.command(['systemctl', 'daemon-reload'])
        self.command(['systemctl', 'restart', 'ssh.service'])


def atomic(path, data, mode=0o600):
    path = Path(path)
    for parent in [path, *path.parents]:
        if parent.is_symlink():
            raise AccessError('Refusing a symlink in a managed access path')
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, temporary = tempfile.mkstemp(prefix='.apex-access-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)
    directory = os.open(path.parent, os.O_RDONLY | os.O_DIRECTORY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


class Transition:
    def __init__(self, host=None, now=time.monotonic, profile=None):
        if profile is not None and profile not in MANUAL_PROFILES:
            raise AccessError('Unknown SSH access profile')
        self.profile = profile
        self.host = host or Host()
        self.host.profile = profile
        self.now = now
        self.directory = self.host.path('/var/lib/apex-init/access')

    def load(self):
        path = self.directory / 'manual-state.json'
        return json.loads(path.read_text()) if path.exists() else None

    def save(self, state):
        atomic(self.directory / 'manual-state.json', json.dumps(state, sort_keys=True).encode())

    def execute(self, operation, token=None, original_port=None, client_address=None):
        # The watchdog must recover SSH even when another APEX action holds init.lock.
        if operation in ('tick', 'rollback'):
            return self._execute(operation, token, original_port, client_address)
        from .state import ProvisionError, exclusive_lock
        try:
            with exclusive_lock(self.host.path('/var/lib/apex-init/init.lock')):
                return self._execute(operation, token, original_port, client_address)
        except ProvisionError as error:
            raise AccessError(str(error)) from None

    def _execute(self, operation, token=None, original_port=None, client_address=None):
        if token is not None and not re.fullmatch(r'[a-f0-9]{32}', token):
            raise AccessError('Invalid access transition token')
        for path in [self.directory, *self.directory.parents]:
            if path.is_symlink():
                raise AccessError('Invalid access state directory')
        self.directory.mkdir(parents=True, exist_ok=True, mode=0o700)
        if self.directory.stat().st_uid != os.geteuid():
            raise AccessError('Access state directory is not owned by the provisioner user')
        os.chmod(self.directory, 0o700)
        descriptor = os.open(self.directory / 'lock', os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        with os.fdopen(descriptor, 'w') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = self.load()
            active = ('transition', 'final', 'recovery_pending')
            # A historical bundle owns its pending rollback and the shared timer.
            legacy_path = self.directory / 'state.json'
            if legacy_path.exists():
                legacy = json.loads(legacy_path.read_text())
                if legacy['status'] in active or legacy.get('disarm_pending'):
                    raise AccessError('Another access workflow is pending; wait for rollback before retrying')
            if state and self.profile and state.get('profile') != self.profile and operation != 'tick':
                if operation != 'begin' or state['status'] in active:
                    raise AccessError('Access transition belongs to a different platform')
                state = None
            if state and state.get('disarm_pending') and state['status'] in ('committed', 'rolled_back'):
                self._disarm(state)
            if (state and token and token == state.get('token')
                    and (operation, state['status']) in (('commit', 'committed'), ('rollback', 'rolled_back'))):
                return {'status': state['status']}
            if state and state['status'] == 'recovery_pending':
                self._rollback(state)
                if operation != 'tick':
                    raise AccessError('Pending recovery completed; retry initialization')
                return {'status': 'rolled_back'}
            pending = state and state['status'] in ('transition', 'final')
            if pending and (self.now() >= state['deadline'] or self.host.boot_id() != state['boot_id']):
                self._rollback(state)
                if operation != 'tick':
                    raise AccessError('Uncommitted access transition expired or rebooted; previous access restored')
                return {'status': 'rolled_back'}
            if operation == 'tick':
                return {'status': state['status'] if state else 'absent'}
            if operation == 'begin':
                if pending:
                    raise AccessError('A pending access transition exists; wait for rollback before retrying')
                if not token or not original_port or not 1 <= original_port <= 65535:
                    raise AccessError('Access transition requires an original port and token')
                try:
                    ipaddress.ip_address(client_address)
                except ValueError:
                    raise AccessError('Invalid SSH client address') from None
                self.host.preflight()
                path = self.host.path('/etc/systemd/system/ssh.service.d/apex-init.conf')
                return self._begin_manual(token, original_port, client_address, path)
            if not pending or token != state['token']:
                raise AccessError('No matching pending access transition')
            if operation == 'rollback':
                self._rollback(state)
                return {'status': 'rolled_back'}
            if state.get('fingerprint') != self.host.fingerprint():
                raise AccessError('External SSH configuration changed; finalization stopped, rollback remains armed')
            if operation == 'finalize':
                if state['status'] != 'transition':
                    raise AccessError('Access transition is not ready for final policy')
                try:
                    if not state.get('preserve_policy'):
                        state['ssh_changed'] = True
                        self.save(state)
                        self.host.install_policy(self.host.final_ports, True, state['client_address'])
                    state['authenticated_after_us'] = time.monotonic_ns() // 1000
                    state['status'] = 'final'
                    state['fingerprint'] = self.host.fingerprint()
                    self.save(state)
                except Exception:
                    self._rollback(state)
                    raise
                return {'status': 'final'}
            if operation == 'commit' and state['status'] == 'final':
                self.host.verify_active(self.host.final_ports, True, state['client_address'])
                state['status'] = 'committed'
                state['disarm_pending'] = True
                self.save(state)  # Timer observes this before it can restore anything.
                self._disarm(state)
                return {'status': 'committed'}
            raise AccessError('Invalid access transition operation')

    def _begin_manual(self, token, original_port, address, path):
        _, pid = self.host.live_arguments()
        ports = self.host.listener_ports(pid)
        if original_port not in ports:
            raise AccessError('Original SSH port is not active on the managed daemon')
        preserve_policy = False
        if ports == set(self.host.final_ports):
            try:
                self.host.verify_active(self.host.final_ports, True, address)
                preserve_policy = True
            except AccessError:
                pass
        previous = base64.b64encode(path.read_bytes()).decode() if path.exists() else None
        state = {'status': 'transition', 'profile': self.profile, 'token': token,
                 'deadline': self.now() + 300, 'boot_id': self.host.boot_id(),
                 'bundle': str(Path(__file__).resolve().parents[1]),
                 'client_address': address, 'original_port': original_port, 'previous': previous,
                 'preserve_policy': preserve_policy, 'ssh_changed': 2222 not in ports}
        self.host.arm()
        self.save(state)
        try:
            if 2222 not in ports:
                self.host.install_policy(sorted(ports | {2222}), False, address)
            state['fingerprint'] = self.host.fingerprint()
            state['authenticated_after_us'] = time.monotonic_ns() // 1000
            self.save(state)
        except Exception:
            self._rollback(state)
            raise
        return {'status': 'transition'}

    def _rollback(self, state):
        errors = []
        if not state.get('ssh_restored') and state.get('ssh_changed', True):
            try:
                self.host.restore(state)
                state['ssh_restored'] = True
            except Exception:
                errors.append('ssh')
        if errors:
            state['status'] = 'recovery_pending'
            state['recovery_errors'] = errors
            self.save(state)
            raise AccessError('Recovery remains pending for ' + ', '.join(errors) +
                              '; watchdog remains armed; inspect the root-owned access record')
        state['status'] = 'rolled_back'
        state.pop('recovery_errors', None)
        state['disarm_pending'] = True
        self.save(state)
        self._disarm(state)

    def _disarm(self, state):
        self.host.disarm()
        state.pop('disarm_pending', None)
        self.save(state)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=['begin', 'finalize', 'commit', 'rollback', 'tick'])
    parser.add_argument('--profile', choices=MANUAL_PROFILES)
    parser.add_argument('--token')
    parser.add_argument('--original-port', type=int)
    parser.add_argument('--client-address')
    args = parser.parse_args(argv)
    if os.geteuid() != 0:
        parser.error('Access handover requires root')
    try:
        if args.operation != 'tick' and not args.profile:
            parser.error('A host profile is required')
        result = Transition(profile=args.profile).execute(args.operation, args.token, args.original_port, args.client_address)
        print(json.dumps(result))
        return 0
    except AccessError as exc:
        print(str(exc), file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
