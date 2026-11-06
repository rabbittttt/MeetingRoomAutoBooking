"""保留Tk子进程的真实错误，避免把环境冲突、超时统一说成组件缺失。"""
from pathlib import Path
import json
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_exe


class BuildTkDiagnosticsTests(unittest.TestCase):
    def resources(self):
        temporary = tempfile.TemporaryDirectory(prefix="booking-tk-diagnostic-")
        self.addCleanup(temporary.cleanup)
        base = Path(temporary.name)
        for relative in ("tcl/tcl8.6/init.tcl", "tcl/tk8.6/tk.tcl"):
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# synthetic fixture", encoding="utf-8")
        return base, json.dumps({"base": str(base), "tcl": "8.6", "tk": "8.6"})

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

    def test_recovery_only_uses_selected_pythons_verified_resource_paths(self):
        base, metadata = self.resources()
        def check(python):
            self.assertEqual(build_exe.os.environ["TCL_LIBRARY"], str(base / "tcl/tcl8.6"))
            self.assertEqual(build_exe.os.environ["TK_LIBRARY"], str(base / "tcl/tk8.6"))
            return True, "Tkinter OK"
        with patch.dict(build_exe.os.environ), \
                patch.object(build_exe, "python_command", return_value=(True, metadata)), \
                patch.object(build_exe, "check_tkinter", side_effect=check):
            ok, detail = build_exe.recover_tkinter_paths(Path(sys.executable))
            self.assertTrue(ok)
            self.assertIn("Tkinter OK", detail)
            self.assertEqual(build_exe.os.environ["TCL_LIBRARY"], str(base / "tcl/tcl8.6"))

    def test_missing_files_never_override_environment_or_claim_recovery(self):
        base, metadata = self.resources()
        (base / "tcl/tcl8.6/init.tcl").unlink()
        with patch.dict(build_exe.os.environ, {"TCL_LIBRARY": "previous"}), \
                patch.object(build_exe, "python_command", return_value=(True, metadata)), \
                patch.object(build_exe, "check_tkinter") as check:
            ok, detail = build_exe.recover_tkinter_paths(Path(sys.executable))
            self.assertFalse(ok)
            self.assertIn(str(base / "tcl/tcl8.6/init.tcl"), detail)
            self.assertEqual(build_exe.os.environ["TCL_LIBRARY"], "previous")
            check.assert_not_called()

    def test_failed_recovery_restores_previous_environment(self):
        base, metadata = self.resources()
        with patch.dict(build_exe.os.environ, {"TCL_LIBRARY": "previous-tcl", "TK_LIBRARY": "previous-tk"}), \
                patch.object(build_exe, "python_command", return_value=(True, metadata)), \
                patch.object(build_exe, "check_tkinter", return_value=(False, "fixture Tk version conflict")):
            ok, detail = build_exe.recover_tkinter_paths(Path(sys.executable))
            self.assertFalse(ok)
            self.assertIn("version conflict", detail)
            self.assertEqual(build_exe.os.environ["TCL_LIBRARY"], "previous-tcl")
            self.assertEqual(build_exe.os.environ["TK_LIBRARY"], "previous-tk")

    def test_other_tk_errors_do_not_trigger_path_recovery(self):
        with patch.object(build_exe, "candidate_pythons", return_value=iter([Path(sys.executable)])), \
                patch.object(build_exe, "check_tkinter", return_value=(False, "fixture display error")), \
                patch.object(build_exe, "recover_tkinter_paths") as recover:
            with self.assertRaisesRegex(RuntimeError, "fixture display error"):
                build_exe.select_build_python()
            recover.assert_not_called()

    @unittest.skipUnless(sys.platform == "win32", "Windows Tcl path compatibility")
    def test_init_tcl_error_can_recover_and_keep_selected_shared_python(self):
        base, metadata = self.resources()
        python = Path(sys.executable)
        with patch.dict(build_exe.os.environ), \
                patch.object(build_exe, "candidate_pythons", return_value=iter([python])), \
                patch.object(build_exe, "python_command", return_value=(True, metadata)), \
                patch.object(build_exe, "check_tkinter", side_effect=[
                    (False, "_tkinter.TclError: Can't find a usable init.tcl"), (True, "Tkinter OK")]), \
                patch.object(build_exe, "out"):
            self.assertEqual(build_exe.select_build_python(), python)
            self.assertEqual(build_exe.os.environ["TK_LIBRARY"], str(base / "tcl/tk8.6"))

    def test_main_restores_build_environment_even_after_failure(self):
        def fail_build():
            build_exe.os.environ.update(TCL_LIBRARY="temporary-tcl", TK_LIBRARY="temporary-tk")
            raise RuntimeError("fixture packaging failure")
        with patch.dict(build_exe.os.environ, {"TCL_LIBRARY": "previous-tcl"}), \
                patch.object(build_exe, "build_from_args", side_effect=fail_build):
            build_exe.os.environ.pop("TK_LIBRARY", None)
            with self.assertRaisesRegex(RuntimeError, "fixture packaging failure"):
                build_exe.main()
            self.assertEqual(build_exe.os.environ["TCL_LIBRARY"], "previous-tcl")
            self.assertNotIn("TK_LIBRARY", build_exe.os.environ)

    def test_exe_smoke_never_inherits_build_machine_tcl_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            exe = Path(directory) / "fixture.exe"
            exe.write_bytes(b"MZ" + b"0" * 100000)
            def run(command, **kwargs):
                self.assertNotIn("TCL_LIBRARY", kwargs["env"])
                self.assertNotIn("TK_LIBRARY", kwargs["env"])
                Path(command[-1]).write_text("gui-core-config-ok", encoding="ascii")
                return SimpleNamespace(returncode=0)
            with patch.dict(build_exe.os.environ, {"TCL_LIBRARY": "build-tcl", "TK_LIBRARY": "build-tk"}), \
                    patch.object(build_exe.subprocess, "run", side_effect=run):
                build_exe.smoke_test(exe)

    @unittest.skipUnless(sys.platform == "win32", "Windows Tcl installation layout")
    def test_actual_tk_opens_with_its_base_installation_resources(self):
        with patch.dict(build_exe.os.environ):
            ok, detail = build_exe.recover_tkinter_paths(Path(sys.executable))
        self.assertTrue(ok, detail)
        self.assertIn("Tkinter OK", detail)


if __name__ == "__main__":
    unittest.main()
