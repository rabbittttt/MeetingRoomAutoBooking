"""Offline recovery checks for a slow new tab; no network or booking requests."""
import asyncio
from contextlib import ExitStack
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking_core as core


def fake_page(error=None):
    button = SimpleNamespace(count=AsyncMock(return_value=1))
    button.first = SimpleNamespace(is_visible=AsyncMock(return_value=True))
    return SimpleNamespace(
        goto=AsyncMock(side_effect=error), close=AsyncMock(),
        get_by_role=lambda *args, **kwargs: button)


class PagePoolRecoveryTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "activate_booking_page", AsyncMock()))
        self.forbidden = self.stack.enter_context(patch.object(core, "is_403_page", AsyncMock(return_value=False)))
        self.screenshot = self.stack.enter_context(patch.object(core, "save_debug", AsyncMock()))
        self.sleep = self.stack.enter_context(patch.object(core, "sleep_or_stop", AsyncMock()))
        self.stack.enter_context(patch.object(core, "AUTH_STAGGER_SECONDS", 0))
        self.stack.enter_context(patch.object(core, "RELEASE_RETRY_SECONDS", 0.01))
        self.first = object()

    def pool(self, *pages):
        context = SimpleNamespace(new_page=AsyncMock(side_effect=list(pages)))
        return core.PagePool(context, self.first)

    def assert_no_global_block(self):
        self.assertFalse(core.AUTH_BLOCKED.is_set())
        self.assertFalse(core.DRIVER_LOST.is_set())

    async def test_navigation_timeout_closes_failed_tab_without_global_auth_block(self):
        failed = fake_page(core.PlaywrightTimeout("navigation timed out"))
        pool = self.pool(failed)
        with self.assertRaises(core.PlaywrightTimeout):
            await pool._new_authenticated_page(2)
        failed.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.first])
        self.assert_no_global_block()

    async def test_missing_form_auth_wait_timeout_is_recoverable(self):
        failed = fake_page()
        pool = self.pool(failed)
        with patch.object(core, "AUTH_WAIT_SECONDS", 0):
            with self.assertRaises(core.PlaywrightTimeout):
                await pool._new_authenticated_page(2)
        failed.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.first])
        self.assert_no_global_block()

    async def test_403_still_blocks_all_modes_and_never_delivers_failed_tab(self):
        failed = fake_page()
        pool = self.pool(failed)
        self.forbidden.return_value = True
        with self.assertRaises(core.AuthBlocked):
            await pool._new_authenticated_page(2)
        self.assertTrue(core.AUTH_BLOCKED.is_set())
        self.assertFalse(core.DRIVER_LOST.is_set())
        failed.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.first])

    async def test_driver_disconnect_still_stops_all_modes_without_screenshot(self):
        failed = fake_page(RuntimeError("Connection closed while reading from the driver"))
        pool = self.pool(failed)
        with self.assertRaises(RuntimeError):
            await pool._new_authenticated_page(2)
        self.assertTrue(core.DRIVER_LOST.is_set())
        self.screenshot.assert_not_awaited()
        failed.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.first])

    async def test_watch_returns_from_failed_allocation_and_authenticates_next_round(self):
        failed, recovered = fake_page(TimeoutError("slow navigation")), fake_page()
        pool = self.pool(failed, recovered)
        deadline = asyncio.Event()
        first_round = [page async for page in pool.iter_ready(2, interrupted=deadline.is_set)]
        self.assertEqual(first_round, [self.first])
        failed.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.first])
        self.assert_no_global_block()
        second_round = [page async for page in pool.iter_ready(2, interrupted=deadline.is_set)]
        self.assertEqual(second_round, [self.first, recovered])
        self.assertEqual(pool._pages, [self.first, recovered])
        recovered.close.assert_not_awaited()
        self.assertEqual(pool.context.new_page.await_count, 2)

    async def test_release_retries_failed_authentication_and_delivers_only_verified_page(self):
        failed, recovered = fake_page(core.PlaywrightTimeout("slow navigation")), fake_page()
        pool = self.pool(failed, recovered)
        deadline = asyncio.Event()
        pages = [page async for page in pool.iter_ready(
            2, interrupted=deadline.is_set, retry_authentication=True)]
        self.assertEqual(pages, [self.first, recovered])
        self.assertEqual(pool.context.new_page.await_count, 2)
        failed.close.assert_awaited_once()
        recovered.goto.assert_awaited_once()
        recovered.close.assert_not_awaited()
        self.sleep.assert_awaited_once()
        self.assert_no_global_block()

    async def test_no_stop_condition_does_not_retry_unboundedly(self):
        failed = fake_page(TimeoutError("slow navigation"))
        pool = self.pool(failed)
        pages = [page async for page in pool.iter_ready(2, retry_authentication=True)]
        self.assertEqual(pages, [self.first])
        self.assertEqual(pool.context.new_page.await_count, 1)
        self.sleep.assert_not_awaited()
        self.assert_no_global_block()

    async def test_existing_worker_success_interrupts_auth_retry_wait(self):
        failed = fake_page(TimeoutError("slow navigation"))
        pool = self.pool(failed)
        done = asyncio.Event()
        retry_wait_started = asyncio.Event()
        hold_retry = asyncio.Event()

        async def retry_wait(seconds):
            retry_wait_started.set()
            await hold_retry.wait()

        self.sleep.side_effect = retry_wait
        stream = pool.iter_ready(2, interrupted=done.is_set, retry_authentication=True)
        self.assertIs(await anext(stream), self.first)
        allocation = asyncio.create_task(anext(stream))
        try:
            await asyncio.wait_for(retry_wait_started.wait(), 1)
            self.assertFalse(allocation.done())
            # The already delivered page can complete while the next page waits.
            done.set()
            with self.assertRaises(core.PreparationInterrupted):
                await asyncio.wait_for(allocation, 1)
            self.assertEqual(pool.context.new_page.await_count, 1)
            self.assertEqual(pool._pages, [self.first])
            failed.close.assert_awaited_once()
            self.assert_no_global_block()
        finally:
            allocation.cancel()
            await asyncio.gather(allocation, return_exceptions=True)
            await stream.aclose()

    async def test_deadline_at_timeout_stops_auth_retry_before_new_page(self):
        deadline = asyncio.Event()
        failed = fake_page()

        async def timeout(*args, **kwargs):
            deadline.set()
            raise core.PlaywrightTimeout("slow navigation")

        failed.goto.side_effect = timeout
        pool = self.pool(failed)
        stream = pool.iter_ready(2, interrupted=deadline.is_set, retry_authentication=True)
        self.assertIs(await anext(stream), self.first)
        with self.assertRaises(core.PreparationInterrupted):
            await anext(stream)
        self.assertEqual(pool.context.new_page.await_count, 1)
        failed.close.assert_awaited_once()
        self.assert_no_global_block()

    async def test_cancelled_authentication_is_closed_and_not_retried(self):
        entered = asyncio.Event()
        stalled = asyncio.Event()
        failed = fake_page()

        async def wait_forever(*args, **kwargs):
            entered.set()
            await stalled.wait()

        failed.goto.side_effect = wait_forever
        pool = self.pool(failed)
        task = asyncio.create_task(pool._new_authenticated_page(2))
        await asyncio.wait_for(entered.wait(), 1)
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        failed.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.first])
        self.assert_no_global_block()

    async def test_unknown_error_is_reported_without_treating_it_as_auth_denial(self):
        failed = fake_page(ValueError("unexpected page state"))
        pool = self.pool(failed)
        with self.assertRaises(ValueError):
            await pool._new_authenticated_page(2)
        failed.close.assert_awaited_once()
        self.assert_no_global_block()

    async def test_global_environment_failure_interrupts_authentication_without_caller_check(self):
        for flag in ("DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            with self.subTest(flag=flag):
                entered = asyncio.Event()
                stalled = asyncio.Event()
                failed = fake_page()

                async def wait_forever(*args, **kwargs):
                    entered.set()
                    await stalled.wait()

                failed.goto.side_effect = wait_forever
                pool = self.pool(failed)
                stream = pool.iter_ready(2, interrupted=lambda: False)
                self.assertIs(await anext(stream), self.first)
                allocation = asyncio.create_task(anext(stream))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    getattr(core, flag).set()
                    with self.assertRaises(core.PreparationInterrupted):
                        await asyncio.wait_for(allocation, 1)
                    failed.close.assert_awaited_once()
                    self.assertEqual(pool._pages, [self.first])
                finally:
                    allocation.cancel()
                    await asyncio.gather(allocation, return_exceptions=True)
                    await stream.aclose()
                    getattr(core, flag).clear()

    async def test_global_environment_failure_interrupts_retry_wait_without_caller_check(self):
        for flag in ("DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            with self.subTest(flag=flag):
                entered = asyncio.Event()
                stalled = asyncio.Event()
                failed = fake_page(TimeoutError("slow navigation"))

                async def wait_forever(seconds):
                    entered.set()
                    await stalled.wait()

                self.sleep.side_effect = wait_forever
                pool = self.pool(failed)
                stream = pool.iter_ready(2, interrupted=lambda: False, retry_authentication=True)
                self.assertIs(await anext(stream), self.first)
                allocation = asyncio.create_task(anext(stream))
                try:
                    await asyncio.wait_for(entered.wait(), 1)
                    getattr(core, flag).set()
                    with self.assertRaises(core.PreparationInterrupted):
                        await asyncio.wait_for(allocation, 1)
                    self.assertEqual(pool.context.new_page.await_count, 1)
                    failed.close.assert_awaited_once()
                finally:
                    allocation.cancel()
                    await asyncio.gather(allocation, return_exceptions=True)
                    await stream.aclose()
                    getattr(core, flag).clear()


if __name__ == "__main__":
    unittest.main()
