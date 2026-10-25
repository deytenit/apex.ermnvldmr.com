"""Shared root runner for remote and cloud-init enrollment."""
from __future__ import annotations

import argparse
import json
import os
from pathlib import Path
import platform
import pwd
import subprocess
import sys
import tempfile

from .state import Journal, ProvisionError, atomic_write, exclusive_lock, safe_path
from .userdata import decode_user_data

STATE = Path('/var/lib/apex-init')


def run(args, **kwargs):
    result = subprocess.run(args, text=True, capture_output=True, timeout=300, **kwargs)
    if result.returncode:
        raise ProvisionError('required target operation failed: ' + args[0])
    return result.stdout.strip()


def preflight():
    if os.geteuid() != 0:
        raise ProvisionError('target enrollment requires root privileges')
    release = {}
    for line in Path('/etc/os-release').read_text().splitlines():
        key, separator, value = line.partition('=')
        if separator:
            release[key] = value.strip('"')
    if release.get('ID') != 'debian' or release.get('VERSION_ID') != '13' or platform.machine() != 'x86_64':
        raise ProvisionError('initialization supports Debian 13 amd64 only')


def validate_keys(spec):
    for key in spec.authorized_keys:
        with tempfile.NamedTemporaryFile(mode='w') as stream:
            stream.write(key + '\n')
            stream.flush()
            try:
                run(['ssh-keygen', '-l', '-f', stream.name])
            except ProvisionError:
                raise ProvisionError('administrator public key failed cryptographic validation') from None


def administrator(spec):
    try:
        account = pwd.getpwnam('adam')
    except KeyError:
        run(['useradd', '--create-home', '--shell', '/bin/bash', '--groups', 'sudo,docker', 'adam'])
        account = pwd.getpwnam('adam')
    if account.pw_dir != '/home/adam' or account.pw_uid == 0:
        raise ProvisionError('existing administrator has incompatible home or UID')
    safe_path(account.pw_dir)
    if account.pw_shell != '/bin/bash':
        run(['usermod', '--shell', '/bin/bash', 'adam'])
    run(['usermod', '--append', '--groups', 'sudo,docker', 'adam'])
    directory = safe_path(Path(account.pw_dir) / '.ssh')
    directory.mkdir(mode=0o700, exist_ok=True)
    os.chown(directory, account.pw_uid, account.pw_gid)
    os.chmod(directory, 0o700)
    authorized = safe_path(directory / 'authorized_keys')
    existing = authorized.read_text().splitlines() if authorized.exists() else []
    keys = existing + [key for key in spec.authorized_keys if key not in existing]
    atomic_write(authorized, '\n'.join(keys) + '\n', uid=account.pw_uid, gid=account.pw_gid)
    policy = 'adam ALL=(ALL:ALL) NOPASSWD: ALL\n'
    with tempfile.NamedTemporaryFile(mode='w') as stream:
        stream.write(policy)
        stream.flush()
        run(['visudo', '-cf', stream.name])
    atomic_write('/etc/sudoers.d/99-apex-adam', policy, 0o440)
    run(['visudo', '-c'])
    run(['runuser', '-u', 'adam', '--', 'sudo', '-n', 'true'])
    for item in spec.files:
        uid, gid = (0, 0) if item.owner == 'root:root' else (account.pw_uid, account.pw_gid)
        atomic_write(item.path, item.content, item.mode, uid=uid, gid=gid)
    if run(['hostname']) != spec.hostname:
        run(['hostnamectl', 'set-hostname', spec.hostname])
    groups = run(['id', '-nG', 'adam']).split()
    if not {'sudo', 'docker'} <= set(groups):
        raise ProvisionError('administrator groups did not converge')
    return {'administrator': 'adam', 'hostname': spec.hostname, 'sudo': 'verified'}


def enroll(spec, revision, bundle_digest, heartbeat=None):
    from .baseline import apply_baseline
    from .storage import acknowledge_storage_ownership, reconcile_storage
    from .repository import reconcile_repository

    preflight()
    validate_keys(spec)
    safe_path(STATE).mkdir(mode=0o700, exist_ok=True)
    if STATE.stat().st_uid != 0:
        raise ProvisionError('enrollment state directory must be root-owned')
    os.chmod(STATE, 0o700)
    with exclusive_lock(STATE / 'init.lock'):
        journal = Journal(STATE / 'enrollment.json', spec.identity(), revision, bundle_digest)
        journal.save()
        journal.stage('baseline', apply_baseline, heartbeat)
        journal.stage('administrator', lambda: administrator(spec), heartbeat)
        result = journal.stage('storage', lambda: reconcile_storage(spec, journal.data, journal.save), heartbeat)
        account = pwd.getpwnam('adam')
        for path in result['ownership_pending']:
            os.chown(safe_path(path), account.pw_uid, account.pw_gid)
            acknowledge_storage_ownership(spec, journal.data, [path], journal.save)
        journal.stage('repository', lambda: reconcile_repository(spec, journal.data, journal.save), heartbeat)
        journal.data['status'] = 'enrolled'
        journal.data['external_access'] = 'pending'
        journal.save()
        return journal.data


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('validate', 'enroll', 'status', 'complete'))
    parser.add_argument('--user-data')
    parser.add_argument('--revision', default='unknown')
    parser.add_argument('--bundle-digest', default='unknown')
    parser.add_argument('--heartbeat')
    args = parser.parse_args(argv)
    try:
        preflight()
        if args.operation == 'status':
            path = safe_path(STATE / 'enrollment.json')
            print(path.read_text() if path.exists() else json.dumps({'status': 'not-started'}))
            return 0
        if args.operation == 'complete':
            with exclusive_lock(STATE / 'init.lock'):
                access = json.loads(safe_path(STATE / 'access/state.json').read_text())
                path = safe_path(STATE / 'enrollment.json')
                record = json.loads(path.read_text())
                if access.get('status') != 'committed' or record.get('status') not in ('enrolled', 'complete'):
                    raise ProvisionError('enrollment and access must be verified before completion')
                record['external_access'] = 'verified'
                record['status'] = 'complete'
                atomic_write(path, json.dumps(record, sort_keys=True) + '\n')
                print(json.dumps(record, sort_keys=True))
            return 0
        if not args.user_data:
            raise ProvisionError('--user-data is required')
        path = safe_path(args.user_data)
        if path.stat().st_uid != 0 or path.stat().st_mode & 0o077:
            raise ProvisionError('user-data must be root-owned and private')
        if not path.is_file() or path.stat().st_size > 1024 * 1024:
            raise ProvisionError('user-data must be a regular file no larger than 1 MiB')
        spec = decode_user_data(path.read_bytes())
        validate_keys(spec)
        if args.operation == 'validate':
            print(json.dumps({'status': 'valid', 'hostname': spec.hostname}))
        else:
            print(json.dumps(enroll(spec, args.revision, args.bundle_digest, args.heartbeat), sort_keys=True))
        return 0
    except Exception as error:
        from .contract import ContractError
        from .baseline import BaselineError
        from .storage import StorageError
        if isinstance(error, (ProvisionError, ContractError, BaselineError, StorageError)):
            print(str(error), file=sys.stderr)
        else:
            # Unexpected dependency failures must not render raw credential input.
            print('initialization failed (' + type(error).__name__ + '); inspect stage status', file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
