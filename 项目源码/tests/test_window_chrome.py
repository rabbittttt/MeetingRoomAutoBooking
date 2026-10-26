"""Window-frame regressions without company login or a booking browser."""
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import window_chrome as chrome


class FakeRoot:
    def __init__(self):
        self.override = False
        self.current_state = "normal"
        self.mapped = True
        self.width, self.height, self.x, self.y = 1110, 655, -120, 80
        self.changes = []

    def overrideredirect(self, value=None):
        if value is not None:
            self.override = bool(value)
            self.changes.append(("override", bool(value)))
        return self.override

    def state(self): return self.current_state
    def winfo_ismapped(self): return self.mapped
    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def winfo_x(self): return self.x
    def winfo_y(self): return self.y
    def update_idletasks(self): pass
    def winfo_screenwidth(self): return 1920
    def winfo_screenheight(self): return 1080

    def geometry(self, value):
        self.changes.append(("geometry", value))
        found = re.fullmatch(r"(\d+)x(\d+)\+(-?\d+)\+(-?\d+)", value)
        if found:
            self.width, self.height, self.x, self.y = map(int, found.groups())

    def iconify(self):
        if self.override:
            raise AssertionError("override must be cleared before iconify")
        self.current_state = "iconic"
        self.mapped = False
        self.changes.append(("iconify",))


class BorderlessWindowTests(unittest.TestCase):
    def setUp(self):
        self.root = FakeRoot()
        self.frame = chrome.BorderlessWindow(self.root)
        self.shell = Mock()
        self.shell.work_area.return_value = (-1920, 0, 0, 1040)
        self.frame._shell = self.shell

    def test_init_uses_only_tk_decoration_setting(self):
        self.assertEqual(self.root.changes, [("override", True)])
        self.assertFalse(self.frame.maximized)
        self.shell.assert_not_called()

    def test_withdrawn_init_prepares_taskbar_before_first_map(self):
        root = FakeRoot()
        root.current_state, root.mapped = "withdrawn", False
        native = Mock()
        with patch.object(chrome.sys, "platform", "win32"), patch.object(
                chrome, "_WindowsShell", return_value=native):
            chrome.BorderlessWindow(root)
        native.enable_taskbar.assert_called_once_with(root)
        self.assertEqual(root.current_state, "withdrawn")
        self.assertFalse(root.mapped)
        self.assertEqual(root.changes, [("override", True)])

    def test_enable_repeatedly_never_changes_geometry(self):
        with patch.object(chrome.sys, "platform", "win32"):
            for _ in range(30):
                self.assertTrue(self.frame.enable())
        self.assertEqual(self.root.changes, [("override", True)])
        self.assertEqual(self.shell.enable_taskbar.call_count, 30)

    def test_pending_map_callback_cannot_restore_minimized_window(self):
        self.frame.minimize()
        self.assertFalse(self.frame.enable())
        self.assertFalse(self.root.override)
        self.assertEqual(self.root.current_state, "iconic")
        self.shell.enable_taskbar.assert_not_called()

    def test_unmapped_normal_window_stays_untouched(self):
        self.root.mapped = False
        self.assertFalse(self.frame.enable())
        self.shell.enable_taskbar.assert_not_called()

    def test_map_restores_borderless_mode_after_minimization(self):
        self.frame.minimize()
        self.root.current_state, self.root.mapped = "normal", True
        with patch.object(chrome.sys, "platform", "win32"):
            self.assertTrue(self.frame.enable())
        self.assertTrue(self.root.override)
        self.shell.enable_taskbar.assert_called_once_with(self.root)
        self.assertFalse(any(item[0] == "geometry" for item in self.root.changes))

    def test_maximize_uses_current_monitor_work_area_and_restores_negative_origin(self):
        with patch.object(chrome.sys, "platform", "win32"):
            self.frame.toggle_maximize()
        self.assertTrue(self.frame.maximized)
        self.assertEqual((self.root.width, self.root.height, self.root.x, self.root.y),
                         (1920, 1040, -1920, 0))
        self.frame.toggle_maximize()
        self.assertFalse(self.frame.maximized)
        self.assertEqual((self.root.width, self.root.height, self.root.x, self.root.y),
                         (1110, 655, -120, 80))

    def test_restore_preserves_latest_normal_size_after_user_resize(self):
        self.root.width, self.root.height = 900, 700
        with patch.object(chrome.sys, "platform", "win32"):
            self.frame.toggle_maximize()
        self.frame.restore()
        self.assertEqual((self.root.width, self.root.height), (900, 700))
        count = len(self.root.changes)
        self.frame.restore()
        self.assertEqual(len(self.root.changes), count)

    def test_invalid_work_area_does_not_mark_maximized_or_change_geometry(self):
        self.shell.work_area.return_value = (0, 0, 0, 0)
        with patch.object(chrome.sys, "platform", "win32"):
            with self.assertRaisesRegex(OSError, "可用区域"):
                self.frame.toggle_maximize()
        self.assertFalse(self.frame.maximized)
        self.assertFalse(any(item[0] == "geometry" for item in self.root.changes))

    def test_taskbar_style_preserves_unrelated_extended_flags(self):
        native = chrome._WindowsShell.__new__(chrome._WindowsShell)
        native.handle = Mock(return_value=55)
        old = 0x10000000 | chrome._WindowsShell.TOOLWINDOW
        native.get_style = Mock(return_value=old)
        native.set_style = Mock(return_value=old)
        native.ctypes = Mock()
        native.ctypes.get_last_error.return_value = 0
        native.enable_taskbar(self.root)
        native.set_style.assert_called_once_with(55, -20, 0x10000000 | chrome._WindowsShell.APPWINDOW)

    def test_taskbar_style_unchanged_is_not_rewritten(self):
        native = chrome._WindowsShell.__new__(chrome._WindowsShell)
        native.handle = Mock(return_value=55)
        native.get_style = Mock(return_value=chrome._WindowsShell.APPWINDOW)
        native.set_style = Mock()
        native.enable_taskbar(self.root)
        native.set_style.assert_not_called()


def run_tk_probe():
    import ctypes
    from ctypes import wintypes
    import tkinter as tk

    api = ctypes.WinDLL("user32", use_last_error=True)
    api.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
    api.ClientToScreen.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.POINT)]
    root = tk.Tk()
    root.withdraw()
    root.geometry("640x420+120+100")
    root.configure(bg="#fff7fa")
    tk.Label(root, text="窗口框架验证", bg="#fff7fa").pack(fill="both", expand=True)
    frame = chrome.BorderlessWindow(root)
    native = frame._windows_shell()
    hidden_handle = native.handle(root)
    hidden_style = native.get_style(hidden_handle, -20)
    assert root.state() == "withdrawn" and not root.winfo_ismapped(), "initialization mapped hidden window"
    assert hidden_style & native.APPWINDOW and not hidden_style & native.TOOLWINDOW, "first-map taskbar style absent"
    errors = []

    def mapped(event):
        if event.widget is root:
            root.after_idle(frame.enable)

    def callback_error(kind, error, traceback):
        errors.append(f"{kind.__name__}: {error}")

    root.report_callback_exception = callback_error
    root.bind("<Map>", mapped, add="+")

    def verify_frame():
        native = frame._windows_shell()
        hwnd = native.handle(root)
        outer, client = wintypes.RECT(), wintypes.RECT()
        origin = wintypes.POINT()
        assert api.GetWindowRect(hwnd, ctypes.byref(outer)), "GetWindowRect failed"
        assert api.GetClientRect(hwnd, ctypes.byref(client)), "GetClientRect failed"
        assert api.ClientToScreen(hwnd, ctypes.byref(origin)), "ClientToScreen failed"
        size = root.winfo_width(), root.winfo_height()
        assert size == (outer.right - outer.left, outer.bottom - outer.top), ("outer mismatch", size, outer)
        assert size == (client.right, client.bottom), ("client mismatch", size, client)
        assert (origin.x, origin.y) == (outer.left, outer.top), "nonclient offset remains"
        assert (root.winfo_rootx(), root.winfo_rooty()) == (outer.left, outer.top), "Tk client shifted"
        style = native.get_style(hwnd, -20)
        assert style & native.APPWINDOW, "taskbar APPWINDOW flag missing"
        assert not style & native.TOOLWINDOW, "tool-window flag suppresses taskbar"
        assert not native.get_style(hwnd, -16) & 0x00C00000, "native caption remains"
        assert root.overrideredirect(), "Tk lost borderless setting"
        assert not errors, errors

    try:
        root.deiconify()
        root.update()
        assert frame._windows_shell().handle(root) == hidden_handle, "initial mapping replaced prepared wrapper"
        verify_frame()
        initial_size = root.winfo_width(), root.winfo_height()
        for index in range(30):
            root.geometry(f"+{120 + (index % 6) * 12}+{100 + (index % 4) * 9}")
            root.update()
            frame.enable()
            verify_frame()
            assert (root.winfo_width(), root.winfo_height()) == initial_size, "movement changed size"
        root.geometry("+-75+80")
        root.update()
        assert (root.winfo_x(), root.winfo_y()) == (-75, 80), "negative coordinate became margin"
        normal = root.geometry()
        work = frame._work_area()
        frame.toggle_maximize()
        root.update()
        assert frame.maximized
        verify_frame()
        assert (root.winfo_x(), root.winfo_y(), root.winfo_width(), root.winfo_height()) == (
            work[0], work[1], work[2] - work[0], work[3] - work[1]), "maximize missed working area"
        frame.restore()
        root.update()
        assert root.geometry() == normal, ("restore changed normal geometry", root.geometry(), normal)
        verify_frame()
        for _ in range(3):
            frame.minimize()
            root.update()
            assert root.state() == "iconic", "minimize did not iconify"
            root.deiconify()
            root.update()
            verify_frame()
            assert root.geometry() == normal, "minimize restore changed geometry"
        print("tk-borderless-outer-client-taskbar-move-maximize-minimize-ok", flush=True)
    finally:
        root.destroy()


@unittest.skipUnless(sys.platform == "win32", "Windows Tk regression")
class RealWindowChromeTests(unittest.TestCase):
    def test_actual_wrapper_geometry_survives_moves_and_state_changes(self):
        completed = subprocess.run([sys.executable, "-B", "-X", "utf8", str(Path(__file__)), "--tk-probe"],
                                   capture_output=True, text=True, encoding="utf-8", timeout=45,
                                   cwd=Path(__file__).resolve().parents[1])
        self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
        self.assertIn("tk-borderless-outer-client-taskbar-move-maximize-minimize-ok", completed.stdout)


if __name__ == "__main__":
    if "--tk-probe" in sys.argv:
        run_tk_probe()
    else:
        unittest.main(verbosity=2)
