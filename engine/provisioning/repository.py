"""Pinned, non-destructive checkout enrollment as the node administrator."""
from __future__ import annotations

import os
from pathlib import Path
import pwd
import subprocess

from engine.identity import read_env
from . import ENROLLMENT_INTERFACE
from .state import ProvisionError, safe_path


def as_adam(args, cwd=None):
    environment = dict(os.environ, GIT_TERMINAL_PROMPT='0',
                       GIT_SSH_COMMAND='ssh -o BatchMode=yes -o StrictHostKeyChecking=yes',
                       LC_ALL='C')
    result = subprocess.run(['runuser', '-u', 'adam', '--', *args], cwd=cwd,
                            env=environment, text=True, capture_output=True, timeout=300)
    if result.returncode:
        # Git/hook stderr may contain credential files or remote URL credentials.
        raise ProvisionError('repository operation failed; check administrator Git access and the pinned checkout')
    return result.stdout.strip()


def verify_checkout_setup(destination, account):
    if as_adam(['git', 'config', '--get', 'core.hooksPath'], destination) != 'commons/githooks':
        raise ProvisionError('checkout hook configuration is missing or incompatible')
    home = safe_path(account.pw_dir)
    bashrc = safe_path(home / '.bashrc')
    entry = 'export PATH="' + str(destination / 'commons') + ':$PATH"'
    if not bashrc.is_file() or bashrc.read_text().splitlines().count(entry) != 1:
        raise ProvisionError('checkout PATH setup is missing or duplicated; reconcile init.sh before retrying')
    vim = home / '.vimrc'
    if not vim.is_symlink() or vim.resolve() != (destination / 'commons/etc/.vimrc').resolve():
        raise ProvisionError('checkout editor link is missing or incompatible')


def reconcile_repository(spec, state, save, srv=Path('/srv')):
    repository = spec.repository
    destination = safe_path(Path(srv) / repository.name)
    staging = safe_path(Path(srv) / ('.apex-clone-' + repository.name))
    expected = {'name': repository.name, 'url': repository.url, 'ref': repository.ref}
    account = pwd.getpwnam('adam')
    record = state.setdefault('repository', {})
    if record and record.get('request') != expected:
        raise ProvisionError('repository enrollment conflicts with its recorded request')
    if not record:
        if destination.exists() or staging.exists():
            raise ProvisionError('unrecorded repository path exists; explicit adoption is required')
        record.update(request=expected, clone_intent=True)
        save()
    if not destination.exists():
        if staging.exists():
            # An interrupted clone may be incomplete: never erase or guess its state.
            raise ProvisionError('an interrupted clone remains; inspect the staging checkout before retrying')
        staging.mkdir(mode=0o755)
        os.chown(staging, account.pw_uid, account.pw_gid)
        as_adam(['git', 'clone', '--no-checkout', '--', repository.url, str(staging)])
        os.rename(staging, destination)
    safe_path(destination / '.git')
    origin = as_adam(['git', 'remote', 'get-url', 'origin'], destination)
    if origin != repository.url:
        raise ProvisionError('existing checkout has a different origin; it was preserved')
    if 'commit' not in record:
        reference = repository.ref or 'HEAD'
        if repository.ref:
            as_adam(['git', 'fetch', '--no-tags', 'origin', reference], destination)
            reference = 'FETCH_HEAD'
        commit = as_adam(['git', 'rev-parse', '--verify', reference + '^{commit}'], destination)
        record['commit'] = commit
        record['checkout_intent'] = True
        save()
    commit = record['commit']
    if record.get('checkout_intent'):
        # --no-checkout index intentionally looks dirty before the first checkout.
        if any(p.name != '.git' for p in destination.iterdir()):
            head = as_adam(['git', 'rev-parse', 'HEAD'], destination)
            dirty = as_adam(['git', 'status', '--porcelain'], destination)
            if head != commit or dirty:
                raise ProvisionError('interrupted checkout has unexpected contents; it was preserved')
        else:
            as_adam(['git', 'checkout', '--detach', commit], destination)
        record.pop('checkout_intent')
        save()
    if as_adam(['git', 'rev-parse', 'HEAD'], destination) != commit:
        raise ProvisionError('checkout revision changed; initialization will not reset it')
    if as_adam(['git', 'status', '--porcelain'], destination):
        raise ProvisionError('checkout contains local changes; initialization will not reset it')
    node_env = safe_path(destination / 'node.env')
    node_identity = read_env(str(node_env))
    if node_identity.get('APEX_NODE_FQDN') != spec.hostname:
        raise ProvisionError('repository node.env does not declare the requested hostname')
    if node_identity.get('APEX_ENROLLMENT_INTERFACE') != str(ENROLLMENT_INTERFACE):
        raise ProvisionError('node checkout enrollment interface is missing or unsupported; update its init.sh and node.env together')
    as_adam(['git', 'submodule', 'update', '--init', '--recursive'], destination)
    commons = safe_path(destination / 'commons')
    if not commons.is_dir():
        raise ProvisionError('node checkout does not contain its pinned commons submodule')
    commons_commit = as_adam(['git', 'rev-parse', 'HEAD'], commons)
    gitlink = as_adam(['git', 'ls-tree', 'HEAD', '--', 'commons'], destination).split()
    if gitlink != ['160000', 'commit', commons_commit, 'commons']:
        raise ProvisionError('commons is not the exact pinned Git submodule')
    if Path(as_adam(['git', 'rev-parse', '--show-toplevel'], commons)).resolve() != commons.resolve():
        raise ProvisionError('commons does not have its own Git worktree')
    if record.get('commons_commit', commons_commit) != commons_commit:
        raise ProvisionError('pinned commons revision changed unexpectedly')
    hook = safe_path(destination / 'init.sh')
    if not hook.is_file():
        raise ProvisionError('node repository is missing its checkout initializer')
    if not record.get('hook_complete'):
        as_adam(['bash', './init.sh'], destination)
        verify_checkout_setup(destination, account)
        record['hook_complete'] = True
    else:
        verify_checkout_setup(destination, account)
    if as_adam(['git', 'status', '--porcelain'], destination):
        raise ProvisionError('checkout initializer changed tracked files; inspect before resuming')
    record['commons_commit'] = commons_commit
    save()
    return {'path': str(destination), 'commit': commit, 'commons_commit': commons_commit}
