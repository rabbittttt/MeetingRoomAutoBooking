"""Half-hour target validation and local Chrome grid regressions.

All network is blocked. The user confirmed a 30-minute booking needs one grid
click; the synthetic click handlers below test that requirement and are not
evidence about unrecorded company duration-dropdown or final-form DOM.
"""
import asyncio
from contextlib import ExitStack
from datetime import date
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core
from tests.test_grid_regressions import ROOMS, fixture_html


CASES = (
    ("10:00", "10:30", 30, (21, 21)),
    ("10:00", "11:30", 90, (21, 23)),
    ("10:00", "12:30", 150, (21, 25)),
)


class HalfHourTargetTests(unittest.TestCase):
    def setUp(self):
        self.day = date(2026, 10, 26)
        self.rooms = [core.RoomSpec("F5", "7F-A05R", 1)]
        self.area_rules = patch.object(core, "AREA_ADVANCE_DAYS", {"F5": 7})
        self.area_rules.start()
        self.addCleanup(self.area_rules.stop)

    def test_targets_accept_30_90_and_150_minutes_without_rounding(self):
        for start, end, minutes, columns in CASES:
            with self.subTest(minutes=minutes):
                target = core.Target(self.day, start, end, ("F5",))
                self.assertEqual((target.start, target.end), (start, end))
                self.assertEqual(core.parse_clock(target.end) - core.parse_clock(target.start), minutes)
                self.assertEqual(core.cell_columns(target.start, target.end), columns)

    def test_quarter_hour_and_empty_or_reversed_slots_remain_invalid(self):
        for start, end in (("10:00", "10:15"), ("10:15", "10:30"),
                           ("10:00", "10:00"), ("10:30", "10:00")):
            with self.subTest(start=start, end=end), self.assertRaises(ValueError):
                core.Target(self.day, start, end, ("F5",))

    def test_single_and_watch_targets_preserve_half_hour_durations(self):
        raw = [{"date": self.day.isoformat(), "start": start, "end": end, "areas": ["F5"]}
               for start, end, _, _ in CASES]
        for label in ("到点抢订", "候补"):
            with self.subTest(label=label):
                targets = core.parse_targets(raw, label, rooms=self.rooms)
                self.assertEqual([(target.start, target.end) for target in targets],
                                 [(start, end) for start, end, _, _ in CASES])
                self.assertTrue(all(target.day == self.day and target.areas == ("F5",)
                                    for target in targets))

    def test_weekly_rules_and_expansion_preserve_half_hour_durations(self):
        raw = [{"weekday": 1, "start": start, "end": end, "areas": ["F5"]}
               for start, end, _, _ in CASES]
        rules = core.parse_weekly_rules(raw, rooms=self.rooms)
        self.assertEqual(rules, raw)
        targets = core.expand_weekly_targets(rules, self.day, horizon_days=8)
        expected = [(day, start, end, ("F5",))
                    for day in (self.day, date(2026, 11, 2))
                    for start, end, _, _ in CASES]
        self.assertEqual([(target.day, target.start, target.end, target.areas)
                          for target in targets], expected)


class HalfHourGridBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.stack.enter_context(patch.object(core, "save_grid_diagnostic", AsyncMock()))
        self.stack.enter_context(patch.object(core, "ACTION_TIMEOUT_MS", 2000))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 800})
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()

    async def asyncTearDown(self):
        try:
            await self.browser.close()
            await self.pw.stop()
        finally:
            self.stack.close()

    async def load_grid(self, expected_columns):
        await self.page.set_content(fixture_html(redraw=True, header=True))
        await self.page.evaluate("""(expected) => {
            const originalClick=window.cellClicked;
            window.cellClicked=cell=>{
                originalClick(cell);
                const selected=window.selected[Number(cell.dataset.row)]||[];
                document.getElementById('confirm').disabled=!(selected.length===expected.length &&
                    selected.every((column,index)=>column===expected[index]));
            };
        }""", list(expected_columns))

    async def test_90_and_150_minutes_select_exact_endpoints_after_row_redraw(self):
        cases = (("10:00", "11:30", (21, 23)), ("10:00", "12:30", (21, 25)),
                 ("10:30", "12:00", (22, 24)), ("10:30", "13:00", (22, 26)))
        for start, end, columns in cases:
            with self.subTest(start=start, end=end):
                await self.load_grid(columns)
                target = core.Target(date(2030, 1, 3), start, end, ("F5-1A",))
                await core.choose_room_cells(self.page, ROOMS[2], 3, target)
                self.assertEqual(await self.page.evaluate("window.clicked"),
                                 [f"room3-{column}" for column in columns])
                self.assertTrue(await self.page.locator("#confirm").is_enabled())
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_30_minutes_clicks_its_single_cell_once_and_leaves_confirmation_ready(self):
        for start, end, column in (("10:00", "10:30", 21), ("10:30", "11:00", 22)):
            with self.subTest(start=start, end=end):
                await self.load_grid((column,))
                target = core.Target(date(2030, 1, 3), start, end, ("F5-1A",))
                await core.choose_room_cells(self.page, ROOMS[2], 3, target)
                self.assertEqual(await self.page.evaluate("window.clicked"), [f"room3-{column}"])
                self.assertTrue(await self.page.locator("#confirm").is_enabled())
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)


if __name__ == "__main__":
    unittest.main()
