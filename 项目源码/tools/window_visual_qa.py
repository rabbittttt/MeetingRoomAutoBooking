"""Offline visual QA of this application's own Tk window on Windows.

Creates a fresh, dry-run configuration without reading the user's saved settings.
Never starts the booking worker or a browser. Captures only an HWND owned by this
process, using its window DC; it never captures the desktop or another app.
"""
from __future__ import annotations

import argparse
import copy
import ctypes
from ctypes import wintypes
import importlib.util
import json
import os
from pathlib import Path
import struct
import sys
import time
import traceback
import zlib


def save_png(path: Path, width: int, height: int, bgra: bytes) -> dict:
    rows = []
    black_count = bright_count = max_black_run = black_rows = 0
    black_columns = [0] * width
    colors = set()
    for y in range(height):
        rgb = bytearray(width * 3)
        consecutive = row_black = 0
        for x in range(width):
            pos = (y * width + x) * 4
            b, g, r = bgra[pos:pos + 3]
            rgb[x * 3:x * 3 + 3] = bytes((r, g, b))
            is_black = max(r, g, b) < 8
            if is_black:
                black_count += 1
                row_black += 1
                black_columns[x] += 1
                consecutive += 1
                max_black_run = max(max_black_run, consecutive)
            else:
                consecutive = 0
            bright_count += min(r, g, b) > 180
            if len(colors) < 257:
                colors.add((r, g, b))
        black_rows += row_black >= width * 0.95
        rows.append(b"\0" + rgb)

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (struct.pack(">I", len(data)) + tag + data
                + struct.pack(">I", zlib.crc32(tag + data) & 0xffffffff))

    png = (b"\x89PNG\r\n\x1a\n"
           + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
           + chunk(b"IDAT", zlib.compress(b"".join(rows), 6))
           + chunk(b"IEND", b""))
    path.write_bytes(png)
    pixels = width * height
    return {
        "png": path.name, "width": width, "height": height,
        "near_black_pixels": black_count, "near_black_ratio": black_count / pixels,
        "bright_ratio": bright_count / pixels, "sampled_distinct_colors_at_least": len(colors),
        "longest_horizontal_black_run": max_black_run,
        "mostly_black_rows": black_rows,
        "mostly_black_columns": sum(n >= height * 0.95 for n in black_columns),
        "capture_supported": bright_count / pixels > 0.60 and len(colors) > 16,
        "no_large_black_border": (black_count / pixels < 0.01
                                   and black_rows < 3
                                   and sum(n >= height * 0.95 for n in black_columns) < 3),
    }


class OwnWindowCapture:
    def __init__(self):
        self.user = ctypes.WinDLL("user32", use_last_error=True)
        self.gdi = ctypes.WinDLL("gdi32", use_last_error=True)
        self.user.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        self.user.GetAncestor.restype = wintypes.HWND
        self.user.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
        self.user.GetWindowThreadProcessId.restype = wintypes.DWORD
        self.user.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetWindowRect.restype = wintypes.BOOL
        self.user.GetClientRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        self.user.GetClientRect.restype = wintypes.BOOL
        self.user.GetWindowDC.argtypes = [wintypes.HWND]
        self.user.GetWindowDC.restype = wintypes.HDC
        self.user.ReleaseDC.argtypes = [wintypes.HWND, wintypes.HDC]
        self.user.ReleaseDC.restype = ctypes.c_int
        self.gdi.CreateCompatibleDC.argtypes = [wintypes.HDC]
        self.gdi.CreateCompatibleDC.restype = wintypes.HDC
        self.gdi.DeleteDC.argtypes = [wintypes.HDC]
        self.gdi.DeleteDC.restype = wintypes.BOOL
        self.gdi.SelectObject.argtypes = [wintypes.HDC, wintypes.HGDIOBJ]
        self.gdi.SelectObject.restype = wintypes.HGDIOBJ
        self.gdi.DeleteObject.argtypes = [wintypes.HGDIOBJ]
        self.gdi.DeleteObject.restype = wintypes.BOOL
        self.gdi.BitBlt.argtypes = [wintypes.HDC, ctypes.c_int, ctypes.c_int, ctypes.c_int,
                                   ctypes.c_int, wintypes.HDC, ctypes.c_int, ctypes.c_int,
                                   wintypes.DWORD]
        self.gdi.BitBlt.restype = wintypes.BOOL
        self.gdi.CreateDIBSection.argtypes = [wintypes.HDC, ctypes.c_void_p, wintypes.UINT,
                                            ctypes.POINTER(ctypes.c_void_p), wintypes.HANDLE,
                                            wintypes.DWORD]
        self.gdi.CreateDIBSection.restype = wintypes.HBITMAP

    def capture(self, root, path: Path) -> dict:
        hwnd = self.user.GetAncestor(root.winfo_id(), 2)
        pid = wintypes.DWORD()
        self.user.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not hwnd or pid.value != os.getpid():
            raise RuntimeError("Capture rejected: HWND is not owned by the QA process")
        outer, client = wintypes.RECT(), wintypes.RECT()
        if not self.user.GetWindowRect(hwnd, ctypes.byref(outer)):
            raise ctypes.WinError(ctypes.get_last_error())
        if not self.user.GetClientRect(hwnd, ctypes.byref(client)):
            raise ctypes.WinError(ctypes.get_last_error())
        width, height = outer.right - outer.left, outer.bottom - outer.top
        if not 10 <= width <= 8192 or not 10 <= height <= 8192:
            raise RuntimeError(f"Invalid own-window size: {width} x {height}")
        # BITMAPINFOHEADER, top-down 32-bit BI_RGB; capture stays inside this HWND.
        info = struct.pack("<IiiHHIIiiII", 40, width, -height, 1, 32, 0,
                           width * height * 4, 0, 0, 0, 0)
        info_buf = ctypes.create_string_buffer(info)
        dc = self.user.GetWindowDC(hwnd)
        memory = self.gdi.CreateCompatibleDC(dc) if dc else None
        bitmap = previous = None
        bits = ctypes.c_void_p()
        try:
            if not dc or not memory:
                raise ctypes.WinError(ctypes.get_last_error())
            bitmap = self.gdi.CreateDIBSection(dc, info_buf, 0, ctypes.byref(bits), None, 0)
            if not bitmap or not bits.value:
                raise ctypes.WinError(ctypes.get_last_error())
            previous = self.gdi.SelectObject(memory, bitmap)
            if not self.gdi.BitBlt(memory, 0, 0, width, height, dc, 0, 0, 0x00CC0020):
                raise ctypes.WinError(ctypes.get_last_error())
            result = save_png(path, width, height, ctypes.string_at(bits, width * height * 4))
        finally:
            if previous:
                self.gdi.SelectObject(memory, previous)
            if bitmap:
                self.gdi.DeleteObject(bitmap)
            if memory:
                self.gdi.DeleteDC(memory)
            if dc:
                self.user.ReleaseDC(hwnd, dc)
        tk_size = [root.winfo_width(), root.winfo_height()]
        client_size = [client.right - client.left, client.bottom - client.top]
        result.update({"owner_pid_verified": pid.value, "tk_size": tk_size,
                       "window_rect": [outer.left, outer.top, outer.right, outer.bottom],
                       "client_size": client_size,
                       "outer_client_tk_sizes_match": [width, height] == client_size == tk_size})
        return result


def run(output: Path) -> dict:
    if sys.platform != "win32":
        raise RuntimeError("This visual QA requires a Windows desktop session")
    import tkinter as tk
    source = Path(__file__).resolve().parents[1]
    sys.path.insert(0, str(source))
    spec = importlib.util.spec_from_file_location("booking_gui_visual_qa", source / "会议室自动抢订_GUI.py")
    gui = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(gui)
    config = copy.deepcopy(gui.DEFAULT_CONFIG)
    config.update({"dry_run": True, "mode": "watch", "release_targets": [],
                   "weekly_release_rules": [], "watch_targets": []})
    root = tk.Tk()
    root.withdraw()
    errors = []
    root.report_callback_exception = lambda *error: errors.append("".join(traceback.format_exception(*error)))
    app = gui.BookingGUI(root, config_override=config)
    root.title("会议室自动抢订 · 离线窗口视觉测试")
    output.mkdir(parents=True, exist_ok=True)
    result = {"scope": "own QA window only; fresh dry-run config; no booking or browser",
              "captures": [], "checks": [], "callback_errors": errors}
    capture = OwnWindowCapture()

    def settle(milliseconds=180):
        end = time.monotonic() + milliseconds / 1000
        while time.monotonic() < end:
            root.update()
            time.sleep(0.01)
        root.update_idletasks()

    def check(name, passed, **detail):
        result["checks"].append({"name": name, "passed": bool(passed), **detail})

    def snap(name):
        settle()
        root.lift()
        root.update()
        result["captures"].append(capture.capture(root, output / name))

    event_time = 100000

    def emit(widget, kind, *, x, y, rootx, rooty):
        nonlocal event_time
        event_time += 1000  # prevent synthetic movements becoming double-clicks
        widget.event_generate(kind, x=x, y=y, rootx=rootx, rooty=rooty,
                              state=0x100 if kind == "<B1-Motion>" else 0, time=event_time)
        root.update()

    try:
        root.deiconify()
        root.geometry("+60+40")
        settle(300)
        normal_size = [root.winfo_width(), root.winfo_height()]
        snap("01_normal_own_window.png")
        for iteration in range(18):
            surface = app.titlebar
            px, py = surface.winfo_rootx() + 45, surface.winfo_rooty() + 14
            origin = (root.winfo_x(), root.winfo_y())
            emit(surface, "<ButtonPress-1>", x=45, y=14, rootx=px, rooty=py)
            for delta in ((12, 5), (33, 11), (-8, 14), (0, 0)):
                emit(surface, "<B1-Motion>", x=45, y=14,
                     rootx=px + delta[0], rooty=py + delta[1])
            emit(surface, "<ButtonRelease-1>", x=45, y=14, rootx=px, rooty=py)
            check(f"rapid_drag_{iteration + 1}",
                  [root.winfo_width(), root.winfo_height()] == normal_size
                  and (root.winfo_x(), root.winfo_y()) == origin
                  and app._window_drag is None)
        snap("02_after_18_rapid_drags_own_window.png")

        app._minimize_window()
        settle()
        check("minimize", root.state() == "iconic", tk_state=root.state())
        root.deiconify()
        settle(300)
        check("restore_from_taskbar", root.state() == "normal" and bool(root.overrideredirect()))
        snap("03_after_minimize_restore_own_window.png")

        app._toggle_maximize()
        settle(250)
        check("maximize", app._is_maximized())
        snap("04_maximized_own_window.png")
        app._toggle_maximize()
        settle()
        check("restore_normal_size", not app._is_maximized()
              and [root.winfo_width(), root.winfo_height()] == normal_size)

        grip = app._resize_grip
        width, height = root.winfo_width(), root.winfo_height()
        gx, gy = max(1, grip.winfo_width() - 3), max(1, grip.winfo_height() - 3)
        px, py = grip.winfo_rootx() + gx, grip.winfo_rooty() + gy
        emit(grip, "<ButtonPress-1>", x=gx, y=gy, rootx=px, rooty=py)
        emit(grip, "<B1-Motion>", x=gx + 48, y=gy + 32, rootx=px + 48, rooty=py + 32)
        emit(grip, "<ButtonRelease-1>", x=gx + 48, y=gy + 32, rootx=px + 48, rooty=py + 32)
        settle()
        actual = [root.winfo_width(), root.winfo_height()]
        check("sizegrip_real_tk_binding", actual == [width + 48, height + 32],
              before=[width, height], expected=[width + 48, height + 32], actual=actual)
        snap("05_after_sizegrip_resize_own_window.png")
        check("booking_worker_not_started", app.thread is None and app.worker_loop is None)
        check("no_tk_callback_errors", not errors)
        supported = all(item["capture_supported"] for item in result["captures"])
        result["status"] = ("inconclusive_capture_unsupported" if not supported else
                            "passed" if all(item["passed"] for item in result["checks"])
                            and all(item["no_large_black_border"]
                                    and item["outer_client_tk_sizes_match"] for item in result["captures"])
                            else "failed")
    except Exception:
        result["status"] = "failed"
        result["exception"] = traceback.format_exc()
    finally:
        root.destroy()
        (output / "window_visual_qa.json").write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, required=True)
    arguments = parser.parse_args()
    report = run(arguments.output)
    print(json.dumps({"status": report["status"], "captures": len(report["captures"]),
                      "failed_checks": [item for item in report["checks"] if not item["passed"]],
                      "exception": report.get("exception")}, ensure_ascii=False))
    raise SystemExit(0 if report["status"] == "passed" else 1)
