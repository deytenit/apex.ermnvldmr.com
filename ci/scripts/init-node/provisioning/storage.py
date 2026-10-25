"""Conservative target-side tier and optional swap reconciliation.

The caller holds the initialization lock and supplies a zero-argument atomic
journal writer. Ambiguous device or pool ownership always requires remediation.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re
import stat
import subprocess
import tempfile

from .contract import Enrollment


SRV = Path('/srv')
SWAPFILE = Path('/swapfile')
FSTAB = Path('/etc/fstab')
MEMINFO = Path('/proc/meminfo')
SWAPS = Path('/proc/swaps')
_OWNER = 'org.apex:enrollment'
_GIB = 1024 ** 3


class StorageError(RuntimeError):
    """Storage could not be positively identified or safely reconciled."""


def _require(condition, message):
    if not condition:
        raise StorageError(message)


def _run(argv, *, full_vdev_paths=False):
    environment = {**os.environ, 'LC_ALL': 'C'}
    if full_vdev_paths:
        environment.update(ZPOOL_VDEV_NAME_PATH='1', ZPOOL_VDEV_NAME_FOLLOW_LINKS='1')
        environment.pop('ZPOOL_VDEV_NAME_GUID', None)
    try:
        result = subprocess.run(argv, check=False, capture_output=True, text=True,
                                timeout=300, env=environment)
    except (OSError, subprocess.TimeoutExpired):
        raise StorageError('required storage tool is unavailable or timed out: ' + argv[0]) from None
    _require(result.returncode == 0,
             'storage inspection or operation failed: ' + ' '.join(argv[:2]))
    return result.stdout


def _json(argv, key):
    try:
        value = json.loads(_run(argv))[key]
        _require(isinstance(value, list), 'storage inspection returned an invalid list')
        return value
    except (ValueError, KeyError, TypeError):
        raise StorageError('storage inspection returned unsupported JSON') from None


def _flatten(items):
    for item in items:
        yield item
        yield from _flatten(item.get('children', []))


def _mounts():
    return list(_flatten(_json(['findmnt', '--json', '--output', 'TARGET,SOURCE,FSTYPE,MAJ:MIN'],
                               'filesystems')))


def _device_number(path):
    try:
        info = os.stat(path)
    except OSError:
        raise StorageError('selected device is missing or is not a block device') from None
    _require(stat.S_ISBLK(info.st_mode), 'selected device is not a block device')
    return f'{os.major(info.st_rdev)}:{os.minor(info.st_rdev)}'


def _inventory():
    roots = _json(['lsblk', '--json', '--bytes', '--paths', '--output',
                   'NAME,MAJ:MIN,TYPE,PKNAME,MOUNTPOINTS,FSTYPE,WWN,SERIAL,SIZE,PARTUUID'], 'blockdevices')
    nodes, edges = {}, {}

    def visit(items, parent=None):
        for item in items:
            number = item['maj:min']
            nodes[number] = item
            edges.setdefault(number, set())
            if parent:
                edges[number].add(parent)
                edges[parent].add(number)
            visit(item.get('children', []), number)

    try:
        visit(roots)
        by_name = {item['name']: number for number, item in nodes.items()}
        for number, item in nodes.items():
            parent = by_name.get(item.get('pkname'))
            if parent:
                edges[number].add(parent)
                edges[parent].add(number)
    except (KeyError, TypeError):
        raise StorageError('block-device topology could not be established') from None
    return nodes, edges


def _connected(number, edges):
    found, pending = set(), [number]
    while pending:
        item = pending.pop()
        if item not in found:
            found.add(item)
            pending.extend(edges.get(item, ()))
    return found


def _pools():
    result = {}
    for line in _run(['zpool', 'list', '-H', '-o', 'name,guid,health']).splitlines():
        fields = line.split('\t')
        _require(len(fields) == 3 and fields[1].isdigit(), 'active pool identity is ambiguous')
        result[fields[0]] = {'guid': fields[1], 'health': fields[2]}
    return result


def _members(text, name, imported=True):
    """Accept only a plain single-device ONLINE pool, never infer topology."""
    marker = re.search(r'^\s*config:\s*$', text, re.M)
    _require(marker is not None, 'pool membership is unavailable')
    lines = text[marker.end():].splitlines()
    rows = []
    for line in lines:
        fields = line.split()
        if not fields or fields[0] == 'NAME':
            continue
        if fields[0] == 'errors:':
            _require(line.strip() == 'errors: No known data errors', 'pool reports data errors')
            break
        _require(len(fields) >= 2 and fields[1] == 'ONLINE',
                 'pool is not a healthy single-device topology')
        if imported:
            _require(len(fields) == 5 and fields[2:] == ['0', '0', '0'],
                     'pool reports device errors')
        rows.append(fields[0])
    _require(len(rows) == 2 and rows[0] == name,
             'pool must contain exactly one identifiable device')
    leaf = rows[1]
    if not imported and re.fullmatch(r'[A-Za-z0-9_.-]+', leaf):
        leaf = '/dev/' + leaf
    _require(leaf.startswith('/dev/'), 'pool device path cannot be positively resolved')
    return leaf


def _pool_device(name):
    return _members(_run(['zpool', 'status', '-P', name], full_vdev_paths=True), name)


def _inspect_device(device, mounts, allowed_pool=None, allowed_path=None):
    number = _device_number(device)
    nodes, edges = _inventory()
    _require(number in nodes, 'selected device is absent from the block topology')
    node = nodes[number]
    hardware = node.get('wwn') or node.get('serial')
    partition = None
    if node.get('type') == 'part':
        parents = [item for item in nodes.values() if item['name'] == node.get('pkname')]
        _require(len(parents) == 1 and parents[0]['type'] == 'disk' and node.get('partuuid'),
                 'selected partition needs a stable partition UUID and identifiable parent disk')
        hardware = parents[0].get('wwn') or parents[0].get('serial')
        partition = node['partuuid']
    _require(isinstance(hardware, str) and bool(hardware.strip()),
             'selected device needs a stable WWN or hardware serial')
    _require(node.get('type') in ('disk', 'part') and int(node.get('size') or 0) > 0,
             'selected device type or capacity is unsupported')
    related = _connected(number, edges)
    protected = set()
    for other, data in nodes.items():
        for mount in data.get('mountpoints') or []:
            if mount and mount != allowed_path:
                protected.add(other)
    for mount in mounts:
        if mount['target'] != allowed_path:
            protected.add(mount.get('maj:min'))
    active = _pools()
    for pool in active:
        leaf = _device_number(_pool_device(pool))
        if pool != allowed_pool or any(
                m['source'].split('/')[0] == pool and m['target'] != allowed_path for m in mounts):
            protected.add(leaf)
    _require(not (related & protected),
             'selected device intersects mounted, root, boot, swap, or active pool storage')
    identity = {'hardware': hardware, 'size': int(node['size']), 'type': node['type']}
    if partition:
        identity['partition'] = partition
    return {'number': number, 'identity': identity, 'related': related, 'node': node, 'pools': active}


def _safe_path(path):
    for parent in reversed((path, *path.parents)):
        try:
            info = parent.lstat()
        except FileNotFoundError:
            _require(parent == path, 'storage parent directory is missing')
            continue
        _require(stat.S_ISDIR(info.st_mode), 'storage path contains a symlink or non-directory')


def _directory(path, mounts, pool=None):
    _safe_path(path)
    exact = [m for m in mounts if m['target'] == str(path)]
    _require(not exact or (len(exact) == 1 and pool is not None and
                          exact[0]['fstype'] == 'zfs' and exact[0]['source'] == pool),
             'tier path is occupied by an unexpected mount')
    _require(not any(m['target'].startswith(str(path) + '/') for m in mounts),
             'tier path contains an unexpected nested mount')
    created = not path.exists()
    if created:
        path.mkdir(mode=0o755)
        path.chmod(0o755)
    return created


def _directory_identity(path):
    _safe_path(path)
    try:
        info = path.lstat()
    except FileNotFoundError:
        raise StorageError('managed directory identity is missing') from None
    return {'device': info.st_dev, 'inode': info.st_ino}


def _verify_ownership_identity(record, path):
    ownership = record.get('ownership')
    if ownership:
        _require(ownership['identity'] == _directory_identity(path),
                 'managed directory identity changed before ownership handoff')
    return ownership


def _record_pending_ownership(record, path):
    if not _verify_ownership_identity(record, path):
        record['ownership'] = {'status': 'pending', 'identity': _directory_identity(path)}


def _verify_pool(name, guid, device_info, path, owner, require_owner=True):
    pools = _pools()
    _require(name in pools and pools[name] == {'guid': guid, 'health': 'ONLINE'},
             'pool name, GUID, or health does not match enrollment')
    _require(_member_matches(_pool_device(name), device_info),
             'pool device membership does not match the selected device')
    rows = [line.split('\t') for line in _run(
        ['zfs', 'list', '-H', '-r', '-o', 'name,mountpoint,type,mounted', name]).splitlines()]
    _require(len(rows) == 1 and len(rows[0]) == 4 and rows[0][:3] == [name, str(path), 'filesystem'],
             'pool dataset or mountpoint requires explicit reconciliation')
    if require_owner:
        actual = _run(['zfs', 'get', '-H', '-o', 'value', _OWNER, name]).strip()
        _require(actual == owner, 'pool lacks matching enrollment ownership')
    return rows[0][3] == 'yes'


def _member_matches(member, info):
    number = _device_number(member)
    return number == info['number'] or (info['node']['type'] == 'disk' and number in info['related'])


def _import_candidate(device, guid):
    text = _run(['zpool', 'import', '-d', device], full_vdev_paths=True)
    names = re.findall(r'^\s*pool:\s*(\S+)\s*$', text, re.M)
    guids = re.findall(r'^\s*id:\s*(\d+)\s*$', text, re.M)
    states = re.findall(r'^\s*state:\s*(\S+)\s*$', text, re.M)
    _require(len(names) == 1 and guids == [guid] and states == ['ONLINE'],
             'import discovery does not match the declared healthy pool GUID')
    actions = re.findall(r'^\s*action:\s*(.+)$', text, re.M)
    _require(not re.search(r'^\s*status:', text, re.M) and
             all(action == 'The pool can be imported using its name or numeric identifier.' for action in actions),
             'pool import reports ownership or recovery conditions; reconcile manually')
    return names[0], _members(text, names[0], imported=False)


def _block_tier(tier, path, journal, owner, save):
    record = journal.get(str(tier.number))
    created = False
    pool = record.get('pool') if record else None
    info = _inspect_device(tier.device, _mounts(), pool, str(path) if pool else None)
    if record:
        _require(record['device'] == info['identity'], 'selected device identity changed since enrollment')
        _require(record['status'] in ('intent', 'complete'), 'tier journal status is unsupported')
    if pool and pool in info['pools']:
        guid = record.get('guid') or info['pools'][pool]['guid']
        _verify_pool(pool, guid, info, path, owner)
    else:
        _require(not record or record['status'] == 'intent',
                 'previously enrolled pool is not active; explicit reconciliation is required')
        if tier.mode == 'create':
            pool = f'tier-{tier.number}'
            _require(pool not in info['pools'], 'existing pool name does not prove enrollment ownership')
            _require(info['node']['type'] == 'disk', 'pool creation requires an unused whole disk')
            _require(len(info['related']) == 1, 'selected disk has partitions or dependent devices')
            signatures = _json(['wipefs', '--json', '--output', 'TYPE,UUID,LABEL', tier.device], 'signatures')
            _require(not signatures and not info['node'].get('fstype'), 'selected device contains existing signatures')
        else:
            pool, member = _import_candidate(tier.device, tier.pool_guid)
            _require(_member_matches(member, info), 'import membership differs from the selected device')
            _require(pool not in info['pools'], 'pool is already active without matching enrollment ownership')
        created = _directory(path, _mounts())
        _require(not path.exists() or not any(path.iterdir()), 'pool mountpoint contains existing data')
        record = {'status': 'intent', 'device': info['identity'], 'pool': pool,
                  'ownership_intent': tier.mode == 'create'}
        journal[str(tier.number)] = record
        save()
        fresh = _inspect_device(tier.device, _mounts())
        _require(fresh['identity'] == info['identity'] and fresh['number'] == info['number'],
                 'selected device identity changed before pool operation')
        _directory(path, _mounts())
        _require(not any(path.iterdir()), 'pool mountpoint acquired existing data before operation')
        if tier.mode == 'create':
            _require(not _json(['wipefs', '--json', '--output', 'TYPE,UUID,LABEL', tier.device], 'signatures'),
                     'selected device acquired signatures before creation')
            _run(['zpool', 'create', '-o', 'ashift=12', '-O', f'mountpoint={path}',
                  '-O', f'{_OWNER}={owner}', pool, tier.device])
        else:
            _require(_import_candidate(tier.device, tier.pool_guid) == (pool, member),
                     'pool identity changed before import')
            _run(['zpool', 'import', '-N', '-d', tier.device, tier.pool_guid])
        pools = _pools()
        _require(pool in pools, 'pool operation did not establish an active pool')
        guid = pools[pool]['guid']
        _require(tier.mode != 'import' or guid == tier.pool_guid, 'imported pool GUID differs from intent')
        refreshed = _inspect_device(tier.device, _mounts(), pool, str(path))
        _require(refreshed['identity'] == info['identity'], 'selected device changed during pool operation')
        info = refreshed
        _verify_pool(pool, guid, info, path, owner, require_owner=tier.mode == 'create')
        if tier.mode == 'import':
            previous_owner = _run(['zfs', 'get', '-H', '-o', 'value', _OWNER, pool]).strip()
            _require(previous_owner in ('-', owner), 'imported pool belongs to a different enrollment')
            _run(['zfs', 'set', f'{_OWNER}={owner}', pool])
    created = _directory(path, _mounts(), pool) or created
    if not _verify_pool(pool, guid, info, path, owner):
        _require(not any(path.iterdir()), 'refusing to hide existing files beneath a pool mount')
        _run(['zfs', 'mount', pool])
    _require(any(m['target'] == str(path) and m['source'] == pool and m['fstype'] == 'zfs'
                 for m in _mounts()), 'pool is not mounted at its enrolled path')
    record.update(status='complete', guid=guid)
    if tier.mode == 'create' and record.get('ownership_intent'):
        _record_pending_ownership(record, path)
    save()
    return {'path': str(path), 'mode': tier.mode, 'created': created and tier.mode == 'create',
            'pool': pool, 'guid': guid}


def _persist_swap():
    info = FSTAB.lstat()
    _require(stat.S_ISREG(info.st_mode) and info.st_uid == 0, 'fstab must be a root-owned regular file')
    original = FSTAB.read_text()
    lines = [line.split() for line in original.splitlines() if line.strip() and not line.lstrip().startswith('#')]
    matches = [line for line in lines if line[0] == str(SWAPFILE)]
    if matches:
        _require(len(matches) == 1 and len(matches[0]) >= 4 and matches[0][2] == 'swap'
                 and 'noauto' not in matches[0][3].split(','),
                 'existing swap fstab entry is incompatible')
        return
    fd, temporary = tempfile.mkstemp(prefix='.apex-swap-', dir=FSTAB.parent)
    try:
        with os.fdopen(fd, 'w') as output:
            os.fchmod(output.fileno(), stat.S_IMODE(info.st_mode))
            os.fchown(output.fileno(), info.st_uid, info.st_gid)
            output.write(original + ('' if original.endswith('\n') else '\n') +
                         f'{SWAPFILE} none swap sw 0 0\n')
            output.flush()
            os.fsync(output.fileno())
        current = FSTAB.lstat()
        _require(all(getattr(current, key) == getattr(info, key) for key in
                     ('st_dev', 'st_ino', 'st_size', 'st_mtime_ns', 'st_ctime_ns')),
                 'fstab changed during swap setup')
        os.replace(temporary, FSTAB)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def _active_swaps():
    lines = SWAPS.read_bytes().splitlines()
    _require(bool(lines) and lines[0].split() == [b'Filename', b'Type', b'Size', b'Used', b'Priority'],
             'kernel swap inventory header is invalid')
    paths = []
    for line in lines[1:]:
        fields = line.split()
        _require(len(fields) == 5 and fields[0].startswith(b'/') and fields[1] in (b'file', b'partition')
                 and fields[2].isdigit() and fields[3].isdigit()
                 and re.fullmatch(rb'-?[0-9]+', fields[4]) is not None,
                 'kernel swap inventory entry is invalid')
        _require(re.search(rb'\\(?![0-3][0-7]{2})', fields[0]) is None,
                 'kernel swap inventory path escaping is invalid')
        decoded = re.sub(rb'\\([0-3][0-7]{2})', lambda match: bytes([int(match.group(1), 8)]), fields[0])
        paths.append(os.fsdecode(decoded))
    return paths


def _swap(journal, save):
    try:
        active = _active_swaps()
        if active:
            return {'status': 'skipped', 'reason': 'existing active swap preserved'}
        if os.path.lexists(SWAPFILE):
            return {'status': 'skipped', 'reason': 'existing swapfile preserved; no overwrite or resize'}
        mounts = _mounts()
        covering = [m for m in mounts if str(SWAPFILE).startswith(m['target'].rstrip('/') + '/')]
        _require(bool(covering), 'swap filesystem could not be identified')
        filesystem = max(covering, key=lambda m: len(m['target']))['fstype']
        _require(filesystem in ('ext4', 'xfs'), 'optional swap requires a supported ext4 or XFS filesystem')
        match = re.search(r'^MemTotal:\s+(\d+)\s+kB$', MEMINFO.read_text(), re.M)
        _require(match is not None, 'RAM size could not be established')
        size = (4 if int(match.group(1)) * 1024 > 4 * _GIB else 2) * _GIB
        space = os.statvfs(SWAPFILE.parent)
        _require(space.f_bavail * space.f_frsize >= size + _GIB,
                 'insufficient free space for optional swap and reserve')
        _require(hasattr(os, 'posix_fallocate'), 'safe swap allocation is unavailable')
        journal['swap'] = {'status': 'intent', 'size': size}
        save()
        fd = os.open(SWAPFILE, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600)
        try:
            os.posix_fallocate(fd, 0, size)
            os.fsync(fd)
        finally:
            os.close(fd)
        _run(['mkswap', str(SWAPFILE)])
        _run(['swapon', str(SWAPFILE)])
        active = _active_swaps()
        _require(str(SWAPFILE) in active,
                 'optional swap activation did not become observable')
        _persist_swap()
        return {'status': 'complete', 'size': size}
    except (StorageError, OSError) as error:
        return {'status': 'skipped', 'reason': str(error) if isinstance(error, StorageError)
                else 'optional swap allocation or persistence failed; existing resources preserved'}


def _enrollment_identity(spec):
    return hashlib.sha256(json.dumps(spec.identity(), sort_keys=True,
                                     separators=(',', ':')).encode()).hexdigest()


def acknowledge_storage_ownership(spec: Enrollment, state: dict, paths, save) -> None:
    """Persist the caller's successful, nonrecursive ownership handoff for paths."""
    journal = state.get('storage', {})
    _require(journal.get('identity') == _enrollment_identity(spec),
             'storage enrollment identity conflicts with ownership acknowledgement')
    allowed = {str(SRV / f'tier-{tier.number}.{spec.repository.name}'): tier for tier in spec.tiers}
    acknowledged = []
    for path in paths:
        _require(path in allowed, 'ownership acknowledgement path is outside enrollment')
        tier = allowed[path]
        record = journal.get('tiers', {}).get(str(tier.number), {})
        ownership = _verify_ownership_identity(record, Path(path))
        _require(tier.mode != 'import' and ownership is not None,
                 'ownership acknowledgement requires a directory created by enrollment')
        _require(ownership['status'] in ('pending', 'applied'), 'ownership journal status is unsupported')
        acknowledged.append(ownership)
    for ownership in acknowledged:
        ownership['status'] = 'applied'
    save()


def reconcile_storage(spec: Enrollment, state: dict, save) -> dict:
    """Return ownership_pending paths until the caller acknowledges their chown."""
    identity = _enrollment_identity(spec)
    journal = state.setdefault('storage', {'identity': identity, 'tiers': {}})
    _require(journal.get('identity') == identity, 'storage enrollment identity conflicts with journal')
    _safe_path(SRV)
    _require(SRV.exists(), 'storage parent directory is missing')
    selected = set()
    for tier in spec.tiers:
        if tier.device:
            number = _device_number(tier.device)
            _require(number not in selected, 'multiple tiers select aliases of the same device')
            selected.add(number)
    result = {'tiers': [], 'ownership_pending': []}
    for tier in spec.tiers:
        path = SRV / f'tier-{tier.number}.{spec.repository.name}'
        if tier.mode == 'directory':
            record = journal['tiers'].get(str(tier.number), {})
            _verify_ownership_identity(record, path)
            _safe_path(path)
            _require(not (record.get('ownership_intent') and not record.get('ownership') and path.exists()),
                     'directory creation was interrupted before identity was recorded; reconcile manually')
            if not path.exists():
                record = {'status': 'intent', 'path': str(path), 'ownership_intent': True}
                journal['tiers'][str(tier.number)] = record
                save()
            created = _directory(path, _mounts())
            if created:
                _record_pending_ownership(record, path)
            entry = {'path': str(path), 'mode': tier.mode, 'created': created}
            record.update(status='complete', path=str(path))
            journal['tiers'][str(tier.number)] = record
            save()
        else:
            entry = _block_tier(tier, path, journal['tiers'], identity, save)
        ownership = journal['tiers'][str(tier.number)].get('ownership')
        if ownership and ownership['status'] == 'pending':
            result['ownership_pending'].append(str(path))
        result['tiers'].append(entry)
    result['swap'] = _swap(journal, save)
    journal['swap'] = result['swap']
    save()
    return result
