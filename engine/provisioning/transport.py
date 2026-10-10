"""Workstation SSH transport. User-data stays opaque until target validation."""
from __future__ import annotations

import hashlib
import io
import ipaddress
import json
import os
from pathlib import Path
import re
import shlex
import shutil
import subprocess
import tarfile
import tempfile
import time
import uuid


class TransportError(RuntimeError):
    pass


class HandoverUncertain(TransportError):
    pass


# This runs before Python is guaranteed to exist on the Debian target. All
# ancestors must be trusted; a root-owned sticky temporary directory is safe
# only because root owns each managed child and exclusive mktemp creates it.
REMOTE_PATH_GUARD = r'''
set -eu
umask 077
apex_guard_dir() {
    apex_checked=$1
    while :; do
        test ! -L "$apex_checked" && test -d "$apex_checked" || exit 91
        test "$(stat -c %u -- "$apex_checked")" = 0 || exit 91
        apex_mode=$(stat -c %a -- "$apex_checked")
        test "$((0$apex_mode & 0022))" = 0 || test "$((0$apex_mode & 01000))" != 0 || exit 91
        test "$apex_checked" != / || break
        apex_checked=${apex_checked%/*}
        test -n "$apex_checked" || apex_checked=/
    done
}
apex_ensure_dir() {
    apex_new_dir=$1
    if test -e "$apex_new_dir" || test -L "$apex_new_dir"; then
        apex_guard_dir "$apex_new_dir"
        apex_mode=$(stat -c %a -- "$apex_new_dir")
        test "$((0$apex_mode & 0022))" = 0 || exit 91
    else
        apex_guard_dir "${apex_new_dir%/*}"
        mkdir -m "$2" -- "$apex_new_dir"
        apex_guard_dir "$apex_new_dir"
    fi
}
apex_guard_file() {
    apex_file=$1
    apex_guard_dir "${apex_file%/*}"
    apex_mode=$(stat -c %a -- "${apex_file%/*}")
    test "$((0$apex_mode & 0022))" = 0 || exit 91
    test ! -L "$apex_file" || exit 91
    if test -e "$apex_file"; then
        test -f "$apex_file" || exit 91
        test "$(stat -c %u -- "$apex_file")" = 0 || exit 91
        test "$(stat -c %h -- "$apex_file")" = 1 || exit 91
        apex_mode=$(stat -c %a -- "$apex_file")
        test "$((0$apex_mode & 0022))" = 0 || exit 91
    fi
}
apex_atomic_input() {
    apex_destination=$1
    apex_guard_file "$apex_destination"
    apex_temporary=$(mktemp "${apex_destination%/*}/.apex-write.XXXXXXXXXX")
    trap 'rm -f -- "$apex_temporary"' EXIT HUP INT TERM
    cat > "$apex_temporary"
    chmod 0600 "$apex_temporary"
    apex_guard_file "$apex_destination"
    mv -T -- "$apex_temporary" "$apex_destination"
    trap - EXIT HUP INT TERM
}
'''


def remote_atomic_input(path):
    return REMOTE_PATH_GUARD + '\napex_atomic_input ' + shlex.quote(path)


def remote_bundle_install(stage, installed, digest):
    return REMOTE_PATH_GUARD + '\n' + (
        'apex_guard_dir ' + shlex.quote(stage) + '; apex_guard_file ' + shlex.quote(stage + '/bundle.tar') + '; '
        'test "$(sha256sum ' + shlex.quote(stage + '/bundle.tar') + ' | cut -d " " -f 1)" = ' + digest + '; '
        'apex_ensure_dir /usr/local/lib/apex-provisioner 0755; '
        'if test -e ' + installed + ' || test -L ' + installed + '; then '
        'apex_guard_dir ' + installed + '; '
        'apex_bad=$(find ' + installed + ' -xdev \\( -type l -o ! -user root -o -perm /022 -o \\( ! -type d -a ! -type f \\) \\) -print -quit); test -z "$apex_bad"; '
        'else apex_install_tmp=$(mktemp -d /usr/local/lib/apex-provisioner/.install.XXXXXXXXXX); '
        'trap \'rm -rf -- "$apex_install_tmp"\' EXIT HUP INT TERM; '
        'tar --no-same-owner --same-permissions -xf ' + stage + '/bundle.tar -C "$apex_install_tmp"; '
        'mv -T -- "$apex_install_tmp" ' + installed + '; trap - EXIT HUP INT TERM; fi; '
        'tar --compare -f ' + stage + '/bundle.tar -C ' + installed)


def local_file(value, name):
    if not value or any(ord(c) < 32 for c in str(value)):
        raise TransportError(f'{name} must name a readable regular file')
    try:
        path = Path(value).expanduser().resolve(strict=True)
    except (OSError, ValueError):
        raise TransportError(f'{name} must name a readable regular file') from None
    if not path.is_file() or not os.access(path, os.R_OK):
        raise TransportError(f'{name} must name a readable regular file')
    return path


def destination(value, port=None):
    match = re.fullmatch(r'([A-Za-z_][A-Za-z0-9_.-]{0,31})@([A-Za-z0-9][A-Za-z0-9_.:-]*|\[[0-9a-fA-F:]+\])', value or '')
    if not match or '..' in match[2]:
        raise TransportError('Target must be USER@HOST without SSH options; use root or an existing administrator with sudo -n')
    hostname = match[2].strip('[]')
    if ':' in hostname:
        try:
            ipaddress.IPv6Address(hostname)
        except ValueError:
            raise TransportError('IPv6 targets must contain a valid address; pass ports separately') from None
    if port is not None and (not re.fullmatch(r'[0-9]{1,5}', str(port)) or not 1 <= int(port) <= 65535):
        raise TransportError('Port must be an integer from 1 through 65535')
    return match[1], hostname, int(port) if port is not None else None


def build_bundle(commons):
    root = Path(commons)
    files = sorted((root / 'engine').rglob('*.py'))
    seed = root / 'engine/provisioning/seed.sh'
    if not seed.is_file():
        raise TransportError('Provisioning seed is missing')
    files.append(seed)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for path in files:
            if path.is_symlink() or '__pycache__' in path.parts:
                raise TransportError('Provisioning bundle contains an unsafe source path')
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size, info.mode, info.mtime = len(data), (0o755 if path == seed else 0o644), 0
            info.uid = info.gid = 0
            archive.addfile(info, io.BytesIO(data))
    data = output.getvalue()
    revision = 'unversioned-modified'
    if shutil.which('git'):
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and re.fullmatch(r'[a-f0-9]{40,64}\n?', result.stdout):
            revision = result.stdout.strip()
            dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--', 'engine'], capture_output=True, text=True, timeout=30)
            if dirty.returncode or dirty.stdout.strip():
                revision += '-modified'
    return data, hashlib.sha256(data).hexdigest(), revision


class SSH:
    def __init__(self, target, port=None, identity_file=None, runner=subprocess.run):
        self.user, self.host, self.port = destination(target, port)
        self.identity = local_file(identity_file, 'Identity file') if identity_file else None
        self.runner = runner
        self.resolved_host = None
        self.resolved_port = None
        self.trust_file = None

    def arguments(self, fresh=False):
        args = ['ssh', '-T', '-o', 'StrictHostKeyChecking=yes', '-o', 'ConnectTimeout=15',
                '-o', 'ServerAliveInterval=10', '-o', 'ServerAliveCountMax=3']
        if self.identity:
            args += ['-i', str(self.identity)]
        args += ['-l', 'adam' if fresh else self.user]
        if fresh:
            if not self.trust_file or not self.resolved_host:
                raise TransportError('Fresh access requires the verified bootstrap host identity')
            args += ['-p', '2222', '-o', 'ControlMaster=no', '-o', 'ControlPath=none',
                     '-o', 'ControlPersist=no', '-o', 'BatchMode=yes',
                     '-o', 'PreferredAuthentications=publickey', '-o', 'PasswordAuthentication=no',
                     '-o', 'KbdInteractiveAuthentication=no', '-o', 'Hostname=' + self.resolved_host,
                     '-o', 'HostKeyAlias=apex-init-verified-target',
                     '-o', 'UserKnownHostsFile=' + str(self.trust_file),
                     '-o', 'GlobalKnownHostsFile=/dev/null']
        elif self.port:
            args += ['-p', str(self.port)]
        return args

    def resolve(self):
        result = self.runner(self.arguments() + ['-G', '--', self.host], capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise TransportError('Unable to resolve SSH connection configuration')
        values = dict(line.split(' ', 1) for line in result.stdout.splitlines() if ' ' in line)
        hostname = values.get('hostname', '')
        port = values.get('port', '')
        destination(self.user + '@' + hostname, port)
        self.resolved_host, self.resolved_port = hostname, int(port)

    def run(self, script, data=None, fresh=False, privileged=True, check=True, timeout=300):
        command = ['sh', '-c', script]
        if privileged and (fresh or self.user != 'root'):
            command = ['sudo', '-n', '--', *command]
        result = self.runner(self.arguments(fresh) + ['--', self.host, shlex.join(command)],
                             input=data, capture_output=True, timeout=timeout)
        if check and result.returncode:
            # SSH and child diagnostics can contain user-data or credential-bearing URLs.
            raise TransportError('Remote operation failed; inspect the root-owned target status for a redacted diagnosis')
        return result

    def fresh_login(self):
        self.run('test "$(id -u)" = 0', fresh=True)


def protected_handover(ssh, invoke, token, port, client_address):
    result = invoke('begin', token, port, client_address, False)
    try:
        ssh.fresh_login()
        if result['status'] == 'committed':
            return
        if result['status'] != 'existing':
            invoke('finalize', token, None, None, True)
            ssh.fresh_login()
        try:
            invoke('commit', token, None, None, True)
        except Exception:
            try:
                state = invoke('status', token, None, None, True)
                if state.get('token') != token or state.get('status') not in ('committed', 'existing', 'transition', 'final', 'rolled_back', 'recovery_pending'):
                    raise TransportError('Unexpected access transition state')
            except Exception:
                raise HandoverUncertain('Access commit outcome is unknown; verify the administrator and bootstrap endpoints before resuming') from None
            if state['status'] != 'committed':
                raise
    except HandoverUncertain:
        raise
    except Exception:
        try:
            invoke('rollback', token, None, None, True)
        except Exception:
            try:
                invoke('rollback', token, None, None, False)
            except Exception:
                pass  # Persistent timer is authoritative if both connections are gone.
        raise


def initialize(commons, target, user_data, port=None, identity_file=None, progress=print):
    if not shutil.which('ssh'):
        raise TransportError('OpenSSH ssh is required on the workstation')
    source = local_file(user_data, 'User-data')
    if source.stat().st_size > 1024 * 1024:
        raise TransportError('User-data must be no larger than 1 MiB')
    ssh = SSH(target, port, identity_file)
    ssh.resolve()
    bundle, digest, revision = build_bundle(commons)
    run_id = uuid.uuid4().hex
    installed = '/usr/local/lib/apex-provisioner/' + digest
    stage = None
    launched = False
    finished = False
    access_committed = False
    access_uncertain = False
    cleanup_policy = '/etc/tmpfiles.d/apex-init-' + run_id + '.conf'
    with tempfile.TemporaryDirectory(prefix='apex-init-trust-') as local:
        try:
            preflight = ssh.run('set -eu; test "$(id -u)" = 0; . /etc/os-release; '
                                'test "$ID" = debian; test "$VERSION_ID" = 13; '
                                'test "$(dpkg --print-architecture)" = amd64; '
                                'command -v systemd-run >/dev/null; '
                                'printf "%s\\n" "${SSH_CONNECTION:-}"; '
                                'cat /etc/ssh/ssh_host_*_key.pub')
            lines = preflight.stdout.decode().splitlines()
            connection = lines[0].split() if lines else []
            if len(connection) != 4:
                # sudo commonly clears SSH_CONNECTION; read it before elevation instead.
                raw = ssh.run('printf "%s\\n" "$SSH_CONNECTION"', privileged=False).stdout.decode().split()
                connection = raw
            if len(connection) != 4:
                raise TransportError('Unable to inspect the established SSH connection')
            keys = [line for line in lines[1:] if re.fullmatch(r'(ssh-[\w-]+|ecdsa-[\w-]+) [A-Za-z0-9+/=]+(?: .*?)?', line)]
            if not keys:
                raise TransportError('Unable to obtain public host keys through the verified connection')
            trust = Path(local) / 'known_hosts'
            trust.write_text(''.join('apex-init-verified-target,[apex-init-verified-target]:2222 ' + ' '.join(key.split()[:2]) + '\n' for key in keys))
            trust.chmod(0o600)
            ssh.trust_file = trust
            existing = ssh.run(REMOTE_PATH_GUARD + '\napex_ensure_dir /var/lib/apex-init 0700; '
                               'apex_guard_file /var/lib/apex-init/remote.json; '
                               'if test -f /var/lib/apex-init/remote.json; then cat /var/lib/apex-init/remote.json; fi').stdout
            if existing:
                prior = json.loads(existing)
                previous_unit = prior.get('unit', '')
                if not re.fullmatch(r'apex-init-[a-f0-9]{32}', previous_unit):
                    raise TransportError('Existing remote coordinator state requires manual inspection')
                active = ssh.run(shlex.join(['systemctl', 'is-active', '--quiet', previous_unit]), check=False)
                if active.returncode == 0:
                    raise TransportError('An enrollment unit is still running: ' + previous_unit + '; wait for it to settle before resuming')
            stage = ssh.run(REMOTE_PATH_GUARD + '\napex_guard_dir /var/tmp; mktemp -d /var/tmp/apex-init.XXXXXXXXXX').stdout.decode().strip()
            if not re.fullmatch(r'/var/tmp/apex-init\.[A-Za-z0-9]{10}', stage):
                raise TransportError('Target returned an invalid staging path')
            # Hard reboot can bypass the shell trap; boot-time tmpfiles removes opaque inputs.
            cleanup = ('r ' + stage + '/user-data - - - -\n' +
                       'r ' + stage + '/heartbeat - - - -\n').encode()
            ssh.run(REMOTE_PATH_GUARD + '\napex_ensure_dir /etc/tmpfiles.d 0755; apex_atomic_input ' + cleanup_policy, cleanup)
            ssh.run(remote_atomic_input(stage + '/bundle.tar'), bundle)
            ssh.run(remote_atomic_input(stage + '/user-data'), source.read_bytes())
            install = remote_bundle_install(stage, installed, digest)
            progress('Installing the versioned target runtime and validating enrollment input.')
            ssh.run(install)
            base = ['python3', '-m', 'engine.provisioning.target']
            options = ['--user-data', stage + '/user-data', '--revision', revision, '--bundle-digest', digest]
            # State remains inspectable after disconnect; user-data is removed by the durable unit.
            enrollment = shlex.join(base + ['enroll'] + options + ['--heartbeat', stage + '/heartbeat'])
            validation = shlex.join(base + ['validate'] + options)
            access_preflight = shlex.join(['python3', '-m', 'engine.provisioning.access', 'preflight'])
            seed = shlex.join(['bash', installed + '/engine/provisioning/seed.sh'])
            wrapper = ('umask 077; cd ' + installed + '; '
                       'trap ' + shlex.quote('rm -f ' + stage + '/user-data ' + stage + '/heartbeat ' + cleanup_policy) + ' EXIT; '
                       '{ ' + seed + ' && ' + validation + ' && ' + access_preflight + ' && ' + enrollment + '; } >' + stage + '/runner.log 2>&1; rc=$?; '
                       'printf "%s\\n" "$rc" >' + stage + '/result.new; mv ' + stage + '/result.new ' + stage + '/result; exit "$rc"')
            unit = 'apex-init-' + run_id
            record = json.dumps({'unit': unit, 'staging': stage, 'revision': revision, 'bundle_digest': digest}).encode()
            ssh.run(remote_atomic_input('/var/lib/apex-init/remote.json'), record)
            ssh.run('touch ' + stage + '/heartbeat; ' + shlex.join(['systemd-run', '--quiet', '--unit', unit,
                    '--property=Type=exec', '--property=KillMode=process', '/bin/sh', '-c', wrapper]))
            launched = True
            progress('Enrollment is running under ' + unit + '; target staging: ' + stage)
            while True:
                status = ssh.run('if test -f ' + stage + '/result; then cat ' + stage + '/result; '
                                 'elif systemctl is-active --quiet ' + unit + '; then touch ' + stage + '/heartbeat; printf running; '
                                 'else printf stopped; fi').stdout.decode().strip()
                if status != 'running':
                    finished = True
                    if status != '0':
                        raise TransportError('Target provisioning failed; inspect root-owned ' + stage +
                                             '/runner.log and /var/lib/apex-init/enrollment.json')
                    break
                time.sleep(5)
            progress('Enrollment verified. Checking protected administrator SSH handover.')

            def access(operation, token, original_port, address, fresh):
                if operation == 'status':
                    result = ssh.run(REMOTE_PATH_GUARD + '\napex_guard_file /var/lib/apex-init/access/state.json; '
                                     'cat /var/lib/apex-init/access/state.json', fresh=True)
                    return json.loads(result.stdout)
                args = ['python3', '-m', 'engine.provisioning.access', operation, '--token', token]
                if original_port:
                    args += ['--original-port', str(original_port), '--client-address', address]
                result = ssh.run('cd ' + installed + ' && ' + shlex.join(args), fresh=fresh)
                return json.loads(result.stdout)

            protected_handover(ssh, access, run_id, int(connection[3]), connection[0])
            access_committed = True
            result = ssh.run('cd ' + installed + ' && ' + shlex.join(base + ['complete']), fresh=True)
            completed = json.loads(result.stdout)
            progress('Initialization and fresh administrator access verified; provisioner ' + revision + ', bundle ' + digest)
            repository = completed.get('stages', {}).get('repository', {}).get('result', {})
            if repository.get('commit'):
                progress('Repository ' + repository['commit'] + '; commons ' + repository.get('commons_commit', 'unknown'))
            return completed
        except HandoverUncertain:
            access_uncertain = True
            raise
        finally:
            if stage and (not launched or finished):
                for fresh in (True, False):
                    try:
                        ssh.run('rm -f ' + stage + '/user-data ' + stage + '/heartbeat ' + stage + '/bundle.tar ' + cleanup_policy, fresh=fresh)
                        break
                    except Exception:
                        pass
            if launched and not finished:
                progress('Coordinator stopped. The current target stage may finish; later stages stop after heartbeat expiry.')
            if access_uncertain:
                progress('Access commit outcome is unknown; verify which endpoint is active before resuming. Rollback may already be disabled.')
                endpoints = [('Administrator candidate', 'adam', 2222), ('Bootstrap candidate', ssh.user, ssh.resolved_port)]
            else:
                label = 'Safe resume' + ('' if access_committed else ' after any pending rollback')
                endpoints = [(label, 'adam' if access_committed else ssh.user, 2222 if access_committed else ssh.resolved_port)]
            for label, resume_user, resume_port in endpoints:
                resume = './apex init ' + resume_user + '@' + ssh.host + ' --port ' + str(resume_port) + ' --user-data ' + shlex.quote(str(source))
                if ssh.identity:
                    resume += ' --identity-file ' + shlex.quote(str(ssh.identity))
                progress(label + ': ' + resume)
