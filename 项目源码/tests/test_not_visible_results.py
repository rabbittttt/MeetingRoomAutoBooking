# -*- coding: utf-8 -*-
"""名单暂未显示房间时的离线回归；不访问真实网页或提交预约。"""
import asyncio
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking_core as core


class NotVisibleResultTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.room = core.RoomSpec("F5-1A", "1F-A11R", 2)
        self.other_room = core.RoomSpec("F5-1A", "1F-A13R", 4)
        self.target = core.Target(date(2030, 10, 21), "10:00", "12:00", ("F5-1A",))
        self.opened = core.at_cn(date(2030, 10, 18), "08:30:00")
        self.clock = self.opened - timedelta(seconds=5)
        self.deadline = self.opened + timedelta(seconds=300)
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        for name in ("GRID_SELECTOR_BLOCKED", "SELECTION_DIAGNOSTICS_RECORDED"):
            self.stack.enter_context(patch.object(core, name, set()))
        for name, value in (
                ("RELEASE_OPEN_AT", "08:30:00"),
                ("RELEASE_PREPARE_BEFORE_SECONDS", 300),
                ("RELEASE_STOP_AFTER_SECONDS", 300),
                ("RELEASE_RETRY_SECONDS", 0.9),
                ("WATCH_PREP_GUARD_SECONDS", 120),
                ("DRY_RUN", True)):
            self.stack.enter_context(patch.object(core, name, value))
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.clock))

    def job(self):
        return core.BookingJob(self.target, asyncio.Event())

    def assert_no_blocks(self):
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.assertFalse(getattr(core, name).is_set(), name)

    def preparation_page(self):
        button = SimpleNamespace(wait_for=AsyncMock(), click=AsyncMock())
        page = SimpleNamespace(url="about:blank", goto=AsyncMock(),
                               get_by_role=lambda *args, **kwargs: button)
        mocks = {}
        for name in ("activate_booking_page", "select_date", "select_start_time", "select_duration",
                     "open_room_list", "verify_room_identity", "choose_room_cells", "save_debug",
                     "save_selection_diagnostic_once", "confirm_room_selection"):
            mocks[name] = self.stack.enter_context(patch.object(core, name, AsyncMock()))
        self.stack.enter_context(patch.object(core, "is_403_page", AsyncMock(return_value=False)))
        return page, button, mocks

    async def test_missing_room_returns_distinct_result_without_block_or_selection(self):
        page, button, mocks = self.preparation_page()
        mocks["verify_room_identity"].side_effect = core.RoomNotVisibleError(
            "当前区域名单未显示 1F-A11R")
        for watch_mode in (False, True):
            with self.subTest(watch_mode=watch_mode):
                result = await core.prepare_page(page, self.room, self.target, watch_mode=watch_mode)
                self.assertEqual(result, "not_visible")
                self.assert_no_blocks()
        mocks["choose_room_cells"].assert_not_awaited()
        mocks["confirm_room_selection"].assert_not_awaited()
        mocks["save_debug"].assert_not_awaited()
        mocks["save_selection_diagnostic_once"].assert_not_awaited()
        button.click.assert_not_awaited()

    async def test_missing_room_pauses_early_preparation_at_first_attempt(self):
        with patch.object(core, "prepare_page", AsyncMock(return_value="not_visible")) as prepare, \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep:
            result = await core.prepare_until_release(
                object(), self.room, self.target, self.opened, self.deadline)
        self.assertEqual(result, "not_visible")
        prepare.assert_awaited_once()
        sleep.assert_not_awaited()
        self.assertLess(self.clock, self.opened)
        self.assert_no_blocks()

    async def test_watch_missing_room_does_not_prevent_other_room_success(self):
        job = self.job()
        missing_page, ready_page = object(), object()

        async def prepare(page, room, target, **kwargs):
            return "not_visible" if page is missing_page else True

        async def successful_receipt(page, room, submitted_job, **kwargs):
            self.assertIs(page, ready_page)
            self.assertEqual(room, self.other_room)
            submitted_job.done.set()
            return "success"

        with patch.object(core, "prepare_page", AsyncMock(side_effect=prepare)) as preparation, \
                patch.object(core, "confirm_and_book", AsyncMock(side_effect=successful_receipt)) as submit, \
                self.assertLogs(level="INFO") as captured:
            await core.watch_cycle([missing_page, ready_page], job, [self.room, self.other_room],
                                   deadline=self.deadline)
        self.assertEqual(preparation.await_count, 2)
        submit.assert_awaited_once()
        self.assertTrue(job.done.is_set())
        self.assertFalse(job.uncertain.is_set())
        summary = next(line for line in captured.output if "[候补] 时段完成" in line)
        for fragment in ("检查2间", "名单未显示1间", "已占用/不可选0间", "准备异常0间", "成功1间"):
            self.assertIn(fragment, summary)
        self.assert_no_blocks()

    async def test_watch_rechecks_missing_room_next_round_and_can_submit(self):
        job = self.job()
        page = object()

        async def successful_receipt(*args, **kwargs):
            job.done.set()
            return "success"

        with patch.object(core, "prepare_page", AsyncMock(side_effect=["not_visible", True])) as prepare, \
                patch.object(core, "confirm_and_book", AsyncMock(side_effect=successful_receipt)) as submit, \
                self.assertLogs(level="INFO") as captured:
            await core.watch_cycle([page], job, [self.room], deadline=self.deadline)
            submit.assert_not_awaited()
            self.assertFalse(job.done.is_set())
            self.assertFalse(job.uncertain.is_set())
            self.assert_no_blocks()
            await core.watch_cycle([page], job, [self.room], deadline=self.deadline)
        self.assertEqual(prepare.await_count, 2)
        submit.assert_awaited_once()
        self.assertTrue(job.done.is_set())
        summaries = [line for line in captured.output if "[候补] 时段完成" in line]
        self.assertEqual(len(summaries), 2)
        self.assertIn("名单未显示1间", summaries[0])
        self.assertIn("提交/模拟0间", summaries[0])
        self.assertIn("名单未显示0间", summaries[1])
        self.assertIn("成功1间", summaries[1])
        self.assert_no_blocks()

    async def test_release_missing_status_waits_for_opening_and_reprepares_before_submit(self):
        job = self.job()
        observations = []
        results = iter(("not_visible", True))

        async def prepare(*args, **kwargs):
            observations.append(self.clock)
            self.assertGreaterEqual(self.clock, self.opened)
            return next(results)

        async def open_window(moment):
            self.clock = moment

        async def successful_receipt(*args, **kwargs):
            self.assertEqual(len(observations), 2, "名单未显示字符串不能被当作表单已就绪")
            job.done.set()
            return "success"

        with patch.object(core, "prepare_page", AsyncMock(side_effect=prepare)) as preparation, \
                patch.object(core, "wait_until", AsyncMock(side_effect=open_window)) as wait, \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep, \
                patch.object(core, "confirm_and_book", AsyncMock(side_effect=successful_receipt)) as submit:
            result = await core.release_worker(
                object(), self.room, job, self.opened, self.deadline, "not_visible")
        self.assertEqual(result, "success")
        self.assertEqual(preparation.await_count, 2)
        submit.assert_awaited_once()
        wait.assert_awaited_once_with(self.opened)
        sleep.assert_awaited_once_with(core.RELEASE_RETRY_SECONDS)
        self.assertTrue(job.done.is_set())
        self.assert_no_blocks()

    async def test_release_continuously_missing_room_retries_to_exact_deadline(self):
        self.clock = self.opened
        deadline = self.opened + timedelta(seconds=2.5)
        job = self.job()
        observations = []

        async def missing(*args, **kwargs):
            observations.append(self.clock)
            return "not_visible"

        async def sleep(seconds):
            self.clock += timedelta(seconds=seconds)

        with patch.object(core, "prepare_page", AsyncMock(side_effect=missing)), \
                patch.object(core, "confirm_and_book", AsyncMock()) as submit, \
                patch.object(core, "sleep_or_stop", AsyncMock(side_effect=sleep)) as waits:
            result = await core.release_worker(
                object(), self.room, job, self.opened, deadline, "not_visible")
        self.assertEqual(result, "expired")
        self.assertEqual(self.clock, deadline)
        self.assertGreaterEqual(len(observations), 2)
        self.assertTrue(all(moment < deadline for moment in observations))
        self.assertAlmostEqual(sum(call.args[0] for call in waits.await_args_list), 2.5)
        submit.assert_not_awaited()
        self.assertFalse(job.done.is_set())
        self.assertFalse(job.uncertain.is_set())
        self.assert_no_blocks()

    async def test_opening_snapshot_counts_missing_separately_from_ready_and_occupied(self):
        opened_signal, workers_entered, finish = asyncio.Event(), asyncio.Event(), asyncio.Event()
        phases = {}
        pool = SimpleNamespace(lock=asyncio.Lock())

        async def pages(*args, **kwargs):
            yield "missing"
            yield "ready"

        async def wait(moment):
            if moment >= self.opened:
                await opened_signal.wait()

        async def prepare(page, *args, **kwargs):
            return "not_visible" if page == "missing" else True

        async def worker(page, room, job, opened, deadline, prepared):
            phases[page] = prepared
            if len(phases) == 2:
                workers_entered.set()
            await finish.wait()
            return "expired" if page == "missing" else "dry_run"

        pool.iter_ready = pages
        batch = core.build_release_batches([self.target], [self.room, self.other_room])[0]
        priority = asyncio.Event()
        with patch.object(core, "wait_until", wait), \
                patch.object(core, "prepare_until_release", prepare), \
                patch.object(core, "release_worker", worker), \
                self.assertLogs(level="INFO") as captured:
            task = asyncio.create_task(core.run_release_batch(pool, priority, {}, batch))
            try:
                await asyncio.wait_for(workers_entered.wait(), 1)
                self.assertEqual(phases, {"missing": "not_visible", "ready": True})
                self.clock = self.opened
                opened_signal.set()
                for _ in range(4):
                    await asyncio.sleep(0)
                snapshots = [line for line in captured.output if "已到计划开放时刻" in line]
                self.assertEqual(len(snapshots), 1)
                for fragment in ("表单就绪1页", "名单未显示1页", "占用待重试0页", "准备异常0页"):
                    self.assertIn(fragment, snapshots[0])
            finally:
                finish.set()
                await asyncio.wait_for(task, 1)
        self.assertFalse(priority.is_set())
        self.assertFalse(pool.lock.locked())
        self.assert_no_blocks()


if __name__ == "__main__":
    unittest.main()
