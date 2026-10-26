"""Date-selector errors must retain their intended stop scope; no browser is used."""
import asyncio
from contextlib import ExitStack
from datetime import date, datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import booking_core as core


class _DatePage:
    def __init__(self):
        self.url = "about:blank"
        self.goto = AsyncMock()
        self.button = SimpleNamespace(wait_for=AsyncMock(), click=AsyncMock())

    def get_by_role(self, *args, **kwargs):
        return self.button


class DateErrorTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.now = datetime(2030, 10, 18, 8, 0, tzinfo=core.CN_TZ)
        self.target = core.Target(self.now.date() + timedelta(days=7), "14:00", "16:00", ("F5",))
        self.room = core.RoomSpec("F5", "7F-A05R", 1)
        self.page = _DatePage()
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.now))
        self.stack.enter_context(patch.object(core, "DRY_RUN", False))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "GRID_SELECTOR_BLOCKED", set()))
        self.stack.enter_context(patch.object(core, "activate_booking_page", AsyncMock()))
        self.stack.enter_context(patch.object(core, "is_403_page", AsyncMock(return_value=False)))
        self.stack.enter_context(patch.object(core, "save_debug", AsyncMock()))
        self.calendar_selection = self.stack.enter_context(patch.object(
            core, "select_booking_date",
            AsyncMock(side_effect=core.DatePickerStructureError("当前日历月份证据不唯一"))))
        self.after_date = {}
        for name in ("select_start_time", "select_duration", "open_room_list", "verify_room_identity",
                     "choose_room_cells", "wait_selection_confirmation_ready", "confirm_room_selection"):
            self.after_date[name] = self.stack.enter_context(patch.object(core, name, AsyncMock(return_value=True)))

    def assert_no_room_selection_or_submission(self):
        for name, operation in self.after_date.items():
            with self.subTest(operation=name):
                operation.assert_not_awaited()
        self.page.button.click.assert_not_awaited()

    async def test_ambiguous_month_control_preserves_selector_configuration_error(self):
        with self.assertRaisesRegex(core.SelectorConfigurationError, "当前日历月份证据不唯一"):
            await core.select_date(self.page, self.target.day)
        self.calendar_selection.assert_awaited_once()
        self.assert_no_room_selection_or_submission()

    async def test_prepare_stops_globally_on_ambiguous_date_without_selecting_room(self):
        self.assertIs(await core.prepare_page(self.page, self.room, self.target), False)
        self.assertTrue(core.CONFIG_BLOCKED.is_set())
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        self.assertFalse(core.AUTH_BLOCKED.is_set())
        self.assertFalse(core.DRIVER_LOST.is_set())
        self.assert_no_room_selection_or_submission()

    async def test_unsupported_year_only_blocks_current_target_and_room(self):
        target = core.Target(date(self.now.year + 1, 1, 8), "14:00", "16:00", ("F5",))
        self.assertIs(await core.prepare_page(self.page, self.room, target), False)
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, {(target.key, self.room.name)})
        self.assertFalse(core.CONFIG_BLOCKED.is_set())
        self.assertFalse(core.AUTH_BLOCKED.is_set())
        self.assertFalse(core.DRIVER_LOST.is_set())
        self.assertFalse(core.STOP_REQUESTED.is_set())
        self.calendar_selection.assert_not_awaited()
        self.assert_no_room_selection_or_submission()


if __name__ == "__main__":
    unittest.main()
