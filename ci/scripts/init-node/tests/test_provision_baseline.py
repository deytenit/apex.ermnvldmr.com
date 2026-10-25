"""Exercise baseline decisions with a temporary host filesystem and fake commands."""
import hashlib
import importlib.util
import io
import json
from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from engine.provisioning import baseline


class Host:
    def __init__(self, root):
        self.root = root
        self.commands = []
        self.installed = {}
        self.partial = {}
        self.active = set()
        self.enabled = set()
        self.masked = set()
        self.fail_install = False
        self.plan = ''
        self.zfs_available = True
        self.docker_root = '/srv/docker'
        self.bouncer_key = 'testOnlyGeneratedCredential1234567890'
        self.registration_failure = False
        self.registrations = {}
        self.path('/etc/os-release').parent.mkdir(parents=True)
        self.path('/etc/os-release').write_text('ID=debian\nVERSION_ID="13"\n')
        self.path('/etc/debian_version').touch()
        self.path('/usr/sbin').mkdir(parents=True)

    def path(self, path):
        path = Path(path)
        if str(path).startswith(str(self.root)):
            return path
        return self.root / str(path).lstrip('/')

    def command(self, args, **kwargs):
        self.commands.append(args)
        code, output = 0, ''
        if args[:2] == ['dpkg', '--print-architecture']:
            output = 'amd64\n'
        elif args[0] == 'dpkg-query':
            output = ''.join(f'{name}\tinstall ok installed\t{version}\n' for name, version in self.installed.items())
            output += ''.join(f'{name}\t{status}\t1.0\n' for name, status in self.partial.items())
        elif args == ['uname', '-r']:
            output = '6.12.0-amd64\n'
        elif args[0] == 'apt-get' and 'install' in args:
            if '--simulate' in args:
                output = self.plan
            elif self.fail_install:
                code = 100
            else:
                start = args.index('--no-remove') + 1
                # Reproduce dpkg configuring the bouncer before CrowdSec in one batch.
                if 'crowdsec-firewall-bouncer-iptables' in args[start:]:
                    config = self.path('/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml')
                    config.parent.mkdir(parents=True, exist_ok=True)
                    if not config.exists():
                        key = 'packageGeneratedCredential123456789' if 'crowdsec' in self.installed else '<API_KEY>'
                        config.write_text(f'api_url: http://127.0.0.1:8080/\napi_key: {key}\nmode: iptables\n')
                        config.chmod(0o640)
                for name in args[start:]:
                    self.installed[name] = '1.0'
                    self.partial.pop(name, None)
        elif args[:4] == ['cscli', '-oraw', 'bouncers', 'add']:
            if self.registration_failure:
                code, output = 1, 'sensitive diagnostic'
            else:
                output = self.bouncer_key + '\n'
                self.registrations[args[4]] = self.bouncer_key
        elif args[0] == 'systemctl':
            service = args[-1]
            operation = args[1]
            if operation == 'is-active':
                code = 0 if service in self.active else 3
            elif operation == 'is-enabled':
                code = 0 if service in self.enabled else 1
                output = 'masked\n' if service in self.masked else ''
            elif operation == 'start':
                conditions = []
                for dropin in self.path(f'/etc/systemd/system/{service}.service.d').glob('*.conf'):
                    conditions.extend(line.partition('=')[2] for line in dropin.read_text().splitlines()
                                      if line.startswith('ConditionPathExists='))
                if service not in self.masked and all(self.path(path).exists() for path in conditions):
                    self.active.add(service)
            elif operation == 'enable':
                self.enabled.add(service)
            elif operation == 'disable':
                self.enabled.discard(service)
            elif operation == 'mask':
                self.masked.add(service)
                unit = self.path(f'/etc/systemd/system/{service}.service')
                unit.parent.mkdir(parents=True, exist_ok=True)
                unit.symlink_to('/dev/null')
            elif operation == 'unmask':
                self.masked.discard(service)
                unit = self.path(f'/etc/systemd/system/{service}.service')
                if unit.is_symlink():
                    unit.unlink()
        elif args[:2] == ['docker', 'info']:
            output = self.docker_root + '\n'
        elif args[:3] == ['docker', 'compose', 'version']:
            output = '2.39.2\n'
        elif args == ['modprobe', 'zfs']:
            code = 0 if self.zfs_available else 1
        elif args == ['zfs', 'version']:
            output = 'zfs-2.3.2\nzfs-kmod-2.3.2\n'
        return subprocess.CompletedProcess(args, code, output, 'installation failed' if code else '')


class BaselineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Host(Path(self.temp.name).resolve())
        self.helper = b'#!/bin/bash\necho pinned test helper\n'
        patches = [
            patch.object(baseline, 'Path', self.host.path),
            patch.object(baseline, 'POLICY', self.host.path('/usr/sbin/policy-rc.d')),
            patch.object(baseline, 'POLICY_STATE', self.host.path('/usr/sbin/.apex-policy-rc.d')),
            patch.object(baseline, 'SECURITY_STATE', self.host.path('/var/lib/apex/baseline-security.json')),
            patch.object(baseline.os, 'geteuid', return_value=Path(self.temp.name).stat().st_uid),
            patch.object(baseline.subprocess, 'run', side_effect=self.host.command),
            patch.object(baseline.urllib.request, 'urlopen', side_effect=self.download),
            patch.object(baseline, 'HELPER_SHA256', hashlib.sha256(self.helper).hexdigest()),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)

    def download(self, url, **kwargs):
        if url == baseline.HELPER_URL:
            return io.BytesIO(self.helper)
        return io.BytesIO(b'-----BEGIN PGP PUBLIC KEY BLOCK-----\nfixture\n')

    def apply(self):
        # The filesystem remains owned by the test uid; only root preflight is isolated.
        with patch.object(baseline, 'preflight'):
            return baseline.apply_baseline()

    def test_package_database_drives_installation_and_repeat_does_not_upgrade(self):
        self.host.installed['curl'] = '7.0'
        self.host.partial['rsyslog'] = 'deinstall ok config-files'
        self.assertEqual(baseline.install_missing(['curl', 'rsyslog']), ['rsyslog'])
        self.assertEqual(self.host.installed['curl'], '7.0')
        self.assertEqual(self.host.installed['rsyslog'], '1.0')
        count = len(self.host.commands)
        self.assertEqual(baseline.install_missing(['curl', 'rsyslog']), [])
        self.assertFalse(any(command[0] == 'apt-get' for command in self.host.commands[count:]))

    def test_dependency_upgrade_is_rejected_before_install(self):
        self.host.plan = 'Inst libc6 [2.0] (3.0 Debian:13)\n'
        with self.assertRaisesRegex(baseline.BaselineError, 'upgrade/removal'):
            baseline.install_missing(['git'])
        self.assertNotIn('git', self.host.installed)

    def test_held_installed_packages_are_preserved(self):
        self.host.partial['git'] = 'hold ok installed'
        self.assertEqual(baseline.install_missing(['git']), [])
        self.assertFalse(any(command[0] == 'apt-get' for command in self.host.commands))

    def test_conflicting_docker_config_is_rejected_without_host_mutation(self):
        path = self.host.path('/etc/docker/daemon.json')
        path.parent.mkdir(parents=True)
        path.write_text('{"data-root": "/data/docker"}')
        before = sorted(str(path.relative_to(self.host.root)) for path in self.host.root.rglob('*'))
        with self.assertRaisesRegex(baseline.BaselineError, 'Conflicting Docker'):
            self.apply()
        self.assertEqual(before, sorted(str(path.relative_to(self.host.root)) for path in self.host.root.rglob('*')))
        self.assertFalse(any(command[0] in ('apt-get', 'systemctl') for command in self.host.commands))

    def test_existing_docker_data_is_preserved(self):
        data = self.host.path('/var/lib/docker/volumes/user-data')
        data.parent.mkdir(parents=True)
        data.write_text('valuable')
        with self.assertRaisesRegex(baseline.BaselineError, 'Existing /var/lib/docker'):
            self.apply()
        self.assertEqual(data.read_text(), 'valuable')

    def test_conflicting_distribution_docker_package_is_never_removed(self):
        self.host.installed['docker.io'] = '26.1.5'
        with self.assertRaisesRegex(baseline.BaselineError, 'Conflicting Docker packages'):
            self.apply()
        self.assertEqual(self.host.installed['docker.io'], '26.1.5')
        self.assertFalse(any(command[0] == 'apt-get' for command in self.host.commands))

    def test_unmanaged_destination_data_is_not_adopted(self):
        data = self.host.path('/srv/docker/private-data')
        data.parent.mkdir(parents=True)
        data.write_text('preserve me')
        with self.assertRaisesRegex(baseline.BaselineError, 'Unmanaged /srv/docker'):
            self.apply()
        self.assertEqual(data.read_text(), 'preserve me')

    def test_package_failure_restores_existing_service_policy(self):
        policy = self.host.path('/usr/sbin/policy-rc.d')
        policy.write_text('#!/bin/sh\nexit 42\n')
        policy.chmod(0o751)
        self.host.fail_install = True
        with self.assertRaises(baseline.BaselineError):
            self.apply()
        self.assertEqual(policy.read_text(), '#!/bin/sh\nexit 42\n')
        self.assertEqual(policy.stat().st_mode & 0o777, 0o751)
        self.assertFalse(self.host.path('/usr/sbin/.apex-policy-rc.d').exists())
        self.assertFalse(self.host.masked)

    def test_service_policy_symlink_is_restored_after_failure(self):
        policy = self.host.path('/usr/sbin/policy-rc.d')
        original = self.host.path('/usr/sbin/provider-policy')
        original.write_text('provider policy')
        policy.symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, 'boom'):
            with baseline.service_start_guard():
                self.assertFalse(policy.is_symlink())
                raise RuntimeError('boom')
        self.assertTrue(policy.is_symlink())
        self.assertEqual(original.read_text(), 'provider policy')

    def test_interrupted_guard_is_recovered_before_next_attempt(self):
        state = self.host.path('/usr/sbin/.apex-policy-rc.d')
        state.mkdir()
        (state / 'original').write_text('original policy')
        (state / 'ready').touch()
        self.host.path('/usr/sbin/policy-rc.d').write_text('#!/bin/sh\nexit 101\n')
        with baseline.service_start_guard():
            pass
        self.assertEqual(self.host.path('/usr/sbin/policy-rc.d').read_text(), 'original policy')

    def test_external_policy_change_is_not_overwritten(self):
        policy = self.host.path('/usr/sbin/policy-rc.d')
        with self.assertRaisesRegex(baseline.BaselineError, 'changed outside APEX'):
            with baseline.service_start_guard():
                policy.write_text('external policy')
        self.assertEqual(policy.read_text(), 'external policy')

    def test_baseline_reports_capabilities_and_repeats_without_restarts(self):
        result = self.apply()
        self.assertEqual(result['capabilities']['docker_data_root'], '/srv/docker')
        self.assertEqual(result['capabilities']['compose'], '2.39.2')
        self.assertIn('zfs-kmod-2.3.2', result['capabilities']['zfs'])
        self.assertEqual(result['packages']['zfs-dkms'], '1.0')
        self.assertEqual(self.host.path('/etc/apt/apt.conf.d/99-apex').read_text(), 'APT::Keep-Downloaded-Packages "0";\n')
        self.assertFalse(self.host.path('/usr/sbin/policy-rc.d').exists())
        self.assertFalse(self.host.masked)
        self.assertNotIn('ufw', self.host.active)
        self.assertNotIn('crowdsec-firewall-bouncer', self.host.enabled)
        self.assertFalse(any(command[0] in ('ufw', '/usr/local/bin/ufw-docker') for command in self.host.commands))
        count = len(self.host.commands)
        again = self.apply()
        self.assertEqual(again, result)
        repeated = self.host.commands[count:]
        self.assertFalse(any(command[0] == 'apt-get' or command[:2] in
                             (['systemctl', 'restart'], ['systemctl', 'start']) for command in repeated))

    def bouncer_config(self, key='<API_KEY>'):
        self.host.installed.update({'crowdsec': '1.8.1', 'crowdsec-firewall-bouncer-iptables': '0.0.36'})
        config = self.host.path('/etc/crowdsec/bouncers/crowdsec-firewall-bouncer.yaml')
        config.parent.mkdir(parents=True, exist_ok=True)
        config.write_text(f'# custom settings\napi_url: http://127.0.0.1:9090/\napi_key: {key} # keep comment\nmode: iptables\nblacklists_ipv4: [custom]\n')
        config.chmod(0o640)
        return config

    def test_fresh_crowdsec_is_configured_before_bouncer_install(self):
        self.apply()
        installs = [args for args in self.host.commands if args[0] == 'apt-get' and 'install' in args and '--simulate' not in args]
        crowdsec = next(index for index, args in enumerate(installs) if 'crowdsec' in args)
        bouncer = next(index for index, args in enumerate(installs) if 'crowdsec-firewall-bouncer-iptables' in args)
        self.assertLess(crowdsec, bouncer)
        self.assertFalse(self.host.registrations)
        self.assertFalse(self.host.active & {'crowdsec', 'crowdsec-firewall-bouncer'})

    def test_broken_installed_bouncer_is_repaired_and_repeat_preserves_registration(self):
        config = self.bouncer_config()
        before = config.read_text()
        owner = (config.stat().st_uid, config.stat().st_gid)
        self.apply()
        self.assertEqual(config.read_text(), before.replace('<API_KEY>', self.host.bouncer_key))
        registration = config.with_suffix('.yaml.id').read_text().strip()
        self.assertEqual(self.host.registrations[registration], self.host.bouncer_key)
        self.assertEqual((config.stat().st_uid, config.stat().st_gid), owner)
        self.assertEqual(config.stat().st_mode & 0o777, 0o640)
        count = len(self.host.registrations)
        self.apply()
        self.assertEqual(len(self.host.registrations), count)
        self.assertFalse(self.host.active & {'crowdsec', 'crowdsec-firewall-bouncer'})

    def test_existing_bouncer_key_and_custom_settings_are_preserved(self):
        config = self.bouncer_config('existing-custom-key')
        before = config.read_bytes()
        self.apply()
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(self.host.registrations)

    def test_registration_failure_leaves_placeholder_without_exposing_output(self):
        config = self.bouncer_config()
        before = config.read_bytes()
        self.host.registration_failure = True
        with self.assertRaises(baseline.BaselineError) as failure:
            self.apply()
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(config.with_suffix('.yaml.id').exists())
        self.assertNotIn('sensitive diagnostic', str(failure.exception))

    def test_invalid_registered_key_is_not_published(self):
        for key in ('', '<API_KEY>', 'not a key\nsensitive diagnostic'):
            with self.subTest(key=key):
                config = self.bouncer_config()
                before = config.read_bytes()
                self.host.bouncer_key = key
                with self.assertRaises(baseline.BaselineError):
                    self.apply()
                self.assertEqual(config.read_bytes(), before)
                self.assertFalse(config.with_suffix('.yaml.id').exists())

    def test_bouncer_config_symlink_is_rejected_before_registration(self):
        config = self.bouncer_config()
        destination = config.with_suffix('.provider')
        config.rename(destination)
        config.symlink_to(destination)
        with self.assertRaises(baseline.BaselineError):
            self.apply()
        self.assertIn('<API_KEY>', destination.read_text())
        self.assertFalse(self.host.registrations)

    def test_quoted_null_is_an_existing_key_not_an_uninitialized_yaml_value(self):
        config = self.bouncer_config('"null"')
        before = config.read_bytes()
        self.apply()
        self.assertEqual(config.read_bytes(), before)
        self.assertFalse(self.host.registrations)

    def test_package_placeholder_variants_are_repaired(self):
        for placeholder in ('"<API_KEY>"', "'$API_KEY'", '${API_KEY}', 'null', '~', '', '""'):
            with self.subTest(placeholder=placeholder):
                config = self.bouncer_config(placeholder)
                self.apply()
                self.assertIn('api_key: ' + self.host.bouncer_key, config.read_text())

    def test_ambiguous_or_missing_api_key_is_not_mutated(self):
        for content in ('api_key: <API_KEY>\n"api_key": existing-key\n', 'api_url: http://localhost:8080/\n',
                        'api_key: "<API_KEY>x\n'):
            with self.subTest(content=content):
                config = self.bouncer_config()
                config.write_text(content)
                with self.assertRaises(baseline.BaselineError):
                    self.apply()
                self.assertEqual(config.read_text(), content)
                self.assertFalse(self.host.registrations)

    def test_multiline_custom_credentials_are_preserved_without_registration(self):
        for credential in ('\n  existing-custom-credential', ' # comment\n\n  existing-custom-credential',
                           ' <API_KEY>\n  existing-custom-credential', '\n  # comment\n  existing-custom-credential'):
            with self.subTest(credential=credential):
                config = self.bouncer_config()
                config.write_text('api_key:' + credential + '\napi_url: http://localhost:9090/\n')
                identity = config.with_suffix('.yaml.id')
                identity.write_text('existing-registration\n')
                before = config.read_bytes()
                with self.assertRaises(baseline.BaselineError):
                    self.apply()
                self.assertEqual(config.read_bytes(), before)
                self.assertEqual(identity.read_text(), 'existing-registration\n')
                self.assertFalse(self.host.registrations)

    @unittest.skipUnless(importlib.util.find_spec('yaml'), 'target-only python3-yaml is unavailable')
    def test_empty_credential_replacement_parses_as_the_generated_key(self):
        import yaml
        for entry in ('api_key:', 'api_key: # comment', 'api_key:    ',
                      'api_key:\n  # next-line comment'):
            with self.subTest(entry=entry):
                config = self.bouncer_config()
                config.write_text(entry + '\napi_url: http://localhost:9090/\n')
                self.assertIsNone(yaml.safe_load(config.read_text())['api_key'])
                self.apply()
                parsed = yaml.safe_load(config.read_text())
                self.assertEqual(parsed['api_key'], self.host.bouncer_key)
                self.assertEqual(parsed['api_url'], 'http://localhost:9090/')
                if '# ' in entry:
                    self.assertIn(entry[entry.index('# '):], config.read_text())

    def test_redirected_bookkeeping_is_rejected_before_registration(self):
        config = self.bouncer_config()
        destination = config.with_suffix('.provider')
        destination.write_text('provider-data')
        config.with_suffix('.yaml.id').symlink_to(destination)
        with self.assertRaises(baseline.BaselineError):
            self.apply()
        self.assertEqual(destination.read_text(), 'provider-data')
        self.assertFalse(self.host.registrations)

    def test_interrupted_credential_publication_can_retry_without_reusing_wrong_identity(self):
        config = self.bouncer_config()
        replace = baseline.os.replace
        def interrupt_config(source, destination):
            if destination == config:
                raise OSError('interrupted publication')
            return replace(source, destination)
        with patch.object(baseline.os, 'replace', side_effect=interrupt_config):
            with self.assertRaises(baseline.BaselineError):
                self.apply()
        self.assertIn('<API_KEY>', config.read_text())
        orphan = config.with_suffix('.yaml.id').read_text().strip()
        self.assertIn(orphan, self.host.registrations)
        self.apply()
        current = config.with_suffix('.yaml.id').read_text().strip()
        self.assertNotEqual(orphan, current)
        self.assertIn(self.host.registrations[current], config.read_text())
        self.assertFalse(list(config.parent.glob('.apex-*')))

    def test_zfs_missing_for_running_kernel_is_an_explicit_incomplete_result(self):
        self.host.zfs_available = False
        with self.assertRaisesRegex(baseline.BaselineError, 'reboot.*retry'):
            self.apply()

    def test_narrow_base_policy_preserves_scope_and_shared_configuration(self):
        with patch.object(baseline, 'preflight'):
            result = baseline.apply_base_policy()
        self.assertEqual(set(result['packages']), {'curl', 'wget', 'git', 'rsyslog', 'ufw'})
        self.assertFalse(self.host.path('/etc/docker/daemon.json').exists())
        self.assertEqual(self.host.path('/etc/systemd/journald.conf.d/99-apex.conf').read_text(),
                         '[Journal]\nSystemMaxUse=200M\n')
        self.assertIn(['/usr/local/bin/ufw-docker', 'install'], self.host.commands)

    def test_unverified_download_is_not_installed_or_executed(self):
        with patch.object(baseline.urllib.request, 'urlopen', return_value=io.BytesIO(b'tampered')):
            with self.assertRaisesRegex(baseline.BaselineError, 'checksum mismatch'):
                baseline.configure_common()
        self.assertFalse(self.host.path('/usr/local/bin/ufw-docker').exists())
        self.assertFalse(any(command[0] == '/usr/local/bin/ufw-docker' for command in self.host.commands))

    def test_recovery_does_not_remove_preexisting_service_masks(self):
        self.host.masked.add('ufw')
        with baseline.security_service_guard(['ufw']):
            self.host.installed['ufw'] = '0.36'
        self.assertIn('ufw', self.host.masked)

    def test_package_owned_etc_service_file_can_be_unpacked_under_guard(self):
        unit = self.host.path('/etc/systemd/system/crowdsec-firewall-bouncer.service')
        with baseline.security_service_guard(['crowdsec-firewall-bouncer-iptables']):
            self.assertFalse(unit.is_symlink(), 'dpkg must be able to install its own conffile')
            unit.parent.mkdir(parents=True, exist_ok=True)
            unit.write_text('[Service]\nExecStart=/usr/bin/crowdsec-firewall-bouncer\n')
            self.host.installed['crowdsec-firewall-bouncer-iptables'] = '0.0.36'
            self.host.enabled.add('crowdsec-firewall-bouncer')
            baseline.run(['systemctl', 'start', 'crowdsec-firewall-bouncer'])
            self.assertNotIn('crowdsec-firewall-bouncer', self.host.active)
        self.assertTrue(unit.is_file())
        self.assertIn('ExecStart=/usr/bin/crowdsec-firewall-bouncer', unit.read_text())
        self.assertNotIn('crowdsec-firewall-bouncer', self.host.enabled)
        baseline.run(['systemctl', 'start', 'crowdsec-firewall-bouncer'])
        self.assertIn('crowdsec-firewall-bouncer', self.host.active)

    def test_disable_failure_retains_guard_and_retry_recovers_it(self):
        original_command = self.host.command
        def fail_disable(args, **kwargs):
            if args == ['systemctl', 'disable', 'crowdsec-firewall-bouncer']:
                return subprocess.CompletedProcess(args, 1, '', '')
            return original_command(args, **kwargs)
        with patch.object(baseline.subprocess, 'run', side_effect=fail_disable):
            with self.assertRaises(baseline.BaselineError):
                with baseline.security_service_guard(['crowdsec-firewall-bouncer-iptables']):
                    self.host.installed['crowdsec-firewall-bouncer-iptables'] = '0.0.36'
                    self.host.enabled.add('crowdsec-firewall-bouncer')
        baseline.run(['systemctl', 'start', 'crowdsec-firewall-bouncer'])
        self.assertNotIn('crowdsec-firewall-bouncer', self.host.active)
        with baseline.security_service_guard(['crowdsec-firewall-bouncer-iptables']):
            pass
        self.assertNotIn('crowdsec-firewall-bouncer', self.host.enabled)
        baseline.run(['systemctl', 'start', 'crowdsec-firewall-bouncer'])
        self.assertIn('crowdsec-firewall-bouncer', self.host.active)

    def test_generic_package_failure_does_not_suggest_zfs_reboot(self):
        original_command = self.host.command
        def fail_security_packages(args, **kwargs):
            if args[0] == 'apt-get' and 'crowdsec-firewall-bouncer-iptables' in args and '--simulate' not in args:
                return subprocess.CompletedProcess(args, 100, '', 'secret source diagnostic')
            return original_command(args, **kwargs)
        with patch.object(baseline.subprocess, 'run', side_effect=fail_security_packages):
            with self.assertRaises(baseline.BaselineError) as failure:
                self.apply()
        self.assertNotIn('reboot', str(failure.exception))
        self.assertNotIn('secret source diagnostic', str(failure.exception))

    def test_unsupported_os_fails_before_package_commands(self):
        self.host.path('/etc/os-release').write_text('ID=ubuntu\nVERSION_ID=24.04\n')
        with patch.object(baseline.os, 'geteuid', return_value=0):
            with self.assertRaisesRegex(baseline.BaselineError, 'Debian 13 amd64'):
                baseline.apply_baseline()
        self.assertEqual(self.host.commands, [])

    def test_non_root_fails_before_any_command(self):
        with patch.object(baseline.os, 'geteuid', return_value=1000):
            with self.assertRaisesRegex(baseline.BaselineError, 'root'):
                baseline.apply_baseline()
        self.assertEqual(self.host.commands, [])

    def test_unsupported_architecture_fails_before_apt_mutation(self):
        with patch.object(baseline.os, 'geteuid', return_value=0), patch.object(
                baseline.subprocess, 'run', return_value=subprocess.CompletedProcess([], 0, 'arm64\n', '')) as command:
            with self.assertRaisesRegex(baseline.BaselineError, 'Debian 13 amd64'):
                baseline.apply_baseline()
        self.assertEqual([call.args[0] for call in command.call_args_list], [['dpkg', '--print-architecture']])


if __name__ == '__main__':
    unittest.main()
