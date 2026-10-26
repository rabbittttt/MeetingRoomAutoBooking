# -*- coding: utf-8 -*-
"""停止原因、阶段交接与候补到期日志；不访问真实网页或提交预约。"""
import asyncio
from contextlib import ExitStack
from datetime import date, timedelta
from pathlib import Path
from types import SimpleNamespace
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking_core as core


class StopMessageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "GRID_SELECTOR_BLOCKED", set()))
        self.stack.enter_context(patch.object(core, "DRY_RUN", True))
        self.stack.enter_context(patch.object(core, "RELEASE_OPEN_AT", "08:45:00"))
        self.stack.enter_context(patch.object(core, "RELEASE_PREPARE_BEFORE_SECONDS", 300))
        self.stack.enter_context(patch.object(core, "RELEASE_STOP_AFTER_SECONDS", 300))
        self.stack.enter_context(patch.object(core, "WATCH_PREP_GUARD_SECONDS", 120))
        self.stack.enter_context(patch.object(core, "WATCH_DURATION_MINUTES", 120))
        self.stack.enter_context(patch.object(core, "WATCH_STOP_AT_MEETING_START", True))
        self.room = core.RoomSpec("F5", "7F-A05R", 1)
        self.target = core.Target(date(2030, 10, 27), "10:00", "12:00", ("F5",))
        self.opened = core.at_cn(date(2030, 10, 20), "08:45:00")
        self.clock = self.opened + timedelta(seconds=10)
        self.deadline = self.opened + timedelta(seconds=300)
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.clock))

    def job(self):
        return core.BookingJob(self.target, asyncio.Event())

    def assert_no_retry_promise(self, messages):
        combined = "\n".join(messages)
        for fragment in ("占用重试", "秒后重试", "继续监控释放", "继续重试至", "候补可继续"):
            self.assertNotIn(fragment, combined)

    def test_environment_reason_has_priority_over_internal_stop_signal(self):
        core.STOP_REQUESTED.set()
        self.assertEqual(core.run_stop_reason(), "已响应停止请求")
        core.CONFIG_BLOCKED.set()
        self.assertEqual(core.run_stop_reason(), "页面关键控件结构异常")
        core.AUTH_BLOCKED.set()
        self.assertEqual(core.run_stop_reason(), "认证异常")
        core.DRIVER_LOST.set()
        self.assertEqual(core.run_stop_reason(), "浏览器连接中断")
        with self.assertRaisesRegex(core.StopRequested, "浏览器连接中断"):
            core.raise_if_run_stopped()

    def test_occupied_result_records_fact_without_promising_another_attempt(self):
        with self.assertLogs(level="WARNING") as captured:
            core.booking_result("到点抢订", self.target, self.room, "occupied")
            core.booking_result("空闲候补", self.target, self.room, "occupied")
        self.assert_no_retry_promise(captured.output)
        self.assertTrue(all("占用" in message for message in captured.output))

    async def test_stop_after_occupied_preparation_does_not_log_or_schedule_retry(self):
        for flag in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            with self.subTest(flag=flag):
                async def prepare(*args, **kwargs):
                    getattr(core, flag).set()
                    return None
                with patch.object(core, "prepare_page", prepare), \
                        patch.object(core, "confirm_and_book", AsyncMock()) as submit, \
                        patch.object(core, "sleep_or_stop", AsyncMock()) as sleep, \
                        self.assertLogs(level="INFO") as captured:
                    status = await core.release_worker(
                        object(), self.room, self.job(), self.opened, self.deadline, False)
                self.assertEqual(status, "stopped")
                self.assert_no_retry_promise(captured.output)
                submit.assert_not_awaited()
                sleep.assert_not_awaited()
                getattr(core, flag).clear()

    async def test_stop_after_explicit_occupied_receipt_does_not_refresh(self):
        async def receipt(*args, **kwargs):
            core.STOP_REQUESTED.set()
            return "occupied"
        with patch.object(core, "confirm_and_book", receipt), \
                patch.object(core, "prepare_page", AsyncMock()) as prepare, \
                patch.object(core, "sleep_or_stop", AsyncMock()) as sleep, \
                self.assertLogs(level="INFO") as captured:
            status = await core.release_worker(
                object(), self.room, self.job(), self.opened, self.deadline, True)
        self.assertEqual(status, "stopped")
        self.assert_no_retry_promise(captured.output)
        prepare.assert_not_awaited()
        sleep.assert_not_awaited()

    async def test_success_receipt_still_wins_when_stop_arrives_after_submission(self):
        job = self.job()
        async def receipt(*args, **kwargs):
            core.STOP_REQUESTED.set()
            job.done.set()
            return "success"
        with patch.object(core, "confirm_and_book", receipt), self.assertLogs(level="INFO") as captured:
            status = await core.release_worker(
                object(), self.room, job, self.opened, self.deadline, True)
        self.assertEqual(status, "success")
        self.assertTrue(any("预订成功" in message for message in captured.output))

    async def test_preexisting_stop_does_not_promise_occupied_page_will_resume(self):
        self.clock = self.opened - timedelta(seconds=5)
        core.STOP_REQUESTED.set()
        with self.assertNoLogs(level="INFO"):
            with self.assertRaises(core.StopRequested):
                await core.release_worker(
                    object(), self.room, self.job(), self.opened, self.deadline, None)

    async def test_config_block_prevents_entering_prepare_page(self):
        core.CONFIG_BLOCKED.set()
        with patch.object(core, "activate_booking_page", AsyncMock()) as activate:
            self.assertFalse(await core.prepare_page(object(), self.room, self.target))
        activate.assert_not_awaited()

    async def test_stop_requested_in_preparation_is_not_reported_as_form_error(self):
        page = SimpleNamespace(url="about:blank")
        with patch.object(core, "activate_booking_page", AsyncMock(side_effect=core.StopRequested("停止"))), \
                patch.object(core, "save_debug", AsyncMock()) as screenshot, \
                patch.object(core, "is_403_page", AsyncMock()) as check_auth, \
                self.assertLogs(level="INFO") as captured:
            with self.assertRaises(core.StopRequested):
                await core.prepare_page(page, self.room, self.target)
        screenshot.assert_not_awaited()
        check_auth.assert_not_awaited()
        self.assertFalse(any("准备异常" in message for message in captured.output))

    async def test_environment_failure_interrupts_unsubmitted_release_preparation(self):
        entered, stalled, cancelled = asyncio.Event(), asyncio.Event(), asyncio.Event()
        async def prepare(*args, **kwargs):
            entered.set()
            try:
                await stalled.wait()
            finally:
                cancelled.set()
        with patch.object(core, "prepare_page", prepare), \
                patch.object(core, "confirm_and_book", AsyncMock()) as submit, \
                self.assertLogs(level="INFO") as captured:
            task = asyncio.create_task(core.release_worker(
                object(), self.room, self.job(), self.opened, self.deadline, False))
            try:
                await asyncio.wait_for(entered.wait(), 1)
                core.CONFIG_BLOCKED.set()
                self.assertEqual(await asyncio.wait_for(task, 1), "stopped")
            finally:
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertTrue(cancelled.is_set())
        submit.assert_not_awaited()
        self.assert_no_retry_promise(captured.output)

    async def test_late_start_enters_real_preparation_without_false_early_prepare_result(self):
        class Pool:
            lock = asyncio.Lock()
            async def iter_ready(self, needed, **kwargs):
                yield object()
        pool, priority = Pool(), asyncio.Event()
        batch = core.build_release_batches([self.target], [self.room])[0]
        with patch.object(core, "prepare_until_release", AsyncMock()) as early, \
                patch.object(core, "release_worker", AsyncMock(return_value="dry_run")) as worker, \
                self.assertLogs(level="INFO") as captured:
            await core.run_release_batch(pool, priority, {}, batch)
        early.assert_not_awaited()
        self.assertEqual(worker.await_count, 1)
        self.assertIs(worker.await_args.args[-1], False)
        self.assertTrue(any("直接开始抢订准备" in message for message in captured.output))
        self.assertFalse(any("单页准备结果" in message for message in captured.output))
        self.assertFalse(any("候补可继续" in message for message in captured.output))
        self.assertFalse(priority.is_set())
        self.assertFalse(pool.lock.locked())

    async def test_already_started_watch_target_has_explicit_reason_and_no_page_access(self):
        self.clock = self.target.start_dt + timedelta(seconds=1)
        raw = [{"date": self.target.day.isoformat(), "start": "10:00", "end": "12:00", "areas": ["F5"]}]
        pool = SimpleNamespace(iter_ready=Mock(side_effect=AssertionError("不应查询已开始的目标")))
        with patch.object(core, "WATCH_TARGETS", raw), patch.object(core, "ROOMS", [self.room]), \
                self.assertLogs(level="INFO") as captured:
            await core.run_watch(pool, asyncio.Event(), {})
        text = "\n".join(captured.output)
        self.assertIn(self.target.key, text)
        self.assertIn("已到会议开始时间2030-10-27 10:00:00，不再尝试预订", text)
        self.assertIn("成功0个｜结果待核实0个｜已到会议开始时间1个", text)
        self.assertNotIn("已成功、停止或结果待核实", text)
        pool.iter_ready.assert_not_called()

    async def test_watch_terminal_summary_distinguishes_success_uncertain_and_started(self):
        self.clock = self.target.start_dt + timedelta(hours=8)
        raw, registry = [], {}
        for start, end, outcome in (("09:00", "10:00", "done"),
                                    ("11:00", "12:00", "uncertain"),
                                    ("13:00", "14:00", "started")):
            target = core.Target(self.target.day, start, end, ("F5",))
            raw.append({"date": target.day.isoformat(), "start": start, "end": end, "areas": ["F5"]})
            job = core.shared_booking_job(registry, target)
            if outcome != "started":
                getattr(job, outcome).set()
        with patch.object(core, "WATCH_TARGETS", raw), patch.object(core, "ROOMS", [self.room]), \
                self.assertLogs(level="INFO") as captured:
            await core.run_watch(object(), asyncio.Event(), registry)
        self.assertTrue(any("成功1个｜结果待核实1个｜已到会议开始时间1个" in line for line in captured.output))
        self.assertEqual(sum("已停止此目标" in line for line in captured.output), 1)

    async def test_main_reports_structure_failure_instead_of_user_stop(self):
        page = SimpleNamespace(url="about:blank", on=Mock())
        context = SimpleNamespace(pages=[page], on=Mock(), set_default_timeout=Mock(), close=AsyncMock())
        playwright = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=AsyncMock(return_value=context)))
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=playwright)
        manager.__aexit__ = AsyncMock(return_value=False)
        async def failed_mode(*args):
            core.CONFIG_BLOCKED.set()
            raise core.StopRequested("日期结构无法核验")
        with tempfile.TemporaryDirectory(prefix="booking_stop_log_") as folder, \
                patch.object(core, "PROFILE_DIR", Path(folder)), \
                patch.object(core, "setup_logging"), \
                patch.object(core, "require_authorized_account", return_value="z00523094"), \
                patch.object(core, "async_playwright", return_value=manager), \
                patch.object(core, "auth_preflight", AsyncMock(return_value=True)), \
                patch.object(core, "run_release", failed_mode), \
                patch.object(core, "run_watch", AsyncMock()), \
                patch.object(core, "RELEASE_TARGETS", []), \
                patch.object(core, "WEEKLY_RELEASE_RULES", []), \
                patch.object(core, "WATCH_TARGETS", []), \
                self.assertLogs(level="WARNING") as captured:
            await core.main("both")
        self.assertTrue(core.STOP_REQUESTED.is_set())
        context.close.assert_awaited_once()
        self.assertTrue(any("页面关键控件结构异常" in message for message in captured.output))
        self.assertFalse(any("已响应停止请求" in message for message in captured.output))


if __name__ == "__main__":
    unittest.main()
