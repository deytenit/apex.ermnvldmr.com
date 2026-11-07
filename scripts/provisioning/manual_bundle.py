"""Install an immutable watchdog bundle from the operator's checkout."""
import hashlib
import io
import os
from pathlib import Path
import shutil
import stat
import tarfile
import tempfile


def _bundle_from_source(root):
    root = Path(root).resolve()
    files = [root / 'provisioning' / name for name in ('__init__.py', 'access.py', 'state.py')]
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for path in files:
            if (not path.is_file() or '__pycache__' in path.parts
                    or any(part.is_symlink() for part in (path, *path.parents))):
                raise RuntimeError('Provisioning bundle contains an unsafe source path')
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size, info.mode, info.mtime = len(data), 0o644, 0
            info.uid = info.gid = 0
            archive.addfile(info, io.BytesIO(data))
    data = output.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def _trusted(path):
    info = path.lstat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.geteuid() or info.st_mode & 0o022:
        raise RuntimeError(f"Untrusted provisioning directory: {path}")


def install(root=Path('/')):
    source = Path(__file__).resolve().parents[1]
    data, digest = _bundle_from_source(source)
    # All ancestors from the filesystem root must be protected before publishing code.
    parent = root
    _trusted(parent)
    for component in ('usr', 'local', 'lib', 'apex-provisioner'):
        parent = parent / component
        parent.mkdir(mode=0o755, exist_ok=True)
        _trusted(parent)
    destination = parent / digest
    with tempfile.TemporaryDirectory(prefix='.install-', dir=parent) as temporary:
        stage = Path(temporary) / 'bundle'
        stage.mkdir(mode=0o755)
        with tarfile.open(fileobj=io.BytesIO(data)) as archive:
            for member in archive:
                if not member.isfile() or '..' in Path(member.name).parts or Path(member.name).is_absolute():
                    raise RuntimeError('Invalid provisioning bundle member')
                target = stage / member.name
                target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
                with archive.extractfile(member) as source_file, target.open('xb') as output:
                    shutil.copyfileobj(source_file, output)
                target.chmod(member.mode)
        try:
            os.rename(stage, destination)
        except OSError:
            _trusted(destination)
            installed, existing_digest = _bundle_from_source(destination)
            if existing_digest != digest or installed != data:
                raise RuntimeError('Installed provisioning bundle differs from the checkout') from None
            for path in destination.rglob('*'):
                info = path.lstat()
                if info.st_uid != os.geteuid() or info.st_mode & 0o022 or path.is_symlink():
                    raise RuntimeError(f'Untrusted installed provisioning file: {path}') from None
    return destination


if __name__ == '__main__':
    if os.geteuid() != 0:
        raise SystemExit('Run bundle installation as root.')
    print(install())
