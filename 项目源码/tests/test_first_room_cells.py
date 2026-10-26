"""Real-Chrome regressions for F5 A05 first-row cell selection; no network."""
from contextlib import ExitStack
from datetime import date
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core


class FirstRoomCellBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.page = await self.browser.new_page(viewport={"width": 2400, "height": 800})
        await self.page.route("**/*", lambda route: route.abort())
        self.target = core.Target(date(2030, 1, 3), "10:00", "12:00", ("F5",))
        self.stack.enter_context(patch.object(core, "save_grid_diagnostic", AsyncMock()))
        self.stack.enter_context(patch.object(core, "ACTION_TIMEOUT_MS", 3000))

    async def asyncTearDown(self):
        self.stack.close()
        await self.browser.close()
        await self.pw.stop()

    def grid(self, prefix, top, left=300, width=18):
        children = []
        for column in range(1, 49):
            minutes = (column - 1) * 30
            label = f"{minutes // 60:02d}:{minutes % 60:02d}"
            children.append(f'<div id="{prefix}-{column}" title="{label}" '
                            f'onclick="window.clicked.push(this.id)">{label}</div>')
        return (f'<div class="grid" style="top:{top}px;left:{left}px;'
                f'grid-template-columns:repeat(48,{width}px)">' + "".join(children) + '</div>')

    async def load(self, *, background=False, header=False, ambiguous=False):
        outside = self.grid("background", 100, left=1350, width=18) if background else ""
        inside = self.grid("header", 75) if header else ""
        for row, name in enumerate(("7F-A05R", "7F-A28R", "7F-B02R")):
            top = 100 + row * 30
            inside += (f'<span class="room-name" style="top:{top}px">'
                       f'CN-ShenZhen-F5-{name}</span>' + self.grid(f"room{row + 1}", top))
        if ambiguous:
            inside += self.grid("duplicate", 100, left=1350)
        await self.page.set_content('''<style>
            body {margin:0; font-size:10px;}
            .ev_Dialog_Panel {position:absolute;left:0;top:0;width:2350px;height:240px;}
            .ev_Dialog_PanelContent {position:relative;width:2350px;height:240px;}
            .grid {position:absolute;display:grid;height:20px;}
            .ev_Dialog_Panel,.ev_Dialog_PanelContent {pointer-events:none;}
            .grid>div,.room-name,button {pointer-events:auto;}
            .grid>div {height:20px;box-sizing:border-box;border:1px solid #ccc;overflow:hidden;}
            .room-name {position:absolute;left:0;width:270px;height:20px;line-height:20px;}
            </style><script>window.clicked=[];</script>''' + outside +
            '<div class="ev_Dialog_Panel"><div class="ev_Dialog_PanelContent">' +
            inside + '</div><button>确认</button></div>')

    async def test_background_first_match_is_ignored_and_actual_a05_cells_clicked(self):
        await self.load(background=True)
        self.assertEqual(core.cell_columns(self.target.start, self.target.end), (21, 24))
        # The old global first matches are deliberately visible and within its 42px tolerance.
        for column in (21, 24):
            first = self.page.locator(f"div:nth-child({column})").first
            self.assertEqual(await first.get_attribute("id"), f"background-{column}")
        await core.choose_room_cells(self.page, "7F-A05R", 1, self.target)
        self.assertEqual(await self.page.evaluate("window.clicked"), ["room1-21", "room1-24"])

    async def test_nearby_header_inside_dialog_is_ignored(self):
        await self.load(header=True)
        for column in (21, 24):
            first = self.page.locator(f"div:nth-child({column})").first
            self.assertEqual(await first.get_attribute("id"), f"header-{column}")
            box = await first.bounding_box()
            self.assertLess(abs(box["y"] + box["height"] / 2 - 100), 42)
        await core.choose_room_cells(self.page, "7F-A05R", 1, self.target)
        self.assertEqual(await self.page.evaluate("window.clicked"), ["room1-21", "room1-24"])

    async def test_two_real_candidates_in_same_room_row_are_rejected_before_any_click(self):
        await self.load(ambiguous=True)
        with self.assertRaises(core.SelectorConfigurationError):
            await core.choose_room_cells(self.page, "7F-A05R", 1, self.target)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])

    async def test_half_hour_boundaries_preserve_first_and_inclusive_end_columns(self):
        await self.load(header=True)
        target = core.Target(self.target.day, "10:30", "12:30", ("F5",))
        self.assertEqual(core.cell_columns(target.start, target.end), (22, 25))
        await core.choose_room_cells(self.page, "7F-A05R", 1, target)
        self.assertEqual(await self.page.evaluate("window.clicked"), ["room1-22", "room1-25"])


if __name__ == "__main__":
    unittest.main()
