"""Offline scheduling tests; no browser or booking-system access."""
import asyncio
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import AsyncMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking_core as core


class FakePool:
    def __init__(self, pages):
        self.pages = pages
        self.lock = asyncio.Lock()

    async def get(self, needed):
        return self.pages[:needed]

    async def iter_ready(self, needed, *, interrupted=None, retry_authentication=False):
        for page in self.pages[:needed]:
            if interrupted and interrupted():
                raise core.PreparationInterrupted()
            yield page


class ReleaseSchedulingTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.rooms = [core.RoomSpec("F5", "7F-A05R", 1), core.RoomSpec("F5", "7F-A28R", 2)]
        self.target = core.Target(date(2026, 10, 24), "14:00", "16:00", ("F5",))
        self.batch = core.build_release_batches([self.target], self.rooms)[0]
        self.pool = FakePool(["fast", "slow"])
        self.priority = asyncio.Event()
        values = {name: asyncio.Event() for name in
                  ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED")}
        values.update(now_cn=lambda: core.at_cn(self.batch.release_day, "08:24:00"),
                      wait_until=AsyncMock(), booking_result=lambda *args, **kwargs: None)
        for name, value in values.items():
            patcher = patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_ready_room_starts_without_waiting_for_slow_preparation(self):
        release_slow, fast_started = asyncio.Event(), asyncio.Event()
        calls = []

        async def prepare(page, *args):
            if page == "slow":
                await release_slow.wait()
            calls.append((page, "prepared"))
            return True

        async def worker(page, *args):
            calls.append((page, "worker"))
            if page == "fast":
                fast_started.set()
            return "dry_run"

        with patch.object(core, "prepare_until_release", prepare), patch.object(core, "release_worker", worker):
            task = asyncio.create_task(core.run_release_batch(self.pool, self.priority, {}, self.batch))
            try:
                await asyncio.wait_for(fast_started.wait(), 0.3)
                self.assertNotIn(("slow", "prepared"), calls)
            finally:
                release_slow.set()
                await task
        self.assertFalse(self.pool.lock.locked())
        self.assertFalse(self.priority.is_set())

    async def check_workers_drained_before_unlock(self, failure):
        slow_started, finish_slow, slow_finished = asyncio.Event(), asyncio.Event(), asyncio.Event()

        async def worker(page, *args):
            if page == "fast":
                raise failure
            slow_started.set()
            try:
                await finish_slow.wait()
                return "dry_run"
            finally:
                slow_finished.set()

        with patch.object(core, "prepare_until_release", AsyncMock(return_value=True)), \
                patch.object(core, "release_worker", worker):
            task = asyncio.create_task(core.run_release_batch(self.pool, self.priority, {}, self.batch))
            try:
                await asyncio.wait_for(slow_started.wait(), 0.3)
                for _ in range(4):
                    await asyncio.sleep(0)
                self.assertFalse(task.done(), "Pool ownership must last until every worker finishes")
                self.assertTrue(self.pool.lock.locked())
                self.assertTrue(self.priority.is_set())
            finally:
                finish_slow.set()
                with self.assertRaises(type(failure)):
                    await task
                await asyncio.wait_for(slow_finished.wait(), 0.3)
        self.assertFalse(self.pool.lock.locked())
        self.assertFalse(self.priority.is_set())

    async def test_worker_exception_drains_siblings_before_releasing_pool(self):
        await self.check_workers_drained_before_unlock(RuntimeError("offline failure"))

    async def test_stop_drains_siblings_before_releasing_pool(self):
        await self.check_workers_drained_before_unlock(core.StopRequested("offline stop"))

    async def test_external_cancellation_drains_workers_and_clears_priority(self):
        started, finished = asyncio.Event(), []

        async def prepare(page, *args):
            started.set()
            try:
                await asyncio.Event().wait()
            finally:
                finished.append(page)

        with patch.object(core, "prepare_until_release", prepare):
            task = asyncio.create_task(core.run_release_batch(self.pool, self.priority, {}, self.batch))
            await asyncio.wait_for(started.wait(), 0.3)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        self.assertCountEqual(finished, ["fast", "slow"])
        self.assertFalse(self.pool.lock.locked())
        self.assertFalse(self.priority.is_set())

    async def test_watch_ready_room_submits_and_success_stops_slow_room(self):
        release_slow, fast_submitted = asyncio.Event(), asyncio.Event()
        submissions = []
        job = core.BookingJob(self.target, asyncio.Event())

        async def prepare(page, *args, **kwargs):
            if page == "slow":
                await release_slow.wait()
            return True

        async def confirm(page, room, current_job, **kwargs):
            submissions.append(page)
            current_job.done.set()
            fast_submitted.set()
            return "success"

        with patch.object(core, "prepare_page", prepare), patch.object(core, "confirm_and_book", confirm):
            task = asyncio.create_task(core.watch_cycle(self.pool.pages, job, self.rooms))
            try:
                await asyncio.wait_for(fast_submitted.wait(), 0.3)
                self.assertEqual(submissions, ["fast"])
            finally:
                release_slow.set()
                await task
        self.assertEqual(submissions, ["fast"])


    async def test_watch_unknown_result_blocks_later_room(self):
        release_slow, fast_submitted = asyncio.Event(), asyncio.Event()
        submissions = []
        job = core.BookingJob(self.target, asyncio.Event())

        async def prepare(page, *args, **kwargs):
            if page == "slow":
                await release_slow.wait()
            return True

        async def confirm(page, room, current_job, **kwargs):
            submissions.append(page)
            current_job.uncertain.set()
            fast_submitted.set()
            return "uncertain"

        with patch.object(core, "prepare_page", prepare), patch.object(core, "confirm_and_book", confirm):
            task = asyncio.create_task(core.watch_cycle(self.pool.pages, job, self.rooms))
            try:
                await asyncio.wait_for(fast_submitted.wait(), 0.3)
            finally:
                release_slow.set()
                await task
        self.assertEqual(submissions, ["fast"])
        self.assertFalse(job.done.is_set())

    async def test_watch_keeps_same_target_submissions_parallel(self):
        both_submitted = asyncio.Event()
        submitted = []
        job = core.BookingJob(self.target, asyncio.Event())

        async def confirm(page, room, current_job, **kwargs):
            submitted.append(page)
            if len(submitted) == 2:
                both_submitted.set()
            await both_submitted.wait()
            return "failed"

        with patch.object(core, "prepare_page", AsyncMock(return_value=True)), \
                patch.object(core, "confirm_and_book", confirm):
            await asyncio.wait_for(core.watch_cycle(self.pool.pages, job, self.rooms), 0.3)
        self.assertCountEqual(submitted, ["fast", "slow"])

    async def test_release_existing_unknown_skips_only_its_own_target(self):
        other = core.Target(self.target.day, "16:00", "18:00", ("F5",))
        registry = {}
        core.shared_booking_job(registry, self.target).uncertain.set()
        batch = core.build_release_batches([self.target, other], self.rooms[:1])[0]
        calls = []

        async def worker(page, room, job, *args):
            calls.append(job.target.key)
            return "dry_run"

        with patch.object(core, "prepare_until_release", AsyncMock(return_value=True)), \
                patch.object(core, "release_worker", worker):
            await core.run_release_batch(self.pool, self.priority, registry, batch)
        self.assertEqual(calls, [other.key])

    async def test_watch_existing_unknown_skips_only_its_own_target(self):
        other = core.Target(self.target.day, "16:00", "18:00", ("F5",))
        registry = {}
        core.shared_booking_job(registry, self.target).uncertain.set()
        targets = [{"date": target.day.isoformat(), "start": target.start, "end": target.end,
                    "areas": list(target.areas)} for target in (self.target, other)]
        calls = []

        async def cycle(pages, job, rooms, **kwargs):
            calls.append(job.target.key)
            job.done.set()

        with patch.object(core, "WATCH_TARGETS", targets), patch.object(core, "ROOMS", self.rooms[:1]), \
                patch.object(core, "now_cn", lambda: core.at_cn(self.batch.release_day, "08:31:00")), \
                patch.object(core, "sleep_or_stop", AsyncMock()), patch.object(core, "watch_cycle", cycle):
            await asyncio.wait_for(core.run_watch(self.pool, self.priority, registry), 0.3)
        self.assertEqual(calls, [other.key])


class ReleaseCalendarTests(unittest.TestCase):
    def test_same_target_uses_distinct_area_opening_days(self):
        target = core.Target(date(2026, 10, 24), "14:00", "16:00", ("F5", "F5-1A"))
        rooms = [core.RoomSpec("F5", "7F-A05R", 1), core.RoomSpec("F5-1A", "1F-A11R", 1)]
        batches = core.build_release_batches([target], rooms)
        self.assertEqual([b.release_day for b in batches], [date(2026, 10, 17), date(2026, 10, 21)])
        self.assertEqual([b.tasks[0][1].area for b in batches], ["F5", "F5-1A"])

    def test_weekly_expansion_crosses_month_and_merges_manual_area(self):
        rules = [{"weekday": 1, "start": "10:00", "end": "12:00", "areas": ["F5"]}]
        weekly = core.expand_weekly_targets(rules, date(2026, 10, 30), 8)
        self.assertEqual([t.day for t in weekly], [date(2026, 11, 2)])
        manual = core.Target(date(2026, 11, 2), "10:00", "12:00", ("F5-1A",))
        merged = core.merge_release_targets([manual, *weekly])
        self.assertEqual(len(merged), 1)
        self.assertEqual(set(merged[0].areas), {"F5", "F5-1A"})

    def test_different_meeting_dates_share_one_actual_release_batch(self):
        released = date(2026, 10, 17)
        targets = [core.Target(released + timedelta(days=7), "14:00", "16:00", ("F5",)),
                   core.Target(released + timedelta(days=3), "14:00", "16:00", ("F5-1A",))]
        rooms = [core.RoomSpec("F5", "7F-A05R", 1), core.RoomSpec("F5-1A", "1F-A11R", 1)]
        batches = core.build_release_batches(targets, rooms)
        self.assertEqual(len(batches), 1)
        self.assertEqual(batches[0].release_day, released)
        self.assertEqual(batches[0].max_pages, 2)


if __name__ == "__main__":
    unittest.main()

