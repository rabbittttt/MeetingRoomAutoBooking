"""Startup writes use a fake registry. Native tray smoke is explicit opt-in."""
from contextlib import nullcontext
import ctypes
import os
from pathlib import Path
import subprocess
import sys
import time
import unittest
from unittest.mock import patch
from types import SimpleNamespace
from unittest.mock import Mock
import uuid

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import desktop_lifecycle as desktop


class IconMetricsTests(unittest.TestCase):
    def test_common_scaling_has_real_pixel_icon_sizes(self):
        for dpi, small, large in ((96, 16, 32), (120, 20, 40), (144, 24, 48), (192, 32, 64)):
            with self.subTest(dpi=dpi):
                self.assertEqual(desktop._scaled_icon_sizes(dpi), ((small, small), (large, large)))

    def test_dpi_query_restores_thread_context_after_success_and_failure(self):
        for failure in (False, True):
            with self.subTest(failure=failure):
                switch = Mock(return_value=123)
                read = Mock(side_effect=OSError("fixture") if failure else None, return_value=144)
                api = SimpleNamespace(SetThreadDpiAwarenessContext=switch, GetDpiForSystem=read)
                if failure:
                    with self.assertRaises(OSError):
                        desktop._query_system_dpi(api)
                else:
                    self.assertEqual(desktop._query_system_dpi(api), 144)
                self.assertEqual(switch.call_count, 2)
                self.assertEqual(switch.call_args.args, (123,))

    @unittest.skipUnless(sys.platform == "win32", "Windows DPI APIs")
    def test_metrics_query_does_not_change_the_gui_thread_awareness(self):
        user = ctypes.WinDLL("user32")
        user.GetThreadDpiAwarenessContext.argtypes = []
        user.GetThreadDpiAwarenessContext.restype = ctypes.c_void_p
        before = user.GetThreadDpiAwarenessContext()
        dpi = desktop.get_system_dpi()
        self.assertEqual(user.GetThreadDpiAwarenessContext(), before)
        self.assertGreaterEqual(dpi, 96)
        self.assertTrue(all(value > 0 for pair in desktop.system_icon_sizes(dpi) for value in pair))


class FakeRegistry:
    HKEY_CURRENT_USER = 1
    REG_SZ = 1
    KEY_SET_VALUE = 2

    def __init__(self):
        self.values = {}

    def OpenKey(self, hive, path, *args):
        assert hive == self.HKEY_CURRENT_USER and path == desktop.RUN_KEY
        return nullcontext(self)

    CreateKeyEx = OpenKey

    def QueryValueEx(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        return self.values[name], self.REG_SZ

    def SetValueEx(self, key, name, reserved, kind, value):
        assert kind == self.REG_SZ
        self.values[name] = value

    def DeleteValue(self, key, name):
        if name not in self.values:
            raise FileNotFoundError(name)
        del self.values[name]


class StartupTests(unittest.TestCase):
    def test_packaged_startup_quotes_path_and_opens_front_window(self):
        entry = ROOT / "测试 资料" / "会议室自动抢订_F5.exe"
        with patch.object(sys, "frozen", True, create=True):
            manager = desktop.StartupManager("F5", entry)
        self.assertEqual(manager.command, subprocess.list2cmdline([str(entry.resolve())]))
        self.assertNotIn("--background", manager.command)
        self.assertNotIn("--self-test", manager.command)

    def test_source_startup_uses_matching_pythonw_and_script(self):
        entry = ROOT / "会议室自动抢订_GUI.py"
        python = ROOT / "测试 环境" / "python.exe"
        with patch.object(sys, "frozen", False, create=True), \
                patch.object(sys, "executable", str(python)), \
                patch.object(Path, "is_file", return_value=True):
            manager = desktop.StartupManager("H7", entry)
        self.assertEqual(manager.command, subprocess.list2cmdline([
            str(python.with_name("pythonw.exe")), str(entry.resolve())]))

    def test_registry_changes_only_selected_deployment(self):
        registry = FakeRegistry()
        first = desktop.StartupManager("F5", ROOT / "f5.exe")
        second = desktop.StartupManager("H7", ROOT / "h7.exe")
        with patch.object(desktop, "_registry", return_value=registry):
            self.assertFalse(first.is_enabled())
            first.set_enabled(True)
            second.set_enabled(True)
            self.assertTrue(first.is_enabled())
            self.assertTrue(second.is_enabled())
            self.assertEqual(first.registered_command(), first.command)
            first.set_enabled(False)
            first.set_enabled(False)
            self.assertFalse(first.is_enabled())
            self.assertTrue(second.is_enabled())

    def test_moved_executable_gets_updated_command(self):
        registry = FakeRegistry()
        before = desktop.StartupManager("F5", ROOT / "old" / "book.exe")
        after = desktop.StartupManager("F5", ROOT / "new" / "book.exe")
        with patch.object(desktop, "_registry", return_value=registry):
            before.set_enabled(True)
            self.assertFalse(after.is_enabled())
            after.set_enabled(True)
            self.assertEqual(after.registered_command(), after.command)


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("BOOKING_TEST_NATIVE_DESKTOP") == "1",
                     "显式启用 Windows 原生托盘烟测；不操作真实启动项")
class NativeDesktopTests(unittest.TestCase):
    def test_tray_duplicate_activation_and_idempotent_cleanup(self):
        profile = "Test" + uuid.uuid4().hex[:12]
        first = desktop.DesktopSession(profile, "会议室预订 临时测试")
        self.addCleanup(first.close)
        self.assertTrue(first.acquire())
        self.assertTrue(first.start_tray(), first.last_error)
        self.assertTrue(first._tray._owned_icon, "托盘应使用统一应用图标")
        duplicate = desktop.DesktopSession(profile, "duplicate")
        self.addCleanup(duplicate.close)
        self.assertFalse(duplicate.acquire())
        self.assertIn("show", first.drain_actions())
        # Same notification as the tray's double-click, processed by its native thread.
        first._api.user.PostMessageW(first._tray.hwnd, first._tray.CALLBACK, 0, 0x0203)
        for _ in range(20):
            if "show" in first.drain_actions():
                break
            time.sleep(0.025)
        else:
            self.fail("托盘动作未传递到 GUI 队列")
        other = desktop.DesktopSession(profile + "B", "other deployment")
        self.addCleanup(other.close)
        self.assertTrue(other.acquire())
        first.close()
        first.close()
        self.assertFalse(first.tray_available)
        self.assertFalse(first._tray._thread.is_alive())
        self.assertIsNone(first._tray._owned_icon)
        restarted = desktop.DesktopSession(profile, "restart")
        self.addCleanup(restarted.close)
        self.assertTrue(restarted.acquire())


if __name__ == "__main__":
    unittest.main()
