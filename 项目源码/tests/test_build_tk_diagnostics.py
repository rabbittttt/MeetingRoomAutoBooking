"""保留Tk子进程的真实错误，避免把环境冲突、超时统一说成组件缺失。"""
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_exe


class BuildTkDiagnosticsTests(unittest.TestCase):
    def test_failed_check_retains_interpreter_and_original_error(self):
        python = Path(sys.executable)
        detail = "Base Python: D:/python\n_tkinter.TclError: 找不到 init.tcl"
        with patch.object(build_exe, "candidate_pythons", return_value=iter([python])), \
                patch.object(build_exe, "check_tkinter", return_value=(False, detail)):
            with self.assertRaises(RuntimeError) as raised:
                build_exe.select_build_python()
        self.assertIn(str(python), str(raised.exception))
        self.assertIn(detail, str(raised.exception))

    def test_python_probe_returns_readable_chinese_errors(self):
        with patch.dict(build_exe.os.environ, {"PYTHONIOENCODING": "ascii"}):
            ok, detail = build_exe.python_command(
                Path(sys.executable), "import sys; print('诊断：测试路径'); sys.exit(7)")
        self.assertFalse(ok)
        self.assertEqual(detail, "诊断：测试路径")

    def test_tk_probe_keeps_diagnostics_and_timeout(self):
        failure = (False, "TimeoutExpired: fixture")
        with patch.object(build_exe, "python_command", return_value=failure) as command:
            self.assertEqual(build_exe.check_tkinter(Path(sys.executable)), failure)
        self.assertEqual(command.call_args.kwargs["timeout"], 20)
        self.assertIn("sys.base_prefix", command.call_args.args[1])


if __name__ == "__main__":
    unittest.main()
