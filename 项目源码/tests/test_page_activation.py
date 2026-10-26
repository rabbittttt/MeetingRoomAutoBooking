"""Local Chrome focus-initialization regressions; never visits the booking service.

Default Playwright already emulates focus. Tests explicitly disable that emulation
on their own local pages to model a page/browser policy requiring native activation.
No production cause is inferred from this controlled reproduction.
"""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core


LOCAL_URL = 'http://127.0.0.1:18763/booking'
HTML = '''<!doctype html><html><body>
<button aria-label="图标 添加会议室">添加会议室</button>
<button class="ev_datepicker_icon_calendar" style="display:none">日期</button>
<button>预定</button>
<script>
window.initialized=false;
window.focusEvents=0;
window.addEventListener('focus', () => {
    window.focusEvents++;
    setTimeout(() => {
        if (!document.hasFocus()) return;
        window.initialized=true;
        document.querySelector('.ev_datepicker_icon_calendar').style.display='block';
    }, 100);
});
</script></body></html>'''


class PageActivationBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.pw = await async_playwright().start()
        # A native foreground tab is the behavior under test, so use headed Chrome.
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get('BOOKING_TEST_BROWSER', 'chrome'), headless=False)
        self.context = await self.browser.new_context()
        self.requests = []
        async def local_only(route):
            self.requests.append(route.request.url)
            if route.request.url.startswith(LOCAL_URL):
                await route.fulfill(status=200, content_type='text/html; charset=utf-8', body=HTML)
            else:
                await route.abort()
        await self.context.route('**/*', local_only)
        self.page = await self.context.new_page()
        self.other = await self.context.new_page()
        self.stack.enter_context(patch.object(core, 'BOOKING_URL', LOCAL_URL))
        self.stack.enter_context(patch.object(core, 'NAVIGATION_TIMEOUT_MS', 3000))
        self.stack.enter_context(patch.object(core, 'ACTION_TIMEOUT_MS', 1500))
        self.stack.enter_context(patch.object(core, 'AUTH_WAIT_SECONDS', 3))
        self.current = datetime(2030, 1, 1, 10, 0, tzinfo=core.CN_TZ)
        self.target = core.Target(self.current.date() + timedelta(days=2),
                                  '10:00', '12:00', ('F5',))
        self.room = core.RoomSpec('F5', '7F-A05R', 1)
        self.stack.enter_context(patch.object(core, 'now_cn', lambda: self.current))
        for name in ('STOP_REQUESTED', 'DRIVER_LOST', 'AUTH_BLOCKED', 'CONFIG_BLOCKED'):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, 'GRID_SELECTOR_BLOCKED', set()))
        self.stack.enter_context(patch.object(core, 'is_403_page', AsyncMock(return_value=False)))
        self.stack.enter_context(patch.object(core, 'save_debug', AsyncMock()))

    async def asyncTearDown(self):
        self.stack.close()
        await self.browser.close()
        await self.pw.stop()

    async def require_native_activation(self, page, foreground=None):
        # Navigation can cause Playwright to reinstall default focus emulation.
        # Disable it after navigation, only in this controlled local test.
        session = await self.context.new_cdp_session(page)
        await session.send('Emulation.setFocusEmulationEnabled', {'enabled': False})
        if foreground is not None:
            await foreground.bring_to_front()
        await page.evaluate("""() => {
            window.initialized=false;
            window.focusEvents=0;
            document.querySelector('.ev_datepicker_icon_calendar').style.display='none';
        }""")
        return session

    async def assert_no_external_requests(self):
        self.assertTrue(all(url.startswith(LOCAL_URL) for url in self.requests))

    async def test_native_activation_initializes_calendar_without_manual_click(self):
        await self.page.goto(LOCAL_URL)
        await self.require_native_activation(self.page, self.other)
        self.assertFalse(await self.page.evaluate('document.hasFocus()'))
        self.assertFalse(await self.page.locator(core.CALENDAR_ICON).is_visible())
        await core.activate_booking_page(self.page)
        self.assertTrue(await self.page.evaluate('window.initialized'))
        self.assertGreaterEqual(await self.page.evaluate('window.focusEvents'), 1)
        self.assertTrue(await self.page.locator(core.CALENDAR_ICON).is_visible())
        await self.assert_no_external_requests()

    async def test_prepare_reactivates_after_initial_navigation_and_every_reload(self):
        actual_goto, actual_reload = self.page.goto, self.page.reload
        navigations = []
        async def gated_goto(*args, **kwargs):
            response = await actual_goto(*args, **kwargs)
            navigations.append('goto')
            await self.require_native_activation(self.page, self.other)
            return response
        async def gated_reload(*args, **kwargs):
            response = await actual_reload(*args, **kwargs)
            navigations.append('reload')
            await self.require_native_activation(self.page, self.other)
            return response
        async def select_date_only_when_initialized(page, _day):
            self.assertTrue(await page.evaluate('window.initialized'))
            self.assertTrue(await page.locator(core.CALENDAR_ICON).is_visible())
        self.stack.enter_context(patch.object(self.page, 'goto', gated_goto))
        self.stack.enter_context(patch.object(self.page, 'reload', gated_reload))
        date_step = self.stack.enter_context(patch.object(
            core, 'select_date', AsyncMock(side_effect=select_date_only_when_initialized)))
        for name in ('select_start_time', 'select_duration', 'open_room_list',
                     'verify_room_identity', 'choose_room_cells'):
            self.stack.enter_context(patch.object(core, name, AsyncMock()))
        self.stack.enter_context(patch.object(core, 'wait_selection_confirmation_ready',
                                             AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(core, 'confirm_room_selection',
                                             AsyncMock(return_value=True)))
        for _ in range(2):
            self.assertTrue(await core.prepare_page(
                self.page, self.room, self.target, watch_mode=True))
        self.assertEqual(navigations, ['goto', 'reload'])
        self.assertEqual(date_step.await_count, 2)
        await self.assert_no_external_requests()

    async def test_first_authentication_activates_without_waiting_for_calendar(self):
        native_front = self.page.bring_to_front
        activations = self.stack.enter_context(patch.object(
            self.page, 'bring_to_front', AsyncMock(wraps=native_front)))
        pool = core.PagePool(self.context, self.other)
        await pool._authenticate_new_page(self.page, 2)
        self.assertGreaterEqual(activations.await_count, 1)
        self.assertTrue(await self.page.get_by_role(
            'button', name=core.ADD_ROOM_BUTTON).is_visible())
        self.assertFalse(core.AUTH_BLOCKED.is_set())
        await self.assert_no_external_requests()

    async def test_concurrent_page_initialization_does_not_steal_focus_before_ready(self):
        await self.page.goto(LOCAL_URL + '?room=1')
        await self.other.goto(LOCAL_URL + '?room=2')
        third = await self.context.new_page()
        await self.require_native_activation(self.page, third)
        await self.require_native_activation(self.other, third)
        self.assertFalse(await self.page.evaluate('document.hasFocus()'))
        self.assertFalse(await self.other.evaluate('document.hasFocus()'))
        # The local page requires continuous focus for its delayed initialization.
        # Without coordination, activating room 2 immediately makes room 1 stall.
        await asyncio.gather(core.activate_booking_page(self.page),
                             core.activate_booking_page(self.other))
        for page in (self.page, self.other):
            self.assertTrue(await page.evaluate('window.initialized'))
            self.assertTrue(await page.locator(core.CALENDAR_ICON).is_visible())
        await self.assert_no_external_requests()


if __name__ == '__main__':
    unittest.main()
