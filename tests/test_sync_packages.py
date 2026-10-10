import json
import subprocess
import tempfile
from pathlib import Path
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

from actions.sync import packages


def digest(char):
    return 'sha256:' + char * 64


MANIFEST = 'application/vnd.oci.image.manifest.v1+json'
INDEX = 'application/vnd.oci.image.index.v1+json'


class Commands:
    def __init__(self):
        self.containers = [{'Image': digest('a'), 'Reference': 'alpine:3.21'}]
        self.local = {digest('a'): {'Id': digest('a'), 'Os': 'linux',
                                  'Architecture': 'amd64', 'RepoDigests': ['alpine@' + digest('b')]}}
        self.remote = {'alpine:3.21': {'mediaType': MANIFEST,
                                     'digest': digest('c')}}
        self.raw = {'alpine@' + digest('c'): {'schemaVersion': 2, 'mediaType': MANIFEST,
                                            'config': {'digest': digest('a')}, 'layers': []}}
        self.config = {'alpine@' + digest('c'): {'os': 'linux', 'architecture': 'amd64'}}
        self.calls = []
        self.fail = set()
        self.apt = ''
        self.compose = {"name": "apex", "services": {}}
        self.compose_options = []

    def ok(self, cmd):
        self.calls.append(cmd)
        return tuple(cmd) not in self.fail

    def run(self, cmd, check=False, capture=False, **kwargs):
        self.calls.append(cmd)
        if cmd[:1] == ['timeout']:
            cmd = cmd[4:]
        if tuple(cmd) in self.fail:
            return subprocess.CompletedProcess(cmd, 1, '', 'registry unavailable')
        if cmd[:2] == ['docker', 'compose']:
            self.compose_options.append(kwargs)
            data = self.compose if isinstance(self.compose, str) else json.dumps(self.compose)
        elif cmd[:2] == ['bash', '-c']:
            if 'apt-get --just-print' not in cmd[2]:
                raise AssertionError('Image references must never reach a shell')
            data = self.apt
        elif cmd == ['docker', 'ps', '--quiet', '--no-trunc']:
            data = '\n'.join(str(i) for i in range(len(self.containers)))
        elif cmd[:3] == ['docker', 'container', 'inspect']:
            data = '\n'.join(json.dumps(c) for c in self.containers)
        elif cmd[:3] == ['docker', 'image', 'inspect']:
            data = json.dumps([self.local[cmd[3]]])
        elif cmd == ['docker', 'buildx', 'version']:
            data = 'github.com/docker/buildx v0.37.1'
        elif cmd[:4] == ['docker', 'buildx', 'imagetools', 'inspect']:
            table = self.raw if cmd[4] == '--raw' else (
                self.remote if cmd[5] == '{{json .Manifest}}' else self.config)
            value = table[cmd[-1]]
            data = value if isinstance(value, str) else json.dumps(value)
        else:
            raise AssertionError(f'Unexpected command: {cmd}')
        return subprocess.CompletedProcess(cmd, 0, data, '')


class TestSyncPackages(unittest.TestCase):
    def setUp(self):
        self.commands = Commands()
        self.ctx = SimpleNamespace(sys=self.commands, log=Mock(), node=SimpleNamespace(name='node'),
                                   notify=Mock())
        self.ctx.notify.info.return_value = True
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.ctx.paths = SimpleNamespace(compositions=self.tmp.name)
        self.ctx.vars = lambda: {'APEX_NODE_HOST': 'test-node'}
        self.args = SimpleNamespace(telegram_bot_url='test-notifier')

    def report(self):
        with patch.object(packages.os.path, 'isfile', return_value=True), \
                patch.object(packages.shutil, 'which', return_value='/usr/bin/docker'):
            packages.run(self.ctx, self.args)
        return self.ctx.notify.info.call_args.args[2]

    def containerd(self, manifest='c'):
        local = self.commands.local.pop(digest('a'))
        local.update(Id=digest('b'), Descriptor={'mediaType': INDEX, 'digest': digest('b')})
        self.commands.local[digest('b')] = local
        self.commands.containers[0].update(Image=digest('b'), ImageManifestDescriptor={
            'mediaType': MANIFEST, 'digest': digest(manifest),
            'platform': {'os': 'linux', 'architecture': 'amd64'}})

    def test_containerd_index_id_compares_actual_platform_manifest(self):
        self.containerd()
        self.index()
        self.assertIn('1 current', self.report())

    def test_containerd_single_manifest_is_current_despite_different_config_digest(self):
        self.containerd()
        self.assertIn('1 current', self.report())

    def test_containerd_changed_platform_manifest_is_update(self):
        self.containerd('d')
        self.index()
        self.assertIn('1 updates', self.report())

    def test_containerd_without_platform_descriptor_is_unchecked(self):
        self.containerd()
        del self.commands.containers[0]['ImageManifestDescriptor']
        self.assertIn('1 unchecked', self.report())

    def test_two_containerd_platforms_of_one_index_are_checked_separately(self):
        self.containerd()
        self.index()
        self.commands.containers.append({
            'Image': digest('b'), 'Reference': 'alpine:3.21', 'ImageManifestDescriptor': {
                'mediaType': MANIFEST, 'digest': digest('d'),
                'platform': {'os': 'linux', 'architecture': 'arm64'}}})
        self.commands.raw['alpine@' + digest('d')] = {
            'schemaVersion': 2, 'mediaType': MANIFEST, 'config': {'digest': digest('f')}}
        self.assertIn('2 current', self.report())

    def test_invalid_container_platform_descriptor_is_unchecked(self):
        self.containerd()
        self.commands.containers[0]['ImageManifestDescriptor']['digest'] = 'invalid'
        self.assertIn('1 unchecked', self.report())

    def build_service(self, image='alpine:3.21'):
        core = Path(self.tmp.name) / 'apex'
        core.mkdir()
        for filename in ('docker-compose.yml', '.env', 'apex.env'):
            (core / filename).touch()
        self.commands.compose = {'name': 'apex', 'services': {
            'custom': {'build': {'context': '.'}, 'environment': {'TOKEN': 'private-test-value'}}}}
        if image is not None:
            self.commands.compose['services']['custom']['image'] = image
        self.commands.containers[0]['Labels'] = {
            'com.docker.compose.project': 'apex', 'com.docker.compose.service': 'custom'}

    def test_declared_compose_build_with_synthetic_repo_digest_is_local(self):
        self.containerd()
        self.build_service()
        self.assertIn('1 local/untracked', self.report())
        self.assertFalse(any('imagetools' in c for c in self.commands.calls))
        self.assertIn(['timeout', '--signal=TERM', '--kill-after=5s', '30s', 'docker',
                       'compose', '--env-file', '.env', '--env-file', 'apex.env',
                       'config', '--format', 'json'], self.commands.calls)
        self.assertEqual(self.commands.compose_options[0]['env']['APEX_NODE_HOST'], 'test-node')
        self.assertNotIn('private-test-value', str(self.ctx.log.mock_calls))

    def test_implicit_compose_build_image_uses_service_provenance(self):
        self.containerd()
        self.build_service(image=None)
        self.assertIn('1 local/untracked', self.report())

    def test_changed_build_declaration_does_not_hide_old_registry_image(self):
        self.build_service(image='different:latest')
        self.assertIn('1 current', self.report())

    def test_same_image_name_without_build_service_provenance_is_registry_checked(self):
        self.build_service()
        self.commands.containers[0]['Labels']['com.docker.compose.service'] = 'other'
        self.assertIn('1 current', self.report())

    def test_failed_compose_resolution_is_unchecked_without_secret_output(self):
        self.build_service()
        self.commands.compose = 'private-test-value'
        self.assertIn('Images: unchecked (Compose inspection failed)', self.report())
        self.assertNotIn('private-test-value', str(self.ctx.log.mock_calls))

    def test_matching_config_digest_is_current_without_comparing_index_digest(self):
        self.assertIn('1 current', self.report())
        self.assertIn(['docker', 'image', 'inspect', digest('a')], self.commands.calls)

    def test_moved_tag_compares_running_image_not_new_local_tag(self):
        self.commands.raw['alpine@' + digest('c')]['config']['digest'] = digest('d')
        self.assertIn('1 updates', self.report())
        self.assertNotIn(['docker', 'image', 'inspect', 'alpine:3.21'], self.commands.calls)

    def index(self, variants=None):
        self.commands.remote['alpine:3.21'] = {'schemaVersion': 2, 'mediaType': INDEX,
            'digest': digest('e'), 'manifests': variants or [
                {'mediaType': MANIFEST, 'digest': digest('d'),
                 'platform': {'os': 'linux', 'architecture': 'arm64'}},
                {'mediaType': MANIFEST, 'digest': digest('c'),
                 'platform': {'os': 'linux', 'architecture': 'amd64'}},
                {'mediaType': MANIFEST, 'digest': digest('f'),
                 'platform': {'os': 'unknown', 'architecture': 'unknown'}}]}

    def test_index_only_other_architecture_changed_is_current(self):
        self.index()
        self.assertIn('1 current', self.report())
        self.assertFalse(any(c[-1] == 'alpine@' + digest('d') for c in self.commands.calls))

    def test_index_matching_platform_changed_is_update(self):
        self.index()
        self.commands.raw['alpine@' + digest('c')]['config']['digest'] = digest('d')
        self.assertIn('1 updates', self.report())

    def test_ambiguous_or_missing_platform_is_unchecked(self):
        for platforms in ([{'os': 'linux', 'architecture': 'arm64'}],
                          [{'os': 'linux', 'architecture': 'amd64'}] * 2):
            with self.subTest(platforms=platforms):
                self.index([{'mediaType': MANIFEST, 'digest': digest('c'), 'platform': p}
                            for p in platforms])
                self.assertIn('1 unchecked', self.report())

    def test_arm64_default_variant_matches_v8(self):
        self.commands.local[digest('a')]['Architecture'] = 'arm64'
        self.index([{'mediaType': MANIFEST, 'digest': digest('c'),
                     'platform': {'os': 'linux', 'architecture': 'arm64', 'variant': 'v8'}}])
        self.assertIn('1 current', self.report())

    def test_single_manifest_different_platform_is_unchecked(self):
        self.commands.raw['alpine@' + digest('c')]['config']['digest'] = digest('d')
        self.commands.config['alpine@' + digest('c')]['architecture'] = 'arm64'
        self.assertIn('1 unchecked', self.report())

    def test_registry_port_and_tag_are_preserved(self):
        ref = 'registry.example:5000/team/app:stable'
        self.commands.containers[0]['Reference'] = ref
        self.commands.remote[ref] = self.commands.remote['alpine:3.21']
        self.commands.raw['registry.example:5000/team/app@' + digest('c')] = self.commands.raw['alpine@' + digest('c')]
        self.assertIn('1 current', self.report())
        self.assertIn(['timeout', '--signal=TERM', '--kill-after=5s', '30s',
                       'docker', 'buildx', 'imagetools', 'inspect', '--raw',
                       'registry.example:5000/team/app@' + digest('c')], self.commands.calls)

    def test_registry_port_without_tag_uses_the_whole_repository(self):
        ref = 'localhost:5000/team/app'
        self.commands.containers[0]['Reference'] = ref
        self.commands.remote[ref] = self.commands.remote['alpine:3.21']
        self.commands.raw[ref + '@' + digest('c')] = self.commands.raw['alpine@' + digest('c')]
        self.assertIn('1 current', self.report())

    def test_two_running_versions_of_one_tag_are_both_checked(self):
        self.commands.containers.append({'Image': digest('d'), 'Reference': 'alpine:3.21'})
        self.commands.local[digest('d')] = {**self.commands.local[digest('a')], 'Id': digest('d')}
        message = self.report()
        self.assertIn('1 current', message)
        self.assertIn('1 updates', message)

    def test_malformed_local_image_is_unchecked_not_a_local_build(self):
        del self.commands.local[digest('a')]['RepoDigests']
        self.assertIn('1 unchecked', self.report())

    def test_untrusted_reference_is_argv_not_shell_input(self):
        self.commands.containers[0]['Reference'] = "registry.example/app:$(touch /tmp/nope)"
        self.assertIn('1 unchecked', self.report())
        self.assertTrue(all('touch' not in c[-1] for c in self.commands.calls if c[0] == 'bash'))

    def test_apt_failure_is_explicit_and_images_still_checked(self):
        self.commands.fail.add(('sudo', 'apt-get', 'update'))
        message = self.report()
        self.assertIn('APT: failed to check', message)
        self.assertIn('1 current', message)

    def test_pinned_and_local_images_are_explicit_and_not_queried(self):
        self.commands.containers = [{'Image': digest('a'), 'Reference': 'alpine@' + digest('b')},
                                    {'Image': digest('d'), 'Reference': 'project-local:latest'}]
        self.commands.local[digest('d')] = {**self.commands.local[digest('a')],
                                          'Id': digest('d'), 'RepoDigests': []}
        message = self.report()
        self.assertIn('1 pinned', message)
        self.assertIn('1 local/untracked', message)
        self.assertFalse(any('imagetools' in c for c in self.commands.calls))

    def test_registry_timeout_is_bounded_and_reported_unchecked(self):
        original = self.commands.run

        def time_out(cmd, **kwargs):
            if 'imagetools' in cmd:
                self.assertEqual(cmd[:4], ['timeout', '--signal=TERM', '--kill-after=5s', '30s'])
                return subprocess.CompletedProcess(cmd, 124, '', '')
            return original(cmd, **kwargs)

        self.commands.run = time_out
        self.assertIn('1 unchecked', self.report())

    def test_failed_registry_and_malformed_results_are_unchecked(self):
        for value in ('not-json', '{}', '[]', '{"mediaType":"unknown"}'):
            with self.subTest(value=value):
                self.commands.remote['alpine:3.21'] = value
                self.assertIn('1 unchecked', self.report())
        self.commands.fail.add(('docker', 'buildx', 'imagetools', 'inspect', '--format',
                                '{{json .Manifest}}', 'alpine:3.21'))
        self.assertIn('1 unchecked', self.report())

    def test_bad_config_digest_is_unchecked(self):
        self.commands.raw['alpine@' + digest('c')]['config'] = {}
        self.assertIn('1 unchecked', self.report())

    def test_docker_listing_failure_is_not_empty_success(self):
        self.commands.fail.add(('docker', 'ps', '--quiet', '--no-trunc'))
        self.assertIn('unchecked', self.report())

    def test_buildx_missing_still_reports_unchecked(self):
        self.commands.fail.add(('docker', 'buildx', 'version'))
        self.assertIn('1 unchecked', self.report())

    def test_image_counts_precede_long_package_details_and_notification_failure_exits(self):
        self.commands.apt = '\n'.join('package-' + str(i) for i in range(100))
        message = self.report()
        self.assertIn('Images:', message[:200])
        self.assertIn('1 current', message[:200])
        self.assertIn('100 pending', message[:200])
        self.assertNotIn('Skopeo', message)
        self.ctx.notify.info.return_value = False
        with self.assertRaises(SystemExit):
            self.report()


if __name__ == '__main__':
    unittest.main()
