"""Pure validation for the supported APEX enrollment contract."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any, Dict, Mapping, Optional, Tuple
from urllib.parse import urlsplit


class ContractError(ValueError):
    """A safe diagnostic that never includes a supplied value."""


@dataclass(frozen=True)
class Repository:
    name: str
    url: str
    ref: Optional[str]


@dataclass(frozen=True)
class Tier:
    number: int
    mode: str
    device: Optional[str]
    pool_guid: Optional[str]


@dataclass(frozen=True)
class ManagedFile:
    path: str
    owner: str
    mode: int
    content: str = field(repr=False)


@dataclass(frozen=True)
class Enrollment:
    hostname: str
    repository: Repository
    tiers: Tuple[Tier, ...]
    authorized_keys: Tuple[str, ...] = field(repr=False)
    files: Tuple[ManagedFile, ...] = field(repr=False)

    def identity(self) -> Dict[str, Any]:
        return {
            'hostname': self.hostname,
            'repository': {
                'name': self.repository.name,
                'url': self.repository.url,
                'ref': self.repository.ref,
            },
            'tiers': [
                {'number': t.number, 'mode': t.mode,
                 'device': t.device, 'pool_guid': t.pool_guid}
                for t in self.tiers
            ],
        }


_ENV_KEYS = {'APEX_REPO_NAME', 'APEX_REPO_URL', 'APEX_REPO_REF'} | {
    f'APEX_TIER{number}_{suffix}'
    for number in (1, 2, 3)
    for suffix in ('DEVICE', 'MODE', 'POOL_GUID')
}
_ASSIGNMENT = re.compile(
    r'''\s*([A-Z][A-Z0-9_]*)\s*=\s*(?:"([^"\\]*)"|'([^'\\]*)'|([^\s'"\\()#]*))\s*'''
)
_FILE_POLICY = {
    '/etc/apex/bootstrap.env': ('root:root', 0o600, False),
    '/home/adam/.ssh/id_ed25519': ('adam:adam', 0o600, True),
    '/home/adam/.ssh/known_hosts': ('adam:adam', 0o644, True),
}


def _require(condition: bool, diagnostic: str) -> None:
    if not condition:
        raise ContractError(diagnostic)


def _mapping(value: Any, required: set, optional: set, diagnostic: str) -> Mapping:
    _require(isinstance(value, Mapping), diagnostic)
    _require(set(value) >= required and set(value) <= required | optional, diagnostic)
    return value


def _plain(value: Any, diagnostic: str, allow_empty: bool = False) -> str:
    _require(type(value) is str, diagnostic)
    _require(allow_empty or bool(value), diagnostic)
    _require(not any(ord(c) < 32 or ord(c) == 127 for c in value), diagnostic)
    return value


def parse_bootstrap_env(text: str) -> Dict[str, str]:
    _require(type(text) is str, 'bootstrap.env must contain literal assignments')
    result = {}
    for line in text.splitlines():
        if not line.strip() or line.lstrip().startswith('#'):
            continue
        match = _ASSIGNMENT.fullmatch(line)
        _require(match is not None, 'bootstrap.env must contain literal assignments')
        key = match.group(1)
        _require(key in _ENV_KEYS, 'bootstrap.env contains an unsupported assignment')
        _require(key not in result, 'bootstrap.env contains a duplicate assignment')
        value = next(part for part in match.groups()[1:] if part is not None)
        _require(not any(c in value for c in '$`;|&<>'),
                 'bootstrap.env contains unsupported shell syntax')
        result[key] = _plain(value, 'bootstrap.env contains an invalid literal', True)
    return result


def _repository(values: Mapping[str, str]) -> Repository:
    name = values.get('APEX_REPO_NAME', '')
    _require(bool(re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name))
             and name not in ('.', '..'), 'repository name must be one safe directory name')
    url = values.get('APEX_REPO_URL', '')
    _require(bool(url) and not any(c.isspace() for c in url), 'repository URL is invalid')
    if '://' in url:
        try:
            parsed = urlsplit(url)
            port = parsed.port
        except ValueError:
            raise ContractError('repository URL is invalid') from None
        _require(parsed.scheme in ('https', 'ssh') and bool(parsed.hostname)
                 and parsed.password is None and not parsed.query and not parsed.fragment
                 and bool(parsed.path.strip('/')) and (port is None or port > 0),
                 'repository URL is invalid or contains credentials')
        _require(parsed.scheme != 'https' or parsed.username is None,
                 'HTTPS repository URL must not contain credentials')
    else:
        _require(bool(re.fullmatch(
            r'[A-Za-z0-9._-]+@[A-Za-z0-9.-]+:[A-Za-z0-9_./-]+', url)),
            'repository URL must use HTTPS or SSH')
        remote_path = url.split(':', 1)[1]
        _require(not remote_path.startswith('-') and remote_path not in ('.', '..'),
                 'repository URL is invalid')
    ref = values.get('APEX_REPO_REF') or None
    if ref is not None:
        _require(not ref.startswith('-') and not any(c.isspace() for c in ref),
                 'repository ref is invalid')
    return Repository(name, url, ref)


def _tiers(values: Mapping[str, str]) -> Tuple[Tier, ...]:
    result = []
    devices = set()
    guids = set()
    for number in (1, 2, 3):
        prefix = f'APEX_TIER{number}_'
        device = values.get(prefix + 'DEVICE') or None
        mode = values.get(prefix + 'MODE')
        guid = values.get(prefix + 'POOL_GUID') or None
        _require(mode is not None or device is None,
                 'a mapped tier device requires an explicit mode')
        mode = mode or 'directory'
        _require(mode in ('directory', 'create', 'import'), 'tier mode is invalid')
        if mode == 'directory':
            _require(device is None and guid is None,
                     'directory tier must not select a device or pool')
        else:
            _require(device is not None and device.startswith('/dev/')
                     and not any(c.isspace() for c in device)
                     and '..' not in PurePosixPath(device).parts
                     and str(PurePosixPath(device)) == device,
                     'block-backed tier requires a normalized device path')
            _require(device not in devices, 'a device is selected by multiple tiers')
            devices.add(device)
            if mode == 'create':
                _require(guid is None, 'create tier must not name an existing pool')
            else:
                _require(guid is not None and bool(re.fullmatch(r'[1-9][0-9]{0,19}', guid))
                         and int(guid) < 2**64, 'import tier requires a valid pool GUID')
                _require(guid not in guids, 'a pool is selected by multiple tiers')
                guids.add(guid)
        result.append(Tier(number, mode, device, guid))
    return tuple(result)


def normalize_user_data(document: Any) -> Enrollment:
    doc = _mapping(document,
                   {'preserve_hostname', 'hostname', 'users', 'write_files', 'runcmd'},
                   set(), 'unsupported cloud-config structure')
    _require(doc['preserve_hostname'] is False, 'preserve_hostname must be false')
    hostname = _plain(doc['hostname'], 'hostname is invalid')
    labels = hostname.split('.')
    _require(len(hostname) <= 253 and len(labels) >= 2
             and all(re.fullmatch(r'[A-Za-z0-9](?:[A-Za-z0-9-]{0,61}[A-Za-z0-9])?', x)
                     for x in labels), 'hostname must be a valid FQDN')
    users = doc['users']
    _require(type(users) is list and len(users) == 1, 'exactly one administrator is required')
    user = _mapping(users[0],
                    {'name', 'sudo', 'groups', 'shell', 'ssh_authorized_keys'},
                    set(), 'unsupported administrator configuration')
    _require(user['name'] == 'adam' and user['shell'] == '/bin/bash',
             'administrator must be adam with Bash')
    _require(type(user['sudo']) is str and ''.join(user['sudo'].split()) in
             ('ALL=(ALL)NOPASSWD:ALL', 'ALL=(ALL:ALL)NOPASSWD:ALL'),
             'administrator requires passwordless sudo')
    groups = user['groups']
    _require(type(groups) is list and len(groups) == 2
             and all(type(g) is str for g in groups) and set(groups) == {'sudo', 'docker'},
             'administrator requires sudo and docker groups')
    keys = user['ssh_authorized_keys']
    _require(type(keys) is list and len(keys) > 0, 'administrator requires public SSH keys')
    authorized_keys = tuple(_plain(k, 'administrator SSH key is invalid') for k in keys)
    _require(all(k.strip() for k in authorized_keys), 'administrator SSH key is empty')
    commands = doc['runcmd']
    _require(commands in (['/usr/local/bin/apex-bootstrap'],
                          [['/usr/local/bin/apex-bootstrap']]),
             'only the standard APEX bootstrap command is supported')
    writes = doc['write_files']
    _require(type(writes) is list, 'write_files must be a list')
    files = []
    seen = set()
    for write in writes:
        item = _mapping(write, {'path', 'owner', 'permissions', 'content'}, {'defer'},
                        'unsupported write_files entry')
        path = _plain(item['path'], 'managed file path is invalid')
        _require(path in _FILE_POLICY and path not in seen,
                 'managed file path is unsupported or repeated')
        seen.add(path)
        owner, mode, deferred = _FILE_POLICY[path]
        permission = item['permissions']
        _require(type(permission) is str and bool(re.fullmatch(r'0?[0-7]{3}', permission))
                 and int(permission, 8) == mode and item['owner'] == owner,
                 'managed file ownership or permissions are invalid')
        _require(item.get('defer', False) is deferred, 'managed file defer setting is invalid')
        content = item['content']
        _require(type(content) is str and bool(content.strip()) and '\x00' not in content,
                 'managed file content is invalid')
        files.append(ManagedFile(path, owner, mode, content))
    bootstrap = next((f for f in files if f.path == '/etc/apex/bootstrap.env'), None)
    _require(bootstrap is not None, 'bootstrap.env is required')
    values = parse_bootstrap_env(bootstrap.content)
    return Enrollment(hostname, _repository(values), _tiers(values), authorized_keys, tuple(files))
