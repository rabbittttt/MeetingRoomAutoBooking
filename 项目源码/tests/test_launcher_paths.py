"""Exercise the batch environment search without starting Python or a build."""

import os
from pathlib import Path
import re
import subprocess
import tempfile
import unittest


ROOT = Path(__file__).resolve().parents[1]


@unittest.skipUnless(os.name == "nt", "Windows batch launcher")
class LauncherPathTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory(prefix="booking launcher ")
        self.addCleanup(temporary.cleanup)
        self.workspace = Path(temporary.name)
        self.project = self.workspace / "web tools" / "booking" / "source"
        self.project.mkdir(parents=True)
        source = (ROOT / "一键打包EXE.cmd").read_text(encoding="utf-8")
        # Keep real batch control flow; replace only the interpreter invocation.
        source, count = re.subn(
            r'(?m)^(.*?) "%~dp0build_exe.py" %\*$',
            r'echo SELECTED_PYTHON=\1', source)
        self.assertEqual(count, 4)
        source = re.sub(r'(?m)^pause$', 'rem offline test', source)
        self.script = self.project / "launcher.cmd"
        self.script.write_text(source, encoding="utf-8", newline="\r\n")

    def interpreter(self, root, folder=".venv"):
        path = root / folder / "Scripts" / "python.exe"
        path.parent.mkdir(parents=True)
        path.touch()
        return path.resolve()

    def selected_interpreter(self):
        result = subprocess.run(
            [os.environ.get("COMSPEC", "cmd.exe"), "/d", "/c", str(self.script)],
            cwd=self.workspace, input="", capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        line = next(line for line in result.stdout.splitlines() if line.startswith("SELECTED_PYTHON="))
        return Path(line.split("=", 1)[1].strip('"')).resolve()

    def test_workspace_environment_is_found_across_multiple_ancestors(self):
        expected = self.interpreter(self.workspace)
        self.assertEqual(self.selected_interpreter(), expected)

    def test_nearest_project_environment_wins(self):
        self.interpreter(self.workspace)
        expected = self.interpreter(self.project)
        self.assertEqual(self.selected_interpreter(), expected)

    def test_packaging_environment_keeps_first_priority(self):
        self.interpreter(self.workspace)
        self.interpreter(self.project)
        expected = self.interpreter(self.project, ".packaging_env")
        self.assertEqual(self.selected_interpreter(), expected)


if __name__ == "__main__":
    unittest.main()
