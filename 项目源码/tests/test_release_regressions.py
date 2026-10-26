"""Offline release regression cases drawn from the 13-room failure log."""
import asyncio
from contextlib import ExitStack
from datetime import date, timedelta
import logging
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import booking_core as core


class ReleaseFailureRegressionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.room = core.RoomSpec("F5-1A", "1F-A13R", 4)
        self.target = core.Target(date(2030, 10, 21), "10:00", "12:00", ("F5-1A",))
        self.opened = core.at_cn(date(2030, 10, 18), "08:30:00")
        self.clock = self.opened - timedelta(seconds=5)
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.clock))
        self.stack.enter_context(patch.object(core, "RELEASE_OPEN_AT", "08:30:00"))
        self.stack.enter_context(patch.object(core, "DRY_RUN", True))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "GRID_SELECTOR_BLOCKED", set()))
        self.stack.enter_context(patch.object(core, "SELECTION_DIAGNOSTICS_RECORDED", set()))

    def setup_preparation(self):
        button = SimpleNamespace(wait_for=AsyncMock(), click=AsyncMock())
        page = SimpleNamespace(url="about:blank", goto=AsyncMock(),
                               get_by_role=lambda *args, **kwargs: button)
        mocks = {}
        for name in ("activate_booking_page", "select_date", "select_start_time", "select_duration",
                     "open_room_list", "verify_room_identity", "choose_room_cells", "save_debug",
                     "save_selection_diagnostic_once"):
            mocks[name] = self.stack.enter_context(patch.object(core, name, AsyncMock()))
        for name, value in (("is_403_page", False), ("wait_selection_confirmation_ready", True),
                            ("confirm_room_selection", True), ("is_final_book_button_clickable", True),
                            ("watch_confirm_disabled", False)):
            mocks[name] = self.stack.enter_context(patch.object(core, name, AsyncMock(return_value=value)))
        return page, button, mocks

    async def test_transient_grid_failure_is_diagnosed_and_next_attempt_can_prepare(self):
        page, button, mocks = self.setup_preparation()
        mocks["choose_room_cells"].side_effect = [core.GridNotReadyError("格子第21列无坐标"), None]
        self.assertIs(await core.prepare_page(page, self.room, self.target), False)
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        self.assertFalse(core.CONFIG_BLOCKED.is_set())
        mocks["save_selection_diagnostic_once"].assert_awaited_once_with(
            page, self.room, self.target, "网格暂未稳定")
        mocks["confirm_room_selection"].assert_not_awaited()
        self.assertIs(await core.prepare_page(page, self.room, self.target), True)
        mocks["confirm_room_selection"].assert_awaited_once()
        button.click.assert_not_awaited()

    async def test_disabled_confirmation_before_opening_is_unfinished_not_structure_failure(self):
        page, button, mocks = self.setup_preparation()
        mocks["wait_selection_confirmation_ready"].return_value = False
        mocks["watch_confirm_disabled"].return_value = True
        self.assertIsNone(await core.prepare_page(page, self.room, self.target))
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        mocks["confirm_room_selection"].assert_not_awaited()
        mocks["save_debug"].assert_not_awaited()
        button.click.assert_not_awaited()

    async def test_disabled_control_alone_is_not_occupied_evidence(self):
        tip = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        tip.first = tip
        tip.filter = lambda **kwargs: tip
        page = SimpleNamespace(get_by_text=lambda *args, **kwargs: tip)
        self.clock = self.opened + timedelta(minutes=10)
        with patch.object(core, "watch_confirm_disabled", AsyncMock(return_value=True)):
            self.assertFalse(await core.is_room_occupied_state(page, self.room, self.target))
        self.assertNotIn("视为已被预订", core.confirmation_unavailable_reason(self.target, self.room.area))
        tip.is_visible.return_value = True
        self.assertTrue(await core.is_room_occupied_state(page, self.room, self.target))

    async def test_none_at_opening_retains_unfinished_state_for_worker(self):
        async def prepare(*args, **kwargs):
            self.clock = self.opened
            return None
        with patch.object(core, "prepare_page", prepare):
            result = await core.prepare_until_release(object(), self.room, self.target,
                                                      self.opened, self.opened + timedelta(minutes=5))
        self.assertIsNone(result)

    async def test_unavailable_selection_waits_for_opening_before_repreparing(self):
        job = core.BookingJob(self.target, asyncio.Event())
        async def after_opening(*args, **kwargs):
            self.assertGreaterEqual(self.clock, self.opened)
            return True
        async def open_window(moment):
            self.clock = moment
        prepare = AsyncMock(side_effect=after_opening)
        submit = AsyncMock(return_value="dry_run")
        with patch.object(core, "prepare_page", prepare), \
                patch.object(core, "confirm_and_book", submit), \
                patch.object(core, "wait_until", AsyncMock(side_effect=open_window)) as wait, \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep, \
                self.assertLogs(level=logging.INFO) as captured:
            result = await core.release_worker(object(), self.room, job, self.opened,
                                                self.opened + timedelta(minutes=5), None)
        self.assertEqual(result, "dry_run")
        prepare.assert_awaited_once()
        submit.assert_awaited_once()
        wait.assert_awaited_once_with(self.opened)
        sleep.assert_not_awaited()
        self.assertFalse(job.done.is_set())
        self.assertFalse(job.uncertain.is_set())
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        messages = "\n".join(captured.output)
        self.assertIn("提前准备暂停", messages)
        self.assertIn("放号后恢复重试", messages)
        self.assertNotIn("跳过本次抢订", messages)

    async def test_preparation_none_stops_at_first_attempt_before_opening(self):
        prepare = AsyncMock(return_value=None)
        with patch.object(core, "prepare_page", prepare), \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep:
            result = await core.prepare_until_release(object(), self.room, self.target,
                                                      self.opened, self.opened + timedelta(minutes=5))
        self.assertIsNone(result)
        prepare.assert_awaited_once()
        sleep.assert_not_awaited()

    async def test_real_technical_failure_still_retries_and_can_become_ready(self):
        prepare = AsyncMock(side_effect=[False, True])
        with patch.object(core, "prepare_page", prepare), \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep:
            result = await core.prepare_until_release(object(), self.room, self.target,
                                                      self.opened, self.opened + timedelta(minutes=5))
        self.assertIs(result, True)
        self.assertEqual(prepare.await_count, 2)
        sleep.assert_awaited_once()

    async def test_unavailable_after_opening_keeps_retrying_and_can_recover(self):
        self.clock = self.opened
        prepare = AsyncMock(side_effect=[None, None, True])
        submit = AsyncMock(return_value="dry_run")
        with patch.object(core, "prepare_page", prepare), \
                patch.object(core, "confirm_and_book", submit), \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep:
            result = await core.release_worker(object(), self.room,
                core.BookingJob(self.target, asyncio.Event()), self.opened,
                self.opened + timedelta(minutes=5), False)
        self.assertEqual(result, "dry_run")
        self.assertEqual(prepare.await_count, 3)
        submit.assert_awaited_once()
        self.assertEqual(sleep.await_count, 2)
        self.assertEqual([call.args[0] for call in sleep.await_args_list],
                         [core.RELEASE_RETRY_SECONDS] * 2)

    async def test_explicit_occupied_submission_refreshes_and_retries(self):
        self.clock = self.opened
        job = core.BookingJob(self.target, asyncio.Event())
        with patch.object(core, "confirm_and_book", AsyncMock(side_effect=["occupied", "dry_run"])) as submit, \
                patch.object(core, "prepare_page", AsyncMock(return_value=True)) as prepare, \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep:
            result = await core.release_worker(object(), self.room, job, self.opened,
                                                self.opened + timedelta(minutes=5), True)
        self.assertEqual(result, "dry_run")
        self.assertEqual(submit.await_count, 2)
        prepare.assert_awaited_once()
        sleep.assert_awaited_once_with(core.RELEASE_RETRY_SECONDS)
        self.assertFalse(job.done.is_set())

    async def test_continuous_occupancy_retries_to_exact_deadline_without_submitting(self):
        self.clock = self.opened
        deadline = self.opened + timedelta(seconds=2.5)
        observations = []
        async def occupied(*args, **kwargs):
            observations.append(self.clock)
            return None
        async def sleep(seconds):
            self.clock += timedelta(seconds=seconds)
        with patch.object(core, "prepare_page", occupied), \
                patch.object(core, "confirm_and_book", AsyncMock()) as submit, \
                patch.object(core, "sleep_or_stop", AsyncMock(side_effect=sleep)) as waits:
            result = await core.release_worker(object(), self.room,
                core.BookingJob(self.target, asyncio.Event()), self.opened, deadline, None)
        self.assertEqual(result, "expired")
        self.assertEqual(self.clock, deadline)
        self.assertGreaterEqual(len(observations), 2)
        self.assertTrue(all(moment < deadline for moment in observations))
        self.assertAlmostEqual(sum(call.args[0] for call in waits.await_args_list), 2.5)
        submit.assert_not_awaited()

    async def test_repeated_server_occupancy_also_runs_to_deadline(self):
        self.clock = self.opened
        deadline = self.opened + timedelta(seconds=2.5)
        async def sleep(seconds):
            self.clock += timedelta(seconds=seconds)
        with patch.object(core, "prepare_page", AsyncMock(return_value=True)) as prepare, \
                patch.object(core, "confirm_and_book", AsyncMock(return_value="occupied")) as submit, \
                patch.object(core, "sleep_or_stop", AsyncMock(side_effect=sleep)):
            result = await core.release_worker(object(), self.room,
                core.BookingJob(self.target, asyncio.Event()), self.opened, deadline, True)
        self.assertEqual(result, "expired")
        self.assertEqual(self.clock, deadline)
        self.assertGreaterEqual(submit.await_count, 2)
        self.assertEqual(prepare.await_count, submit.await_count - 1)

    async def test_occupied_retry_stops_when_peer_succeeds_or_result_is_uncertain(self):
        for event_name, expected in (("done", "skipped"), ("uncertain", "uncertain")):
            with self.subTest(event=event_name):
                self.clock = self.opened
                job = core.BookingJob(self.target, asyncio.Event())
                async def sleep(seconds):
                    getattr(job, event_name).set()
                with patch.object(core, "prepare_page", AsyncMock(return_value=None)) as prepare, \
                        patch.object(core, "confirm_and_book", AsyncMock()) as submit, \
                        patch.object(core, "sleep_or_stop", sleep):
                    result = await core.release_worker(object(), self.room, job, self.opened,
                        self.opened + timedelta(minutes=5), False)
                self.assertEqual(result, expected)
                prepare.assert_awaited_once()
                submit.assert_not_awaited()

    async def test_closed_selection_but_disabled_final_is_not_classified_occupied(self):
        page, button, mocks = self.setup_preparation()
        mocks["is_final_book_button_clickable"].return_value = False
        self.assertIs(await core.prepare_page(page, self.room, self.target), False)
        button.click.assert_not_awaited()

    async def test_missing_confirmation_control_remains_technical_failure(self):
        page, button, mocks = self.setup_preparation()
        mocks["wait_selection_confirmation_ready"].return_value = False
        mocks["watch_confirm_disabled"].return_value = False
        self.assertIs(await core.prepare_page(page, self.room, self.target), False)
        button.click.assert_not_awaited()

    async def test_not_open_response_reuses_ready_form_and_can_succeed(self):
        self.clock = self.opened
        job = core.BookingJob(self.target, asyncio.Event())
        page = SimpleNamespace(get_by_role=lambda *args, **kwargs: object())
        with patch.object(core, "confirm_and_book", AsyncMock(side_effect=["not_open", "dry_run"])) as submit, \
                patch.object(core, "prepare_page", AsyncMock()) as prepare, \
                patch.object(core, "dismiss_advance_limit_dialog", AsyncMock()), \
                patch.object(core, "is_final_book_button_clickable", AsyncMock(return_value=True)), \
                patch.object(core, "sleep_or_stop", AsyncMock()):
            result = await core.release_worker(page, self.room, job, self.opened,
                                                self.opened + timedelta(minutes=5), True)
        self.assertEqual(result, "dry_run")
        self.assertEqual(submit.await_count, 2)
        prepare.assert_not_awaited()

    async def test_opening_snapshot_is_not_blocked_by_preparation(self):
        opened_signal, started, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        pool = SimpleNamespace(lock=asyncio.Lock())

        async def pages(*args, **kwargs):
            yield object()

        async def wait(moment):
            if moment >= self.opened:
                await opened_signal.wait()

        async def prepare(*args, **kwargs):
            started.set()
            await finish.wait()
            return True

        pool.iter_ready = pages
        batch = core.build_release_batches([self.target], [self.room])[0]
        priority = asyncio.Event()
        with patch.object(core, "wait_until", wait), \
                patch.object(core, "prepare_until_release", prepare), \
                patch.object(core, "release_worker", AsyncMock(return_value="dry_run")), \
                self.assertLogs(level=logging.INFO) as captured:
            task = asyncio.create_task(core.run_release_batch(pool, priority, {}, batch))
            try:
                await asyncio.wait_for(started.wait(), 0.5)
                self.clock = self.opened
                opened_signal.set()
                for _ in range(4):
                    await asyncio.sleep(0)
                self.assertFalse(task.done())
                snapshot = [line for line in captured.output if "已到计划开放时刻" in line]
                self.assertEqual(len(snapshot), 1)
                self.assertIn("表单就绪0页｜准备中1页", snapshot[0])
                self.assertIn("不是提交回执", snapshot[0])
            finally:
                finish.set()
                await task
        self.assertFalse(priority.is_set())

    async def test_cancelled_batch_drains_opening_observer(self):
        started = asyncio.Event()
        pool = SimpleNamespace(lock=asyncio.Lock())

        async def pages(*args, **kwargs):
            yield object()

        async def wait(moment):
            if moment >= self.opened:
                await asyncio.Event().wait()

        async def prepare(*args, **kwargs):
            started.set()
            await asyncio.Event().wait()

        pool.iter_ready = pages
        batch = core.build_release_batches([self.target], [self.room])[0]
        priority = asyncio.Event()
        with patch.object(core, "wait_until", wait), patch.object(core, "prepare_until_release", prepare):
            task = asyncio.create_task(core.run_release_batch(pool, priority, {}, batch))
            await asyncio.wait_for(started.wait(), 0.5)
            observers = [candidate for candidate in asyncio.all_tasks()
                         if "observe_opening" in candidate.get_coro().__qualname__]
            self.assertEqual(len(observers), 1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
            self.assertTrue(observers[0].done())
        self.assertFalse(priority.is_set())
        self.assertFalse(pool.lock.locked())


if __name__ == "__main__":
    unittest.main()
