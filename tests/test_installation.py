"""Offline packaging and agent protocol regression tests (no Claude/network)."""

import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import zipfile

from lib.agent import _parse_output, spawn_agent

ROOT = Path(__file__).resolve().parents[1]


class AgentProtocolTests(unittest.TestCase):
    def test_objects_and_fenced_objects(self):
        for raw in ('{"ok": true}', json.dumps({"type": "result", "result": '{"ok": true}'}),
                    json.dumps({"type": "result", "result": '```json\n{"ok": true}\n```'}),
                    json.dumps({"type": "result", "result": {"ok": True}})):
            with self.subTest(raw=raw):
                self.assertEqual(_parse_output(raw, "classify"), {"ok": True})

    def test_errors_and_non_object_results_are_rejected(self):
        invalid = [None, [], 42, True, "hello"]
        for value in invalid:
            for raw in (json.dumps(value), json.dumps({"type": "result", "result": value}),
                        json.dumps({"type": "result", "result": json.dumps(value)})):
                with self.subTest(raw=raw), self.assertRaises(RuntimeError):
                    _parse_output(raw, "classify")
        for value in ('{"ok": true}', {"ok": True}):
            with self.assertRaises(RuntimeError):
                _parse_output(json.dumps({"type": "result", "is_error": True, "result": value}), "classify")

    def test_tempfiles_removed_on_success_failure_and_retry(self):
        success = subprocess.CompletedProcess([], 0, '{"ok": true}', '')
        failure = subprocess.CompletedProcess([], 1, '', 'failed')
        timeout = subprocess.TimeoutExpired('claude', 600)
        cases = [([success], False), ([failure, success], False),
                 ([timeout, success], False), ([timeout, timeout], True),
                 ([failure, failure], True), ([FileNotFoundError('claude')], True),
                 ([subprocess.CompletedProcess([], 0, 'invalid', '')], True)]
        for outcomes, raises in cases:
            with self.subTest(outcomes=outcomes), tempfile.TemporaryDirectory() as directory:
                project = Path(directory)
                responses = iter(outcomes)
                def fake_run(*args, **kwargs):
                    # Evidence is stdin-only; no context file is written in the project.
                    self.assertEqual(list(project.glob('.ctx_*')), [])
                    self.assertNotEqual(Path(kwargs['cwd']), project)
                    self.assertTrue(Path(kwargs['cwd']).is_dir())
                    self.assertIn('context', kwargs['input'])
                    result = next(responses)
                    if isinstance(result, Exception):
                        raise result
                    return result
                with patch('lib.agent.subprocess.run', side_effect=fake_run):
                    if raises:
                        with self.assertRaises((RuntimeError, FileNotFoundError)):
                            spawn_agent('classify', {}, project)
                    else:
                        self.assertEqual(spawn_agent('classify', {}, project), {"ok": True})
                self.assertEqual(list(project.glob('.ctx_*')), [])


class InstalledWheelTests(unittest.TestCase):
    def test_build_install_and_use_resources_outside_checkout(self):
        # Build with already installed setuptools/wheel; never resolve downloads.
        with tempfile.TemporaryDirectory() as directory:
            temp = Path(directory)
            wheels = temp / 'wheels'
            installed = temp / 'installed'
            subprocess.run([sys.executable, '-m', 'pip', 'wheel', '--no-deps', '--no-index',
                            '--no-build-isolation', '--wheel-dir', str(wheels), str(ROOT)],
                           check=True, capture_output=True, text=True)
            wheel, = wheels.glob('samovar-*.whl')
            with zipfile.ZipFile(wheel) as archive:
                names = set(archive.namelist())
                for source in (ROOT / 'samovar_resources').rglob('*'):
                    if source.is_file() and source.suffix not in ('.pyc',) and '__pycache__' not in source.parts:
                        self.assertIn(source.relative_to(ROOT).as_posix(), names)
            subprocess.run([sys.executable, '-m', 'pip', 'install', '--no-deps', '--no-index',
                            '--target', str(installed), str(wheel)],
                           check=True, capture_output=True, text=True)
            smoke = '''
import argparse
import json
from pathlib import Path
import subprocess
from unittest.mock import patch
import samovar
from lib.agent import spawn_agent
from samovar_resources import SKILLS_DIR, TEMPLATES_DIR
assert str(Path(samovar.__file__).resolve()).startswith(str(Path('installed').resolve()))
assert (SKILLS_DIR / 'setup.md').is_file()
assert (TEMPLATES_DIR / '.gitignore').is_file()
assert (TEMPLATES_DIR / 'sources' / 'collector_template.py').is_file()
with patch.object(samovar, '_run_setup_agent') as setup, patch.object(samovar.subprocess, 'run'):
    samovar.cmd_init(argparse.Namespace(name='smoke'))
    setup.assert_called_once()
    assert setup.call_args.args[0] == (SKILLS_DIR / 'setup.md').read_text()
project = Path('smoke').resolve()
assert (project / 'samovar.yaml').is_file()
assert (project / '.gitignore').is_file()
for skill in ('coordinator', 'classify', 'investigate', 'review', 'report'):
    with patch('lib.agent.subprocess.run', return_value=subprocess.CompletedProcess([], 0, '{"ok": true}', '')) as run:
        assert spawn_agent(skill, {}, project) == {'ok': True}
        assert (SKILLS_DIR / (skill + '.md')).read_text() in run.call_args.args[0][run.call_args.args[0].index('--system-prompt') + 1]
assert not list(project.glob('.ctx_*'))
print('Installed wheel init and all five agent skills passed')
'''
            environment = dict(os.environ, PYTHONPATH=str(installed), PYTHONNOUSERSITE='1')
            result = subprocess.run([sys.executable, '-c', smoke], cwd=temp, env=environment,
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            self.assertIn('all five agent skills passed', result.stdout)


if __name__ == '__main__':
    unittest.main()
