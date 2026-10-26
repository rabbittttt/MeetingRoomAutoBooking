"""Account-visible room subsets on local fixtures; all network is blocked.

The user confirmed an 11-room F5-1A list missing A11/A20. DOM markup and
click handlers below are synthetic; they test identity/geometry, not access
permissions or the company's booking service.
"""
import asyncio
from contextlib import ExitStack
from datetime import date
import unittest
import os
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core
import deployment_profiles as profiles
from tests import test_grid_regressions as grid_fixture
from tests.test_area_filters import STYLE, control


AREA = "F5-1A"
FULL_ROOMS = grid_fixture.ROOMS
MISSING_ROOMS = ("1F-A11R", "1F-A20R")
VISIBLE_ROOMS = tuple(name for name in FULL_ROOMS if name not in MISSING_ROOMS)


def observed(names):
    return [{"name": "CN-ShenZhen-F5-" + name, "y": 100 + row * 24}
            for row, name in enumerate(names)]


def patch_profile(stack):
    profile = profiles.get_profile("F5")
    rows = {area: dict(config["rooms"])
            for area, config in profile["room_catalog"].items()}
    for name, value in (
        ("DEPLOYMENT_PROFILE", profile),
        ("ROOM_CATALOG", profile["room_catalog"]),
        ("AREA_ROOM_ROWS", rows),
        ("AREA_LABELS", {area: config["label"]
                         for area, config in profile["room_catalog"].items()}),
        ("AREA_EXPECTED_SHORT_NAMES", {area: tuple(names) for area, names in rows.items()}),
        ("ROOM_FULL_NAME_PATTERN", profile["full_name_pattern"]),
        ("ROOM_SHORT_NAME_PATTERN", profile["short_name_pattern"]),
    ):
        stack.enter_context(patch.object(core, name, value))
    return rows[AREA]


def grid_html(names):
    with patch.object(grid_fixture, "ROOMS", tuple(names)):
        return grid_fixture.fixture_html(redraw=True, header=True, background=True)


class VisibleSubsetRulesTests(unittest.TestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.rows = patch_profile(self.stack)

    def test_strict_default_remains_complete_and_partial_requires_opt_in(self):
        self.assertTrue(core.rooms_match_area(observed(FULL_ROOMS), AREA))
        self.assertFalse(core.rooms_match_area(observed(VISIBLE_ROOMS), AREA))
        self.assertTrue(core.rooms_match_area(observed(VISIBLE_ROOMS), AREA, allow_partial=True))
        self.assertTrue(core.rooms_match_area(observed(FULL_ROOMS), AREA, allow_partial=True))
        self.assertEqual(tuple(self.rows), FULL_ROOMS)
        self.assertEqual(self.rows["1F-A26R"], 13)

    def test_partial_accepts_single_known_room_but_rejects_invalid_lists(self):
        self.assertTrue(core.rooms_match_area(observed(("1F-A26R",)), AREA, allow_partial=True))
        wrong_order = list(VISIBLE_ROOMS)
        wrong_order[1:3] = reversed(wrong_order[1:3])
        cases = {
            "empty": (),
            "unknown": (*VISIBLE_ROOMS, "1F-A99R"),
            "duplicate": (*VISIBLE_ROOMS, VISIBLE_ROOMS[-1]),
            "out_of_order_subset": wrong_order,
            "out_of_order_full": tuple(reversed(FULL_ROOMS)),
            "other_area": ("7F-A05R", "7F-A28R", "7F-B02R"),
        }
        for label, names in cases.items():
            with self.subTest(case=label):
                self.assertFalse(core.rooms_match_area(observed(names), AREA, allow_partial=True))
        self.assertFalse(core.rooms_match_area(observed(VISIBLE_ROOMS), "unknown", allow_partial=True))

    def test_missing_room_exception_is_not_a_selector_failure(self):
        self.assertTrue(issubclass(core.RoomNotVisibleError, RuntimeError))
        self.assertFalse(issubclass(core.RoomNotVisibleError, core.SelectorConfigurationError))


class VisibleSubsetBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.rows = patch_profile(self.stack)
        for name, value in (("DRY_RUN", False), ("ACTION_TIMEOUT_MS", 2000),
                            ("AREA_RESULT_WAIT_SECONDS", 1.2), ("GRID_SELECTOR_BLOCKED", set())):
            self.stack.enter_context(patch.object(core, name, value))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        for name in ("save_grid_diagnostic", "save_area_filter_diagnostic", "record_filter_ui_probe"):
            self.stack.enter_context(patch.object(core, name, AsyncMock()))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 800})
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        self.target = core.Target(date(2030, 1, 3), "10:00", "12:00", (AREA,))

    async def asyncTearDown(self):
        try:
            await self.browser.close()
            await self.pw.stop()
        finally:
            self.stack.close()

    async def select(self, name):
        room = core.RoomSpec(AREA, name, self.rows[name])
        await core.verify_room_identity(self.page, room)
        await core.choose_room_cells(self.page, room.name, room.row, self.target)

    async def assert_no_clicks(self):
        self.assertEqual(await self.page.evaluate("window.clicked"), [])
        self.assertFalse(await self.page.locator("#confirm").is_enabled())

    async def test_all_eleven_rooms_follow_actual_y_after_two_missing_rooms(self):
        for actual_row, name in enumerate(VISIBLE_ROOMS, 1):
            with self.subTest(room=name, canonical=self.rows[name], actual=actual_row):
                await self.page.set_content(grid_html(VISIBLE_ROOMS))
                names = await core.read_visible_room_names(self.page)
                self.assertEqual([core.room_short_name(item["name"]) for item in names],
                                 list(VISIBLE_ROOMS))
                await self.select(name)
                self.assertEqual(await self.page.evaluate("window.clicked"),
                                 [f"room{actual_row}-21", f"room{actual_row}-24"])
                self.assertTrue(await self.page.locator("#confirm").is_enabled())
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)
        self.assertEqual(self.rows["1F-A12R"], 3)
        self.assertEqual(self.rows["1F-A26R"], 13)

    async def test_absent_targets_never_click_the_neighbor_or_set_global_blocks(self):
        for name in MISSING_ROOMS:
            with self.subTest(room=name):
                await self.page.set_content(grid_html(VISIBLE_ROOMS))
                room = core.RoomSpec(AREA, name, self.rows[name])
                with self.assertRaises(core.RoomNotVisibleError):
                    await core.verify_room_identity(self.page, room)
                with self.assertRaises(core.RoomNotVisibleError):
                    await core.choose_room_cells(self.page, name, room.row, self.target)
                await self.assert_no_clicks()
                self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
                for event in (core.STOP_REQUESTED, core.DRIVER_LOST,
                              core.AUTH_BLOCKED, core.CONFIG_BLOCKED):
                    self.assertFalse(event.is_set())

    async def test_complete_thirteen_room_layout_still_verifies_and_selects(self):
        await self.page.set_content(grid_html(FULL_ROOMS))
        for name in FULL_ROOMS:
            await core.verify_room_identity(self.page, core.RoomSpec(AREA, name, self.rows[name]))
        for name in ("1F-A10R", "1F-A11R", "1F-A20R", "1F-A26R"):
            with self.subTest(room=name):
                await self.page.set_content(grid_html(FULL_ROOMS))
                await self.select(name)
                row = self.rows[name]
                self.assertEqual(await self.page.evaluate("window.clicked"),
                                 [f"room{row}-21", f"room{row}-24"])
                self.assertTrue(await self.page.locator("#confirm").is_enabled())

    async def test_invalid_rosters_reject_both_identity_and_direct_grid_selection(self):
        reordered = list(VISIBLE_ROOMS)
        reordered[1:3] = reversed(reordered[1:3])
        cases = {
            "duplicate": (*VISIBLE_ROOMS, "1F-A12R"),
            "unknown": (*VISIBLE_ROOMS, "1F-A99R"),
            "wrong_order": reordered,
            "other_area": ("7F-A05R", "7F-A28R", "7F-B02R"),
        }
        room = core.RoomSpec(AREA, "1F-A12R", self.rows["1F-A12R"])
        for label, names in cases.items():
            with self.subTest(case=label):
                await self.page.set_content(grid_html(names))
                with self.assertRaises(core.SelectorConfigurationError):
                    await core.verify_room_identity(self.page, room)
                with self.assertRaises(core.SelectorConfigurationError):
                    await core.choose_room_cells(self.page, room.name, room.row, self.target)
                await self.assert_no_clicks()

    async def test_correct_name_with_wrong_canonical_row_is_rejected(self):
        await self.page.set_content(grid_html(VISIBLE_ROOMS))
        # A12 is actually visible at row 2, but the source identity is row 3.
        with self.assertRaises(core.SelectorConfigurationError):
            await core.verify_room_identity(self.page, core.RoomSpec(AREA, "1F-A12R", 2))
        with self.assertRaises(core.SelectorConfigurationError):
            await core.choose_room_cells(self.page, "1F-A12R", 2, self.target)
        await self.assert_no_clicks()

    async def load_filter(self, names):
        await self.page.set_content(
            STYLE + '<button aria-label="图标 添加会议室" '
            'onclick="document.querySelector(\'.ev_Dialog_Panel\').style.display=\'block\'">添加</button>'
            '<div class="ev_Dialog_Panel" style="display:none">'
            '<button id="all" style="position:absolute;left:400px;top:5px">全部会议室</button>'
            + control(core.AREA_LABELS[AREA], nested=True) +
            '<div id="options" style="display:none;position:absolute;top:55px">'
            '<div role="option" aria-label="选项2, 科研楼F5-1A外来接待区">科研楼F5-1A外来接待区</div></div>'
            '<button id="query" style="position:absolute;top:85px">查询</button>'
            '<div class="ev_Dialog_PanelContent" style="position:absolute;top:120px"></div></div>')
        await self.page.evaluate("""names => {
          window.queryClicks=0; window.buildingClicks=0; window.allClicks=0;
          document.querySelector('.ev_Dialog_PanelContent').innerHTML=
            names.map(name=>'<div>CN-ShenZhen-F5-'+name+'</div>').join('');
          document.querySelector('.ev_select_drop_icon').onclick=()=>{
            window.buildingClicks++; document.getElementById('options').style.display='block';
          };
          document.querySelector('[role=option]').onclick=()=>{
            document.getElementById('options').style.display='none';
          };
          document.getElementById('query').onclick=()=>window.queryClicks++;
          document.getElementById('all').onclick=()=>window.allClicks++;
        }""", list(names))

    async def test_initial_partial_requires_explicit_query_and_all_rooms_tab(self):
        await self.load_filter(VISIBLE_ROOMS)
        self.assertEqual(await core.select_area(self.page, AREA), "explicit")
        counts = await self.page.evaluate("[window.queryClicks,window.buildingClicks,window.allClicks]")
        self.assertEqual(counts, [1, 1, 1])
        self.assertTrue(core.rooms_match_area(await core.read_visible_room_names(self.page),
                                              AREA, allow_partial=True))

    async def test_initial_complete_roster_preserves_no_query_fast_path(self):
        await self.load_filter(FULL_ROOMS)
        self.assertEqual(await core.select_area(self.page, AREA), "matched_current")
        self.assertEqual(await self.page.evaluate("[window.queryClicks,window.buildingClicks]"), [0, 0])

    async def test_query_requires_two_equal_partial_snapshots_not_first_loading_frame(self):
        await self.load_filter(VISIBLE_ROOMS)
        await self.page.locator('.ev_Dialog_Panel').evaluate("e=>e.style.display='block'")
        snapshots = [observed(("1F-A10R",)), observed(VISIBLE_ROOMS), observed(VISIBLE_ROOMS)]
        reads = 0

        async def read_snapshot(page):
            nonlocal reads
            value = snapshots[min(reads, len(snapshots) - 1)]
            reads += 1
            return value

        with patch.object(core, "read_visible_room_names", side_effect=read_snapshot):
            await core.query_area_rooms(self.page, AREA)
        self.assertGreaterEqual(reads, 3)
        self.assertEqual(await self.page.evaluate("window.queryClicks"), 1)


if __name__ == "__main__":
    unittest.main()
