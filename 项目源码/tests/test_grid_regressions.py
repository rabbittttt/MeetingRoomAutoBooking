"""Real Chromium grid regressions; local fixtures only, all network is blocked.

The fixtures deliberately vary DOM sibling order while preserving the room/time
layout, and redraw cells after the first real click.  Assertions check the actual
DOM click receipts and confirmation state, not locator implementation details.
"""
import asyncio
from contextlib import ExitStack
from datetime import date
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core


ROOMS = ("1F-A10R", "1F-A11R", "1F-A12R", "1F-A13R", "1F-A14R",
         "1F-A15R", "1F-A16R", "1F-A17R", "1F-A18R", "1F-A20R",
         "1F-A24R", "1F-A25R", "1F-A26R")


def fixture_html(*, redraw=False, hide_after_start=False, initial_hidden=False,
                 background=False, header=False, ambiguous_row=None,
                 columns=48, time_labels=True, hidden_name_copy=False):
    def grid(prefix, row, top, left=300):
        cells = []
        for column in range(1, columns + 1):
            minutes = (column - 1) * 30
            title = f' title="{minutes // 60:02d}:{minutes % 60:02d}"' if time_labels else ""
            cells.append(f'<div id="{prefix}-{column}" data-row="{row}" '
                         f'data-column="{column}"{title} onclick="cellClicked(this)"></div>')
        return (f'<div class="grid" data-grid-row="{row}" '
                f'style="top:{top}px;left:{left}px;grid-template-columns:repeat({columns},18px)">'
                + "".join(cells) + '</div>')

    outside = grid("background", 0, 124, 1350) if background else ""
    inside = grid("header", 0, 76) if header else ""
    for row, name in enumerate(ROOMS, 1):
        top = 100 + (row - 1) * 24
        inside += (f'<span class="room-name" style="top:{top}px">'
                   f'CN-ShenZhen-F5-{name}</span>' + grid(f"room{row}", row, top))
    if ambiguous_row:
        inside += grid("duplicate", ambiguous_row, 100 + (ambiguous_row - 1) * 24, 1350)
    if hidden_name_copy:
        inside += '<span class="room-name" style="top:124px;visibility:hidden">CN-ShenZhen-F5-1F-A11R</span>'
    config = json.dumps({"redraw": redraw, "hideAfterStart": hide_after_start,
                         "initialHidden": initial_hidden})
    return r'''<!doctype html><meta charset="utf-8"><style>
        body {margin:0; font-size:10px;}
        .ev_Dialog_Panel {position:absolute;left:0;top:0;width:2350px;height:500px;}
        .ev_Dialog_PanelContent {position:relative;width:2350px;height:450px;}
        .grid {position:absolute;display:grid;height:20px;}
        .ev_Dialog_Panel,.ev_Dialog_PanelContent {pointer-events:none;}
        .grid>div,.room-name,button {pointer-events:auto;}
        .grid>div {height:20px;box-sizing:border-box;border:1px solid #ccc;}
        .room-name {position:absolute;left:0;width:270px;height:20px;line-height:20px;}
        </style>''' + outside + '<div class="ev_Dialog_Panel"><div class="ev_Dialog_PanelContent">' + inside + r'''
        </div><button id="confirm" disabled>确认</button></div><script>
        window.fixtureConfig = ''' + config + r''';
        window.clicked = [];
        window.selected = {};
        window.redraws = 0;
        function cellClicked(cell) {
          const row = Number(cell.dataset.row), column = Number(cell.dataset.column);
          window.clicked.push(cell.id);
          if (!(row > 0)) return;
          const selected = window.selected[row] || (window.selected[row] = []);
          selected.push(column);
          document.getElementById('confirm').disabled =
            !(selected.length === 2 && selected[0] === 21 && selected[1] === 24);
          if (selected.length === 1 && window.fixtureConfig.redraw) {
            const old = cell.parentElement, replacement = old.cloneNode(true);
            // Replacing the entire row invalidates both previously held endpoints.
            old.replaceWith(replacement);
            window.redraws++;
            if (window.fixtureConfig.hideAfterStart) {
              replacement.style.display = 'none';
              setTimeout(() => replacement.style.display = 'grid', 180);
            }
          }
        }
        if (window.fixtureConfig.initialHidden) {
          document.querySelectorAll('.grid').forEach(e => e.style.display = 'none');
          setTimeout(() => document.querySelectorAll('.grid').forEach(e => e.style.display = 'grid'), 200);
        }
        </script>'''


def observed_scroll_grid_html(*, start_column=33, end_column=36,
                              viewport_width=784, initial_scroll=478):
    """Use dimensions from the user's watch-mode 1F-A12R DOM diagnostic.

    Unlabelled row cells have 48 direct DIV children, 30x24 px each. The
    viewport starts at x=509,y=290 and is 784x250; scrollLeft=478 places the
    third row's columns 33/36 at x=991/1081,y=338. Narrow-width variants are
    explicit stress fixtures, not a claim about the captured corporate page.
    """
    names, rows = [], []
    for row, name in enumerate(ROOMS, 1):
        names.append(f'<span>CN-ShenZhen-F5-{name}</span>')
        cells = ''.join(f'<div class="order" id="room{row}-{column}" '
                        f'data-row="{row}" data-column="{column}" '
                        'onclick="observedCellClicked(this)"></div>'
                        for column in range(1, 49))
        rows.append(f'<div class="time-row">{cells}</div>')
    return r'''<!doctype html><meta charset="utf-8"><style>
        body {margin:0;font-size:12px;}
        .ev_Dialog_Panel {position:absolute;left:218px;top:100px;width:1100px;height:551px;}
        .ev_Dialog_PanelContent {position:relative;width:1100px;height:490px;}
        .room-names {position:absolute;left:26px;top:190px;width:264px;height:250px;overflow:hidden;}
        .room-names>span {display:block;height:24px;line-height:24px;}
        #time-viewport {position:absolute;left:291px;top:190px;height:250px;overflow:auto;}
        .time-row {display:flex;width:1440px;height:24px;}
        .order {box-sizing:border-box;flex:0 0 30px;width:30px;height:24px;border:1px solid #ddd;}
        </style><div class="ev_Dialog_Panel"><div class="ev_Dialog_PanelContent">
        <div class="room-names">''' + ''.join(names) + '</div><div id="time-viewport" style="width:' + str(viewport_width) + 'px">' + ''.join(rows) + r'''
        </div></div><button id="confirm" disabled>确认</button></div><script>
        window.clicked=[]; window.selected={}; window.clickGeometry=[]; window.scrollPositions=[];
        const viewport=document.getElementById('time-viewport');
        viewport.scrollLeft=''' + str(initial_scroll) + r''';
        window.scrollPositions.push(viewport.scrollLeft);
        viewport.addEventListener('scroll',()=>window.scrollPositions.push(viewport.scrollLeft));
        function observedCellClicked(cell) {
          const row=Number(cell.dataset.row), column=Number(cell.dataset.column);
          const r=cell.getBoundingClientRect(), v=viewport.getBoundingClientRect();
          window.clicked.push(cell.id);
          window.clickGeometry.push({row,column,scrollLeft:viewport.scrollLeft,
             center:r.x+r.width/2,viewportLeft:v.x,viewportRight:v.x+viewport.clientWidth});
          const selected=window.selected[row]||(window.selected[row]=[]);
          selected.push(column);
          document.getElementById('confirm').disabled=
            !(selected.length===2 && selected[0]===''' + str(start_column) + ' && selected[1]===' + str(end_column) + r''');
        }
        </script>'''


class DynamicGridBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 800})
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        self.target = core.Target(date(2030, 1, 3), "10:00", "12:00", ("F5-1A",))
        self.production_action_timeout = core.ACTION_TIMEOUT_MS
        self.stack.enter_context(patch.object(core, "save_grid_diagnostic", AsyncMock()))
        self.stack.enter_context(patch.object(core, "ACTION_TIMEOUT_MS", 2000))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))

    async def asyncTearDown(self):
        self.stack.close()
        await self.browser.close()
        await self.pw.stop()

    async def assert_selected(self, page, row):
        self.assertEqual(await page.evaluate("window.clicked"), [f"room{row}-21", f"room{row}-24"])
        self.assertTrue(await page.locator("#confirm").is_enabled())

    async def test_all_later_rows_use_actual_room_layout_not_global_sibling_index(self):
        for row in range(2, 14):
            with self.subTest(row=row, room=ROOMS[row - 1]):
                await self.page.set_content(fixture_html(background=True, header=True))
                await core.choose_room_cells(self.page, ROOMS[row - 1], row, self.target)
                await self.assert_selected(self.page, row)

    async def test_start_click_replacing_whole_first_row_still_selects_end_once(self):
        await self.page.set_content(fixture_html(redraw=True))
        await core.choose_room_cells(self.page, ROOMS[0], 1, self.target)
        await self.assert_selected(self.page, 1)
        self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_second_row_never_uses_adjacent_first_row_within_old_42px_tolerance(self):
        await self.page.set_content(fixture_html())
        await core.choose_room_cells(self.page, ROOMS[1], 2, self.target)
        await self.assert_selected(self.page, 2)

    async def test_later_row_redraw_does_not_repeat_start_or_choose_neighbor(self):
        await self.page.set_content(fixture_html(redraw=True, header=True))
        await core.choose_room_cells(self.page, ROOMS[6], 7, self.target)
        await self.assert_selected(self.page, 7)
        self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_end_row_temporarily_hidden_after_start_recovers_without_double_click(self):
        await self.page.set_content(fixture_html(redraw=True, hide_after_start=True))
        await core.choose_room_cells(self.page, ROOMS[3], 4, self.target)
        await self.assert_selected(self.page, 4)

    async def test_grid_initial_render_delay_recovers(self):
        await self.page.set_content(fixture_html(initial_hidden=True))
        await core.choose_room_cells(self.page, ROOMS[12], 13, self.target)
        await self.assert_selected(self.page, 13)

    async def test_same_room_two_distinct_grid_candidates_are_rejected_before_click(self):
        await self.page.set_content(fixture_html(ambiguous_row=8))
        with self.assertRaises(core.SelectorConfigurationError):
            await core.choose_room_cells(self.page, ROOMS[7], 8, self.target)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])

    async def test_unlabelled_partial_day_grid_is_not_assumed_to_start_at_midnight(self):
        await self.page.set_content(fixture_html(columns=24, time_labels=False))
        with self.assertRaises(RuntimeError):
            await core.choose_room_cells(self.page, ROOMS[0], 1, self.target)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])

    async def test_full_unlabelled_48_half_hour_grid_remains_supported(self):
        await self.page.set_content(fixture_html(time_labels=False))
        await core.choose_room_cells(self.page, ROOMS[9], 10, self.target)
        await self.assert_selected(self.page, 10)

    async def test_observed_30_by_24_grid_at_scroll_478_selects_16_to_18(self):
        await self.page.set_content(observed_scroll_grid_html())
        first = await self.page.locator('#room3-33').bounding_box()
        last = await self.page.locator('#room3-36').bounding_box()
        self.assertEqual({key: round(first[key]) for key in ('x', 'y', 'width', 'height')},
                         {'x': 991, 'y': 338, 'width': 30, 'height': 24})
        self.assertEqual(round(last['x']), 1081)
        target = core.Target(self.target.day, '16:00', '18:00', ('F5-1A',))
        await core.choose_room_cells(self.page, ROOMS[2], 3, target)
        self.assertEqual(await self.page.evaluate('window.clicked'), ['room3-33', 'room3-36'])
        self.assertTrue(await self.page.locator('#confirm').is_enabled())

    async def test_real_width_scroller_reveals_start_before_relocating_end(self):
        await self.page.set_content(observed_scroll_grid_html(
            start_column=21, end_column=24, initial_scroll=656))
        before = await self.page.locator('#time-viewport').evaluate('e => e.scrollLeft')
        box = await self.page.locator('#room3-21').bounding_box()
        self.assertLess(box['x'], 509)
        await core.choose_room_cells(self.page, ROOMS[2], 3, self.target)
        await self.assert_selected(self.page, 3)
        clicks = await self.page.evaluate('window.clickGeometry')
        self.assertLess(clicks[0]['scrollLeft'], before)
        for click in clicks:
            self.assertGreaterEqual(click['center'], click['viewportLeft'])
            self.assertLessEqual(click['center'], click['viewportRight'])

    async def test_each_endpoint_scrolls_into_view_without_clicking_neighbor_row(self):
        await self.page.set_content(observed_scroll_grid_html(
            start_column=21, end_column=24, viewport_width=60, initial_scroll=0))
        await core.choose_room_cells(self.page, ROOMS[3], 4, self.target)
        await self.assert_selected(self.page, 4)
        clicks = await self.page.evaluate('window.clickGeometry')
        self.assertGreater(clicks[0]['scrollLeft'], 0)
        self.assertGreater(clicks[1]['scrollLeft'], clicks[0]['scrollLeft'])
        for click in clicks:
            self.assertEqual(click['row'], 4)
            self.assertGreaterEqual(click['center'], click['viewportLeft'])
            self.assertLessEqual(click['center'], click['viewportRight'])

    async def test_thirteen_pages_concurrently_keep_their_own_room_and_range(self):
        pages = [self.page] + [await self.context.new_page() for _ in range(12)]
        await asyncio.gather(*(page.set_content(fixture_html(redraw=True, header=True)) for page in pages))
        # The 13-page load case uses the production budget; only single-page
        # negative fixtures shorten waits. Always drain peers before closing.
        with patch.object(core, "ACTION_TIMEOUT_MS", self.production_action_timeout):
            results = await asyncio.gather(*(core.choose_room_cells(page, name, row, self.target)
                                             for row, (page, name) in enumerate(zip(pages, ROOMS), 1)),
                                           return_exceptions=True)
        self.assertEqual([str(value) for value in results if isinstance(value, BaseException)], [])
        for row, page in enumerate(pages, 1):
            await self.assert_selected(page, row)
            self.assertEqual(await page.evaluate("window.redraws"), 1)


if __name__ == "__main__":
    unittest.main()
