"""选格后房名摘要/提示层的本地浏览器回归，全部网络请求均被阻断。

提示层结构是为重现误收集风险设计的合成 DOM，不声称来自真实预约网页。
横滚尺寸复用既有诊断夹具；点击回执来自浏览器真实点击事件。
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
from tests import test_grid_regressions as grids


F5_ROOMS = ("7F-A05R", "7F-A28R", "7F-B02R")


def room_grid_html(*, names=F5_ROOMS, redraw=False, scrolling=False, half_hour=False):
    with patch.object(grids, "ROOMS", tuple(names)):
        if scrolling:
            return grids.observed_scroll_grid_html(start_column=29, end_column=36,
                                                    initial_scroll=478, viewport_width=784)
        html = grids.fixture_html(redraw=redraw, header=True, background=True)
    condition = ("selected.length === 1 && selected[0] === 29" if half_hour else
                 "selected.length === 2 && selected[0] === 29 && selected[1] === 36")
    return html.replace("selected.length === 2 && selected[0] === 21 && selected[1] === 24",
                        condition)


OVERLAY_SCRIPT = r"""({kind, names, scrolling, redraw}) => {
    const handler = scrolling ? 'observedCellClicked' : 'cellClicked';
    const original = window[handler];
    window.overlayInsertions = 0;
    window.redraws = window.redraws || 0;
    window[handler] = function(cell) {
        original(cell);
        const row = Number(cell.dataset.row);
        if (row < 1 || window.selected[row]?.length !== 1) return;
        let current = document.getElementById(cell.id);
        // 滚动夹具没有内置重绘，这里只替换当前时间行，保留真实滚动容器。
        if (scrolling && redraw) {
            const old = current.parentElement;
            old.replaceWith(old.cloneNode(true));
            window.redraws++;
            current = document.getElementById(cell.id);
        }
        const content = document.querySelector('.ev_Dialog_PanelContent');
        const cr = content.getBoundingClientRect(), r = current.getBoundingClientRect();
        const fullName = 'CN-ShenZhen-F5-' + names[row - 1];
        const overlay = document.createElement('span');
        overlay.dataset.syntheticOverlay = kind;
        overlay.style.cssText = 'position:absolute;display:block;pointer-events:none;' +
            'width:260px;height:16px;line-height:16px;background:#fff;z-index:2;';
        if (kind === 'footer_summary') {
            // 左侧摘要含完整房名，但不在任何真实会议室时间行的Y位置。
            overlay.textContent = '已选择 ' + fullName + ' 14:00-18:00';
            overlay.style.left = scrolling ? '26px' : '0px';
            overlay.style.top = scrolling ? '455px' : '380px';
            content.appendChild(overlay);
        } else if (kind === 'grid_tooltip') {
            // 与目标时间行中心Y相同的纯房名提示，位于时间网格区域。
            overlay.textContent = fullName;
            overlay.style.left = (r.left - cr.left) + 'px';
            overlay.style.top = (r.top - cr.top + r.height / 2 - 8) + 'px';
            content.appendChild(overlay);
        } else if (kind === 'inline_tooltip') {
            // 纯房名提示直接成为时间格的子元素，不能被当作左侧名单。
            overlay.textContent = fullName;
            overlay.style.left = '1px';
            overlay.style.top = '1px';
            current.style.position = 'relative';
            current.appendChild(overlay);
        } else {
            throw new Error('合成提示类型无效');
        }
        window.overlayInsertions++;
    };
}"""


class GridRoomOverlayBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        profile = profiles.get_profile("F5")
        catalog = profile["room_catalog"]
        rows = {area: dict(config["rooms"]) for area, config in catalog.items()}
        self.assertEqual(tuple(rows["F5"]), F5_ROOMS)
        for name, value in (
                ("AREA_ROOM_ROWS", rows),
                ("AREA_EXPECTED_SHORT_NAMES", {area: tuple(names) for area, names in rows.items()}),
                ("ROOM_FULL_NAME_PATTERN", profile["full_name_pattern"]),
                ("ROOM_SHORT_NAME_PATTERN", profile["short_name_pattern"]),
                ("DRY_RUN", False), ("ACTION_TIMEOUT_MS", 2000),
                ("GRID_SELECTOR_BLOCKED", set())):
            self.stack.enter_context(patch.object(core, name, value))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "save_grid_diagnostic", AsyncMock()))
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 900})
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        self.target = core.Target(date(2030, 10, 30), "14:00", "18:00", ("F5",))

    async def asyncTearDown(self):
        try:
            await self.browser.close()
            await self.pw.stop()
        finally:
            self.stack.close()

    async def load(self, kind, *, scrolling=False, redraw=False, half_hour=False):
        # 重置JS上下文，避免scroll夹具顶层const在下一次set_content中重名。
        await self.page.goto("about:blank")
        await self.page.set_content(room_grid_html(
            scrolling=scrolling, redraw=redraw, half_hour=half_hour))
        await self.page.evaluate(OVERLAY_SCRIPT, {"kind": kind, "names": F5_ROOMS,
                                                  "scrolling": scrolling, "redraw": redraw})

    async def assert_room_selected(self, row, *, half_hour=False):
        room = core.RoomSpec("F5", F5_ROOMS[row - 1], row)
        target = (core.Target(self.target.day, "14:00", "14:30", ("F5",))
                  if half_hour else self.target)
        # 首格前名单完整有效，覆盖用户日志中“核验成功之后才出错”的顺序。
        await core.verify_room_identity(self.page, room)
        try:
            await core.choose_room_cells(self.page, room.name, room.row, target)
        except Exception as exc:
            clicks = await self.page.evaluate("window.clicked")
            self.fail(f"真实点击回执={clicks}，选格异常={type(exc).__name__}: {exc}")
        expected = [f"room{row}-29"] if half_hour else [f"room{row}-29", f"room{row}-36"]
        self.assertEqual(await self.page.evaluate("window.clicked"), expected)
        self.assertEqual(await self.page.evaluate("window.overlayInsertions"), 1)
        self.assertTrue(await self.page.locator("#confirm").is_enabled())

    async def test_f5_four_hour_footer_summary_after_first_click_keeps_end_click(self):
        for row in range(1, 4):
            with self.subTest(room=F5_ROOMS[row - 1]):
                await self.load("footer_summary")
                await self.assert_room_selected(row)

    async def test_f5_four_hour_grid_tooltip_at_target_y_keeps_end_click(self):
        for row in range(1, 4):
            with self.subTest(room=F5_ROOMS[row - 1]):
                await self.load("grid_tooltip")
                await self.assert_room_selected(row)

    async def test_f5_four_hour_inline_tooltip_survives_whole_row_redraw(self):
        for row in range(1, 4):
            with self.subTest(room=F5_ROOMS[row - 1]):
                await self.load("inline_tooltip", redraw=True)
                await self.assert_room_selected(row)
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_scrolled_grid_left_of_name_column_still_resolves_after_redraw(self):
        for row, kind in enumerate(("footer_summary", "grid_tooltip", "inline_tooltip"), 1):
            with self.subTest(room=F5_ROOMS[row - 1], overlay=kind):
                await self.load(kind, scrolling=True, redraw=True)
                geometry = await self.page.evaluate("""() => ({
                    rowLeft: document.querySelector('.time-row').getBoundingClientRect().left,
                    namesLeft: document.querySelector('.room-names').getBoundingClientRect().left,
                    viewportLeft: document.querySelector('#time-viewport').getBoundingClientRect().left
                })""")
                self.assertEqual(geometry, {"rowLeft": 31, "namesLeft": 244, "viewportLeft": 509})
                await self.assert_room_selected(row)
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)
                for click in await self.page.evaluate("window.clickGeometry"):
                    self.assertEqual(click["row"], row)
                    self.assertGreaterEqual(click["center"], click["viewportLeft"])
                    self.assertLessEqual(click["center"], click["viewportRight"])

    async def test_real_name_column_duplicate_unknown_and_reordered_rows_still_reject(self):
        cases = {
            "duplicate": (F5_ROOMS[0], F5_ROOMS[1], F5_ROOMS[1]),
            "reordered": (F5_ROOMS[1], F5_ROOMS[0], F5_ROOMS[2]),
            "unknown": (F5_ROOMS[0], F5_ROOMS[1], "7F-A99R"),
        }
        for label, names in cases.items():
            with self.subTest(case=label):
                await self.page.goto("about:blank")
                await self.page.set_content(room_grid_html(names=names))
                with self.assertRaises(core.SelectorConfigurationError):
                    await core.choose_room_cells(self.page, F5_ROOMS[0], 1, self.target)
                self.assertEqual(await self.page.evaluate("window.clicked"), [])
                self.assertFalse(await self.page.locator("#confirm").is_enabled())

    async def test_half_hour_with_inline_room_tooltip_still_clicks_only_once(self):
        await self.load("inline_tooltip", half_hour=True, redraw=True)
        await self.assert_room_selected(2, half_hour=True)


if __name__ == "__main__":
    unittest.main()
