import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock

from actions import compose
from engine.descriptor import build_parser


class TestCompose(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmpdir.cleanup)
        self.comp_dir = Path(self.tmpdir.name) / 'compositions'
        for name in ('apex', 'sso', 'arr'):
            directory = self.comp_dir / name
            directory.mkdir(parents=True)
            (directory / 'docker-compose.yml').write_text(f'name: {name}\n')
        self.ctx = MagicMock()
        self.ctx.paths.compositions = str(self.comp_dir)
        self.ctx.paths.stier3 = self.tmpdir.name
        self.ctx.vars.return_value = {'APEX_NODE_HOST': 'test-host', 'OTHER': 'ignored'}

    def run_compose(self, *argv):
        args = build_parser(compose.METADATA, 'apex compose').parse_args(argv)
        compose.run(self.ctx, args)

    def visited(self):
        return [os.path.basename(call.kwargs['cwd'])
                for call in self.ctx.sys.run.call_args_list]

    def test_sequence_all_and_down_reverse(self):
        for argv, expected in [(('restart',), ['apex', 'arr', 'sso']),
                               (('-p', 'all', 'up'), ['apex', 'arr', 'sso']),
                               (('down', 'all'), ['sso', 'arr', 'apex'])]:
            with self.subTest(argv=argv):
                self.ctx.sys.run.reset_mock()
                self.run_compose(*argv)
                self.assertEqual(self.visited(), expected)

    def test_explicit_target_preserves_service_named_like_composition(self):
        self.run_compose('-p', 'apex', 'restart', 'sso')
        self.assertEqual(self.visited(), ['apex'])
        call = self.ctx.sys.run.call_args
        self.assertEqual(call.args[0], ['docker', 'compose', 'restart', 'sso'])
        self.assertEqual(call.kwargs['env']['APEX_NODE_HOST'], 'test-host')
        self.assertTrue(call.kwargs['check'])

    def test_immediate_positional_composition_compatibility(self):
        self.run_compose('restart', 'apex', 'traefik')
        self.assertEqual(self.visited(), ['apex'])
        self.assertEqual(self.ctx.sys.run.call_args.args[0],
                         ['docker', 'compose', 'restart', 'traefik'])

    def test_up_detached_compatibility_and_default_detached(self):
        for argv in [('up', '-d', 'apex'), ('-p', 'apex', 'up'),
                     ('up', 'apex', '-d')]:
            with self.subTest(argv=argv):
                self.ctx.sys.run.reset_mock()
                self.run_compose(*argv)
                self.assertEqual(self.visited(), ['apex'])
                self.assertEqual(self.ctx.sys.run.call_args.args[0],
                                 ['docker', 'compose', 'up', '-d'])

    def test_flag_values_and_unknown_services_do_not_retarget(self):
        for argv in [('logs', '--tail', 'sso'), ('restart', 'traefik'),
                     ('up', '--scale', 'sso'), ('logs', '--', 'apex')]:
            with self.subTest(argv=argv):
                self.ctx.sys.run.reset_mock()
                self.run_compose(*argv)
                self.assertEqual(self.visited(), ['apex', 'arr', 'sso'])
                prefix = ['docker', 'compose', argv[0]]
                if argv[0] == 'up':
                    prefix += ['-d']
                self.assertEqual(self.ctx.sys.run.call_args.args[0], prefix + list(argv[1:]))

    def test_invalid_wrapper_options_fail_before_execution(self):
        for argv, code in [(('-p', 'unknown', 'up'), 1), (('-p', '', 'up'), 1),
                           (('-p',), 64),
                           (('up', '-p', 'apex'), 64),
                           (('up', '--dry-run'), 64), ((), 64),
                           (('up', '-d', 'apex', 'traefik'), 64)]:
            with self.subTest(argv=argv), contextlib.redirect_stdout(io.StringIO()), \
                    contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as error:
                    self.run_compose(*argv)
                self.assertEqual(error.exception.code, code)
                self.ctx.sys.run.assert_not_called()

    def test_separator_preserves_wrapper_looking_passthrough_tokens(self):
        self.run_compose('-p', 'apex', 'exec', '--', 'probe', '--dry-run', '-p', 'sso')
        self.assertEqual(self.ctx.sys.run.call_args.args[0],
                         ['docker', 'compose', 'exec', '--', 'probe', '--dry-run', '-p', 'sso'])

    def test_env_files_layer_before_action(self):
        for filename in ('.env', 'apex.env'):
            (self.comp_dir / 'apex' / filename).write_text('PORT=1234\n')
        self.run_compose('-p', 'apex', 'config')
        self.assertEqual(self.ctx.sys.run.call_args.args[0],
                         ['docker', 'compose', '--env-file', '.env', '--env-file',
                          'apex.env', 'config'])

    def test_socket_survives_restart_up_and_dry_run(self):
        shm = Path(self.tmpdir.name) / 'apex' / 'xray' / 'shm'
        shm.mkdir(parents=True)
        path = shm / 'live.sock'
        path.write_bytes(b"socket path must survive")
        for argv in [('restart', 'apex'), ('-p', 'apex', 'restart', 'traefik'),
                     ('up',), ('--dry-run', '-p', 'apex', 'logs', '--tail', '20', 'xray')]:
            with self.subTest(argv=argv):
                self.ctx.sys.run.reset_mock()
                self.run_compose(*argv)
                self.assertTrue(path.exists())
                if argv[0] == '--dry-run':
                    self.ctx.sys.run.assert_not_called()
                    self.assertIn('logs --tail 20 xray', self.ctx.log.info.call_args.args[0])


if __name__ == '__main__':
    unittest.main()
