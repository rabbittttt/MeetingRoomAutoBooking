"""Preparation-time authentication recovery without network or stored credentials.

Pages model delayed navigation and calendar readiness. The existing native Chrome
activation suite separately checks continuous focus during form initialization.
"""
import asyncio
from contextlib import ExitStack
from datetime import date
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import booking_core as core


LOGIN_URL = 'https://login.huawei.com/login1/'


class Calendar:
    def __init__(self, page):
        self.page = page
        self.first = self

    async def is_visible(self):
        return self.page.calendar_ready


class Page:
    def __init__(self, context, url, *, ready=False):
        self.context = context
        self.url = url
        self.calendar_ready = ready
        self.bring_to_front = AsyncMock()
        self.reload = AsyncMock(side_effect=self._reload)
        self.goto = AsyncMock()
        self.button = SimpleNamespace(wait_for=AsyncMock())

    async def _reload(self, **kwargs):
        self.url = LOGIN_URL
        self.calendar_ready = False

    def locator(self, selector):
        if selector != core.CALENDAR_ICON:
            raise AssertionError(f'unexpected selector: {selector}')
        return Calendar(self)

    def get_by_role(self, *args, **kwargs):
        return self.button

    def recover(self):
        self.url = core.BOOKING_URL
        self.calendar_ready = True


class PrepareAuthRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('STOP_REQUESTED', 'DRIVER_LOST', 'AUTH_BLOCKED', 'CONFIG_BLOCKED'):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, 'GRID_SELECTOR_BLOCKED', set()))
        self.stack.enter_context(patch.object(core, 'ACTIVATE_TAB_DURING_PREPARE', True))
        self.stack.enter_context(patch.object(core, 'NAVIGATION_TIMEOUT_MS', 500))
        self.stack.enter_context(patch.object(core, 'AUTH_WAIT_SECONDS', 1.0))
        self.forbidden = self.stack.enter_context(patch.object(
            core, 'is_403_page', AsyncMock(return_value=False)))
        self.fallback = self.stack.enter_context(patch.object(
            core, 'try_w3_backup_login', AsyncMock(return_value='manual')))
        self.context = SimpleNamespace()

    async def test_reload_reauthenticates_then_continues_same_page_without_extra_reload(self):
        page = Page(self.context, core.BOOKING_URL, ready=True)
        async def recover(work_page, *, stopped):
            self.assertFalse(stopped())
            self.assertFalse(self.context._booking_activation_lock.locked())
            work_page.recover()
            return 'submitted'
        self.fallback.side_effect = recover
        async def checked_date(work_page, _):
            self.assertTrue(work_page.calendar_ready)
            self.assertEqual(work_page.url, core.BOOKING_URL)
        selected_date = self.stack.enter_context(patch.object(
            core, 'select_date', AsyncMock(side_effect=checked_date)))
        for name in ('select_start_time', 'select_duration', 'open_room_list',
                     'verify_room_identity', 'choose_room_cells', 'save_debug'):
            self.stack.enter_context(patch.object(core, name, AsyncMock()))
        for name in ('wait_selection_confirmation_ready', 'confirm_room_selection',
                     'is_final_book_button_clickable'):
            self.stack.enter_context(patch.object(core, name, AsyncMock(return_value=True)))
        target = core.Target(date(2030, 1, 2), '14:00', '18:00', ('F5',))
        room = core.RoomSpec('F5', '7F-A05R', 1)
        self.assertTrue(await core.prepare_page(page, room, target))
        page.reload.assert_awaited_once()
        page.goto.assert_not_awaited()
        selected_date.assert_awaited_once()
        self.fallback.assert_awaited_once()
        self.assertGreaterEqual(page.bring_to_front.await_count, 4)
        self.assertFalse(core.CONFIG_BLOCKED.is_set())

    async def test_redirect_beginning_after_calendar_wait_is_detected(self):
        page = Page(self.context, core.BOOKING_URL)
        async def redirect_later():
            await asyncio.sleep(0.03)
            page.url = LOGIN_URL
        async def recover(work_page, **kwargs):
            work_page.recover()
        redirect = asyncio.create_task(redirect_later())
        self.fallback.side_effect = recover
        await core.activate_booking_page(page)
        await redirect
        self.fallback.assert_awaited_once()
        self.assertTrue(page.calendar_ready)
        self.assertFalse(self.context._booking_activation_lock.locked())

    async def test_waiting_login_does_not_block_another_ready_page(self):
        login_page = Page(self.context, LOGIN_URL)
        ready_page = Page(self.context, core.BOOKING_URL, ready=True)
        entered, finish = asyncio.Event(), asyncio.Event()
        async def recover(work_page, **kwargs):
            entered.set()
            await finish.wait()
            work_page.recover()
        self.fallback.side_effect = recover
        waiting = asyncio.create_task(core.activate_booking_page(login_page))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            await asyncio.wait_for(core.activate_booking_page(ready_page), timeout=0.2)
            self.assertFalse(waiting.done())
            finish.set()
            await asyncio.wait_for(waiting, timeout=1)
        finally:
            if not waiting.done():
                waiting.cancel()
            await asyncio.gather(waiting, return_exceptions=True)

    async def test_missing_credentials_still_allows_manual_return_without_navigation(self):
        page = Page(self.context, LOGIN_URL)
        entered = asyncio.Event()
        async def manual(*args, **kwargs):
            entered.set()
            return 'manual'
        self.fallback.side_effect = manual
        async def complete_manually():
            await entered.wait()
            page.recover()
        manual_task = asyncio.create_task(complete_manually())
        await core.activate_booking_page(page)
        await manual_task
        page.reload.assert_not_awaited()
        page.goto.assert_not_awaited()
        self.assertTrue(page.calendar_ready)

    async def test_authentication_timeout_is_distinct_from_missing_calendar(self):
        page = Page(self.context, LOGIN_URL)
        with patch.object(core, 'AUTH_WAIT_SECONDS', 0.05):
            with self.assertRaisesRegex(RuntimeError, '登录尚未恢复'):
                await asyncio.wait_for(core.activate_booking_page(page), timeout=0.5)
        self.assertFalse(self.context._booking_activation_lock.locked())
        self.assertFalse(core.CONFIG_BLOCKED.is_set())
        page.reload.assert_not_awaited()

    async def test_second_redirect_before_calendar_ready_keeps_original_auth_deadline(self):
        page = Page(self.context, LOGIN_URL)
        clock = [0.0]
        async def advance(seconds):
            clock[0] += seconds
            await asyncio.sleep(0)
        async def first_return(work_page, **kwargs):
            if self.fallback.await_count == 1:
                work_page.url = core.BOOKING_URL
            return 'manual'
        async def calendar_not_ready():
            # Authentication returned to the booking URL, but the calendar has
            # not initialized when another SSO navigation starts.
            self.assertEqual(page.url, core.BOOKING_URL)
            page.url = LOGIN_URL
            return False
        calendar = SimpleNamespace(is_visible=AsyncMock(side_effect=calendar_not_ready))
        calendar.first = calendar
        self.fallback.side_effect = first_return
        with patch.object(core, 'monotonic_time', SimpleNamespace(monotonic=lambda: clock[0])), \
                patch.object(core, 'sleep_or_stop', advance), \
                patch.object(page, 'locator', return_value=calendar):
            with self.assertRaisesRegex(RuntimeError, '登录尚未恢复'):
                await core.activate_booking_page(page)
        self.assertAlmostEqual(clock[0], core.AUTH_WAIT_SECONDS)
        self.assertGreaterEqual(page.bring_to_front.await_count, 2)
        calendar.is_visible.assert_awaited_once()
        self.assertFalse(self.context._booking_activation_lock.locked())

    async def test_normal_page_missing_calendar_keeps_normal_bounded_failure(self):
        page = Page(self.context, core.BOOKING_URL)
        with patch.object(core, 'NAVIGATION_TIMEOUT_MS', 50):
            with self.assertRaisesRegex(core.PlaywrightTimeout, '表单初始化'):
                await asyncio.wait_for(core.activate_booking_page(page), timeout=0.5)
        self.fallback.assert_not_awaited()
        self.assertFalse(self.context._booking_activation_lock.locked())

    async def test_403_authentication_preserves_global_auth_block(self):
        page = Page(self.context, LOGIN_URL)
        self.forbidden.return_value = True
        with self.assertRaises(core.AuthBlocked):
            await core.activate_booking_page(page)
        self.assertTrue(core.AUTH_BLOCKED.is_set())
        self.fallback.assert_not_awaited()
        self.assertFalse(self.context._booking_activation_lock.locked())

    async def test_user_stop_interrupts_manual_authentication_wait(self):
        page = Page(self.context, LOGIN_URL)
        async def request_stop(*args, **kwargs):
            core.STOP_REQUESTED.set()
            return 'manual'
        self.fallback.side_effect = request_stop
        with self.assertRaises(core.StopRequested):
            await core.activate_booking_page(page)
        self.fallback.assert_awaited_once()
        self.assertFalse(self.context._booking_activation_lock.locked())

    async def test_external_cancellation_during_authentication_releases_lock(self):
        page = Page(self.context, LOGIN_URL)
        entered = asyncio.Event()
        async def pending(*args, **kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.fallback.side_effect = pending
        task = asyncio.create_task(core.activate_booking_page(page))
        await entered.wait()
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse(self.context._booking_activation_lock.locked())


if __name__ == '__main__':
    unittest.main()
