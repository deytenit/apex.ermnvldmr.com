"""Durable enrollment records and writes that refuse redirected paths."""
from __future__ import annotations

import contextlib
import fcntl
import json
import os
from pathlib import Path
import stat
import tempfile
import time

from . import ENROLLMENT_INTERFACE


class ProvisionError(RuntimeError):
    pass


def safe_path(path):
    path = Path(path).absolute()
    for part in (*reversed(path.parents), path):
        try:
            info = part.lstat()
        except FileNotFoundError:
            continue
        if stat.S_ISLNK(info.st_mode):
            raise ProvisionError('managed path contains a symbolic link')
    return path


def atomic_write(path, content, mode=0o600, uid=None, gid=None):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.exists() and (not path.is_file() or path.stat().st_nlink != 1):
        raise ProvisionError('managed file is not an ordinary unlinked file')
    data = content.encode() if isinstance(content, str) else content
    fd, temporary = tempfile.mkstemp(prefix='.apex-', dir=path.parent)
    try:
        with os.fdopen(fd, 'wb') as stream:
            os.fchmod(stream.fileno(), mode)
            if uid is not None:
                os.fchown(stream.fileno(), uid, gid)
            stream.write(data)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
        directory = os.open(path.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


@contextlib.contextmanager
def exclusive_lock(path):
    path = safe_path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(path, os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise ProvisionError('another initialization is running; inspect status before retrying') from None
        yield
    finally:
        os.close(fd)


def check_heartbeat(path, now=None):
    if path is None:
        return
    try:
        age = (time.time() if now is None else now) - safe_path(path).stat().st_mtime
    except FileNotFoundError:
        raise ProvisionError('coordinator heartbeat is missing; stopping before the next stage') from None
    if not 0 <= age < 90:
        raise ProvisionError('coordinator heartbeat expired; stopping before the next stage')


class Journal:
    def __init__(self, path, identity, revision, bundle_digest):
        self.path = safe_path(path)
        if self.path.exists():
            try:
                self.data = json.loads(self.path.read_text())
            except (ValueError, OSError):
                raise ProvisionError('invalid enrollment journal; reconcile it before retrying') from None
            if self.data.get('enrollment_interface') != ENROLLMENT_INTERFACE:
                raise ProvisionError('recorded enrollment interface is missing or unsupported')
            if self.data.get('identity') != identity:
                raise ProvisionError('enrollment identity conflicts with the recorded target')
            if self.data.get('bundle_digest') != bundle_digest:
                raise ProvisionError('provisioner bundle changed; resume using the recorded bundle')
        else:
            self.data = dict(identity=identity, revision=revision, bundle_digest=bundle_digest,
                             baseline_revision=revision, enrollment_interface=ENROLLMENT_INTERFACE,
                             status='running', stages={})

    def save(self):
        atomic_write(self.path, json.dumps(self.data, sort_keys=True, indent=2) + '\n')

    def stage(self, name, operation, heartbeat=None):
        try:
            check_heartbeat(heartbeat)
        except ProvisionError:
            self.data['status'] = 'stopped'
            self.data['failed_stage'] = name
            self.save()
            raise
        self.data['status'] = 'running'
        self.data['stages'][name] = {'status': 'running'}
        self.save()
        try:
            result = operation()
        except Exception:
            self.data['status'] = 'failed'
            self.data['failed_stage'] = name
            self.data['stages'][name] = {'status': 'failed'}
            self.save()
            raise
        self.data['stages'][name] = {'status': 'done', 'result': result}
        self.data.pop('failed_stage', None)
        self.save()
        return result
