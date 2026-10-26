"""Offline submission regressions; never connects to the booking service."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

import booking_core as core


class PageDouble:
    def __init__(self):
        self.final = SimpleNamespace(count=AsyncMock(return_value=1),
                                     is_visible=AsyncMock(return_value=True),
                                     click=AsyncMock())
        self.success = SimpleNamespace(is_visible=AsyncMock(return_value=False))
        self.success.filter = lambda **kwargs: SimpleNamespace(first=self.success)
        self.close = SimpleNamespace(count=AsyncMock(return_value=0))
        self.close.filter = lambda **kwargs: self.close

    def get_by_role(self, *args, **kwargs):
        return self.final

    def get_by_text(self, *args, **kwargs):
        return self.success

    def locator(self, *args, **kwargs):
        return self.close


class SubmissionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.current = datetime(2030, 1, 1, 8, 30, tzinfo=core.CN_TZ)
        self.target = core.Target(self.current.date() + timedelta(days=7), "14:00", "16:00", ("F5",))
        self.room = core.RoomSpec("F5", "7F-A05R", 1)
        self.job = core.BookingJob(self.target, asyncio.Event())
        self.page = PageDouble()
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "DRY_RUN", False))
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.current))
        self.clickable = self.stack.enter_context(patch.object(core, "is_final_book_button_clickable", AsyncMock(return_value=True)))
        self.stack.enter_context(patch.object(core, "is_room_occupied_state", AsyncMock(return_value=False)))
        self.dismiss = self.stack.enter_context(patch.object(core, "dismiss_booking_failure_dialog", AsyncMock(return_value=True)))
        self.sleep = self.stack.enter_context(patch.object(core, "sleep_or_stop", AsyncMock()))
        self.popup = self.stack.enter_context(patch.object(core, "booking_failure_dialog", AsyncMock(return_value=("", ""))))
        self.feedback = self.stack.enter_context(patch.object(core, "booking_feedback", AsyncMock(return_value="timeout")))

    async def test_explicit_rejection_retries_after_interval_but_waits_for_new_reply(self):
        waits = []
        async def feedback(page, timeout_ms):
            waits.append(timeout_ms)
            if len(waits) == 1:
                return "not_open"
            return "success" if timeout_ms >= 1000 else "timeout"
        self.feedback.side_effect = feedback
        self.popup.side_effect = lambda page: ("not_open", "超过最大可提前预约天数") if len(waits) == 1 else ("", "")
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertEqual(result, "success")
        self.assertEqual(self.page.final.click.await_count, 2)
        self.assertEqual(waits, [core.SUCCESS_TIMEOUT_MS, core.SUCCESS_TIMEOUT_MS])
        self.sleep.assert_awaited_once_with(core.POPUP_RETRY_SECONDS)

    async def test_unresolved_submission_is_not_clicked_again(self):
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertEqual(result, "uncertain")
        self.assertEqual(self.page.final.click.await_count, 1)
        self.assertTrue(self.job.uncertain.is_set())
        self.assertFalse(self.job.done.is_set())

    async def test_explicit_occupied_does_not_depend_on_result_popup_closing(self):
        self.feedback.return_value = "occupied"
        self.popup.return_value = ("occupied", "会议室已被预订")
        async def dismiss(page, **kwargs):
            if kwargs.get("verify_cleared"):
                self.fail("known occupancy must return for refresh without waiting for dismissal")
            return True
        self.dismiss.side_effect = dismiss
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertEqual(result, "occupied")
        self.page.final.click.assert_awaited_once()
        self.sleep.assert_not_awaited()
        self.assertFalse(self.job.uncertain.is_set())
        self.assertFalse(self.job.done.is_set())

    async def test_unknown_popup_does_not_authorize_another_submission(self):
        self.popup.return_value = ("unknown", "系统已受理，请稍后查询结果")
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertEqual(result, "uncertain")
        self.assertEqual(self.page.final.click.await_count, 1)
        self.assertTrue(self.job.uncertain.is_set())

    async def test_stop_arriving_during_last_button_check_prevents_click(self):
        calls = 0
        async def clickable(button):
            nonlocal calls
            calls += 1
            if calls == 2:
                core.STOP_REQUESTED.set()
            return True
        self.clickable.side_effect = clickable
        await core.confirm_and_book(self.page, self.room, self.job)
        self.page.final.click.assert_not_awaited()

    async def test_meeting_start_arriving_during_last_button_check_prevents_click(self):
        calls = 0
        async def clickable(button):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.current = self.target.start_dt
            return True
        self.clickable.side_effect = clickable
        await core.confirm_and_book(self.page, self.room, self.job)
        self.page.final.click.assert_not_awaited()

    async def test_environment_failure_during_last_button_check_prevents_click(self):
        calls = 0
        async def clickable(button):
            nonlocal calls
            calls += 1
            if calls == 2:
                core.AUTH_BLOCKED.set()
            return True
        self.clickable.side_effect = clickable
        await core.confirm_and_book(self.page, self.room, self.job)
        self.page.final.click.assert_not_awaited()

    async def test_submission_before_deadline_can_receive_success_after_deadline(self):
        deadline = self.current + timedelta(milliseconds=50)
        async def click(**kwargs):
            self.current += timedelta(milliseconds=100)
        self.page.final.click.side_effect = click
        async def feedback(page, timeout_ms):
            self.assertEqual(timeout_ms, core.SUCCESS_TIMEOUT_MS)
            return "success"
        self.feedback.side_effect = feedback
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job, deadline=deadline), "success")
        self.assertTrue(self.job.done.is_set())

    async def test_uncertainty_shared_across_modes_blocks_new_submission(self):
        registry = {}
        first = core.shared_booking_job(registry, self.target)
        second = core.shared_booking_job(registry, self.target)
        first.uncertain.set()
        self.assertIs(first.uncertain, second.uncertain)
        self.assertEqual(await core.confirm_and_book(self.page, self.room, second), "uncertain")
        self.page.final.click.assert_not_awaited()

    async def test_success_has_priority_over_shared_uncertainty(self):
        self.job.uncertain.set()
        self.job.done.set()
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "skipped")
        self.page.final.click.assert_not_awaited()

    async def test_inflight_success_still_wins_after_peer_becomes_uncertain(self):
        async def feedback(page, timeout_ms):
            self.job.uncertain.set()
            return "success"
        self.feedback.side_effect = feedback
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "success")
        self.assertTrue(self.job.done.is_set())

    async def test_success_is_shared_before_reading_popup_text(self):
        self.feedback.return_value = "success"
        async def read_popup(page):
            self.assertTrue(self.job.done.is_set())
            return "success", core.SUCCESS_TEXT
        self.popup.side_effect = read_popup
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "success")

    async def test_peer_success_during_last_button_check_prevents_new_click(self):
        calls = 0
        async def clickable(button):
            nonlocal calls
            calls += 1
            if calls == 2:
                self.job.done.set()
            return True
        self.clickable.side_effect = clickable
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "skipped")
        self.page.final.click.assert_not_awaited()

    async def test_click_exception_does_not_authorize_blind_retry(self):
        self.page.final.click.side_effect = core.PlaywrightTimeout("click response timeout")
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "uncertain")
        self.assertEqual(self.page.final.click.await_count, 1)

    async def test_driver_loss_after_click_preserves_uncertainty(self):
        self.feedback.side_effect = RuntimeError("Connection closed while reading from the driver")
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "uncertain")
        self.assertTrue(core.DRIVER_LOST.is_set())
        self.assertTrue(self.job.uncertain.is_set())
        self.assertEqual(self.page.final.click.await_count, 1)

    async def test_explicit_other_failure_keeps_ten_extra_retries(self):
        self.feedback.return_value = "failure"
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "failed")
        self.assertEqual(self.page.final.click.await_count, core.OTHER_FAILURE_EXTRA_RETRIES + 1)
        self.assertEqual(self.sleep.await_count, core.OTHER_FAILURE_EXTRA_RETRIES)
        self.assertFalse(self.job.uncertain.is_set())

    async def test_retry_wait_precedes_button_state_check_after_dismissal(self):
        events = []
        self.feedback.side_effect = ["not_open", "success"]
        async def dismiss(page, **kwargs):
            if kwargs.get("verify_cleared"):
                events.append("dismiss")
            return True
        async def sleep(seconds):
            events.append("sleep")
        async def clickable(button):
            if events and events[-1] == "dismiss":
                self.fail("button checked during closing animation before retry wait")
            events.append("button")
            return True
        self.dismiss.side_effect = dismiss
        self.sleep.side_effect = sleep
        self.clickable.side_effect = clickable
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "success")
        position = events.index("dismiss")
        self.assertEqual(events[position:position + 3], ["dismiss", "sleep", "button"])


class DismissalTests(unittest.IsolatedAsyncioTestCase):
    async def test_modal_is_hidden_before_returning_without_extra_retry_sleep(self):
        close = SimpleNamespace(count=AsyncMock(return_value=1),
                                is_visible=AsyncMock(return_value=True), click=AsyncMock())
        close.first = close
        panel = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                                inner_text=AsyncMock(return_value="预约失败"),
                                locator=lambda selector: close,
                                wait_for=AsyncMock())
        panels = SimpleNamespace(count=AsyncMock(return_value=1), nth=lambda i: panel)
        page = SimpleNamespace(locator=lambda selector: panels)
        with patch.object(core, "sleep_or_stop", AsyncMock()) as sleep:
            self.assertTrue(await core.dismiss_booking_failure_dialog(page))
            sleep.assert_not_awaited()
        panel.wait_for.assert_awaited_once_with(state="hidden", timeout=1200)

    async def test_stale_toast_cannot_be_used_as_next_submission_result(self):
        empty = SimpleNamespace(count=AsyncMock(return_value=0))
        tip = SimpleNamespace(is_visible=AsyncMock(return_value=True),
                              wait_for=AsyncMock(side_effect=core.PlaywrightTimeout("still visible")))
        tip.filter = lambda **kwargs: SimpleNamespace(first=tip)
        page = SimpleNamespace(locator=lambda selector: empty, get_by_text=lambda *args, **kwargs: tip)
        self.assertFalse(await core.dismiss_booking_failure_dialog(page, verify_cleared=True))
        tip.wait_for.assert_awaited_once_with(state="hidden", timeout=core.ACTION_TIMEOUT_MS)

    async def test_initial_submit_does_not_query_old_toasts(self):
        panels = SimpleNamespace(count=AsyncMock(return_value=0))
        def get_text(*args, **kwargs):
            self.fail("toast queries should only run after a known failure")
        page = SimpleNamespace(locator=lambda selector: panels, get_by_text=get_text)
        self.assertTrue(await core.dismiss_booking_failure_dialog(page))


class FeedbackTests(unittest.IsolatedAsyncioTestCase):
    async def test_driver_disconnect_is_propagated_instead_of_timeout(self):
        locator = SimpleNamespace(wait_for=AsyncMock(side_effect=RuntimeError("Connection closed while reading from the driver")))
        locator.filter = lambda **kwargs: SimpleNamespace(first=locator)
        page = SimpleNamespace(get_by_text=lambda *args, **kwargs: locator)
        with self.assertRaisesRegex(RuntimeError, "Connection closed"):
            await core.booking_feedback(page, 100)


if __name__ == "__main__":
    unittest.main()
