"""Selected-room matching on a synthetic 36-row local page; network is blocked.

The four full names and physical rows 22-25 were supplied by the user. Other
room names and the nonconsecutive stress layout are synthetic fixture data.
The actual B2a production profile supplies the match rules and relative rows.
"""
import asyncio
from contextlib import ExitStack
from datetime import date
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core
import deployment_profiles as profiles
from tests import test_grid_regressions as grid_fixture


AREA = "B2a"
SELECTED = ("2-A48R", "2-A50R", "2-B35R", "2-B41R")
CONFIRMED_ROWS = (22, 23, 24, 25)
STRESS_ROWS = (5, 8, 15, 20)
RELATIVE_ROWS = dict(zip(SELECTED, range(1, 5)))


def selected_rooms_html(*, actual_rows=CONFIRMED_ROWS, swap=False, missing=None,
                        suffix_replacement=False):
    # These 32 filler names deliberately make no claim about the company list.
    names = [f"2-X{number:02d}R" for number in range(1, 37)]
    for name, actual_row in zip(SELECTED, actual_rows):
        names[actual_row - 1] = name
    names[actual_rows[0] - 2] = "2-A48R2"  # Similar non-target above actual A48R.
    if swap:
        names[actual_rows[0] - 1], names[actual_rows[1] - 1] = (
            names[actual_rows[1] - 1], names[actual_rows[0] - 1])
    if missing is not None:
        names[names.index(missing)] = "2-X99R"
    if suffix_replacement:
        names[names.index("2-A48R")] = "2-A48R2"
    with patch.object(grid_fixture, "ROOMS", tuple(names)):
        html = grid_fixture.fixture_html(redraw=True, header=True)
    return (html.replace("CN-ShenZhen-F5-", "CN-HQ-B2a-")
            .replace("height:500px", "height:1050px")
            .replace("height:450px", "height:1000px"))


class SelectedRoomsProfileBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        selected_profile = profiles.get_profile("B2a")
        catalog = selected_profile["room_catalog"]
        self.relative_rows = catalog[AREA]["rooms"]
        self.assertEqual(list(self.relative_rows.items()), list(RELATIVE_ROWS.items()))
        self.assertEqual(selected_profile["expected_counts"], {AREA: 4})
        self.assertEqual(selected_profile["release_open_at"], "08:30:00")
        self.assertEqual(catalog[AREA]["advance_days"], 7)
        rows = {area: dict(config["rooms"]) for area, config in catalog.items()}
        for name, value in (
            ("DEPLOYMENT_PROFILE", selected_profile),
            ("ROOM_CATALOG", selected_profile["room_catalog"]),
            ("AREA_ROOM_ROWS", rows),
            ("AREA_LABELS", {area: config["label"] for area, config in catalog.items()}),
            ("AREA_EXPECTED_SHORT_NAMES", {area: tuple(names) for area, names in rows.items()}),
            ("ROOM_FULL_NAME_PATTERN", selected_profile["full_name_pattern"]),
            ("ROOM_SHORT_NAME_PATTERN", selected_profile["short_name_pattern"]),
            ("DRY_RUN", False),
            ("ACTION_TIMEOUT_MS", 2000),
        ):
            self.stack.enter_context(patch.object(core, name, value))
        self.stack.enter_context(patch.object(core, "save_grid_diagnostic", AsyncMock()))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 1200})
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        self.target = core.Target(date(2030, 1, 3), "10:00", "12:00", (AREA,))

    async def asyncTearDown(self):
        try:
            await self.browser.close()
            await self.pw.stop()
        finally:
            self.stack.close()

    async def select_room(self, name):
        room = core.RoomSpec(AREA, name, self.relative_rows[name])
        await core.verify_room_identity(self.page, room)
        await core.choose_room_cells(self.page, room.name, room.row, self.target)

    async def assert_no_grid_clicks(self):
        self.assertEqual(await self.page.evaluate("window.clicked"), [])
        self.assertFalse(await self.page.locator("#confirm").is_enabled())

    async def assert_rooms_selected_at(self, actual_rows):
        for name, actual_row in zip(SELECTED, actual_rows):
            with self.subTest(name=name, relative_row=RELATIVE_ROWS[name], actual_row=actual_row):
                await self.page.set_content(selected_rooms_html(actual_rows=actual_rows))
                self.assertEqual(await self.page.locator(".room-name").count(), 36)
                observed = await core.read_visible_room_names(self.page)
                self.assertEqual([room["name"] for room in observed],
                                 [f"CN-HQ-B2a-{short}" for short in SELECTED])
                self.assertTrue(core.rooms_match_area(observed, AREA))
                await self.select_room(name)
                self.assertEqual(await self.page.evaluate("window.clicked"),
                                 [f"room{actual_row}-21", f"room{actual_row}-24"])
                self.assertTrue(await self.page.locator("#confirm").is_enabled())
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_confirmed_profile_selects_physical_rows_22_through_25(self):
        await self.assert_rooms_selected_at(CONFIRMED_ROWS)

    async def test_four_filtered_rooms_also_select_nonconsecutive_rows(self):
        await self.assert_rooms_selected_at(STRESS_ROWS)

    async def test_changed_selected_room_order_is_rejected_before_click(self):
        await self.page.set_content(selected_rooms_html(swap=True))
        observed = await core.read_visible_room_names(self.page)
        self.assertTrue(core.rooms_match_area(observed, AREA))
        self.assertEqual(core.room_short_name(observed[0]["name"]), "2-A50R")
        with self.assertRaises(core.SelectorConfigurationError):
            await self.select_room("2-A48R")
        # The low-level grid locator must also reject the wrong relative row.
        with self.assertRaises(core.SelectorConfigurationError):
            await core.choose_room_cells(self.page, "2-A48R", 1, self.target)
        await self.assert_no_grid_clicks()

    async def test_missing_selected_room_blocks_the_incomplete_roster(self):
        await self.page.set_content(selected_rooms_html(missing="2-B35R"))
        observed = await core.read_visible_room_names(self.page)
        self.assertEqual(len(observed), 3)
        self.assertFalse(core.rooms_match_area(observed, AREA))
        for name in SELECTED:
            with self.subTest(name=name), self.assertRaises(core.SelectorConfigurationError):
                await self.select_room(name)
        await self.assert_no_grid_clicks()

    async def test_similar_suffix_cannot_replace_a_missing_exact_target(self):
        await self.page.set_content(selected_rooms_html(suffix_replacement=True))
        self.assertEqual(await self.page.get_by_text("CN-HQ-B2a-2-A48R2", exact=True).count(), 2)
        observed = await core.read_visible_room_names(self.page)
        self.assertEqual([core.room_short_name(room["name"]) for room in observed],
                         ["2-A50R", "2-B35R", "2-B41R"])
        with self.assertRaises(core.SelectorConfigurationError):
            await self.select_room("2-A48R")
        with self.assertRaises(core.SelectorConfigurationError):
            await core.choose_room_cells(self.page, "2-A48R", 1, self.target)
        await self.assert_no_grid_clicks()


if __name__ == "__main__":
    unittest.main()
