"""Window movement regressions; no browser or reservation is opened."""
from pathlib import Path
import subprocess
import sys
import tempfile
from types import SimpleNamespace
import unittest
from unittest.mock import Mock

import 会议室自动抢订_GUI as gui


class FakeRoot:
    def __init__(self, *, zoomed=False, x=100, y=80):
        self.x, self.y = x, y
        self.width, self.height = (1920, 1080) if zoomed else (1110, 655)
        self.current_state = "zoomed" if zoomed else "normal"
        self.geometries = []

    def winfo_x(self): return self.x
    def winfo_y(self): return self.y
    def winfo_rootx(self): return self.x + 8
    def winfo_rooty(self): return self.y + 8
    def winfo_width(self): return self.width
    def winfo_height(self): return self.height
    def update_idletasks(self): pass

    def state(self, value=None):
        if value is not None:
            self.current_state = value
            if value == "normal":
                self.width, self.height = 1110, 655
        return self.current_state

    def geometry(self, value):
        self.geometries.append(value)


def event(x, y):
    return SimpleNamespace(x_root=x, y_root=y)


class WindowDragTests(unittest.TestCase):
    def app(self, **kwargs):
        app = gui.BookingGUI.__new__(gui.BookingGUI)
        app.root = FakeRoot(**kwargs)
        app._window_drag = None
        app._schedule_window_frame = Mock()
        return app

    def test_plain_click_does_not_move_or_restore(self):
        for zoomed in (False, True):
            with self.subTest(zoomed=zoomed):
                app = self.app(zoomed=zoomed)
                app._start_window_drag(event(200, 100))
                app._end_window_drag()
                self.assertFalse(app.root.geometries)
                self.assertEqual(app.root.state(), "zoomed" if zoomed else "normal")
                self.assertIsNone(app._window_drag)

    def test_jitter_does_not_start_drag(self):
        app = self.app()
        app._start_window_drag(event(200, 100))
        app._drag_window(event(202, 102))
        self.assertFalse(app.root.geometries)

    def test_repeated_motion_uses_same_origin(self):
        app = self.app()
        app._start_window_drag(event(200, 100))
        app._drag_window(event(240, 125))
        app._drag_window(event(260, 135))
        self.assertEqual(app.root.geometries, ["+140+105", "+160+115"])

    def test_negative_coordinates_keep_absolute_plus_separator(self):
        app = self.app(x=-50, y=-20)
        app._start_window_drag(event(100, 100))
        app._drag_window(event(20, 70))
        self.assertEqual(app.root.geometries, ["+-130+-50"])

    def test_release_prevents_late_motion(self):
        app = self.app()
        app._start_window_drag(event(200, 100))
        app._drag_window(event(240, 125))
        app._end_window_drag()
        app._drag_window(event(260, 135))
        self.assertEqual(len(app.root.geometries), 1)

    def test_maximized_drag_restores_at_pointer_ratio_then_moves(self):
        app = self.app(zoomed=True, x=0, y=0)
        app._start_window_drag(event(968, 20))
        app._drag_window(event(1000, 60))
        self.assertEqual(app.root.state(), "normal")
        self.assertEqual(app.root.geometries, ["+437+40"])
        app._drag_window(event(1030, 80))
        self.assertEqual(app.root.geometries[-1], "+467+60")
        app._schedule_window_frame.assert_called_once()

    def test_maximize_clears_pending_drag(self):
        app = self.app()
        app._start_window_drag(event(200, 100))
        app._toggle_maximize()
        self.assertIsNone(app._window_drag)
        self.assertEqual(app.root.state(), "zoomed")

    def test_window_frame_has_no_native_move_entry(self):
        self.assertFalse(hasattr(gui.WindowsWindowFrame, "start_move"))
        import inspect
        source = inspect.getsource(gui.WindowsWindowFrame)
        self.assertNotIn("SendMessage", source)
        self.assertNotIn("PostMessage", source)
        self.assertNotIn("ReleaseCapture", source)


@unittest.skipUnless(sys.platform == "win32", "Windows Tk movement regression")
class ActualTkDragTests(unittest.TestCase):
    def test_real_tk_events_in_fresh_process(self):
        folder = Path(gui.__file__).resolve().parent
        with tempfile.TemporaryDirectory(prefix="booking_window_drag_") as temp:
            marker = Path(temp) / "passed.txt"
            completed = subprocess.run(
                [sys.executable, "-B", "-X", "utf8", str(folder / "会议室自动抢订_GUI.py"),
                 "--self-test", str(marker)],
                cwd=folder, capture_output=True, text=True, encoding="utf-8", timeout=45)
            self.assertEqual(completed.returncode, 0, completed.stdout + completed.stderr)
            self.assertEqual(marker.read_text(encoding="ascii"), "gui-core-config-ok")


if __name__ == "__main__":
    unittest.main(verbosity=2)
