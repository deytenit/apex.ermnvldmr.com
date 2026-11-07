"""Serialize host configuration changes across provisioning commands."""
import contextlib
import fcntl
import os
from pathlib import Path
import stat


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
