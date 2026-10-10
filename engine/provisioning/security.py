"""Security activation owned by the protected access transaction."""
from __future__ import annotations

import base64
import hashlib
import ipaddress
import json
import os
import re
import shlex
import uuid

from .access import atomic
from .baseline import HELPER_SHA256


SERVICES = ('ufw', 'crowdsec', 'crowdsec-firewall-bouncer')
FILES = ('/etc/default/ufw', '/etc/ufw/ufw.conf', '/etc/ufw/before.rules',
         '/etc/ufw/before6.rules', '/etc/ufw/after.rules', '/etc/ufw/after6.rules',
         '/etc/ufw/user.rules', '/etc/ufw/user6.rules')
HELPER = '/usr/local/bin/ufw-docker'
BLOCK = re.compile(r'^# BEGIN UFW AND DOCKER\n.*?^# END UFW AND DOCKER\n?', re.M | re.S)


def _helper_lines(ipv6):
    prefix = 'ufw6' if ipv6 else 'ufw'
    subnets = ('fd00::/8',) if ipv6 else ('10.0.0.0/8', '172.16.0.0/12', '192.168.0.0/16')
    return ['# BEGIN UFW AND DOCKER', '*filter', ':' + prefix + '-user-forward - [0:0]',
            ':' + prefix + '-docker-logging-deny - [0:0]', ':DOCKER-USER - [0:0]',
            '-A DOCKER-USER -j ' + prefix + '-user-forward',
            '-A DOCKER-USER -m conntrack --ctstate RELATED,ESTABLISHED -j RETURN',
            '-A DOCKER-USER -m conntrack --ctstate INVALID -j DROP',
            '-A DOCKER-USER -i docker0 -o docker0 -j ACCEPT',
            *['-A DOCKER-USER -j RETURN -s ' + subnet for subnet in subnets],
            *['-A DOCKER-USER -j ' + prefix + '-docker-logging-deny -m conntrack --ctstate NEW -d ' + subnet
              for subnet in subnets],
            '-A DOCKER-USER -j RETURN',
            '-A ' + prefix + '-docker-logging-deny -m limit --limit 3/min --limit-burst 10 -j LOG --log-prefix "[UFW DOCKER BLOCK] "',
            '-A ' + prefix + '-docker-logging-deny -j DROP', 'COMMIT', '# END UFW AND DOCKER']


def _runtime_rule(line):
    tokens = shlex.split(line)
    if len(tokens) % 2:
        raise SecurityError('Unsupported runtime firewall rule')
    pairs = []
    for key, value in zip(tokens[::2], tokens[1::2]):
        if key in ('-s', '-d'):
            value = str(ipaddress.ip_network(value, strict=False))
        elif key == '--ctstate':
            value = ','.join(sorted(value.split(',')))
        pairs.append((key, value))
    return sorted(pairs)


class SecurityError(RuntimeError):
    pass


class Security:
    def __init__(self, host):
        self.host = host

    def preflight(self):
        services = {name: self._service(name) for name in SERVICES}
        for name, state in services.items():
            if (state.get('LoadState') != 'loaded' or state.get('UnitFileState') not in ('enabled', 'disabled')
                    or state.get('ActiveState') not in ('active', 'inactive')
                    or state.get('DropInPaths') or state.get('FragmentPath') not in
                    ('/usr/lib/systemd/system/' + name + '.service', '/lib/systemd/system/' + name + '.service')):
                raise SecurityError('Unsupported or masked security service: ' + name)
        if self._digest(self._path(HELPER).read_bytes()) != HELPER_SHA256:
            raise SecurityError('The ufw-docker helper does not match the pinned baseline')
        files = self._files()
        active = self._active()
        pristine = False
        if not active:
            output = self.host.command(['dpkg-query', '-W', '-f=${Conffiles}', 'ufw']).stdout
            conffiles = dict(re.findall(r'^\s*(/\S+)\s+([a-f0-9]{32})(?:\s|$)', output, re.M))
            pristine = (hashlib.md5(base64.b64decode(files['/etc/default/ufw']['data'])).hexdigest()
                        == conffiles.get('/etc/default/ufw'))
            manifest = dict((name, digest) for digest, name in re.findall(
                r'^([a-f0-9]{32})\s+(\S+)\s*$', self._text('/var/lib/dpkg/info/ufw.md5sums'), re.M))
            # Debian's postinst copies/ucf-installs these files; they are not dpkg conffiles.
            for name in FILES[1:]:
                filename = name.rsplit('/', 1)[1]
                template = '/usr/share/ufw/' + ('' if filename == 'ufw.conf' else 'iptables/') + filename
                data = self._path(template).read_bytes()
                pristine = pristine and (hashlib.md5(data).hexdigest() == manifest.get(template.lstrip('/'))
                                         and data == base64.b64decode(files[name]['data']))
            if not pristine:
                raise SecurityError('Inactive UFW is not provably pristine; reconcile its existing policy first')
        texts = [self._text('/etc/ufw/' + name) for name in ('after.rules', 'after6.rules')]
        present = any('UFW AND DOCKER' in text for text in texts)
        if present:
            self._check_helper_files()
        else:
            for executable in ('iptables', 'ip6tables'):
                result = self.host.command([executable, '-S', 'DOCKER-USER'], False)
                if result.returncode == 0 and any(line not in ('-N DOCKER-USER', '-A DOCKER-USER -j RETURN')
                                                for line in result.stdout.splitlines()):
                    raise SecurityError('Unmanaged Docker firewall rules require reconciliation')
        return {'version': 1, 'pristine': pristine, 'active': active, 'services': services,
                'files': files, 'helper_present': present,
                'ipv6': bool(re.search(r'^IPV6\s*=\s*[\"\x27]?yes', self._text('/etc/default/ufw'), re.M))}

    def _path(self, name):
        path = self.host.path(name)
        for parent in (path, *path.parents):
            if parent.is_symlink():
                raise SecurityError('Refusing redirected security path: ' + name)
        if not path.is_file():
            raise SecurityError('Required security file is missing: ' + name)
        return path

    def _text(self, name):
        return self._path(name).read_text()

    @staticmethod
    def _digest(data):
        return hashlib.sha256(data).hexdigest()

    def _files(self):
        return {name: self._file(name) for name in FILES}

    def _file(self, name):
        path = self._path(name)
        data, metadata = path.read_bytes(), path.stat()
        result = {'data': base64.b64encode(data).decode(), 'digest': self._digest(data),
                  'mode': metadata.st_mode & 0o777, 'uid': metadata.st_uid, 'gid': metadata.st_gid}
        result['fingerprint'] = self._digest(json.dumps(result, sort_keys=True).encode())
        return result

    def _service(self, name):
        output = self.host.command(['systemctl', 'show', name, '-p',
                                   'LoadState,ActiveState,UnitFileState,FragmentPath,DropInPaths']).stdout
        return dict(line.split('=', 1) for line in output.splitlines() if '=' in line)

    def _active(self):
        output = self.host.command(['ufw', 'status']).stdout
        if 'Status: inactive' in output:
            return False
        if 'Status: active' in output:
            return True
        raise SecurityError('Cannot determine UFW runtime state')

    def snapshot(self, plan, original_port, client_address):
        if not isinstance(original_port, int) or not 1 <= original_port <= 65535:
            raise SecurityError('Invalid bootstrap port')
        try:
            address = str(ipaddress.ip_address(client_address))
        except ValueError:
            raise SecurityError('Invalid bootstrap peer address') from None
        if plan != self.preflight():
            raise SecurityError('Security configuration changed after preflight')
        if ipaddress.ip_address(address).version == 6 and not plan['ipv6']:
            raise SecurityError('IPv6 bootstrap access requires enabled UFW IPv6 support')
        if plan['services']['crowdsec']['ActiveState'] == 'active':
            self._check_decisions(address)
        return {'version': 1, 'plan': plan, 'original_port': original_port,
                'client_address': address, 'token': uuid.uuid4().hex, 'rules': [],
                'expected': {name: value['fingerprint'] for name, value in plan['files'].items()},
                'service_changes': [], 'inflight': None, 'phase': 'prepared',
                'access_recovered': False, 'cleanup': 'not-started', 'helper_blocks': {}}

    def activate(self, state, persist):
        if state['phase'] != 'prepared' or state['inflight']:
            raise SecurityError('Security activation requires a fresh prepared snapshot')
        self._assert_files(state)
        state['phase'] = 'activating'
        persist(state)
        self._add_rule(state, state['original_port'], state['client_address'], True, persist)
        self._add_rule(state, 2222, '0.0.0.0/0', False, persist)
        if state['plan']['ipv6']:
            self._add_rule(state, 2222, '::/0', False, persist)
        if state['plan']['pristine']:
            for policy, direction in (('deny', 'incoming'), ('allow', 'outgoing'), ('allow', 'routed')):
                self._mutate(state, ['ufw', 'default', policy, direction], ['/etc/default/ufw'], persist)
        if not state['plan']['helper_present']:
            names = ['/etc/ufw/after.rules', '/etc/ufw/after6.rules']
            self._mutate(state, [HELPER, 'install'], names, persist)
            for name in names:
                blocks = BLOCK.findall(self._text(name))
                if len(blocks) != 1:
                    raise SecurityError('ufw-docker did not install a verifiable integration block')
                state['helper_blocks'][name] = blocks[0]
            persist(state)
        self._check_helper_files()
        self._mutate(state, ['ufw', '--force', 'enable'], ['/etc/ufw/ufw.conf'], persist)
        self._mutate(state, ['ufw', 'reload'], [], persist)
        self._enable(state, 'ufw', persist)
        self.host.command(['crowdsec', '-t', '-c', '/etc/crowdsec/config.yaml'])
        self._enable(state, 'crowdsec', persist)
        self.host.command(['cscli', 'lapi', 'status'])
        self._check_decisions(state['client_address'])
        self._enable(state, 'crowdsec-firewall-bouncer', persist)
        state['phase'] = 'active'
        persist(state)
        self.verify(state)

    def finalize(self, state, persist):
        self.verify(state)
        for rule in state['rules']:
            if rule['temporary'] and rule['owned']:
                self._delete_rule(state, rule, persist)
        state['phase'] = 'final'
        persist(state)
        self.verify(state)

    def verify(self, state):
        if state['phase'] not in ('active', 'final') or state['inflight']:
            raise SecurityError('Security activation is incomplete')
        self._assert_files(state)
        if not self._active():
            raise SecurityError('UFW is not active')
        for name in SERVICES:
            service = self._service(name)
            if service.get('ActiveState') != 'active' or service.get('UnitFileState') != 'enabled':
                raise SecurityError('Security service is not active and enabled: ' + name)
        self.host.command(['cscli', 'lapi', 'status'])
        self._check_decisions(state['client_address'])
        self._check_helper_files()
        self._check_helper_runtime(state['plan']['ipv6'])
        for rule in state['rules']:
            if not (rule['temporary'] and state['phase'] == 'final') and self._rule_comment(rule) is None:
                raise SecurityError('Required SSH firewall allowance is missing')

    def _check_helper_files(self):
        for ipv6, name in ((False, 'after.rules'), (True, 'after6.rules')):
            text = self._text('/etc/ufw/' + name)
            blocks = BLOCK.findall(text)
            if (len(blocks) != 1 or text.count('UFW AND DOCKER') != 2
                    or [line.strip() for line in blocks[0].splitlines() if line.strip()] != _helper_lines(ipv6)):
                raise SecurityError('Existing ufw-docker integration is custom or incomplete')

    def _check_helper_runtime(self, ipv6):
        for version in (False, True) if ipv6 else (False,):
            executable = 'ip6tables' if version else 'iptables'
            prefix = 'ufw6' if version else 'ufw'
            for chain in ('DOCKER-USER', prefix + '-docker-logging-deny'):
                result = self.host.command([executable, '-S', chain], False)
                expected = ['-N ' + chain] + [line for line in _helper_lines(version)
                                              if line.startswith('-A ' + chain + ' ')]
                try:
                    matches = (not result.returncode and [_runtime_rule(line) for line in result.stdout.splitlines()]
                               == [_runtime_rule(line) for line in expected])
                except ValueError:
                    matches = False
                if not matches:
                    raise SecurityError('ufw-docker runtime chain is missing or changed: ' + executable + ' ' + chain)
            if self.host.command([executable, '-C', 'FORWARD', '-j', 'DOCKER-USER'], False).returncode:
                raise SecurityError('Docker forwarding hook is missing: ' + executable)

    def restore(self, state, persist):
        state['cleanup'] = 'pending'
        state['access_recovered'] = False
        persist(state)
        errors = []
        for change in reversed(state['service_changes']):
            name, field = change['service'], change['field']
            if change['status'] == 'restored':
                continue
            if name == 'ufw' and field == 'ActiveState' and state['plan']['active']:
                continue
            try:
                current = self._service(name)
                if any(current.get(key) != change['metadata'].get(key) for key in change['metadata']):
                    raise SecurityError('Service unit changed during activation')
                if current[field] == change['after']:
                    actions = {'active': 'start', 'inactive': 'stop', 'enabled': 'enable', 'disabled': 'disable'}
                    change['status'] = 'restoring'
                    persist(state)
                    self.host.command(['systemctl', actions[change['before']], name])
                elif current[field] != change['before']:
                    raise SecurityError('Service state changed outside this transition')
                if self._service(name)[field] != change['before']:
                    raise SecurityError('Service state restoration did not converge')
                change['status'] = 'restored'
                persist(state)
            except Exception:
                errors.append('service:' + name + ':' + field)
        ambiguous = []
        for name in FILES:
            try:
                if self._file(name)['fingerprint'] not in (state['plan']['files'][name]['fingerprint'], state['expected'][name]):
                    ambiguous.append(name)
            except Exception:
                ambiguous.append(name)
        pending = bool(ambiguous or state['inflight'] or errors)
        original_active = state['plan']['active']
        # Recover runtime access even when an unrelated edit prevents file cleanup.
        try:
            if not original_active:
                try:
                    self._path('/etc/ufw/ufw.conf')
                except SecurityError:
                    # A service stop removes runtime rules without following the replaced enable file.
                    self.host.command(['systemctl', 'stop', 'ufw'])
                else:
                    self.host.command(['ufw', 'disable'])
                state['access_recovered'] = not self._active()
            elif pending:
                self._retain_bootstrap(state, persist)
                if not self._active():
                    self.host.command(['ufw', '--force', 'enable'])
                state['access_recovered'] = self._active()
        except Exception:
            errors.append('bootstrap-firewall')
        for name, previous in state['plan']['files'].items():
            try:
                path = self._path(name)
                content = path.read_bytes()
                fingerprint = self._file(name)['fingerprint']
                if fingerprint == previous['fingerprint']:
                    continue
                if fingerprint == state['expected'][name] and not (original_active and pending and name.endswith(('user.rules', 'user6.rules'))):
                    atomic(path, base64.b64decode(previous['data']), previous['mode'])
                    os.chown(path, previous['uid'], previous['gid'])
                    continue
                block = state['helper_blocks'].get(name)
                if block and content.decode().count(block) == 1:
                    metadata = path.stat()
                    atomic(path, content.replace(block.encode(), b'', 1), metadata.st_mode & 0o777)
                    os.chown(path, metadata.st_uid, metadata.st_gid)
                else:
                    errors.append('file:' + name)
            except Exception:
                errors.append('file:' + name)
        # Rule removal is ownership-checked independently of whole-file snapshots.
        for rule in state['rules']:
            if rule['owned'] and not (original_active and pending and rule['temporary']):
                try:
                    if self._rule_comment(rule) == rule['comment']:
                        self.host.command(self._rule_command(rule, delete=True))
                    elif self._rule_comment(rule) is not None:
                        errors.append('rule-ownership')
                except Exception:
                    errors.append('rule-cleanup')
        try:
            if original_active:
                if errors or pending:
                    self._retain_bootstrap(state, persist)
                else:
                    self.host.command(['ufw', 'reload'])
                state['access_recovered'] = self._active()
            else:
                # Restoring the former ufw.conf must not reactivate the runtime.
                state['access_recovered'] = not self._active()
        except Exception:
            errors.append('bootstrap-firewall')
        if not state['access_recovered']:
            errors.append('bootstrap-firewall')
        if errors or pending:
            state['diagnostics'] = sorted(set(errors + ['file:' + name for name in ambiguous]
                                              + (['interrupted-mutation'] if state['inflight'] else [])))
            persist(state)
            raise SecurityError('Security cleanup pending; bootstrap firewall recovery=' + str(state['access_recovered'])
                                + '; inspect the root-owned recovery record')
        state.update(cleanup='complete', phase='restored', inflight=None, diagnostics=[])
        persist(state)

    def _assert_files(self, state):
        if any(value['fingerprint'] != state['expected'][name] for name, value in self._files().items()):
            raise SecurityError('Security files changed outside this transition; recovery remains required')

    def _mutate(self, state, args, changed, persist):
        self._assert_files(state)
        state['inflight'] = {'command': args[:2], 'files': changed}
        persist(state)
        self.host.command(args)
        current = self._files()
        for name in changed:
            state['expected'][name] = current[name]['fingerprint']
        if any(value['fingerprint'] != state['expected'][name] for name, value in current.items()):
            raise SecurityError('Concurrent security file change during activation')
        state['inflight'] = None
        persist(state)

    def _enable(self, state, name, persist):
        for field, before, after, operation in (
                ('UnitFileState', 'disabled', 'enabled', 'enable'),
                ('ActiveState', 'inactive', 'active', 'start')):
            current = self._service(name)
            if current[field] == after:
                continue
            if current[field] != before:
                raise SecurityError('Security service changed before activation: ' + name)
            metadata = {key: current.get(key) for key in ('LoadState', 'FragmentPath', 'DropInPaths')}
            if any(value != state['plan']['services'][name].get(key) for key, value in metadata.items()):
                raise SecurityError('Security service unit changed before activation: ' + name)
            change = {'service': name, 'field': field, 'before': before, 'after': after,
                      'metadata': metadata, 'status': 'pending'}
            state['service_changes'].append(change)
            persist(state)
            self.host.command(['systemctl', operation, name])
            if self._service(name)[field] != after:
                raise SecurityError('Security service did not activate: ' + name)
            change['status'] = 'applied'
            persist(state)

    def _check_decisions(self, peer):
        self.host.command(['cscli', 'lapi', 'status'])
        try:
            alerts = json.loads(self.host.command(
                ['cscli', 'decisions', 'list', '--all', '--limit', '0', '--no-simu', '-o', 'json']).stdout)
            if not isinstance(alerts, list):
                raise ValueError()
            address = ipaddress.ip_address(peer)
            for alert in alerts:
                if not isinstance(alert, dict) or not isinstance(alert.get('decisions'), list):
                    raise ValueError()
                for decision in alert['decisions']:
                    if (not isinstance(decision, dict) or any(not isinstance(decision.get(key), str)
                            or not decision[key] for key in ('scope', 'value', 'type'))
                            or decision.get('simulated', False) is not False):
                        raise ValueError()
                    if decision['scope'].lower() not in ('ip', 'range'):
                        continue
                    network = ipaddress.ip_network(decision['value'], strict=False)
                    if address in network:
                        raise SecurityError('An existing CrowdSec decision covers the bootstrap peer')
        except (ValueError, TypeError, KeyError):
            raise SecurityError('Cannot verify CrowdSec decisions safely') from None

    @staticmethod
    def _rule_file(rule):
        return '/etc/ufw/user6.rules' if ipaddress.ip_network(rule['source'], strict=False).version == 6 else '/etc/ufw/user.rules'

    def _rule_comment(self, rule):
        source = ipaddress.ip_network(rule['source'], strict=False)
        any_address = '::/0' if source.version == 6 else '0.0.0.0/0'
        for line in self._text(self._rule_file(rule)).splitlines():
            if not line.startswith('### tuple ### '):
                continue
            parts = line[len('### tuple ### '):].split()
            if len(parts) < 7 or parts[:5] != ['allow', 'tcp', str(rule['port']), any_address, 'any'] or parts[6] != 'in':
                continue
            try:
                if ipaddress.ip_network(parts[5], strict=False) != source:
                    continue
                comment = next((p[8:] for p in parts[7:] if p.startswith('comment=')), '')
                return bytes.fromhex(comment).decode() if comment else ''
            except (ValueError, UnicodeError):
                raise SecurityError('Cannot prove ownership of an existing SSH firewall rule') from None
        return None

    @staticmethod
    def _rule_command(rule, delete=False):
        command = ['ufw', '--force', 'delete'] if delete else (['ufw', 'insert', '1'] if rule['temporary'] else ['ufw'])
        return command + ['allow', 'proto', 'tcp', 'from', rule['source'], 'to', 'any', 'port', str(rule['port']),
                          'comment', rule['comment']]

    def _add_rule(self, state, port, source, temporary, persist):
        rule = {'port': port, 'source': source, 'temporary': temporary,
                'comment': 'apex-init-' + state['token'] + ('-bootstrap' if temporary else '-final')}
        rule['owned'] = self._rule_comment(rule) is None
        state['rules'].append(rule)
        persist(state)
        if rule['owned']:
            self._mutate(state, self._rule_command(rule), ['/etc/ufw/user.rules', '/etc/ufw/user6.rules'], persist)

    def _delete_rule(self, state, rule, persist):
        if self._rule_comment(rule) != rule['comment']:
            raise SecurityError('Owned SSH rule changed; preserve it for explicit reconciliation')
        self._mutate(state, self._rule_command(rule, delete=True), ['/etc/ufw/user.rules', '/etc/ufw/user6.rules'], persist)

    def _retain_bootstrap(self, state, persist):
        rule = next((rule for rule in state['rules'] if rule['temporary']), None)
        if rule is None:
            raise SecurityError('No recorded bootstrap allowance exists')
        if self._rule_comment(rule) is None:
            rule['owned'] = True
            state['bootstrap_retained'] = True
            persist(state)
            self.host.command(self._rule_command(rule))
