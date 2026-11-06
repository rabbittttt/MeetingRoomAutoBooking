"""桌面后台运行回归；使用真实Tk，启动项和系统托盘全部替换为模拟。"""
import copy
import ctypes
from ctypes import wintypes
import os
from pathlib import Path
from unittest.mock import Mock, patch
import sys
import tempfile
import time
import unittest
import uuid

import 会议室自动抢订_GUI as gui


class DesktopGUITests(unittest.TestCase):
    def make_app(self, *, startup=True, tray=True):
        root = gui.tk.Tk()
        root.withdraw()
        app = None
        def cleanup():
            try:
                app._finish_exit() if app is not None else root.destroy()
            except gui.tk.TclError:
                pass
        self.addCleanup(cleanup)
        desktop = Mock(tray_available=tray)
        desktop.start_tray.return_value = tray
        desktop.drain_actions.return_value = []
        manager = Mock()
        cfg = copy.deepcopy(gui.DEFAULT_CONFIG)
        cfg["start_with_windows"] = startup
        app = gui.BookingGUI(root, config_override=cfg, check_credentials=False,
                             desktop_session=desktop, startup_manager=manager)
        return root, app, desktop, manager

    def test_default_startup_and_saved_opt_out_are_applied_without_starting_jobs(self):
        for enabled in (True, False):
            with self.subTest(enabled=enabled):
                root, app, desktop, manager = self.make_app(startup=enabled)
                manager.set_enabled.assert_called_once_with(enabled)
                self.assertEqual(app.get_config()["start_with_windows"], enabled)
                self.assertIsNone(app.thread)
                self.assertFalse(app.is_running())
                self.assertEqual(app.desktop_exit_button.winfo_manager(), "")
                app._finish_exit()

    def test_close_keeps_worker_running_and_tray_show_restores_window(self):
        root, app, desktop, manager = self.make_app()
        root.deiconify()
        root.update_idletasks()
        app.thread = Mock()
        app.thread.is_alive.return_value = True
        with patch.object(app, "stop_run") as stop:
            app.on_close()
        self.assertEqual(root.state(), "withdrawn")
        self.assertTrue(app.is_running())
        self.assertFalse(app.stop_event.is_set())
        stop.assert_not_called()
        desktop.close.assert_not_called()
        desktop.drain_actions.return_value = ["show"]
        app._drain_queue()
        self.assertEqual(root.state(), "normal")

    def test_failed_tray_never_hides_the_only_accessible_window(self):
        root, app, desktop, manager = self.make_app(tray=False)
        root.deiconify()
        with patch.object(gui.messagebox, "showwarning") as warning:
            app.on_close()
        self.assertEqual(root.state(), "normal")
        warning.assert_called_once()
        desktop.close.assert_not_called()
        self.assertEqual(app.desktop_exit_button.winfo_manager(), "pack")

    def test_exit_fallback_appears_only_while_tray_is_unavailable(self):
        root, app, desktop, manager = self.make_app()
        self.assertEqual(app.desktop_exit_button.winfo_manager(), "")
        desktop.tray_available = False
        app._drain_queue()
        self.assertEqual(app.desktop_exit_button.winfo_manager(), "pack")
        desktop.tray_available = True
        app._drain_queue()
        self.assertEqual(app.desktop_exit_button.winfo_manager(), "")

    def test_exit_waits_for_worker_completion_and_then_removes_tray(self):
        root, app, desktop, manager = self.make_app()
        app.thread = Mock()
        app.thread.is_alive.return_value = True
        with patch.object(gui.messagebox, "askyesno", return_value=True), \
                patch.object(app, "stop_run") as stop:
            app.request_exit()
        self.assertTrue(app.close_after_stop)
        stop.assert_called_once()
        desktop.close.assert_not_called()
        app.queue.put(("done", 0, ""))
        app._drain_queue()
        desktop.close.assert_called_once()
        self.assertTrue(app._exiting)

    def test_opt_out_saves_immediately_and_survives_next_config_load(self):
        root, app, desktop, manager = self.make_app()
        with tempfile.TemporaryDirectory() as directory, \
                patch.object(gui, "CONFIG_FILE", Path(directory) / "config.json"):
            saved = copy.deepcopy(gui.DEFAULT_CONFIG)
            saved["watch_targets"] = [{"date": "2026-12-01", "start": "14:00", "end": "16:00", "areas": [gui.AREAS[0]]}]
            gui.save_config(saved)
            app.startup_var.set(False)
            app._change_startup()
            self.assertFalse(gui.load_config()["start_with_windows"])
            self.assertEqual(gui.load_config()["watch_targets"], saved["watch_targets"])
        manager.set_enabled.assert_called_with(False)

    def test_failed_startup_update_reverts_checkbox_and_does_not_save(self):
        root, app, desktop, manager = self.make_app()
        manager.set_enabled.side_effect = OSError("fixture permission denial")
        app.startup_var.set(False)
        with patch.object(gui, "load_config", return_value=copy.deepcopy(gui.DEFAULT_CONFIG)), \
                patch.object(gui, "save_config") as save, \
                patch.object(gui.messagebox, "showerror"):
            app._change_startup()
        self.assertTrue(app.startup_var.get())
        save.assert_not_called()


class DesktopEntryTests(unittest.TestCase):
    def test_normal_entry_opens_foreground_without_starting_tasks(self):
        root = Mock()
        with patch.object(sys, "argv", ["booking.exe"]), \
                patch.object(gui.tk, "Tk", return_value=root), \
                patch.object(gui.core, "require_authorized_account", return_value="fixture_user"), \
                patch.object(gui, "DesktopSession") as desktop, \
                patch.object(gui, "StartupManager"), \
                patch.object(gui, "BookingGUI") as app, \
                patch("gui_diagnostics.GuiDiagnostics"):
            desktop.return_value.acquire.return_value = True
            self.assertEqual(gui.main(), 0)
        app.return_value.show_window.assert_called_once()
        root.mainloop.assert_called_once()
        app.return_value.start_run.assert_not_called()
        desktop.return_value.close.assert_called_once()

    def test_self_test_entry_does_not_touch_startup_or_tray(self):
        with patch.object(sys, "argv", ["booking.exe", "--self-test", "fixture.txt"]), \
                patch.object(gui, "run_self_test") as self_test, \
                patch.object(gui, "DesktopSession") as desktop, \
                patch.object(gui, "StartupManager") as startup:
            self.assertEqual(gui.main(), 0)
        self_test.assert_called_once_with(Path("fixture.txt"))
        desktop.assert_not_called()
        startup.assert_not_called()


@unittest.skipUnless(sys.platform == "win32" and os.environ.get("BOOKING_TEST_NATIVE_DESKTOP") == "1",
                     "显式启用真实Tk与Windows托盘联调；不注册启动项")
class NativeDesktopGUITests(unittest.TestCase):
    def test_close_and_native_tray_activation_round_trip(self):
        session = gui.DesktopSession("Test" + uuid.uuid4().hex[:12], "会议室预订 临时界面测试")
        self.addCleanup(session.close)
        self.assertTrue(session.acquire())
        root = gui.tk.Tk()
        root.withdraw()
        with patch.object(root, "iconbitmap", wraps=root.iconbitmap) as set_icon:
            app = gui.BookingGUI(root, config_override=copy.deepcopy(gui.DEFAULT_CONFIG),
                                 check_credentials=False, desktop_session=session)
        set_icon.assert_any_call(str(gui.APP_ICON_PATH))
        self.addCleanup(app._finish_exit)
        root.deiconify()
        app._select_tab("advanced")
        root.update()
        self.assertTrue(session.tray_available, session.last_error)
        self.assertTrue(session._tray._owned_icon, "托盘没有加载应用图标")
        hwnd = app._window_frame._windows_shell().handle(root)
        get_icon = session._api.user.SendMessageW
        get_icon.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        get_icon.restype = ctypes.c_ssize_t
        self.assertTrue(get_icon(hwnd, 0x007F, 1, 0), "窗口没有设置Windows大图标")
        self.assertTrue(get_icon(hwnd, 0x007F, 0, 0), "窗口没有设置Windows小图标")
        self.assertIsNone(app.thread)
        app.on_close()
        self.assertEqual(root.state(), "withdrawn")
        session._api.user.PostMessageW(session._tray.hwnd, session._tray.CALLBACK, 0, 0x0203)
        deadline = time.monotonic() + 3
        while root.state() == "withdrawn" and time.monotonic() < deadline:
            root.update()
            time.sleep(0.025)
        self.assertEqual(root.state(), "normal")
        app.request_exit()
        self.assertTrue(app._exiting)
        self.assertFalse(session.tray_available)


if __name__ == "__main__":
    unittest.main()
