"""Tk owns the window frame and geometry; Win32 only supplies shell metadata.

Changing WS_CAPTION behind Tk's back leaves its frame-size bookkeeping stale.
Use Tk's supported borderless mode instead, and keep taskbar integration separate
from sizing, movement, and painting.
"""
from __future__ import annotations

import sys
import logging
from pathlib import Path

from desktop_lifecycle import system_icon_sizes


class _WindowsShell:
    APPWINDOW = 0x00040000
    TOOLWINDOW = 0x00000080

    def __init__(self):
        import ctypes
        from ctypes import wintypes

        self.ctypes = ctypes
        self.wintypes = wintypes
        self.api = ctypes.WinDLL("user32", use_last_error=True)
        self.api.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.api.GetAncestor.restype = wintypes.HWND
        long_type = ctypes.c_ssize_t if ctypes.sizeof(ctypes.c_void_p) == 8 else wintypes.LONG
        self.get_style = (self.api.GetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8
                          else self.api.GetWindowLongW)
        self.get_style.argtypes = [wintypes.HWND, ctypes.c_int]
        self.get_style.restype = long_type
        self.set_style = (self.api.SetWindowLongPtrW if ctypes.sizeof(ctypes.c_void_p) == 8
                          else self.api.SetWindowLongW)
        self.set_style.argtypes = [wintypes.HWND, ctypes.c_int, long_type]
        self.set_style.restype = long_type
        self.api.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        self.api.MonitorFromWindow.restype = wintypes.HANDLE

        class MonitorInfo(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("rcMonitor", wintypes.RECT),
                        ("rcWork", wintypes.RECT), ("dwFlags", wintypes.DWORD)]

        self.MonitorInfo = MonitorInfo
        self.api.GetMonitorInfoW.argtypes = [wintypes.HANDLE, ctypes.POINTER(MonitorInfo)]
        self.api.GetMonitorInfoW.restype = wintypes.BOOL
        self.api.LoadImageW.argtypes = [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
                                       ctypes.c_int, ctypes.c_int, wintypes.UINT]
        self.api.LoadImageW.restype = wintypes.HICON
        self.api.DestroyIcon.argtypes, self.api.DestroyIcon.restype = [wintypes.HICON], wintypes.BOOL
        self.api.IsWindow.argtypes, self.api.IsWindow.restype = [wintypes.HWND], wintypes.BOOL
        self.api.SendMessageW.argtypes = [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM]
        self.api.SendMessageW.restype = ctypes.c_ssize_t
        self._icon_handles = None
        self._icon_key = None
        self._icon_hwnd = None
        self._icon_windows = set()

    def _detach_icons(self, hwnd, handles):
        if not hwnd or not self.api.IsWindow(hwnd):
            return
        for kind, handle in enumerate(handles):
            if self.api.SendMessageW(hwnd, 0x007F, kind, 0) == handle:  # WM_GETICON
                self.api.SendMessageW(hwnd, 0x0080, kind, 0)  # WM_SETICON

    def set_application_icons(self, root, icon_path):
        hwnd = self.handle(root)
        sizes = system_icon_sizes()
        key = (str(Path(icon_path).resolve()), sizes)
        old = self._icon_handles
        if key != self._icon_key:
            loaded = []
            try:
                for width, height in sizes:
                    icon = self.api.LoadImageW(None, key[0], 1, width, height, 0x10)
                    if not icon:
                        raise self.ctypes.WinError(self.ctypes.get_last_error())
                    loaded.append(icon)
            except BaseException:
                for icon in set(loaded):
                    self.api.DestroyIcon(icon)
                raise
            if old:
                for previous_window in self._icon_windows:
                    self._detach_icons(previous_window, old)
                self._icon_windows.clear()
            self._icon_handles = tuple(loaded)
            self._icon_key = key
        # Tk can recreate or reset its wrapper on state transitions. Reassign
        # our physical-size icons after each stable Map, reusing loaded handles.
        for kind, icon in enumerate(self._icon_handles):
            self.api.SendMessageW(hwnd, 0x0080, kind, icon)
        self._icon_hwnd = hwnd
        self._icon_windows.add(hwnd)
        if old and old != self._icon_handles:
            for icon in set(old):
                self.api.DestroyIcon(icon)

    def close_icons(self):
        if not self._icon_handles:
            return
        for hwnd in self._icon_windows:
            self._detach_icons(hwnd, self._icon_handles)
        for icon in set(self._icon_handles):
            self.api.DestroyIcon(icon)
        self._icon_handles = self._icon_key = self._icon_hwnd = None
        self._icon_windows.clear()

    def handle(self, root):
        # Tk can recreate its outer wrapper during a map/state change.
        hwnd = self.api.GetAncestor(root.winfo_id(), 2)
        if not hwnd:
            raise OSError("无法取得窗口句柄")
        return hwnd

    def enable_taskbar(self, root):
        hwnd = self.handle(root)
        old_style = self.get_style(hwnd, -20)  # GWL_EXSTYLE, never GWL_STYLE.
        desired = (old_style | self.APPWINDOW) & ~self.TOOLWINDOW
        if desired == old_style:
            return
        self.ctypes.set_last_error(0)
        previous = self.set_style(hwnd, -20, desired)
        error = self.ctypes.get_last_error()
        if previous == 0 and error:
            raise self.ctypes.WinError(error)

    def work_area(self, root):
        monitor = self.api.MonitorFromWindow(self.handle(root), 2)
        info = self.MonitorInfo()
        info.cbSize = self.ctypes.sizeof(info)
        if not monitor or not self.api.GetMonitorInfoW(monitor, self.ctypes.byref(info)):
            raise self.ctypes.WinError(self.ctypes.get_last_error())
        area = info.rcWork
        return area.left, area.top, area.right, area.bottom


class BorderlessWindow:
    """A borderless Tk window with a normal taskbar entry and safe state changes.

    Call enable() after the root's Map event has settled. Its repeated calls do
    not touch geometry. The custom caption uses minimize()/toggle_maximize();
    maximized dragging calls restore() before its usual Tk movement.
    """

    def __init__(self, root, icon_path=None):
        self.root = root
        self.icon_path = icon_path
        self.maximized = False
        self._normal_geometry = None
        self._shell = None
        root.overrideredirect(True)
        if icon_path is not None and hasattr(root, "bind"):
            root.bind("<Destroy>", self._on_destroy, add="+")
        if sys.platform == "win32" and root.state() == "withdrawn":
            # Materialize Tk's wrapper while it is still hidden, so Explorer
            # sees an application window at its very first visible mapping.
            # This does not run general events or remap an already-visible UI.
            root.update_idletasks()
            self._windows_shell().enable_taskbar(root)
            self._apply_application_icons()

    def _apply_application_icons(self):
        if self.icon_path is not None:
            try:
                self._windows_shell().set_application_icons(self.root, self.icon_path)
            except OSError as exc:
                logging.warning("窗口图标加载失败，保留Tk默认图标：%s", exc)

    def close(self):
        if self._shell is not None:
            self._shell.close_icons()

    def _on_destroy(self, event):
        if event.widget is self.root:
            self.close()

    def _windows_shell(self):
        if self._shell is None:
            self._shell = _WindowsShell()
        return self._shell

    def enable(self):
        # A queued Map callback must never undo an in-progress minimization.
        if self.root.state() != "normal" or not self.root.winfo_ismapped():
            return False
        if not self.root.overrideredirect():
            self.root.overrideredirect(True)
        if sys.platform == "win32":
            self._windows_shell().enable_taskbar(self.root)
            self._apply_application_icons()
        return True

    def minimize(self):
        # Tk rejects iconify while override-redirect is set. Tk also owns the
        # reverse transition, so its wrapper and client always agree on size.
        self.root.overrideredirect(False)
        self.root.iconify()

    def _work_area(self):
        if sys.platform == "win32":
            return self._windows_shell().work_area(self.root)
        return 0, 0, self.root.winfo_screenwidth(), self.root.winfo_screenheight()

    def toggle_maximize(self):
        if self.maximized:
            self.restore()
            return
        self.root.update_idletasks()
        normal = (self.root.winfo_width(), self.root.winfo_height(),
                  self.root.winfo_x(), self.root.winfo_y())
        left, top, right, bottom = self._work_area()
        width, height = right - left, bottom - top
        if width <= 0 or height <= 0:
            raise OSError("显示器可用区域无效")
        self.root.geometry(f"{width}x{height}+{left}+{top}")
        self._normal_geometry = normal
        self.maximized = True

    def restore(self):
        if not self.maximized:
            return
        if self._normal_geometry is not None:
            width, height, x, y = self._normal_geometry
            # '+-120' is an absolute negative coordinate in Tk; '-120' alone
            # instead measures a margin from the right/bottom of the display.
            self.root.geometry(f"{width}x{height}+{x}+{y}")
        self.maximized = False
