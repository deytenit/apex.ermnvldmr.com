"""Exercise verification entrypoints in disposable source trees."""
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


class ToolingTests(unittest.TestCase):
    def test_lint_fallback_recurses_and_propagates_each_root_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            shutil.copyfile(ROOT / 'Makefile', root / 'Makefile')
            binaries = root / 'bin'
            binaries.mkdir()
            make = shutil.which('make')
            self.assertIsNotNone(make, 'make is required for the declared verification workflow')
            (binaries / 'make').symlink_to(make)
            (binaries / 'python3').symlink_to(sys.executable)
            environment = dict(os.environ, PATH=str(binaries))
            sources = []
            for name in ('engine', 'actions', 'tests', 'ci/scripts'):
                source = root / name / 'nested' / 'case.py'
                source.parent.mkdir(parents=True)
                source.write_text('value = 1\n')
                sources.append(source)
            for source in sources:
                with self.subTest(root=source.parent.parent):
                    invalid = source.with_name('invalid.py')
                    invalid.write_text('def broken(:\n')
                    result = subprocess.run([str(binaries / 'make'), 'lint'], cwd=root,
                                            env=environment, capture_output=True, text=True,
                                            timeout=30)
                    self.assertNotEqual(result.returncode, 0, result.stdout + result.stderr)
                    self.assertNotIn('verified python bytecode compilation', result.stdout)
                    invalid.unlink()
            result = subprocess.run([str(binaries / 'make'), 'lint'], cwd=root,
                                    env=environment, capture_output=True, text=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('verified python bytecode compilation', result.stdout)

    def test_suite_runner_discovers_both_suites_and_propagates_failures(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            suites = (root / 'tests', root / 'ci/scripts/init-node/tests')
            for suite in suites:
                suite.mkdir(parents=True)
            shutil.copyfile(ROOT / 'tests/run.sh', suites[0] / 'run.sh')
            binaries = root / 'bin'
            binaries.mkdir()
            (binaries / 'python3').symlink_to(sys.executable)
            environment = dict(os.environ, PATH=str(binaries) + os.pathsep + os.environ['PATH'])
            for failure in (None, 0, 1):
                with self.subTest(failing_suite=failure):
                    for index, suite in enumerate(suites):
                        (suite / 'test_case.py').write_text(
                            'import unittest\nclass Case(unittest.TestCase):\n'
                            '    def test_suite_' + str(index) + '(self):\n'
                            '        self.assertTrue(' + repr(index != failure) + ')\n')
                    result = subprocess.run(['/bin/bash', 'tests/run.sh'], cwd=root,
                                            env=environment, capture_output=True, text=True,
                                            timeout=30)
                    if failure is None:
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertIn('test_suite_0', result.stderr)
                        self.assertIn('test_suite_1', result.stderr)
                    else:
                        self.assertNotEqual(result.returncode, 0, result.stderr)
                        self.assertIn('FAIL: test_suite_' + str(failure), result.stderr)
