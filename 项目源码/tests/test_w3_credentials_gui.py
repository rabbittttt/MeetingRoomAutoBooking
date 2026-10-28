"""本机备用登录设置回归；凭据存储全部模拟，不访问真实登录页。"""
import copy
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import 会议室自动抢订_GUI as gui


class Value:
    def __init__(self, value=""):
        self.value = value

    def get(self):
        return self.value

    def set(self, value):
        self.value = value


class W3CredentialsGUIActionsTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.saved = self.stack.enter_context(patch.object(gui.w3_credentials, "save_credentials"))
        self.deleted = self.stack.enter_context(patch.object(gui.w3_credentials, "delete_credentials"))
        self.loaded = self.stack.enter_context(patch.object(gui.w3_credentials, "load_credentials", return_value=None))
        self.info = self.stack.enter_context(patch.object(gui.messagebox, "showinfo"))
        self.error = self.stack.enter_context(patch.object(gui.messagebox, "showerror"))
        self.confirm = self.stack.enter_context(patch.object(gui.messagebox, "askyesno", return_value=True))
        self.config_saved = self.stack.enter_context(patch.object(gui, "save_config"))
        self.app = gui.BookingGUI.__new__(gui.BookingGUI)
        self.app.root = Mock()
        self.app.thread = None
        self.app._w3_dialog = None
        self.app.w3_status_var = Value()
        self.app.w3_settings_button = Mock()
        self.app.w3_clear_button = Mock()
        self.app.append_log = Mock()

    def dialog(self, *, username=" z00523094 ", password=" secret with spaces "):
        dialog = gui.W3CredentialsDialog.__new__(gui.W3CredentialsDialog)
        dialog.app = self.app
        dialog.top = Mock()
        dialog.username_var = Value(username)
        dialog.password_var = Value(password)
        dialog.feedback_var = Value()
        self.app._w3_dialog = dialog
        return dialog

    def test_save_uses_fixed_domain_and_clears_password_without_config_or_log(self):
        dialog = self.dialog()
        dialog.save()
        self.saved.assert_called_once_with("z00523094", " secret with spaces ", "https://login.huawei.com")
        self.assertEqual(dialog.password_var.get(), "")
        dialog.top.destroy.assert_called_once()
        self.assertIsNone(self.app._w3_dialog)
        self.config_saved.assert_not_called()
        self.app.append_log.assert_not_called()
        self.assertNotIn("secret", str(self.info.call_args_list))

    def test_save_error_clears_password_and_never_displays_exception_payload(self):
        dialog = self.dialog(password="sensitive-test-value")
        self.saved.side_effect = gui.w3_credentials.CredentialError("sensitive-test-value")
        dialog.save()
        self.assertEqual(dialog.password_var.get(), "")
        self.assertIn("保存失败", dialog.feedback_var.get())
        self.assertNotIn("sensitive-test-value", dialog.feedback_var.get())
        dialog.top.destroy.assert_not_called()
        self.config_saved.assert_not_called()
        self.app.append_log.assert_not_called()

    def test_blank_password_requires_reentry_and_does_not_reuse_saved_password(self):
        self.loaded.return_value = SimpleNamespace(username="z00523094", password="old-secret",
                                                   login_origin=gui.W3_LOGIN_ORIGIN)
        dialog = self.dialog(password="")
        dialog.save()
        self.saved.assert_not_called()
        self.loaded.assert_not_called()
        self.assertIn("重新输入密码", dialog.feedback_var.get())
        self.assertNotIn("old-secret", dialog.feedback_var.get())

    def test_cancel_clears_password_and_preserves_existing_credentials(self):
        dialog = self.dialog()
        self.assertEqual(dialog.close(), "break")
        self.assertEqual(dialog.password_var.get(), "")
        self.saved.assert_not_called()
        self.deleted.assert_not_called()
        dialog.top.destroy.assert_called_once()
        self.assertIsNone(self.app._w3_dialog)

    def test_running_blocks_open_save_and_clear_even_when_handlers_are_called_directly(self):
        self.app.thread = Mock(is_alive=Mock(return_value=True))
        with patch.object(gui, "W3CredentialsDialog") as constructor:
            self.app.open_w3_credentials()
        constructor.assert_not_called()
        dialog = self.dialog()
        dialog.save()
        self.app.clear_w3_credentials()
        self.saved.assert_not_called()
        self.deleted.assert_not_called()
        self.confirm.assert_not_called()
        self.assertEqual(dialog.password_var.get(), "")
        self.app._set_w3_controls_running(True)
        self.app.w3_settings_button.configure.assert_called_with(state="disabled")
        self.app.w3_clear_button.configure.assert_called_with(state="disabled")
        self.app._set_w3_controls_running(False)
        self.app.w3_settings_button.configure.assert_called_with(state="normal")
        self.app.w3_clear_button.configure.assert_called_with(state="normal")

    def test_clear_cancel_keeps_credentials_then_confirmed_clear_updates_status(self):
        self.confirm.return_value = False
        self.app.clear_w3_credentials()
        self.deleted.assert_not_called()
        self.confirm.return_value = True
        self.app.clear_w3_credentials()
        self.deleted.assert_called_once_with()
        self.assertEqual(self.app.w3_status_var.get(), "未设置")
        self.config_saved.assert_not_called()
        self.app.append_log.assert_not_called()

    def test_wrong_domain_and_unreadable_credentials_show_safe_recovery_status(self):
        self.loaded.return_value = SimpleNamespace(username="old-user", password="private-test-value",
                                                   login_origin="https://other.example")
        self.app._refresh_w3_credentials_status()
        self.assertIn("登录域名不符", self.app.w3_status_var.get())
        with patch.object(gui, "W3CredentialsDialog") as constructor:
            self.app.open_w3_credentials()
        constructor.assert_called_once_with(self.app, "")
        self.loaded.side_effect = gui.w3_credentials.CredentialError("private-test-value")
        self.app._refresh_w3_credentials_status()
        self.assertIn("无法读取", self.app.w3_status_var.get())
        self.assertNotIn("private-test-value", self.app.w3_status_var.get())
        self.deleted.side_effect = gui.w3_credentials.CredentialError("private-test-value")
        self.app.clear_w3_credentials()
        self.assertNotIn("private-test-value", str(self.error.call_args_list))


class W3CredentialsGUIWidgetTests(unittest.TestCase):
    def test_build_self_test_never_reads_or_changes_local_credentials(self):
        # 独立进程隔离自检对核心参数的修改；真实窗口拖动已有专门回归。
        script = '''
from pathlib import Path
import sys
from unittest.mock import patch
import 会议室自动抢订_GUI as gui
with patch.object(gui.w3_credentials, "load_credentials", side_effect=AssertionError("自检不应读取凭据")) as load, \\
     patch.object(gui.w3_credentials, "save_credentials") as save, \\
     patch.object(gui.w3_credentials, "delete_credentials") as delete, \\
     patch.object(gui, "run_window_drag_self_test"):
    gui.run_self_test(Path(sys.argv[1]))
    load.assert_not_called()
    save.assert_not_called()
    delete.assert_not_called()
'''
        with tempfile.TemporaryDirectory() as directory:
            marker = Path(directory) / "self-test.txt"
            result = subprocess.run([sys.executable, "-B", "-c", script, str(marker)],
                                    cwd=Path(gui.__file__).parent, capture_output=True, timeout=30)
            self.assertEqual(result.returncode, 0, result.stderr.decode("utf-8", errors="replace"))
            self.assertEqual(marker.read_text(encoding="ascii"), "gui-core-config-ok")

    def test_unavailable_storage_keeps_the_optional_setting_from_blocking_window(self):
        root = gui.tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        with patch.object(gui.w3_credentials, "load_credentials",
                          side_effect=gui.w3_credentials.CredentialError("环境不支持")), \
                patch.object(gui.w3_credentials, "credential_path",
                             side_effect=gui.w3_credentials.CredentialError("环境不支持")):
            app = gui.BookingGUI(root, config_override=copy.deepcopy(gui.DEFAULT_CONFIG))
            self.assertIn("无法读取", app.w3_status_var.get())
            self.assertIsNone(app._w3_dialog)

    def test_real_dialog_masks_password_fixes_origin_and_keeps_main_config_clean(self):
        root = gui.tk.Tk()
        root.withdraw()
        self.addCleanup(root.destroy)
        # 所有文件读写替换为模拟，避免触碰使用者本机凭据。
        credentials = SimpleNamespace(username="z00523094", password="never-prefill-this",
                                      login_origin=gui.W3_LOGIN_ORIGIN)
        with patch.object(gui.w3_credentials, "load_credentials", return_value=credentials), \
                patch.object(gui.w3_credentials, "credential_path", return_value=Path("C:/temporary/w3_credentials.dat")), \
                patch.object(gui.w3_credentials, "save_credentials") as save, \
                patch.object(gui.w3_credentials, "delete_credentials") as delete:
            app = gui.BookingGUI(root, config_override=copy.deepcopy(gui.DEFAULT_CONFIG))
            app._select_tab("advanced")
            root.update_idletasks()
            before = app.get_config()
            app.open_w3_credentials()
            dialog = app._w3_dialog
            self.assertEqual(dialog.username_var.get(), "z00523094")
            self.assertEqual(dialog.password_var.get(), "")
            self.assertEqual(dialog.password_entry.cget("show"), "*")
            self.assertEqual(str(dialog.origin_entry.cget("state")), "readonly")
            self.assertEqual(dialog.origin_entry.get(), "https://login.huawei.com")
            self.assertTrue(dialog.top.bind("<Escape>"))
            self.assertTrue(dialog.top.bind("<Return>"))
            dialog.password_var.set("cancelled-secret")
            dialog.close()
            self.assertEqual(dialog.password_var.get(), "")
            self.assertIsNone(app._w3_dialog)
            self.assertEqual(app.get_config(), before)
            self.assertNotIn("password", str(before))
            self.assertNotIn("cancelled-secret", app.log_text.get("1.0", "end"))
            app.open_w3_credentials()
            dialog = app._w3_dialog
            dialog.password_var.set("destroyed-secret")
            dialog.top.destroy()
            self.assertEqual(dialog.password_var.get(), "")
            self.assertIsNone(app._w3_dialog)
            save.assert_not_called()
            delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
