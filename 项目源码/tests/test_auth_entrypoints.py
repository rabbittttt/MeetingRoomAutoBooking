"""Verify that denied accounts never reach configuration or a booking browser."""
import asyncio
import json
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

import booking_core as core
import 会议室自动抢订_GUI as gui
from gui_diagnostics import GuiDiagnostics


class AccountEntryTests(unittest.TestCase):
    def test_gui_denial_never_constructs_working_window(self):
        root = Mock()
        with patch.object(sys, "argv", ["booking.exe"]), \
             patch.object(gui.tk, "Tk", return_value=root), \
             patch.object(core, "require_authorized_account", side_effect=core.AuthorizationError("denied")), \
             patch.object(gui, "BookingGUI") as booking_gui, \
             patch.object(gui.messagebox, "showerror") as show_error, \
             patch("gui_diagnostics.GuiDiagnostics"):
            self.assertEqual(gui.main(), 2)
            booking_gui.assert_not_called()
            root.mainloop.assert_not_called()
            root.destroy.assert_called_once()
            show_error.assert_called_once()

    def test_start_denial_never_reads_configuration_or_starts_worker(self):
        app = SimpleNamespace(is_running=Mock(return_value=False), root=Mock(),
                              get_config=Mock())
        with patch.object(core, "require_authorized_account", side_effect=core.AuthorizationError("denied")), \
             patch.object(gui.messagebox, "showerror") as show_error:
            gui.BookingGUI.start_run(app)
        app.get_config.assert_not_called()
        show_error.assert_called_once()

    def test_headless_core_denial_never_creates_browser(self):
        with patch.object(core, "setup_logging"), \
             patch.object(core, "require_authorized_account", side_effect=core.AuthorizationError("denied")), \
             patch.object(core, "async_playwright") as browser:
            with self.assertRaises(core.AuthorizationError):
                asyncio.run(core.main("watch"))
        browser.assert_not_called()

    def test_authorization_diagnostic_accepts_validated_identity(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "result.json"
            with patch.object(core, "require_authorized_account", return_value="z00523094"):
                self.assertEqual(gui.check_account_to_file(marker), 0)
            self.assertEqual(json.loads(marker.read_text(encoding="utf-8")),
                             {"authorized": True, "account": "z00523094"})

    def test_authorization_diagnostic_returns_denial(self):
        with tempfile.TemporaryDirectory() as folder:
            marker = Path(folder) / "result.json"
            with patch.object(core, "require_authorized_account", side_effect=core.AuthorizationError("denied")):
                self.assertEqual(gui.check_account_to_file(marker), 2)
            self.assertFalse(json.loads(marker.read_text(encoding="utf-8"))["authorized"])


class DiagnosticsTests(unittest.TestCase):
    def test_callback_trace_is_retained_and_popup_is_not_repeated(self):
        original = sys.excepthook
        with tempfile.TemporaryDirectory() as folder, \
             patch("gui_diagnostics.faulthandler.is_enabled", return_value=False), \
             patch("gui_diagnostics.faulthandler.enable") as enable, \
             patch("gui_diagnostics.faulthandler.disable") as disable, \
             patch("tkinter.messagebox.showerror") as popup:
            diagnostics = GuiDiagnostics(Path(folder))
            root = Mock()
            diagnostics.attach(root)
            try:
                try:
                    raise ValueError("callback detail")
                except ValueError:
                    root.report_callback_exception(*sys.exc_info())
                    root.report_callback_exception(*sys.exc_info())
                saved = diagnostics.path.read_text(encoding="utf-8")
                self.assertIn("Traceback", saved)
                self.assertIn("callback detail", saved)
                popup.assert_called_once()
                enable.assert_called_once()
            finally:
                diagnostics.close()
                diagnostics.close()
            disable.assert_called_once()
        self.assertIs(sys.excepthook, original)

    def test_existing_fault_handler_is_not_disabled(self):
        with tempfile.TemporaryDirectory() as folder, \
             patch("gui_diagnostics.faulthandler.is_enabled", return_value=True), \
             patch("gui_diagnostics.faulthandler.enable") as enable, \
             patch("gui_diagnostics.faulthandler.disable") as disable:
            diagnostics = GuiDiagnostics(Path(folder))
            diagnostics.close()
            enable.assert_not_called()
            disable.assert_not_called()


if __name__ == "__main__":
    unittest.main()
