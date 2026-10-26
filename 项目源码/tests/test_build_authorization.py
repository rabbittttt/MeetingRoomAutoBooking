"""打包与授权文件分开管理；全部构建输入和输出均位于临时目录。"""
from contextlib import ExitStack
import json
import marshal
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import build_exe
from deployment_profiles import get_profile
from tools import verify_embedded


LICENSE_NAME = "账号授权.json"
OLD_LICENSE = b'{"sentinel": "old-source-list-not-a-real-license"}\n'
NEW_LICENSE = b'{"sentinel": "new-user-list-not-a-real-license"}\n'


class BuildAuthorizationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="booking_build_auth_")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "project"
        self.root.mkdir()
        self.source = self.root / "会议室预定.py"
        # 仅提供构建器必需的函数声明，不导入真实核心或启动预约流程。
        required = (
            "apply_gui_settings", "build_release_batches", "parse_targets", "main",
            "parse_weekly_rules", "expand_weekly_targets", "merge_release_targets",
            "log_booking_popup_raw", "booking_failure_dialog", "confirm_and_book",
            "watch_confirm_disabled",
        )
        self.source.write_text("\n".join(f"def {name}():\n    pass\n" for name in required),
                               encoding="utf-8")
        self.gui = self.root / "会议室自动抢订_GUI.py"
        self.gui.write_text("TITLE = '离线构建夹具'\n", encoding="utf-8")
        for filename in build_exe.RUNTIME_MODULES:
            (self.root / filename).write_text(f"MODULE_NAME = {filename!r}\n", encoding="utf-8")
        self.output = self.root / "dist" / "H7" / "会议室自动抢订_H7.exe"
        self.source_license = self.root / LICENSE_NAME
        self.output_license = self.output.parent / LICENSE_NAME
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.stack.enter_context(patch.multiple(
            build_exe, ROOT=self.root, CONFIG_PATH=self.root / "打包配置.json",
            GENERATED_CORE=self.root / "booking_core.py"))
        self.stack.enter_context(patch.object(build_exe, "out"))

    def run_build(self):
        def fake_packager(command, *, workdir):
            self.assertEqual(workdir, self.root / "build" / "region_sources" / "H7")
            self.assertEqual(command[-1], str(workdir / self.gui.name))
            self.assertNotIn(LICENSE_NAME, " ".join(command))
            self.output.parent.mkdir(parents=True, exist_ok=True)
            self.output.write_bytes(b"MZ-local-test-output")

        completed = types.SimpleNamespace(returncode=0)
        with patch.object(sys, "argv", ["build_exe.py", "--profile", "H7"]), \
                patch.object(build_exe, "select_build_python", return_value=Path(sys.executable)), \
                patch.object(build_exe, "ensure_dependencies") as dependencies, \
                patch.object(build_exe, "run_packager", side_effect=fake_packager) as packager, \
                patch.object(build_exe, "smoke_test") as smoke, \
                patch.object(build_exe.subprocess, "run", return_value=completed) as subprocess_run:
            self.assertEqual(build_exe.main(), 0)
        dependencies.assert_called_once()
        packager.assert_called_once()
        smoke.assert_called_once_with(self.output)
        self.assertEqual(subprocess_run.call_count, 2)
        verify_command = subprocess_run.call_args_list[-1].args[0]
        self.assertEqual(verify_command[1:], [str(self.root / "tools" / "verify_embedded.py"),
                                             "--profile", "H7"])
        self.assertTrue(subprocess_run.call_args_list[-1].kwargs["check"])
        self.assertFalse((self.root / "build" / "region_sources" / "H7" / LICENSE_NAME).exists())
        self.assertTrue(self.output.is_file())

    def test_build_completes_without_source_or_output_authorization_file(self):
        self.run_build()
        self.assertFalse(self.source_license.exists())
        self.assertFalse(self.output_license.exists())

    def test_build_does_not_copy_source_authorization_to_empty_output(self):
        self.source_license.write_bytes(OLD_LICENSE)
        self.run_build()
        self.assertEqual(self.source_license.read_bytes(), OLD_LICENSE)
        self.assertFalse(self.output_license.exists())

    def test_build_preserves_new_output_authorization_when_source_is_old(self):
        self.source_license.write_bytes(OLD_LICENSE)
        self.output.parent.mkdir(parents=True)
        self.output_license.write_bytes(NEW_LICENSE)
        self.run_build()
        self.assertEqual(self.source_license.read_bytes(), OLD_LICENSE)
        self.assertEqual(self.output_license.read_bytes(), NEW_LICENSE)

    def fake_archive(self, stage, gui, *, tamper_module=None):
        payloads = {
            name: compile((stage / f"{name}.py").read_text(encoding="utf-8"),
                          f"embedded/{name}.py", "exec", dont_inherit=True)
            for name in verify_embedded.RUNTIME_MODULES
        }
        if tamper_module is not None:
            payloads[tamper_module] = compile("TAMPERED = True\n", "tampered.py", "exec")
        gui_code = compile(gui.read_text(encoding="utf-8"), "embedded/gui.py", "exec",
                           dont_inherit=True)
        pyz = types.SimpleNamespace(toc=dict.fromkeys(payloads), extract=payloads.__getitem__)
        archive = types.SimpleNamespace(
            open_embedded_archive=Mock(return_value=pyz),
            extract=Mock(return_value=marshal.dumps(gui_code)))
        reader = Mock(return_value=archive)
        # 不读取现有 EXE，也不要求测试解释器安装打包器。
        package = types.ModuleType("PyInstaller")
        package.__path__ = []
        archive_package = types.ModuleType("PyInstaller.archive")
        archive_package.__path__ = []
        readers = types.ModuleType("PyInstaller.archive.readers")
        readers.CArchiveReader = reader
        package.archive = archive_package
        archive_package.readers = readers
        modules = {"PyInstaller": package, "PyInstaller.archive": archive_package,
                   "PyInstaller.archive.readers": readers}
        return modules, reader, archive

    def verify(self, *, tamper_module=None):
        settings = dict(build_exe.DEFAULTS, profile_id="H7")
        source, gui = build_exe.prepare_build_sources(
            settings, self.source, self.gui, get_profile("H7"))
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"MZ-local-test-output")
        modules, reader, archive = self.fake_archive(source.parent, gui, tamper_module=tamper_module)
        with patch.dict(sys.modules, modules):
            report = verify_embedded.verify_build(self.output, source.parent, gui, "H7")
        reader.assert_called_once_with(str(self.output.resolve()))
        archive.open_embedded_archive.assert_called_once_with("PYZ.pyz")
        archive.extract.assert_called_once_with(gui.stem)
        self.assertTrue(report["successful"])
        self.assertTrue(report["authorization_managed_separately"])
        self.assertNotIn("external_license_matches", report)
        self.assertEqual(set(report["embedded_modules_match"]),
                         set(verify_embedded.RUNTIME_MODULES) | {gui.stem})
        self.assertEqual(json.loads((self.output.parent / "构建校验.json").read_text(encoding="utf-8")),
                         report)
        return report

    def test_embedded_verification_succeeds_without_authorization_files(self):
        self.verify()
        self.assertFalse(self.source_license.exists())
        self.assertFalse(self.output_license.exists())

    def test_embedded_verification_ignores_different_external_authorization_files(self):
        self.source_license.write_bytes(OLD_LICENSE)
        self.output.parent.mkdir(parents=True)
        self.output_license.write_bytes(NEW_LICENSE)
        self.verify()
        self.assertEqual(self.source_license.read_bytes(), OLD_LICENSE)
        self.assertEqual(self.output_license.read_bytes(), NEW_LICENSE)

    def test_embedded_verification_still_rejects_changed_module_without_authorization(self):
        self.output.parent.mkdir(parents=True)
        report_path = self.output.parent / "构建校验.json"
        report_path.write_text('{"successful": true}', encoding="utf-8")
        with self.assertRaisesRegex(ValueError, "嵌入模块.*不一致.*account_auth"):
            self.verify(tamper_module="account_auth")
        self.assertFalse(report_path.exists(), "核验失败不能残留旧的通过报告")


if __name__ == "__main__":
    unittest.main()
