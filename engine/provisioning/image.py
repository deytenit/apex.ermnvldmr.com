"""Image-only installation and sanitization; never called by remote enrollment."""
from __future__ import annotations

import argparse
import io
import json
import os
from pathlib import Path
import shutil
import tarfile
import uuid

from . import ENROLLMENT_INTERFACE
from .state import ProvisionError, atomic_write, exclusive_lock, safe_path
from .target import preflight, run


def install():
    from .transport import build_bundle
    source = Path(__file__).resolve().parents[2]
    data, digest, _ = build_bundle(source)
    destination = safe_path(Path('/usr/local/lib/apex-provisioner') / digest)
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    with tarfile.open(fileobj=io.BytesIO(data)) as archive:
        archive.extractall(destination, filter='data')
    revision = os.environ.get('APEX_PROVISION_REVISION', 'unversioned-modified')
    metadata = {'bundle_digest': digest, 'revision': revision, 'path': str(destination),
                'baseline_revision': revision, 'enrollment_interface': ENROLLMENT_INTERFACE}
    atomic_write('/etc/apex/provisioner.json', json.dumps(metadata) + '\n')
    for executable, operation in (('apex-provision-image', ''), ('apex-bootstrap', 'cloud-enroll ')):
        content = ('#!/bin/sh\nset -eu\ncd ' + str(destination) + '\n'
                   'exec /usr/bin/python3 -m engine.provisioning.image ' + operation + '"$@"\n')
        atomic_write('/usr/local/bin/' + executable, content, 0o755)
    return metadata


def cloud_enroll():
    from .access import Transition
    from .target import enroll
    from .userdata import decode_user_data
    metadata = json.loads(Path('/etc/apex/provisioner.json').read_text())
    if metadata.get('enrollment_interface') != ENROLLMENT_INTERFACE:
        raise ProvisionError('image enrollment interface is missing or unsupported; rebuild the image')
    instance = Path('/var/lib/cloud/instance').resolve(strict=True)
    try:
        instance.relative_to('/var/lib/cloud/instances')
    except ValueError:
        raise RuntimeError('cloud-init instance path is outside its managed directory') from None
    path = safe_path(instance / 'user-data.txt')
    if path.stat().st_uid != 0 or path.stat().st_size > 1024 * 1024:
        raise RuntimeError('cloud-init user-data is not a supported root-owned file')
    spec = decode_user_data(path.read_bytes())
    result = enroll(spec, metadata['revision'], metadata['bundle_digest'])
    token = uuid.uuid4().hex
    transition = Transition()
    try:
        current = transition.execute('begin', token, 22, '127.0.0.1')
        run(['runuser', '-u', 'adam', '--', 'sudo', '-n', 'true'])
        if current['status'] not in ('existing', 'committed'):
            transition.execute('finalize', token)
        if current['status'] != 'committed':
            transition.execute('commit', token)
    except Exception:
        transition.execute('rollback', token)
        raise
    result['external_access'] = 'not-verified-cloud-init'
    atomic_write('/var/lib/apex-init/enrollment.json', json.dumps(result, sort_keys=True) + '\n')
    return result


def sanitize_files(root=Path('/')):
    root = Path(root)
    for home in ('root', 'home/debian'):
        for name in ('.ssh/authorized_keys', '.ssh/authorized_keys2', '.bash_history'):
            path = root / home / name
            if path.exists() or path.is_symlink():
                path.unlink()
    for path in (root / 'etc/ssh').glob('ssh_host_*'):
        if path.is_file() or path.is_symlink():
            path.unlink()
    atomic_write(root / 'etc/machine-id', '', 0o444)
    dbus = root / 'var/lib/dbus/machine-id'
    if dbus.exists() or dbus.is_symlink():
        dbus.unlink()
    dbus.parent.mkdir(parents=True, exist_ok=True)
    dbus.symlink_to('/etc/machine-id')
    for name in ('var/lib/apex-init', 'tmp/engine', 'var/lib/cloud'):
        path = root / name
        if path.is_symlink():
            path.unlink()
        elif path.exists():
            shutil.rmtree(path)
    autologin = root / 'etc/systemd/system/serial-getty@ttyS0.service.d/autologin.conf'
    autologin.unlink(missing_ok=True)
    atomic_write(root / 'etc/systemd/system/ssh.service.d/10-generate-host-keys.conf',
                 '[Service]\nExecStartPre=\nExecStartPre=/usr/bin/ssh-keygen -A\n'
                 'ExecStartPre=/usr/sbin/sshd -t\n', 0o644)


def main(argv=None):
    parser = argparse.ArgumentParser()
    parser.add_argument('operation', choices=('install', 'baseline', 'install-runner', 'sanitize', 'cloud-enroll'))
    args = parser.parse_args(argv)
    preflight()
    if args.operation == 'install':
        print(json.dumps(install()))
    elif args.operation == 'baseline':
        from .baseline import apply_baseline
        with exclusive_lock('/var/lib/apex-init/init.lock'):
            result = apply_baseline()
            atomic_write('/etc/apex/image-baseline.json', json.dumps(result, sort_keys=True) + '\n')
    elif args.operation == 'install-runner':
        run(['test', '-x', '/usr/local/bin/apex-bootstrap'])
    elif args.operation == 'sanitize':
        run(['usermod', '--password', '!', 'root'])
        run(['usermod', '--password', '!', 'debian'])
        sanitize_files()
    else:
        print(json.dumps(cloud_enroll(), sort_keys=True))
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
