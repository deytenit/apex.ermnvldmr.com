from pathlib import Path
import subprocess
import tempfile
import unittest
from unittest.mock import patch

from provisioning import docker


class Host:
    def __init__(self, root):
        self.root = root
        self.commands = []
        self.installed = {}
        self.partial = {}
        self.plan = ''
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
        elif args[0] == 'apt-get' and 'install' in args:
            if '--simulate' in args:
                output = self.plan
            else:
                for name in args[args.index('--no-remove') + 1:]:
                    self.installed[name] = '1.0'
                    self.partial.pop(name, None)
        return subprocess.CompletedProcess(args, code, output, '')


class DockerPackageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.host = Host(Path(self.temp.name).resolve())
        for item in (
            patch.object(docker, 'Path', self.host.path),
            patch.object(docker, 'POLICY', self.host.path('/usr/sbin/policy-rc.d')),
            patch.object(docker, 'POLICY_STATE', self.host.path('/usr/sbin/.apex-policy-rc.d')),
            patch.object(docker.subprocess, 'run', side_effect=self.host.command),
        ):
            item.start()
            self.addCleanup(item.stop)

    def test_package_database_drives_installation_and_repeat_does_not_upgrade(self):
        self.host.installed['curl'] = '7.0'
        self.host.partial['rsyslog'] = 'deinstall ok config-files'
        self.assertEqual(docker.install_missing(['curl', 'rsyslog']), ['rsyslog'])
        self.assertEqual(self.host.installed['curl'], '7.0')
        self.assertEqual(self.host.installed['rsyslog'], '1.0')
        count = len(self.host.commands)
        self.assertEqual(docker.install_missing(['curl', 'rsyslog']), [])
        self.assertFalse(any(command[0] == 'apt-get' for command in self.host.commands[count:]))

    def test_dependency_upgrade_is_rejected_before_install(self):
        self.host.plan = 'Inst libc6 [2.0] (3.0 Debian:13)\n'
        with self.assertRaisesRegex(docker.DockerError, 'upgrade/removal'):
            docker.install_missing(['git'])
        self.assertNotIn('git', self.host.installed)

    def test_held_installed_packages_are_preserved(self):
        self.host.partial['git'] = 'hold ok installed'
        self.assertEqual(docker.install_missing(['git']), [])
        self.assertFalse(any(command[0] == 'apt-get' for command in self.host.commands))

    def test_conflicting_docker_config_is_rejected_without_host_mutation(self):
        path = self.host.path('/etc/docker/daemon.json')
        path.parent.mkdir(parents=True)
        path.write_text('{"data-root": "/data/docker"}')
        before = sorted(str(path.relative_to(self.host.root)) for path in self.host.root.rglob('*'))
        with self.assertRaisesRegex(docker.DockerError, 'Conflicting Docker'):
            docker.docker_preflight(self.host.installed)
        self.assertEqual(before, sorted(str(path.relative_to(self.host.root)) for path in self.host.root.rglob('*')))
        self.assertFalse(any(command[0] in ('apt-get', 'systemctl') for command in self.host.commands))

    def test_existing_docker_data_is_preserved(self):
        data = self.host.path('/var/lib/docker/volumes/user-data')
        data.parent.mkdir(parents=True)
        data.write_text('valuable')
        with self.assertRaisesRegex(docker.DockerError, 'Existing /var/lib/docker'):
            docker.docker_preflight(self.host.installed)
        self.assertEqual(data.read_text(), 'valuable')

    def test_conflicting_distribution_docker_package_is_never_removed(self):
        self.host.installed['docker.io'] = '26.1.5'
        with self.assertRaisesRegex(docker.DockerError, 'Conflicting Docker packages'):
            docker.docker_preflight(self.host.installed)
        self.assertEqual(self.host.installed['docker.io'], '26.1.5')
        self.assertFalse(any(command[0] == 'apt-get' for command in self.host.commands))

    def test_unmanaged_destination_data_is_not_adopted(self):
        data = self.host.path('/srv/docker/private-data')
        data.parent.mkdir(parents=True)
        data.write_text('preserve me')
        with self.assertRaisesRegex(docker.DockerError, 'Unmanaged /srv/docker'):
            docker.docker_preflight(self.host.installed)
        self.assertEqual(data.read_text(), 'preserve me')

    def test_service_policy_symlink_is_restored_after_failure(self):
        policy = self.host.path('/usr/sbin/policy-rc.d')
        original = self.host.path('/usr/sbin/provider-policy')
        original.write_text('provider policy')
        policy.symlink_to(original)
        with self.assertRaisesRegex(RuntimeError, 'boom'):
            with docker.package_policy_lock(), docker.service_start_guard():
                self.assertFalse(policy.is_symlink())
                raise RuntimeError('boom')
        self.assertTrue(policy.is_symlink())
        self.assertEqual(original.read_text(), 'provider policy')

    def test_unfinished_backup_is_discarded_without_replacing_live_policy(self):
        docker.POLICY_STATE.mkdir()
        (docker.POLICY_STATE / 'original').write_text('partial')
        docker.POLICY.write_text('complete provider policy')
        with docker.package_policy_lock(), docker.service_start_guard():
            pass
        self.assertEqual(docker.POLICY.read_text(), 'complete provider policy')

    def test_interrupted_guard_is_recovered_before_next_attempt(self):
        state = self.host.path('/usr/sbin/.apex-policy-rc.d')
        state.mkdir()
        (state / 'original').write_text('original policy')
        (state / 'ready').touch()
        self.host.path('/usr/sbin/policy-rc.d').write_text('#!/bin/sh\nexit 101\n')
        with docker.package_policy_lock(), docker.service_start_guard():
            pass
        self.assertEqual(self.host.path('/usr/sbin/policy-rc.d').read_text(), 'original policy')

    def test_external_policy_change_is_not_overwritten(self):
        policy = self.host.path('/usr/sbin/policy-rc.d')
        with self.assertRaisesRegex(docker.DockerError, 'changed outside APEX'):
            with docker.package_policy_lock(), docker.service_start_guard():
                policy.write_text('external policy')
        self.assertEqual(policy.read_text(), 'external policy')
