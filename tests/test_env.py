import contextlib
import io
import json
import os
import shutil
import stat
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from actions import env
from engine.descriptor import build_parser
from engine.lib.host import Host


class TestEnv(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.core = Path(self.tmpdir.name) / 'compositions' / 'apex'
        self.core.mkdir(parents=True)
        self.ctx = MagicMock()
        self.ctx.paths.compositions = str(self.core.parent)
        self.ctx.vars.return_value = {
            'APEX_SUBNET': '198.18.16.0/24',
            'APEX_NODE_HOST': 'test-host',
            'OTHER_VAR': 'ignore_me',
        }
        self.ctx.host = Host(self.ctx.log, self.ctx.sys)

    def run_env(self, *argv):
        args = build_parser(env.METADATA, 'apex env').parse_args(argv)
        with contextlib.redirect_stdout(io.StringIO()) as out:
            env.run(self.ctx, args)
        return out.getvalue()

    def test_shell_exports_round_trip_literal_values(self):
        value = "a$b # 'quote' \"double\" \\backslash\r\nnext"
        self.ctx.vars.return_value['APEX_NODE_HOST'] = value
        exports = self.run_env('--export')
        self.assertEqual(self.run_env(), exports)
        script = exports + '\nprintf %s "$APEX_NODE_HOST"'
        result = subprocess.run(['/bin/sh', '-c', script], capture_output=True, check=True,
                                timeout=30, env={})
        self.assertEqual(result.stdout.decode(), value)
        self.assertNotIn('OTHER_VAR', script)

    def test_dotenv_does_not_disclose_other_values_or_existing_secrets(self):
        (self.core / '.env').write_text('SECRET=synthetic-private\n')
        output = self.run_env('--dotenv')
        self.assertIn('APEX_NODE_HOST=', output)
        self.assertNotIn('export ', output)
        self.assertNotIn('SECRET', output)
        self.assertNotIn('OTHER_VAR', output)

    def test_repeat_sync_preserves_credentials_bytes_and_updates_managed_values(self):
        original = b'# user credentials\r\nTOKEN=\'a$#"\\literal\'\r\nUID=1000\r\nGID=1000'
        target = self.core / '.env'
        target.write_bytes(original)
        target.chmod(0o600)
        (self.core / 'apex.env').write_bytes(b'COMPOSE_PROFILES=core\r\nPORT=1234\r\n')
        self.run_env('--sync')
        first = target.read_bytes()
        self.assertTrue(first.startswith(original + b'\n'))
        self.assertIn(b'COMPOSE_PROFILES=core\r\nPORT=1234\r\n', first)
        self.run_env('--sync')
        self.assertEqual(first, target.read_bytes())
        self.ctx.vars.return_value['APEX_NODE_HOST'] = 'new-host'
        self.run_env('--sync')
        self.assertTrue(target.read_bytes().startswith(original + b'\n'))
        self.assertNotIn(b'test-host', target.read_bytes())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_sync_preserves_symlink_and_private_target(self):
        target = Path(self.tmpdir.name) / 'private.env'
        target.write_text('TOKEN=synthetic\n')
        link = self.core / '.env'
        link.symlink_to(target)
        self.run_env('--sync')
        self.assertTrue(link.is_symlink())
        self.assertIn('TOKEN=synthetic', target.read_text())
        self.assertIn('APEX_NODE_HOST=', target.read_text())
        self.assertEqual(stat.S_IMODE(target.stat().st_mode), 0o600)

    def test_write_basename_is_standalone_and_private(self):
        previous = os.getcwd()
        self.addCleanup(os.chdir, previous)
        os.chdir(self.tmpdir.name)
        Path('generated.env').write_text('OLD=removed\n')
        self.run_env('--write', 'generated.env')
        content = Path('generated.env').read_text()
        self.assertIn('APEX_NODE_HOST=', content)
        self.assertNotIn('OLD=', content)
        self.assertNotIn('BEGIN', content)
        self.assertEqual(stat.S_IMODE(Path('generated.env').stat().st_mode), 0o600)

    def test_incompatible_modes_fail_without_writing(self):
        for argv in [('--sync', '--dotenv'), ('--sync', '--write', 'file'),
                     ('--export', '--dotenv'), ('--export', '--write', 'file')]:
            with self.subTest(argv=argv):
                with self.assertRaises(SystemExit) as error:
                    self.run_env(*argv)
                self.assertEqual(error.exception.code, 64)
        self.assertFalse((self.core / '.env').exists())

    def test_uncertain_managed_markers_fail_without_changing_file(self):
        start = '# BEGIN APEX MANAGED ENV'
        end = '# END APEX MANAGED ENV'
        for content in [start+'\n', end+'\n', end+'\n'+start+'\n',
                        start+'\n'+start+'\n'+end+'\n',
                        start+'\n'+end+'\n'+start+'\n'+end+'\n',
                        'TOKEN="'+start+'"\n']:
            target = self.core / '.env'
            target.write_text(content)
            with self.subTest(content=content):
                with self.assertRaises(SystemExit) as error:
                    self.run_env('--sync')
                self.assertEqual(error.exception.code, 65)
                self.assertEqual(target.read_text(), content)

    def test_source_marker_collision_fails_without_writing(self):
        (self.core / 'apex.env').write_text('# BEGIN APEX MANAGED ENV\n')
        with self.assertRaises(SystemExit) as error:
            self.run_env('--sync')
        self.assertEqual(error.exception.code, 65)
        self.assertFalse((self.core / '.env').exists())
        (self.core / 'apex.env').unlink()
        self.ctx.vars.return_value['APEX_NODE_HOST'] = '# END APEX MANAGED ENV'
        with self.assertRaises(SystemExit) as error:
            self.run_env('--sync')
        self.assertEqual(error.exception.code, 65)
        self.assertFalse((self.core / '.env').exists())

    def test_bare_compose_interprets_literals_precedence_and_profiles(self):
        binary = shutil.which('docker-compose')
        if binary is None:
            self.skipTest('Standalone docker-compose required for daemon-free config check')
        values = {
            'APEX_LITERAL': "a$b ${UNSET} $$ # 'quote' \"double\" \\backslash",
            'APEX_TRAILING': 'last\\',
            'APEX_LINES': 'one\r\ntwo\nthree\tfour',
            'APEX_EMPTY': '',
            'APEX_ESCAPES': "\\n \\\\ \\' \\\"",
            'APEX_NODE_HOST': 'synthesized',
        }
        self.ctx.vars.return_value = values
        (self.core / '.env').write_text('UID=1000\nGID=1001\nPORT=1\nAPEX_NODE_HOST=old\n'
                                       "TOKEN='literal$token#hash'\n")
        (self.core / 'apex.env').write_text('PORT=2\nAPEX_NODE_HOST=public\n'
                                           'COMPOSE_PROFILES=chosen\n')
        keys = [*values, 'UID', 'GID', 'PORT', 'TOKEN']
        (self.core / 'docker-compose.yml').write_text(
            'services:\n  probe:\n    image: busybox\n    profiles: [chosen]\n'
            '    environment:\n' + ''.join(f'      {key}: ${{{key}}}\n' for key in keys))
        self.run_env('--sync')
        result = subprocess.run([binary, 'config', '--format', 'json'], cwd=self.core,
                                capture_output=True, check=True, timeout=30, env={})
        actual = json.loads(result.stdout)['services']['probe']['environment']
        expected = {**values, 'UID': '1000', 'GID': '1001', 'PORT': '2',
                    'TOKEN': 'literal$token#hash'}
        # Compose re-escapes literal dollars when serializing a reusable configuration.
        self.assertEqual(actual, {k: v.replace('$', '$$') for k, v in expected.items()})
        result = subprocess.run([binary, 'config', '--environment'], cwd=self.core,
                                capture_output=True, check=True, timeout=30, env={})
        for key, value in {**expected, 'COMPOSE_PROFILES': 'chosen'}.items():
            self.assertIn(f'{key}={value}\n'.encode(), result.stdout)
        with (self.core / '.env').open('a') as target:
            target.write('# later user addition\nPORT=999\n')
        self.run_env('--sync')
        self.assertIn('# later user addition\nPORT=999\n', (self.core / '.env').read_text())
        result = subprocess.run([binary, 'config', '--format', 'json'], cwd=self.core,
                                capture_output=True, check=True, timeout=30, env={})
        self.assertEqual(json.loads(result.stdout)['services']['probe']['environment']['PORT'], '2')
        result = subprocess.run([binary, 'config', '--services'], cwd=self.core,
                                capture_output=True, check=True, timeout=30, env={})
        self.assertEqual(result.stdout.strip(), b'probe')


if __name__ == '__main__':
    unittest.main()
