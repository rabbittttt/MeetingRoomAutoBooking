"""Event-driven shutdown regressions; no browser, files, or real long waits."""
import asyncio
from datetime import timedelta
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import booking_core as core


class ShutdownTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.flags = {}
        for name in ("STOP_REQUESTED", "AUTH_BLOCKED", "CONFIG_BLOCKED", "DRIVER_LOST"):
            event = self.flags[name] = asyncio.Event()
            patcher = patch.object(core, name, event)
            patcher.start()
            self.addCleanup(patcher.stop)

    async def test_environment_events_wake_long_sleep(self):
        for name in ("AUTH_BLOCKED", "CONFIG_BLOCKED", "DRIVER_LOST"):
            with self.subTest(event=name):
                for event in self.flags.values():
                    event.clear()
                entered = asyncio.Event()

                async def sleeper():
                    entered.set()
                    await core.sleep_or_stop(3600)

                task = asyncio.create_task(sleeper())
                await asyncio.wait_for(entered.wait(), timeout=1)
                self.flags[name].set()
                with self.assertRaises(core.StopRequested):
                    await asyncio.wait_for(task, timeout=1)

    async def test_environment_event_wakes_future_release_wait(self):
        entered = asyncio.Event()

        async def waiting_for_release():
            entered.set()
            await core.wait_until(core.now_cn() + timedelta(days=7))

        task = asyncio.create_task(waiting_for_release())
        await asyncio.wait_for(entered.wait(), timeout=1)
        core.DRIVER_LOST.set()
        with self.assertRaises(core.StopRequested):
            await asyncio.wait_for(task, timeout=1)

    async def test_mode_failure_preserves_original_error_and_wakes_other_mode(self):
        entered, exited = asyncio.Event(), asyncio.Event()
        original = RuntimeError("offline page creation failure")

        async def waiting_mode():
            entered.set()
            try:
                await core.sleep_or_stop(3600)
            finally:
                exited.set()

        async def failing_mode():
            await entered.wait()
            raise original

        with self.assertRaises(RuntimeError) as caught:
            await asyncio.wait_for(
                core.run_modes_safely(waiting_mode(), failing_mode()), timeout=1)
        self.assertIs(caught.exception, original)
        self.assertTrue(core.STOP_REQUESTED.is_set())
        self.assertTrue(exited.is_set())

    async def test_mode_failure_drains_in_flight_reply_without_cancelling_it(self):
        submitted, allow_reply = asyncio.Event(), asyncio.Event()
        replied, cancelled = asyncio.Event(), asyncio.Event()
        original = RuntimeError("offline second-mode failure")

        async def in_flight_request():
            submitted.set()
            try:
                await allow_reply.wait()
                replied.set()
                return "success"
            except asyncio.CancelledError:
                cancelled.set()
                raise

        async def failing_mode():
            await submitted.wait()
            raise original

        runner = asyncio.create_task(
            core.run_modes_safely(in_flight_request(), failing_mode()))
        try:
            await asyncio.wait_for(core.STOP_REQUESTED.wait(), timeout=1)
            self.assertFalse(runner.done(), "Wait for the already-sent request's result")
            self.assertFalse(cancelled.is_set())
            allow_reply.set()
            with self.assertRaises(RuntimeError) as caught:
                await asyncio.wait_for(runner, timeout=1)
            self.assertIs(caught.exception, original)
            self.assertTrue(replied.is_set())
            self.assertFalse(cancelled.is_set())
        finally:
            allow_reply.set()
            if not runner.done():
                await asyncio.wait_for(
                    asyncio.gather(runner, return_exceptions=True), timeout=1)


if __name__ == "__main__":
    unittest.main()

