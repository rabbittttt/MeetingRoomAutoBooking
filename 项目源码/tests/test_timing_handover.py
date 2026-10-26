"""Offline regressions for release timing and page handover; no browser access."""
import asyncio
from datetime import date, timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking_core as core


class TimingHandoverTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        values = {name: asyncio.Event() for name in
                  ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED")}
        values.update(RELEASE_OPEN_AT="08:30:00", RELEASE_PREPARE_BEFORE_SECONDS=300,
                      RELEASE_STOP_AFTER_SECONDS=300, WATCH_PREP_GUARD_SECONDS=120,
                      DRY_RUN=True, GRID_SELECTOR_BLOCKED=set())
        for name, value in values.items():
            patcher = patch.object(core, name, value)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_manual_schedule_keeps_future_batches_after_old_target_drops_out(self):
        clock = [core.at_cn(date(2026, 10, 18), "07:00:00")]
        dispatched = []
        targets = [{"date": day, "start": "14:00", "end": "16:00", "areas": ["F5"]}
                   for day in ("2026-10-18", "2026-10-26", "2026-10-27", "2026-10-28")]

        async def finish_batch(pool, priority, registry, batch):
            dispatched.append(batch.release_day)
            clock[0] = core.at_cn(batch.release_day, "08:36:00")

        with patch.multiple(
            core, RELEASE_TARGETS=targets, WEEKLY_RELEASE_RULES=[],
            ROOMS=[core.RoomSpec("F5", "7F-A05R", 1)],
            now_cn=lambda: clock[0], run_release_batch=finish_batch,
        ):
            await core.run_release(None, asyncio.Event(), {})

        self.assertEqual(dispatched, [date(2026, 10, 19), date(2026, 10, 20), date(2026, 10, 21)])

    async def test_first_ready_page_prepares_and_releases_while_second_page_authenticates(self):
        target = core.Target(date(2026, 10, 25), "14:00", "16:00", ("F5",))
        rooms = [core.RoomSpec("F5", "7F-A05R", 1), core.RoomSpec("F5", "7F-A28R", 2)]
        batch = core.build_release_batches([target], rooms)[0]
        opened = core.at_cn(batch.release_day, "08:30:00")
        prepare_at = opened - timedelta(seconds=300)
        guard_at = prepare_at - timedelta(seconds=120)
        clock = [guard_at]
        allow_second = asyncio.Event()
        first_prepared = asyncio.Event()
        first_submitted = asyncio.Event()
        calls = []

        class FakePool:
            def __init__(self):
                self.lock = asyncio.Lock()
                self.allocation_started = None
                self.second_ready = False

            async def iter_ready(self, needed, *, interrupted=None, retry_authentication=False):
                self.allocation_started = clock[0]
                if needed:
                    yield "first"
                if needed > 1:
                    await allow_second.wait()
                    self.second_ready = True
                    yield "second"

            async def get(self, needed):
                # Preserve the old eager interface so the regression fails on
                # its scheduling barrier, rather than a missing mock method.
                return [page async for page in self.iter_ready(needed)]

        pool = FakePool()
        priority = asyncio.Event()

        async def advance_to(moment):
            # An independent opening observer may wait before the worker does.
            # Waiting for a future time must not skip the intervening prepare
            # time merely because that observer was scheduled first.
            if moment >= opened:
                await first_prepared.wait()
            clock[0] = max(clock[0], moment)
            await asyncio.sleep(0)

        async def prepare(page, room, requested, *args, **kwargs):
            calls.append(("prepare", page, clock[0]))
            if page == "first":
                first_prepared.set()
            return True

        async def release(page, room, job, release_at, deadline, ready):
            await core.wait_until(release_at)
            calls.append(("submit", page, clock[0]))
            if page == "first":
                first_submitted.set()
            return "dry_run"

        with patch.multiple(core, now_cn=lambda: clock[0], wait_until=advance_to,
                            prepare_until_release=prepare, release_worker=release):
            task = asyncio.create_task(core.run_release_batch(pool, priority, {}, batch))
            try:
                await asyncio.wait_for(first_submitted.wait(), timeout=1)
                self.assertFalse(pool.second_ready)
                self.assertEqual(pool.allocation_started, guard_at)
                self.assertIn(("prepare", "first", prepare_at), calls)
                self.assertIn(("submit", "first", opened), calls)
                self.assertTrue(pool.lock.locked())
                self.assertTrue(priority.is_set())
            finally:
                allow_second.set()
                await asyncio.wait_for(task, timeout=1)

        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())

    async def test_wait_until_reaches_exact_target_with_fractional_last_wait(self):
        target = core.at_cn(date(2026, 10, 18), "08:30:00")
        clock = [target - timedelta(seconds=2.5)]
        sleeps = []

        async def advance(seconds):
            sleeps.append(seconds)
            clock[0] += timedelta(seconds=seconds)

        with patch.object(core, "now_cn", lambda: clock[0]), patch.object(core, "sleep_or_stop", advance):
            await core.wait_until(target)

        self.assertEqual(clock[0], target)
        self.assertEqual(sleeps, [1.0, 1.0, 0.5])


if __name__ == "__main__":
    unittest.main()

