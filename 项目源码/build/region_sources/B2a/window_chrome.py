"""Tk owns the window frame and geometry; Win32 only supplies shell metadata.

Changing WS_CAPTION behind Tk's back leaves its frame-size bookkeeping stale.
Use Tk's supported borderless mode instead, and keep taskbar integration separate
from sizing, movement, and painting.
"""
from __future__ import annotations

import sys


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

    def __init__(self, root):
        self.root = root
        self.maximized = False
        self._normal_geometry = None
        self._shell = None
        root.overrideredirect(True)
        if sys.platform == "win32" and root.state() == "withdrawn":
            # Materialize Tk's wrapper while it is still hidden, so Explorer
            # sees an application window at its very first visible mapping.
            # This does not run general events or remap an already-visible UI.
            root.update_idletasks()
            self._windows_shell().enable_taskbar(root)

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
