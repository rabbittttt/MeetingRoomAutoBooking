"""Bounded initial authentication retries, without network or real credentials."""
import asyncio
from contextlib import ExitStack
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import booking_core as core
from tests.test_prepare_auth_recovery import Page as RecoveryPage, LOGIN_URL


class InitialAuthenticationRetryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('STOP_REQUESTED', 'DRIVER_LOST', 'AUTH_BLOCKED', 'CONFIG_BLOCKED'):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        for name, value in (('AUTH_INITIAL_ATTEMPTS', 3), ('AUTH_WAIT_SECONDS', 0.05),
                            ('AUTH_RETRY_DELAY_SECONDS', 0), ('NAVIGATION_TIMEOUT_MS', 50)):
            self.stack.enter_context(patch.object(core, name, value))
        self.activation = self.stack.enter_context(patch.object(core, 'activate_booking_page', AsyncMock()))
        self.forbidden = self.stack.enter_context(patch.object(core, 'is_403_page', AsyncMock(return_value=False)))
        self.fallback = self.stack.enter_context(patch.object(core, 'try_w3_backup_login', AsyncMock(return_value='waiting')))
        self.notify = self.stack.enter_context(patch.object(core, 'notify_w3_authenticated', Mock()))
        self.debug = self.stack.enter_context(patch.object(core, 'save_debug', AsyncMock()))
        self.button = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        self.page = SimpleNamespace(context=SimpleNamespace(), goto=AsyncMock(),
                                    get_by_role=lambda *args, **kwargs: self.button)

    async def test_navigation_timeout_retries_same_page_then_succeeds(self):
        async def navigate(*args, **kwargs):
            if self.page.goto.await_count == 1:
                raise core.PlaywrightTimeout('temporary navigation timeout')
            self.button.is_visible.return_value = True
        self.page.goto.side_effect = navigate
        self.assertTrue(await core.auth_preflight(self.page))
        self.assertEqual(self.page.goto.await_count, 2)
        self.notify.assert_called_once_with(self.page)
        self.debug.assert_not_awaited()
        self.assertIsNone(core.run_stop_reason())

    async def test_auth_wait_timeout_retries_without_replacing_shared_fallback_state(self):
        shared = object()
        self.page.context._booking_w3_fallback = shared
        async def navigate(*args, **kwargs):
            self.assertIs(self.page.context._booking_w3_fallback, shared)
            self.button.is_visible.return_value = self.page.goto.await_count == 2
        self.page.goto.side_effect = navigate
        self.assertTrue(await core.auth_preflight(self.page))
        self.assertEqual(self.page.goto.await_count, 2)
        self.fallback.assert_awaited_once()
        self.notify.assert_called_once_with(self.page)
        self.assertIs(self.page.context._booking_w3_fallback, shared)

    async def test_three_failed_rounds_end_without_unbounded_retry(self):
        self.assertFalse(await core.auth_preflight(self.page))
        self.assertEqual(self.page.goto.await_count, 3)
        self.notify.assert_not_called()
        self.debug.assert_awaited_once_with(self.page, '登录未完成', mask_inputs=True)
        self.assertIsNone(core.run_stop_reason())

    async def test_each_round_bounds_navigation_as_well_as_form_wait(self):
        cancelled = []
        async def pending_navigation(*args, **kwargs):
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.append(True)
        self.page.goto.side_effect = pending_navigation
        self.assertFalse(await asyncio.wait_for(core.auth_preflight(self.page), timeout=1))
        self.assertEqual(self.page.goto.await_count, 3)
        self.assertEqual(len(cancelled), 3)
        self.fallback.assert_not_awaited()
        self.notify.assert_not_called()

    async def test_user_stop_interrupts_inflight_navigation_without_next_round(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def pending_navigation(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.page.goto.side_effect = pending_navigation
        with patch.object(core, 'AUTH_WAIT_SECONDS', 60):
            task = asyncio.create_task(core.auth_preflight(self.page))
            await asyncio.wait_for(entered.wait(), timeout=1)
            core.STOP_REQUESTED.set()
            with self.assertRaises(core.StopRequested):
                await asyncio.wait_for(task, timeout=0.5)
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.page.goto.await_count, 1)
        self.debug.assert_not_awaited()

    async def test_stop_during_retry_delay_does_not_navigate_again(self):
        self.page.goto.side_effect = core.PlaywrightTimeout('temporary navigation timeout')
        entered = asyncio.Event()
        original_sleep = core.sleep_or_stop
        async def delay(seconds):
            entered.set()
            await original_sleep(seconds)
        with patch.object(core, 'AUTH_RETRY_DELAY_SECONDS', 60), patch.object(core, 'sleep_or_stop', delay):
            task = asyncio.create_task(core.auth_preflight(self.page))
            await asyncio.wait_for(entered.wait(), timeout=1)
            core.STOP_REQUESTED.set()
            with self.assertRaises(core.StopRequested):
                await asyncio.wait_for(task, timeout=0.5)
        self.assertEqual(self.page.goto.await_count, 1)
        self.debug.assert_not_awaited()

    async def test_403_stops_immediately_instead_of_retrying(self):
        self.forbidden.return_value = True
        self.assertFalse(await core.auth_preflight(self.page))
        self.assertEqual(self.page.goto.await_count, 1)
        self.assertTrue(core.AUTH_BLOCKED.is_set())
        self.fallback.assert_not_awaited()
        self.debug.assert_awaited_once_with(self.page, '登录403', mask_inputs=True)

    async def test_driver_disconnect_stops_without_next_navigation_or_screenshot(self):
        for location in ('navigation', 'form_check'):
            with self.subTest(location=location):
                core.DRIVER_LOST.clear()
                self.page.goto.reset_mock(side_effect=True)
                self.button.is_visible.reset_mock(side_effect=True)
                error = RuntimeError('Connection closed while reading from the driver')
                control = self.page.goto if location == 'navigation' else self.button.is_visible
                control.side_effect = error
                self.assertFalse(await core.auth_preflight(self.page))
                self.assertEqual(self.page.goto.await_count, 1)
                self.assertTrue(core.DRIVER_LOST.is_set())
                self.debug.assert_not_awaited()
                self.notify.assert_not_called()

    async def test_external_cancellation_stops_the_active_round(self):
        entered, cancelled = asyncio.Event(), asyncio.Event()
        async def pending_navigation(*args, **kwargs):
            entered.set()
            try:
                await asyncio.Event().wait()
            finally:
                cancelled.set()
        self.page.goto.side_effect = pending_navigation
        task = asyncio.create_task(core.auth_preflight(self.page))
        await asyncio.wait_for(entered.wait(), timeout=1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertTrue(cancelled.is_set())
        self.assertEqual(self.page.goto.await_count, 1)


class AuthenticationSuccessNotificationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('STOP_REQUESTED', 'DRIVER_LOST', 'AUTH_BLOCKED', 'CONFIG_BLOCKED'):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, 'AUTH_WAIT_SECONDS', 0.8))
        self.stack.enter_context(patch.object(core, 'NAVIGATION_TIMEOUT_MS', 100))
        self.stack.enter_context(patch.object(core, 'ACTIVATE_TAB_DURING_PREPARE', True))
        self.stack.enter_context(patch.object(core, 'is_403_page', AsyncMock(return_value=False)))
        self.notify = self.stack.enter_context(patch.object(core, 'notify_w3_authenticated', Mock()))

    async def test_normal_healthy_preparation_does_not_reset_another_pages_retry_budget(self):
        page = RecoveryPage(SimpleNamespace(), core.BOOKING_URL, ready=True)
        await core.activate_booking_page(page)
        self.notify.assert_not_called()

    async def test_return_to_booking_url_alone_is_not_authentication_success(self):
        page = RecoveryPage(SimpleNamespace(), LOGIN_URL)
        async def return_without_calendar(*args, **kwargs):
            page.url = core.BOOKING_URL
            return 'submitted'
        with patch.object(core, 'try_w3_backup_login', AsyncMock(side_effect=return_without_calendar)):
            with self.assertRaisesRegex(RuntimeError, '日历仍未就绪'):
                await core.activate_booking_page(page)
        self.notify.assert_not_called()

    async def test_recovery_notifies_only_after_calendar_is_visible(self):
        page = RecoveryPage(SimpleNamespace(), LOGIN_URL)
        async def recover(*args, **kwargs):
            page.recover()
            return 'submitted'
        with patch.object(core, 'try_w3_backup_login', AsyncMock(side_effect=recover)):
            await core.activate_booking_page(page)
        self.notify.assert_called_once_with(page)

    async def test_new_work_page_notifies_after_add_room_button_is_visible(self):
        page = RecoveryPage(SimpleNamespace(), core.BOOKING_URL)
        page.button.count = AsyncMock(return_value=1)
        page.button.first = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        pool = core.PagePool(page.context, object())
        await pool._authenticate_new_page(page, 2)
        self.notify.assert_called_once_with(page)


if __name__ == '__main__':
    unittest.main()
