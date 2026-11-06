"""Run relocated launchers against disposable Python environments and fake entries."""
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
import venv

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
import build_exe

PROBE = "import sys,json,os; print(json.dumps({'executable':sys.executable,'args':sys.argv[1:],'path_first':os.environ['PATH'].split(os.pathsep)[0]})); sys.exit(23 if 'return-23' in sys.argv else 0)"

@unittest.skipUnless(os.name == 'nt', 'Windows batch launcher')
class LauncherPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix='booking-shared-python-')
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name).resolve() / '任意父目录 & space!' / 'Project'
        self.project = self.workspace / '会议室预定' / 'booking' / '项目源码'
        self.project.mkdir(parents=True)
        self.outside = Path(temporary.name).resolve() / 'unrelated cwd'
        self.outside.mkdir()
        for name in ('一键打包EXE.cmd', 'run-project.ps1', '启动会议室预定.cmd'):
            shutil.copy2(ROOT / name, self.project / name)
        for name in ('build_exe.py','会议室自动抢订_GUI.py'):
            (self.project / name).write_text(PROBE, encoding='utf-8')
        self.env = {k:v for k,v in os.environ.items() if k != 'BOOKING_PYTHON'}
        self.env.update(PROJECT_NO_PAUSE='1',PYTHONIOENCODING='utf-8')
        self.python = self.environment(self.workspace)

    def environment(self, root, folder='.venv'):
        directory = root / folder
        venv.EnvBuilder(with_pip=False).create(directory)
        return directory / 'Scripts/python.exe'

    def launch(self, *args, filename='一键打包EXE.cmd'):
        return subprocess.run([os.environ.get('COMSPEC','cmd.exe'),'/d','/c','call',str(self.project/filename),*args],
                              cwd=self.outside, env=self.env,capture_output=True,encoding='utf-8',errors='replace',timeout=25)

    def selected(self, result):
        self.assertEqual(result.returncode,0,result.stdout+result.stderr)
        payload=json.loads(result.stdout.splitlines()[-1])
        self.assertTrue(Path(payload['path_first']).samefile(Path(payload['executable']).parent))
        return payload

    def test_workspace_environment_is_found_after_relocation(self):
        self.assertTrue(Path(self.selected(self.launch())['executable']).samefile(self.python))

    def test_project_and_packaging_environments_do_not_take_priority(self):
        self.environment(self.project)
        self.environment(self.project,'.packaging_env')
        self.assertTrue(Path(self.selected(self.launch())['executable']).samefile(self.python))

    def test_explicit_relative_override_still_works(self):
        selected=self.environment(self.project,'explicit runtime')
        self.env['BOOKING_PYTHON']=str(selected.relative_to(self.project))
        self.assertTrue(Path(self.selected(self.launch())['executable']).samefile(selected))

    def test_missing_workspace_python_does_not_use_project_or_path(self):
        self.python.unlink()
        self.environment(self.project)
        result=self.launch()
        self.assertNotEqual(result.returncode,0)
        self.assertIn('Project/.venv',result.stderr)

    def test_gui_uses_the_same_workspace_environment(self):
        self.assertTrue(Path(self.selected(self.launch(filename='启动会议室预定.cmd'))['executable']).samefile(self.python))

    def test_arguments_and_exit_status_are_preserved(self):
        result=self.launch('--profile','H7','return-23')
        self.assertEqual(result.returncode,23,result.stdout+result.stderr)
        self.assertEqual(json.loads(result.stdout.splitlines()[-1])['args'],['--profile','H7','return-23'])

    def test_builder_selects_only_workspace_python(self):
        self.environment(self.project,'.packaging_env')
        with patch.object(build_exe,'ROOT',self.project),patch.dict(os.environ,{'BOOKING_PYTHON':''}),patch.object(build_exe,'check_tkinter',return_value=(True, 'Tkinter OK')):
            self.assertEqual(list(build_exe.candidate_pythons()),[self.python.resolve()])
            self.assertEqual(build_exe.select_build_python(),self.python.resolve())

if __name__ == '__main__':
    unittest.main()
