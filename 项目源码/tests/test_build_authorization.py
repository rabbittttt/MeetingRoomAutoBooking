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

    def test_build_does_not_read_or_copy_local_w3_credentials(self):
        credentials = self.root / "w3_credentials.dat"
        sentinel = b"synthetic-encrypted-data-not-a-real-credential"
        credentials.write_bytes(sentinel)
        original_open = Path.open

        def guarded_open(path, *args, **kwargs):
            if path.name.casefold() == "w3_credentials.dat":
                raise AssertionError("构建不得读取本机凭据文件")
            return original_open(path, *args, **kwargs)

        with patch.object(Path, "open", guarded_open):
            self.run_build()
        self.assertEqual(credentials.read_bytes(), sentinel)
        self.assertFalse((self.output.parent / credentials.name).exists())
        self.assertFalse((self.root / "build" / "region_sources" / "H7" / credentials.name).exists())

    def test_build_rejects_explicit_credentials_data_file_or_parent_directory(self):
        folder = self.root / "local_data"
        folder.mkdir()
        credentials = folder / "W3_CREDENTIALS.DAT"
        credentials.write_bytes(b"synthetic-encrypted-data")
        for source in (credentials, folder):
            with self.subTest(source=source.name):
                settings = dict(build_exe.DEFAULTS, profile_id="H7",
                                data_files=[{"source": str(source), "dest": "."}])
                with self.assertRaisesRegex(ValueError, "W3 本机凭据"):
                    build_exe.build_command(Path(sys.executable), settings, self.gui, self.source)

    def fake_archive(self, stage, gui, *, tamper_module=None, credential_file=None):
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
            toc={f"local_data/{credential_file}": None} if credential_file else {},
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

    def verify(self, *, tamper_module=None, credential_file=None):
        settings = dict(build_exe.DEFAULTS, profile_id="H7")
        source, gui = build_exe.prepare_build_sources(
            settings, self.source, self.gui, get_profile("H7"))
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"MZ-local-test-output")
        modules, reader, archive = self.fake_archive(
            source.parent, gui, tamper_module=tamper_module, credential_file=credential_file)
        with patch.dict(sys.modules, modules):
            report = verify_embedded.verify_build(self.output, source.parent, gui, "H7")
        reader.assert_called_once_with(str(self.output.resolve()))
        archive.open_embedded_archive.assert_called_once_with("PYZ.pyz")
        archive.extract.assert_called_once_with(gui.stem)
        self.assertTrue(report["successful"])
        self.assertTrue(report["authorization_managed_separately"])
        self.assertTrue(report["w3_credentials_file_absent"])
        self.assertEqual(report["runtime_sha256"], build_exe.runtime_module_hashes(source.parent))
        self.assertIn("w3_credentials", report["embedded_modules_match"])
        self.assertIn("w3_login", report["embedded_modules_match"])
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

    def test_embedded_verification_rejects_changed_w3_modules(self):
        for name in ("w3_credentials", "w3_login"):
            with self.subTest(module=name):
                with self.assertRaisesRegex(ValueError, "嵌入模块.*不一致.*" + name):
                    self.verify(tamper_module=name)

    def test_embedded_verification_rejects_stale_runtime_hash(self):
        source, gui = build_exe.prepare_build_sources(
            dict(build_exe.DEFAULTS, profile_id="H7"), self.source, self.gui, get_profile("H7"))
        (source.parent / "w3_login.py").write_text("CHANGED_AFTER_STAGING = True\n", encoding="utf-8")
        self.output.parent.mkdir(parents=True, exist_ok=True)
        self.output.write_bytes(b"MZ-local-test-output")
        with self.assertRaisesRegex(ValueError, "运行模块 SHA256"):
            verify_embedded.verify_build(self.output, source.parent, gui, "H7")

    def test_embedded_verification_rejects_packaged_w3_credentials_without_reading_them(self):
        with self.assertRaisesRegex(ValueError, "W3 本机凭据文件"):
            self.verify(credential_file="w3_credentials.dat")

    def test_build_and_verification_reject_credential_temporary_and_backup_files(self):
        for filename in ("w3_credentials.dat.tmp", "W3_CREDENTIALS.DAT.backup"):
            with self.subTest(filename=filename):
                folder = self.root / filename.rsplit(".", 1)[-1]
                folder.mkdir()
                credentials = folder / filename
                credentials.write_bytes(b"synthetic-encrypted-data")
                for source in (credentials, folder):
                    settings = dict(build_exe.DEFAULTS, profile_id="H7",
                                    data_files=[{"source": str(source), "dest": "."}])
                    with self.assertRaisesRegex(ValueError, "W3 本机凭据"):
                        build_exe.build_command(Path(sys.executable), settings, self.gui, self.source)
                with self.assertRaisesRegex(ValueError, "W3 本机凭据文件"):
                    self.verify(credential_file=filename)


if __name__ == "__main__":
    unittest.main()
