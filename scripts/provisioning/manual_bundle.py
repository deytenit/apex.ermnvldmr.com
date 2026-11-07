"""Install an immutable watchdog bundle from the operator's checkout."""
import errno
import hashlib
import os
from pathlib import Path
import stat
import tempfile


MODULES = ('__init__.py', 'access.py', 'state.py')


def _trusted_directory(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError(f'Untrusted provisioning directory: {path}')


def _verify(destination, files):
    _trusted_directory(destination)
    package = destination / 'provisioning'
    _trusted_directory(package)
    # Python can create bytecode caches when the installed watchdog runs.
    for path in destination.rglob('*'):
        info = path.lstat()
        if (info.st_uid != os.geteuid() or info.st_mode & 0o022
                or not (stat.S_ISDIR(info.st_mode) or stat.S_ISREG(info.st_mode))):
            raise RuntimeError(f'Untrusted installed provisioning file: {path}')
    for name, data in files.items():
        target = package / name
        if not target.is_file() or target.read_bytes() != data:
            raise RuntimeError('Installed provisioning bundle differs from the checkout')


def install(root=Path('/')):
    source = Path(__file__).resolve().parent
    files = {}
    digest = hashlib.sha256()
    for name in MODULES:
        path = source / name
        if not stat.S_ISREG(path.lstat().st_mode):
            raise RuntimeError(f'Unsafe provisioning source file: {path}')
        data = files[name] = path.read_bytes()
        digest.update(name.encode() + b'\0' + len(data).to_bytes(8, 'big') + data)

    parent = root
    _trusted_directory(parent)
    for component in ('usr', 'local', 'lib', 'apex-provisioner'):
        parent = parent / component
        parent.mkdir(mode=0o755, exist_ok=True)
        _trusted_directory(parent)
    destination = parent / digest.hexdigest()
    if destination.exists():
        _verify(destination, files)
        return destination

    with tempfile.TemporaryDirectory(prefix='.install-', dir=parent) as temporary:
        stage = Path(temporary) / 'bundle'
        package = stage / 'provisioning'
        stage.mkdir(mode=0o755)
        package.mkdir(mode=0o755)
        for name, data in files.items():
            target = package / name
            target.write_bytes(data)
            target.chmod(0o644)
        try:
            stage.rename(destination)
        except OSError as error:
            if error.errno not in (errno.EEXIST, errno.ENOTEMPTY):
                raise
            _verify(destination, files)
    return destination


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run bundle installation as root.')
    print(install())
