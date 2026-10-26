"""Offline regressions for yielding watch pages to a release batch.

No browser or booking service is used. Preparation can be cancelled, but a page
that has entered final submission must settle its receipt before handing over.
"""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import booking_core as core


class _Page:
    def __init__(self, name="room"):
        self.name = name
        self.final = SimpleNamespace(count=AsyncMock(return_value=1),
                                     is_visible=AsyncMock(return_value=True),
                                     click=AsyncMock())
        self.success = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        self.success.filter = lambda **kwargs: SimpleNamespace(first=self.success)
        self.empty = SimpleNamespace(count=AsyncMock(return_value=0))
        self.empty.filter = lambda **kwargs: self.empty

    def get_by_role(self, *args, **kwargs):
        return self.final

    def get_by_text(self, *args, **kwargs):
        return self.success

    def locator(self, *args, **kwargs):
        return self.empty


class WatchHandoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.tasks = []
        self.gates = []
        self.now = datetime(2030, 1, 1, 8, 30, tzinfo=core.CN_TZ)
        self.target = core.Target(self.now.date() + timedelta(days=7), "14:00", "16:00", ("F5",))
        self.room = core.RoomSpec("F5", "7F-A05R", 1)
        self.job = core.BookingJob(self.target, asyncio.Event())
        self.page = _Page()
        self.priority = asyncio.Event()
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "DRY_RUN", False))
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.now))
        self.clickable = self.stack.enter_context(patch.object(core, "is_final_book_button_clickable", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(core, "is_room_occupied_state", AsyncMock(return_value=False)))
        self.dismiss = self.stack.enter_context(patch.object(core, "dismiss_booking_failure_dialog", AsyncMock(return_value=True)))
        self.sleep = self.stack.enter_context(patch.object(core, "sleep_or_stop", AsyncMock()))
        self.popup = self.stack.enter_context(patch.object(core, "booking_failure_dialog", AsyncMock(return_value=("", ""))))
        self.feedback = self.stack.enter_context(patch.object(core, "booking_feedback", AsyncMock(return_value="timeout")))
        self.prepare = self.stack.enter_context(patch.object(core, "prepare_page", AsyncMock(return_value=True)))
        self.results = self.stack.enter_context(patch.object(core, "booking_result", Mock()))

    async def asyncTearDown(self):
        for gate in self.gates:
            gate.set()
        for task in self.tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.tasks, return_exceptions=True)

    async def close_stream(self, stream):
        await stream.aclose()

    def gate(self):
        event = asyncio.Event()
        self.gates.append(event)
        return event

    def start(self, coroutine):
        task = asyncio.create_task(coroutine)
        self.tasks.append(task)
        return task

    async def entered(self, event, task):
        waiter = asyncio.create_task(event.wait())
        try:
            completed, _ = await asyncio.wait((waiter, task), timeout=3, return_when=asyncio.FIRST_COMPLETED)
            if task in completed:
                task.result()
            self.assertTrue(event.is_set(), "worker did not reach the expected phase")
        finally:
            waiter.cancel()
            await asyncio.gather(waiter, return_exceptions=True)

    async def test_priority_at_last_button_check_prevents_new_submission(self):
        checks = 0
        async def check(button):
            nonlocal checks
            checks += 1
            if checks == 2:
                self.priority.set()
            return True
        self.clickable.side_effect = check
        status = await core.confirm_and_book(self.page, self.room, self.job, yield_requested=self.priority)
        self.assertEqual(status, "yielded")
        self.page.final.click.assert_not_awaited()
        self.feedback.assert_not_awaited()

    async def test_priority_already_set_prevents_submission(self):
        self.priority.set()
        status = await core.confirm_and_book(self.page, self.room, self.job, yield_requested=self.priority)
        self.assertEqual(status, "yielded")
        self.page.final.click.assert_not_awaited()

    async def _inflight_receipt(self, feedback, expected_status):
        receiving = asyncio.Event()
        allow_receipt = self.gate()
        cancelled = []
        async def wait_receipt(page, timeout_ms):
            self.assertEqual(timeout_ms, core.SUCCESS_TIMEOUT_MS)
            receiving.set()
            try:
                await allow_receipt.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            return feedback
        self.feedback.side_effect = wait_receipt
        task = self.start(core.confirm_and_book(self.page, self.room, self.job, yield_requested=self.priority))
        await self.entered(receiving, task)
        self.priority.set()
        await asyncio.sleep(0)
        self.assertFalse(task.done(), "priority must not discard an in-flight receipt")
        self.assertEqual(cancelled, [])
        allow_receipt.set()
        self.assertEqual(await asyncio.wait_for(task, 3), expected_status)
        self.assertEqual(self.page.final.click.await_count, 1)
        self.assertEqual(self.feedback.await_count, 1)
        self.assertEqual(cancelled, [])

    async def test_inflight_success_wins_over_handover_request(self):
        await self._inflight_receipt("success", "success")
        self.assertTrue(self.job.done.is_set())
        self.assertFalse(self.job.uncertain.is_set())

    async def test_inflight_unknown_result_stays_uncertain_on_handover(self):
        await self._inflight_receipt("timeout", "uncertain")
        self.assertTrue(self.job.uncertain.is_set())
        self.assertFalse(self.job.done.is_set())

    async def test_inflight_not_open_yields_without_retry(self):
        await self._inflight_receipt("not_open", "yielded")
        self.sleep.assert_not_awaited()
        self.assertFalse(self.job.done.is_set())
        self.assertFalse(self.job.uncertain.is_set())

    async def test_inflight_explicit_failure_yields_without_retry(self):
        await self._inflight_receipt("failure", "yielded")
        self.sleep.assert_not_awaited()
        self.assertFalse(self.job.uncertain.is_set())

    async def test_no_handover_argument_preserves_ordinary_submission(self):
        self.feedback.return_value = "success"
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "success")
        self.assertEqual(self.page.final.click.await_count, 1)

    async def test_watch_priority_before_cycle_does_not_prepare_or_submit(self):
        self.priority.set()
        with patch.object(core, "confirm_and_book", AsyncMock()) as submit:
            await core.watch_cycle([self.page], self.job, [self.room], yield_requested=self.priority)
            self.prepare.assert_not_awaited()
            submit.assert_not_awaited()

    async def test_watch_cancels_slow_preparation_and_waits_for_cleanup(self):
        started = asyncio.Event()
        cleaning = asyncio.Event()
        never_ready = self.gate()
        cleanup_allowed = self.gate()
        cancelled = []
        async def slow_prepare(*args, **kwargs):
            started.set()
            try:
                await never_ready.wait()
                return True
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
            finally:
                cleaning.set()
                await cleanup_allowed.wait()
        self.prepare.side_effect = slow_prepare
        with patch.object(core, "confirm_and_book", AsyncMock()) as submit:
            task = self.start(core.watch_cycle([self.page], self.job, [self.room], yield_requested=self.priority))
            await self.entered(started, task)
            self.priority.set()
            await self.entered(cleaning, task)
            self.assertFalse(task.done(), "page must not be released while preparation cleanup is pending")
            submit.assert_not_awaited()
            cleanup_allowed.set()
            await asyncio.wait_for(task, 3)
            self.assertEqual(cancelled, [True])
            submit.assert_not_awaited()

    async def test_priority_set_when_prepare_finishes_prevents_submit(self):
        async def prepare(*args, **kwargs):
            self.priority.set()
            return True
        self.prepare.side_effect = prepare
        with patch.object(core, "confirm_and_book", AsyncMock()) as submit:
            await core.watch_cycle([self.page], self.job, [self.room], yield_requested=self.priority)
            submit.assert_not_awaited()

    async def test_watch_keeps_inflight_page_until_success_while_cancelling_other_prepare(self):
        slow_page = _Page("slow")
        slow_room = core.RoomSpec("F5", "7F-A28R", 2)
        slow_started = asyncio.Event()
        slow_cancelled = asyncio.Event()
        receiving = asyncio.Event()
        never_ready = self.gate()
        allow_receipt = self.gate()
        receipt_cancelled = []
        async def prepare(page, *args, **kwargs):
            if page is slow_page:
                slow_started.set()
                try:
                    await never_ready.wait()
                except asyncio.CancelledError:
                    slow_cancelled.set()
                    raise
            return True
        async def feedback(page, timeout_ms):
            self.assertIs(page, self.page)
            receiving.set()
            try:
                await allow_receipt.wait()
            except asyncio.CancelledError:
                receipt_cancelled.append(True)
                raise
            return "success"
        self.prepare.side_effect = prepare
        self.feedback.side_effect = feedback
        task = self.start(core.watch_cycle([self.page, slow_page], self.job, [self.room, slow_room],
                                          yield_requested=self.priority))
        await self.entered(slow_started, task)
        await self.entered(receiving, task)
        self.priority.set()
        await self.entered(slow_cancelled, task)
        self.assertFalse(task.done(), "in-flight page must retain ownership until its receipt settles")
        self.assertEqual(receipt_cancelled, [])
        allow_receipt.set()
        await asyncio.wait_for(task, 3)
        self.assertTrue(self.job.done.is_set())
        self.assertEqual(self.page.final.click.await_count, 1)
        slow_page.final.click.assert_not_awaited()
        self.assertEqual(receipt_cancelled, [])

    async def test_watch_does_not_cancel_final_click_already_in_progress(self):
        clicking = asyncio.Event()
        allow_click = self.gate()
        cancelled = []
        async def click(**kwargs):
            clicking.set()
            try:
                await allow_click.wait()
            except asyncio.CancelledError:
                cancelled.append(True)
                raise
        self.page.final.click.side_effect = click
        self.feedback.return_value = "success"
        task = self.start(core.watch_cycle([self.page], self.job, [self.room], yield_requested=self.priority))
        await self.entered(clicking, task)
        self.priority.set()
        await asyncio.sleep(0)
        self.assertFalse(task.done())
        self.assertEqual(cancelled, [])
        allow_click.set()
        await asyncio.wait_for(task, 3)
        self.assertTrue(self.job.done.is_set())
        self.feedback.assert_awaited_once()
        self.assertEqual(cancelled, [])


    def pool_with_new_page(self):
        new_page = _Page("authenticating")
        new_page.final.first = new_page.final
        new_page.goto = AsyncMock()
        new_page.close = AsyncMock()
        context = SimpleNamespace(_booking_activation_lock=asyncio.Lock())
        async def new_page_locked():
            self.assertTrue(context._booking_activation_lock.locked(),
                            "new_page must hold the shared activation lock")
            return new_page
        context.new_page = AsyncMock(side_effect=new_page_locked)
        pool = core.PagePool(context, self.page)
        activation = self.stack.enter_context(patch.object(core, "activate_booking_page", AsyncMock()))
        self.stack.enter_context(patch.object(core, "is_403_page", AsyncMock(return_value=False)))
        return pool, context, new_page, activation

    async def test_real_page_pool_yields_existing_page_then_cancels_unfinished_authentication(self):
        pool, context, new_page, activation = self.pool_with_new_page()
        authenticating = asyncio.Event()
        authentication_cancelled = asyncio.Event()
        allow_auth = self.gate()
        async def goto(*args, **kwargs):
            authenticating.set()
            try:
                await allow_auth.wait()
            except asyncio.CancelledError:
                authentication_cancelled.set()
                raise
        new_page.goto.side_effect = goto
        stream = pool.iter_ready(2, interrupted=self.priority.is_set)
        self.addAsyncCleanup(self.close_stream, stream)
        self.assertIs(await anext(stream), self.page)
        context.new_page.assert_not_awaited()
        activation.assert_not_awaited()
        task = self.start(anext(stream))
        await self.entered(authenticating, task)
        self.priority.set()
        with self.assertRaises(core.PreparationInterrupted):
            await asyncio.wait_for(task, 3)
        self.assertTrue(authentication_cancelled.is_set())
        new_page.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.page])
        self.assertFalse(core.AUTH_BLOCKED.is_set())
        self.assertFalse(context._booking_activation_lock.locked())

    async def test_real_page_pool_retains_authenticated_page_for_reuse(self):
        pool, context, new_page, activation = self.pool_with_new_page()
        pages = [page async for page in pool.iter_ready(2)]
        self.assertEqual(pages, [self.page, new_page])
        self.assertEqual(pool._pages, pages)
        context.new_page.assert_awaited_once()
        new_page.goto.assert_awaited_once()
        new_page.close.assert_not_awaited()
        self.assertEqual(activation.await_count, 2)
        again = [page async for page in pool.iter_ready(2)]
        self.assertEqual(again, pages)
        context.new_page.assert_awaited_once()
        new_page.goto.assert_awaited_once()
        new_page.close.assert_not_awaited()
        self.assertFalse(core.AUTH_BLOCKED.is_set())

    async def test_real_page_pool_authentication_does_not_block_first_submission_or_cancel_its_receipt(self):
        pool, context, new_page, activation = self.pool_with_new_page()
        second_room = core.RoomSpec("F5", "7F-A28R", 2)
        authenticating = asyncio.Event()
        authentication_cancelled = asyncio.Event()
        page_closed = asyncio.Event()
        receiving = asyncio.Event()
        allow_auth = self.gate()
        allow_receipt = self.gate()
        receipt_cancelled = []
        async def goto(*args, **kwargs):
            authenticating.set()
            try:
                await allow_auth.wait()
            except asyncio.CancelledError:
                authentication_cancelled.set()
                raise
        async def close():
            page_closed.set()
        async def feedback(page, timeout_ms):
            self.assertIs(page, self.page)
            receiving.set()
            try:
                await allow_receipt.wait()
            except asyncio.CancelledError:
                receipt_cancelled.append(True)
                raise
            return "success"
        new_page.goto.side_effect = goto
        new_page.close.side_effect = close
        self.feedback.side_effect = feedback
        stream = pool.iter_ready(2, interrupted=self.priority.is_set)
        self.addAsyncCleanup(self.close_stream, stream)
        task = self.start(core.watch_cycle(stream, self.job, [self.room, second_room],
                                          yield_requested=self.priority))
        await self.entered(authenticating, task)
        await self.entered(receiving, task)
        self.assertEqual(self.page.final.click.await_count, 1)
        self.assertEqual(pool._pages, [self.page])
        self.assertFalse(allow_auth.is_set(), "first submission must not wait for the second authentication")
        self.priority.set()
        await self.entered(page_closed, task)
        self.assertTrue(authentication_cancelled.is_set())
        self.assertFalse(task.done(), "watch pages remain owned until the first receipt settles")
        self.assertEqual(receipt_cancelled, [])
        self.assertFalse(core.AUTH_BLOCKED.is_set())
        allow_receipt.set()
        await asyncio.wait_for(task, 3)
        self.assertTrue(self.job.done.is_set())
        self.assertEqual(self.page.final.click.await_count, 1)
        new_page.final.click.assert_not_awaited()
        new_page.close.assert_awaited_once()
        self.assertEqual(pool._pages, [self.page])
        self.assertEqual(receipt_cancelled, [])


    async def test_real_page_pool_can_yield_while_waiting_for_activation_lock(self):
        pool, context, new_page, activation = self.pool_with_new_page()
        waiting = asyncio.Event()
        class ObservedLock(asyncio.Lock):
            async def acquire(self):
                if self.locked():
                    waiting.set()
                return await super().acquire()
        lock = ObservedLock()
        context._booking_activation_lock = lock
        await lock.acquire()
        try:
            stream = pool.iter_ready(2, interrupted=self.priority.is_set)
            self.addAsyncCleanup(self.close_stream, stream)
            self.assertIs(await anext(stream), self.page)
            task = self.start(anext(stream))
            await self.entered(waiting, task)
            self.priority.set()
            with self.assertRaises(core.PreparationInterrupted):
                await asyncio.wait_for(task, 3)
            context.new_page.assert_not_awaited()
            activation.assert_not_awaited()
            new_page.close.assert_not_awaited()
            self.assertEqual(pool._pages, [self.page])
            self.assertTrue(lock.locked(), "cancelling a waiter must not release another page's lock")
            self.assertFalse(core.AUTH_BLOCKED.is_set())
        finally:
            lock.release()

    async def test_real_page_pool_closes_page_created_after_handover_without_authenticating_it(self):
        pool, context, new_page, activation = self.pool_with_new_page()
        creating = asyncio.Event()
        page_closed = asyncio.Event()
        allow_creation = self.gate()
        creation_cancelled = []
        async def delayed_new_page():
            self.tasks.append(asyncio.current_task())
            self.assertTrue(context._booking_activation_lock.locked())
            creating.set()
            try:
                await allow_creation.wait()
            except asyncio.CancelledError:
                creation_cancelled.append(True)
                raise
            return new_page
        async def close():
            page_closed.set()
        context.new_page.side_effect = delayed_new_page
        new_page.close.side_effect = close
        stream = pool.iter_ready(2, interrupted=self.priority.is_set)
        self.addAsyncCleanup(self.close_stream, stream)
        self.assertIs(await anext(stream), self.page)
        task = self.start(anext(stream))
        await self.entered(creating, task)
        self.priority.set()
        with self.assertRaises(core.PreparationInterrupted):
            await asyncio.wait_for(task, 3)
        self.assertFalse(allow_creation.is_set(), "handover must not wait for slow new_page to complete")
        self.assertFalse(context._booking_activation_lock.locked())
        self.assertEqual(pool._pages, [self.page])
        self.assertEqual(creation_cancelled, [])
        new_page.close.assert_not_awaited()
        allow_creation.set()
        await asyncio.wait_for(page_closed.wait(), 3)
        await asyncio.gather(*tuple(pool._cleanup_tasks), return_exceptions=True)
        new_page.close.assert_awaited_once()
        new_page.goto.assert_not_awaited()
        activation.assert_not_awaited()
        self.assertEqual(pool._pages, [self.page])
        self.assertEqual(creation_cancelled, [])
        self.assertFalse(core.AUTH_BLOCKED.is_set())


if __name__ == "__main__":
    unittest.main()
