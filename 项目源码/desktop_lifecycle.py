"""Current-user startup, one instance per deployment, and a native Windows tray.

Importing this module changes no startup settings and creates no native objects.
The tray thread sends plain actions to the GUI; it never calls Tk directly.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes
from pathlib import Path
import queue
import re
import subprocess
import sys
import threading


RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
APP_ICON_PATH = Path(__file__).resolve().parent / "assets" / "app_icon.ico"


def _profile_name(profile_id: str) -> str:
    if not re.fullmatch(r"[A-Za-z][A-Za-z0-9_-]{0,31}", profile_id):
        raise ValueError("部署编号格式不正确")
    return "MeetingRoomAutoBooking_" + profile_id


def _registry():
    import winreg
    return winreg


class StartupManager:
    """Only set_enabled() writes the current user's Run entry, without elevation."""

    def __init__(self, profile_id: str, entry_path: str | Path | None = None):
        self.value_name = _profile_name(profile_id)
        frozen = bool(getattr(sys, "frozen", False))
        self.entry_path = Path(entry_path or (sys.executable if frozen else sys.argv[0])).resolve()
        if frozen or self.entry_path.suffix.lower() == ".exe":
            command = [str(self.entry_path)]
        else:
            interpreter = Path(sys.executable)
            pythonw = interpreter.with_name("pythonw.exe")
            if pythonw.is_file():
                interpreter = pythonw
            command = [str(interpreter), str(self.entry_path)]
        # Windows list2cmdline quotes spaces and preserves Chinese paths.
        # Startup opens the main window so the user can review their task settings.
        self.command = subprocess.list2cmdline(command)

    def registered_command(self) -> str | None:
        registry = _registry()
        try:
            with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY) as key:
                value, kind = registry.QueryValueEx(key, self.value_name)
                return value if kind == registry.REG_SZ and isinstance(value, str) else None
        except FileNotFoundError:
            return None

    def is_enabled(self) -> bool:
        return self.registered_command() == self.command

    def set_enabled(self, enabled: bool) -> None:
        registry = _registry()
        if enabled:
            with registry.CreateKeyEx(registry.HKEY_CURRENT_USER, RUN_KEY, 0,
                                      registry.KEY_SET_VALUE) as key:
                registry.SetValueEx(key, self.value_name, 0, registry.REG_SZ, self.command)
        else:
            try:
                with registry.OpenKey(registry.HKEY_CURRENT_USER, RUN_KEY, 0,
                                      registry.KEY_SET_VALUE) as key:
                    registry.DeleteValue(key, self.value_name)
            except FileNotFoundError:
                pass


class _WinAPI:
    """Explicit pointer-sized signatures keep callbacks safe on 32/64-bit Python."""

    def __init__(self):
        self.kernel = ctypes.WinDLL("kernel32", use_last_error=True)
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.shell = ctypes.WinDLL("shell32", use_last_error=True)
        self.WNDPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, wintypes.HWND,
                                         wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM)

        class WNDCLASSEX(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.UINT), ("style", wintypes.UINT),
                        ("lpfnWndProc", self.WNDPROC), ("cbClsExtra", ctypes.c_int),
                        ("cbWndExtra", ctypes.c_int), ("hInstance", wintypes.HINSTANCE),
                        ("hIcon", wintypes.HICON), ("hCursor", wintypes.HANDLE),
                        ("hbrBackground", wintypes.HBRUSH), ("lpszMenuName", wintypes.LPCWSTR),
                        ("lpszClassName", wintypes.LPCWSTR), ("hIconSm", wintypes.HICON)]

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        class NOTIFYICONDATA(ctypes.Structure):
            _fields_ = [("cbSize", wintypes.DWORD), ("hWnd", wintypes.HWND),
                        ("uID", wintypes.UINT), ("uFlags", wintypes.UINT),
                        ("uCallbackMessage", wintypes.UINT), ("hIcon", wintypes.HICON),
                        ("szTip", wintypes.WCHAR * 128), ("dwState", wintypes.DWORD),
                        ("dwStateMask", wintypes.DWORD), ("szInfo", wintypes.WCHAR * 256),
                        ("uVersion", wintypes.UINT), ("szInfoTitle", wintypes.WCHAR * 64),
                        ("dwInfoFlags", wintypes.DWORD), ("guidItem", GUID),
                        ("hBalloonIcon", wintypes.HICON)]

        class MSG(ctypes.Structure):
            _fields_ = [("hwnd", wintypes.HWND), ("message", wintypes.UINT),
                        ("wParam", wintypes.WPARAM), ("lParam", wintypes.LPARAM),
                        ("time", wintypes.DWORD), ("pt", wintypes.POINT),
                        ("lPrivate", wintypes.DWORD)]

        self.WNDCLASSEX, self.NOTIFYICONDATA, self.MSG = WNDCLASSEX, NOTIFYICONDATA, MSG
        signatures = [
            (self.kernel, "CreateMutexW", [ctypes.c_void_p, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            (self.kernel, "CreateEventW", [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL, wintypes.LPCWSTR], wintypes.HANDLE),
            (self.kernel, "SetEvent", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel, "WaitForSingleObject", [wintypes.HANDLE, wintypes.DWORD], wintypes.DWORD),
            (self.kernel, "CloseHandle", [wintypes.HANDLE], wintypes.BOOL),
            (self.kernel, "GetModuleHandleW", [wintypes.LPCWSTR], wintypes.HMODULE),
            (self.user, "RegisterClassExW", [ctypes.POINTER(WNDCLASSEX)], wintypes.ATOM),
            (self.user, "UnregisterClassW", [wintypes.LPCWSTR, wintypes.HINSTANCE], wintypes.BOOL),
            (self.user, "CreateWindowExW", [wintypes.DWORD, wintypes.LPCWSTR, wintypes.LPCWSTR,
             wintypes.DWORD, ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.c_int,
             wintypes.HWND, wintypes.HMENU, wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HWND),
            (self.user, "DefWindowProcW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], ctypes.c_ssize_t),
            (self.user, "PostMessageW", [wintypes.HWND, wintypes.UINT, wintypes.WPARAM, wintypes.LPARAM], wintypes.BOOL),
            (self.user, "DestroyWindow", [wintypes.HWND], wintypes.BOOL),
            (self.user, "PostQuitMessage", [ctypes.c_int], None),
            (self.user, "GetMessageW", [ctypes.POINTER(MSG), wintypes.HWND, wintypes.UINT, wintypes.UINT], wintypes.BOOL),
            (self.user, "TranslateMessage", [ctypes.POINTER(MSG)], wintypes.BOOL),
            (self.user, "DispatchMessageW", [ctypes.POINTER(MSG)], ctypes.c_ssize_t),
            (self.user, "LoadIconW", [wintypes.HINSTANCE, ctypes.c_void_p], wintypes.HICON),
            (self.user, "LoadImageW", [wintypes.HINSTANCE, wintypes.LPCWSTR, wintypes.UINT,
             ctypes.c_int, ctypes.c_int, wintypes.UINT], wintypes.HICON),
            (self.user, "GetSystemMetrics", [ctypes.c_int], ctypes.c_int),
            (self.user, "DestroyIcon", [wintypes.HICON], wintypes.BOOL),
            (self.user, "CreatePopupMenu", [], wintypes.HMENU),
            (self.user, "AppendMenuW", [wintypes.HMENU, wintypes.UINT, ctypes.c_size_t, wintypes.LPCWSTR], wintypes.BOOL),
            (self.user, "DestroyMenu", [wintypes.HMENU], wintypes.BOOL),
            (self.user, "GetCursorPos", [ctypes.POINTER(wintypes.POINT)], wintypes.BOOL),
            (self.user, "SetForegroundWindow", [wintypes.HWND], wintypes.BOOL),
            (self.user, "TrackPopupMenu", [wintypes.HMENU, wintypes.UINT, ctypes.c_int,
             ctypes.c_int, ctypes.c_int, wintypes.HWND, ctypes.c_void_p], wintypes.UINT),
            (self.user, "RegisterWindowMessageW", [wintypes.LPCWSTR], wintypes.UINT),
            (self.user, "SetTimer", [wintypes.HWND, ctypes.c_size_t, wintypes.UINT, ctypes.c_void_p], ctypes.c_size_t),
            (self.user, "KillTimer", [wintypes.HWND, ctypes.c_size_t], wintypes.BOOL),
            (self.shell, "Shell_NotifyIconW", [wintypes.DWORD, ctypes.POINTER(NOTIFYICONDATA)], wintypes.BOOL),
        ]
        for library, name, argtypes, restype in signatures:
            function = getattr(library, name)
            function.argtypes, function.restype = argtypes, restype


class DesktopSession:
    """One instance per deployment and interactive Windows session.

    Call acquire before creating the main window. Poll drain_actions on the GUI
    thread. Only hide the window after start_tray returns True.
    """

    def __init__(self, profile_id: str, label: str):
        self.name = _profile_name(profile_id)
        self.label = label
        self.last_error = ""
        self._actions = queue.SimpleQueue()
        self._api = None
        self._mutex = self._event = None
        self._tray = None
        self._closed = False

    def acquire(self) -> bool:
        if self._closed:
            raise RuntimeError("桌面会话已关闭")
        if self._mutex or sys.platform != "win32":
            return True
        self._api = _WinAPI()
        kernel = self._api.kernel
        # Create the event before the mutex so even simultaneous launches cannot
        # lose the second instance's request while the first initializes Tk.
        self._event = kernel.CreateEventW(None, False, False, "Local\\" + self.name + "_Show")
        if not self._event:
            raise ctypes.WinError(ctypes.get_last_error())
        ctypes.set_last_error(0)
        self._mutex = kernel.CreateMutexW(None, False, "Local\\" + self.name + "_Instance")
        error = ctypes.get_last_error()
        if not self._mutex:
            self.close()
            raise ctypes.WinError(error)
        if error == 183:  # ERROR_ALREADY_EXISTS
            kernel.SetEvent(self._event)
            self.close()
            return False
        return True

    @property
    def tray_available(self) -> bool:
        return bool(self._tray and self._tray.available)

    def start_tray(self) -> bool:
        if self._closed:
            return False
        if self.tray_available:
            return True
        if self._tray:
            self._tray.stop()
        if not self._api:
            self.last_error = "系统托盘仅支持 Windows，且需要先取得桌面会话"
            return False
        self._tray = _NativeTray(self)
        return self._tray.start()

    def drain_actions(self) -> list[str]:
        if self._event and self._api.kernel.WaitForSingleObject(self._event, 0) == 0:
            self._actions.put("show")
        actions = []
        while True:
            try:
                actions.append(self._actions.get_nowait())
            except queue.Empty:
                return actions

    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._tray:
            self._tray.stop()
        if self._api:
            for name in ("_event", "_mutex"):
                handle = getattr(self, name)
                if handle:
                    self._api.kernel.CloseHandle(handle)
                    setattr(self, name, None)


class _NativeTray:
    CALLBACK = 0x8001  # WM_APP + 1

    def __init__(self, session: DesktopSession):
        self.session, self.api = session, session._api
        self.hwnd = None
        self.available = False
        self._ready = threading.Event()
        self._stop_requested = threading.Event()
        self._thread = threading.Thread(target=self._run, name="meetingroom-tray", daemon=True)
        self._callback = self.api.WNDPROC(self._window_proc)
        self._data = None
        self._owned_icon = None
        self._retries = 0
        self._taskbar_created = self.api.user.RegisterWindowMessageW("TaskbarCreated")

    def start(self) -> bool:
        self._thread.start()
        if not self._ready.wait(5):
            self.session.last_error = "系统托盘初始化超时"
            self.stop()
            return False
        return self.available

    def stop(self) -> None:
        self._stop_requested.set()
        if self.hwnd:
            self.api.user.PostMessageW(self.hwnd, 0x0010, 0, 0)  # WM_CLOSE
        if self._thread.is_alive() and threading.current_thread() is not self._thread:
            self._thread.join(3)

    def _add_icon(self) -> bool:
        self.available = bool(self.api.shell.Shell_NotifyIconW(0, ctypes.byref(self._data)))
        if self.available:
            self._data.uVersion = 4  # NOTIFYICON_VERSION_4, set after each NIM_ADD.
            self.api.shell.Shell_NotifyIconW(4, ctypes.byref(self._data))
        return self.available

    def _menu(self):
        user = self.api.user
        menu = user.CreatePopupMenu()
        if not menu:
            self.session._actions.put("show")
            return
        try:
            user.AppendMenuW(menu, 0, 1, "打开")
            user.AppendMenuW(menu, 0, 2, "退出")
            point = wintypes.POINT()
            user.GetCursorPos(ctypes.byref(point))
            user.SetForegroundWindow(self.hwnd)
            choice = user.TrackPopupMenu(menu, 0x0100 | 0x0002, point.x, point.y, 0, self.hwnd, None)
            if choice in (1, 2):
                self.session._actions.put("show" if choice == 1 else "exit")
            user.PostMessageW(self.hwnd, 0, 0, 0)  # Let a dismissed menu release focus.
        finally:
            user.DestroyMenu(menu)

    def _window_proc(self, hwnd, message, wparam, lparam):
        try:
            if message == self.CALLBACK:
                event = lparam & 0xFFFF
                if event in (0x0203, 0x0400, 0x0401):  # double-click / NIN_SELECT / NIN_KEYSELECT
                    self.session._actions.put("show")
                elif event in (0x007B, 0x0205):  # WM_CONTEXTMENU / WM_RBUTTONUP
                    self._menu()
                return 0
            if self._taskbar_created and message == self._taskbar_created and self._data is not None:
                self._retries = 5
                if not self._add_icon():
                    self.api.user.SetTimer(hwnd, 1, 1000, None)
                return 0
            if message == 0x0113:  # WM_TIMER: Explorer can take time to restore its tray.
                self._retries -= 1
                if self._add_icon() or self._retries <= 0:
                    self.api.user.KillTimer(hwnd, 1)
                    if not self.available:
                        self.session.last_error = "系统托盘暂不可用，已恢复主窗口"
                        self.session._actions.put("show")
                return 0
            if message == 0x0010:
                self.api.user.DestroyWindow(hwnd)
                return 0
            if message == 0x0002:
                self.api.user.PostQuitMessage(0)
                return 0
        except Exception:
            self.session.last_error = "系统托盘操作失败，已恢复主窗口"
            self.session._actions.put("show")
        return self.api.user.DefWindowProcW(hwnd, message, wparam, lparam)

    def _run(self):
        user, kernel = self.api.user, self.api.kernel
        instance = kernel.GetModuleHandleW(None)
        class_name = self.session.name + "_Tray_" + str(id(self))
        registered = False
        try:
            window_class = self.api.WNDCLASSEX()
            window_class.cbSize = ctypes.sizeof(window_class)
            window_class.lpfnWndProc = self._callback
            window_class.hInstance = instance
            window_class.lpszClassName = class_name
            if not user.RegisterClassExW(ctypes.byref(window_class)):
                raise ctypes.WinError(ctypes.get_last_error())
            registered = True
            # A hidden top-level window receives TaskbarCreated broadcasts;
            # a message-only window would miss Explorer restarts.
            self.hwnd = user.CreateWindowExW(0, class_name, self.session.label, 0,
                                            0, 0, 0, 0, None, None, instance, None)
            if not self.hwnd:
                raise ctypes.WinError(ctypes.get_last_error())
            data = self.api.NOTIFYICONDATA()
            data.cbSize = ctypes.sizeof(data)
            data.hWnd, data.uID = self.hwnd, 1
            data.uFlags = 1 | 2 | 4 | 0x80  # MESSAGE | ICON | TIP | SHOWTIP
            data.uCallbackMessage = self.CALLBACK
            if APP_ICON_PATH.is_file():
                self._owned_icon = user.LoadImageW(None, str(APP_ICON_PATH), 1,
                    max(16, user.GetSystemMetrics(49)), max(16, user.GetSystemMetrics(50)), 0x10)
            # File-loaded icons belong to this thread; the emergency system icon is shared.
            data.hIcon = self._owned_icon or user.LoadIconW(None, ctypes.c_void_p(32512))
            data.szTip = self.session.label[:127]
            self._data = data
            if not data.hIcon or not self._add_icon():
                raise OSError("系统未能创建托盘图标")
            self._ready.set()
            if self._stop_requested.is_set():
                return
            message = self.api.MSG()
            while True:
                result = user.GetMessageW(ctypes.byref(message), None, 0, 0)
                if result == 0:
                    break
                if result == -1:
                    raise ctypes.WinError(ctypes.get_last_error())
                user.TranslateMessage(ctypes.byref(message))
                user.DispatchMessageW(ctypes.byref(message))
        except Exception as exc:
            self.session.last_error = f"系统托盘不可用：{exc}"
            self.session._actions.put("show")
        finally:
            self.available = False
            if self._data is not None:
                self.api.shell.Shell_NotifyIconW(2, ctypes.byref(self._data))
            if self._owned_icon:
                user.DestroyIcon(self._owned_icon)
                self._owned_icon = None
            if self.hwnd:
                user.DestroyWindow(self.hwnd)
                self.hwnd = None
            if registered:
                user.UnregisterClassW(class_name, instance)
            self._ready.set()
