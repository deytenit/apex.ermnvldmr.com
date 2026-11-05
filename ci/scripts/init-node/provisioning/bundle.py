"""Deterministic provisioning payloads for golden-image builds."""
import hashlib
import io
from pathlib import Path
import re
import shutil
import subprocess
import tarfile

from .state import ProvisionError


def _bundle_from_source(root):
    root = Path(root).resolve()
    package = root / 'provisioning'
    files = sorted(package.rglob('*.py'))
    seed = package / 'seed.sh'
    guard = package / 'profiles.sh'
    if not seed.is_file() or not (package / '__init__.py').is_file():
        raise ProvisionError('Provisioning package or seed is missing')
    files.append(seed)
    if guard.is_file():
        files.append(guard)
    output = io.BytesIO()
    with tarfile.open(fileobj=output, mode='w') as archive:
        for path in files:
            if (not path.is_file() or '__pycache__' in path.parts
                    or any(part.is_symlink() for part in (path, *path.parents))):
                raise ProvisionError('Provisioning bundle contains an unsafe source path')
            data = path.read_bytes()
            info = tarfile.TarInfo(path.relative_to(root).as_posix())
            info.size, info.mode, info.mtime = len(data), (0o755 if path == seed else 0o644), 0
            info.uid = info.gid = 0
            archive.addfile(info, io.BytesIO(data))
    data = output.getvalue()
    return data, hashlib.sha256(data).hexdigest()


def build_bundle(commons):
    root = Path(commons).resolve()
    data, digest = _bundle_from_source(root / 'ci/scripts/init-node')
    revision = 'unversioned-modified'
    if shutil.which('git'):
        result = subprocess.run(['git', '-C', str(root), 'rev-parse', 'HEAD'], capture_output=True, text=True, timeout=30)
        if result.returncode == 0 and re.fullmatch(r'[a-f0-9]{40,64}\n?', result.stdout):
            revision = result.stdout.strip()
            dirty = subprocess.run(['git', '-C', str(root), 'status', '--porcelain', '--', 'ci/scripts/init-node'], capture_output=True, text=True, timeout=30)
            if dirty.returncode or dirty.stdout.strip():
                revision += '-modified'
    return data, digest, revision

