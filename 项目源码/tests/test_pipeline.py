"""Local-Chrome release pipeline checks; never connect to the booking service.

The date/time dropdown and area/navigation steps are replaced by a local page.
Room identity, cell clicks, confirmation transition, final-button guards, final
click/receipt observation, and release scheduling execute production code.
"""
import asyncio
from contextlib import ExitStack
from datetime import date, timedelta
import json
import os
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core
from tests.test_grid_regressions import ROOMS, fixture_html


def pipeline_html(*, confirm_mode="close", columns=(21, 24)):
    html = fixture_html(redraw=True, header=True)
    html = html.replace("selected[0] === 21 && selected[1] === 24",
                        f"selected[0] === {columns[0]} && selected[1] === {columns[1]}")
    return html + r'''<style>
      .ev_Dialog_Panel,.ev_Dialog_PanelContent {pointer-events:auto;}
      </style><button id="add-room" style="position:absolute;top:550px">图标 添加会议室</button>
      <button id="final" style="position:absolute;top:590px">预定</button>
      <script>
      window.finalClicks = 0;
      const confirmMode = ''' + json.dumps(confirm_mode) + r''';
      if (confirmMode === 'disabled') {
        const normalCellClicked = window.cellClicked;
        window.cellClicked = function(cell) {
          normalCellClicked(cell);
          document.getElementById('confirm').disabled = true;
        };
      }
      document.getElementById('confirm').onclick = () => {
        if (confirmMode === 'close')
          document.querySelector('.ev_Dialog_Panel').style.display = 'none';
      };
      document.getElementById('final').onclick = () => {
        window.finalClicks++;
        window.recordSubmit();
        const result = document.createElement('div');
        result.setAttribute('role', 'alert');
        result.innerText = ''' + json.dumps(core.SUCCESS_TEXT) + r''';
        document.body.appendChild(result);
      };
      </script>'''


class LocalBookingPage:
    """Keep production prepare_page intact while replacing external navigation."""
    def __init__(self, page, html, *, before_navigation=None):
        self.page = page
        self.html = html
        self.before_navigation = before_navigation
        self.navigation_started = asyncio.Event()
        self.navigation_cancelled = asyncio.Event()

    @property
    def url(self):
        return "about:blank"

    async def goto(self, *args, **kwargs):
        self.navigation_started.set()
        try:
            if self.before_navigation is not None:
                await self.before_navigation.wait()
            # A real reload replaces the JS context as well as the document.
            # set_content alone preserves lexical bindings and breaks scripts
            # with top-level const declarations on the second preparation.
            await self.page.goto("about:blank")
            await self.page.set_content(self.html)
        except asyncio.CancelledError:
            self.navigation_cancelled.set()
            raise

    async def reload(self, *args, **kwargs):
        return await self.goto(*args, **kwargs)

    def __getattr__(self, name):
        return getattr(self.page, name)


class LocalPagePool:
    def __init__(self, pages):
        self.pages = pages
        self.lock = asyncio.Lock()

    async def iter_ready(self, needed, *, interrupted=None, retry_authentication=False):
        for page in self.pages[:needed]:
            if interrupted and interrupted():
                raise core.PreparationInterrupted()
            yield page


class ReleasePipelineBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.production_select_duration = core.select_duration
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 800})
        await self.context.route("**/*", lambda route: route.abort())
        self.target = core.Target(date(2030, 1, 3), "10:00", "12:00", ("F5-1A",))
        self.opened = core.at_cn(date(2029, 12, 31), "08:30:00")
        self.clock = self.opened - timedelta(minutes=5)
        self.clock_changed = asyncio.Event()
        self.submissions = []
        self.running = []
        self.pages = []
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.multiple(
            core, DRY_RUN=False, RELEASE_OPEN_AT="08:30:00",
            RELEASE_PREPARE_BEFORE_SECONDS=300, RELEASE_STOP_AFTER_SECONDS=300,
            WATCH_PREP_GUARD_SECONDS=120, GRID_SELECTOR_BLOCKED=set(),
            SELECTION_DIAGNOSTICS_RECORDED=set(), RELEASE_RETRY_SECONDS=0.05,
            now_cn=lambda: self.clock, wait_until=self.wait_until,
            activate_booking_page=AsyncMock(), is_403_page=AsyncMock(return_value=False),
            select_date=AsyncMock(), select_start_time=AsyncMock(),
            select_duration=AsyncMock(), open_room_list=AsyncMock(),
            save_grid_diagnostic=AsyncMock(), save_debug=AsyncMock()))

    async def asyncTearDown(self):
        core.STOP_REQUESTED.set()
        self.clock_changed.set()
        for task in self.running:
            if not task.done():
                task.cancel()
        await asyncio.gather(*self.running, return_exceptions=True)
        self.stack.close()
        await self.browser.close()
        await self.pw.stop()

    async def wait_until(self, moment):
        while self.clock < moment and not core.STOP_REQUESTED.is_set():
            self.clock_changed.clear()
            await self.clock_changed.wait()

    def advance(self, moment):
        self.clock = moment
        self.clock_changed.set()

    async def make_page(self, row=1, *, confirm_mode="close", gate=None, columns=(21, 24)):
        page = await self.context.new_page()
        await page.expose_function("recordSubmit", lambda: self.submissions.append((row, self.clock)))
        wrapped = LocalBookingPage(page, pipeline_html(confirm_mode=confirm_mode, columns=columns),
                                   before_navigation=gate)
        self.pages.append(wrapped)
        return wrapped

    def run_batch(self, pages, rooms=None):
        if rooms is None:
            rooms = [core.RoomSpec("F5-1A", ROOMS[index], index + 1)
                     for index in range(len(pages))]
        batch = core.build_release_batches([self.target], rooms)[0]
        self.assertEqual(batch.release_day, self.opened.date())
        pool, priority, registry = LocalPagePool(pages), asyncio.Event(), {}
        task = asyncio.create_task(core.run_release_batch(pool, priority, registry, batch))
        self.running.append(task)
        return task, pool, priority, registry

    async def test_thirteen_real_forms_prepare_then_only_submit_after_opening(self):
        pages = [await self.make_page(row) for row in range(1, 14)]
        ready = asyncio.Event()
        prepared = []
        production_prepare = core.prepare_page

        async def observed_prepare(page, room, target, **kwargs):
            outcome = await production_prepare(page, room, target, **kwargs)
            if outcome is True:
                prepared.append(room.row)
                if len(prepared) == 13:
                    ready.set()
            return outcome

        with patch.object(core, "prepare_page", observed_prepare):
            task, pool, priority, registry = self.run_batch(pages)
            await asyncio.wait_for(ready.wait(), timeout=90)
            self.assertEqual(sorted(prepared), list(range(1, 14)))
            self.assertEqual(self.submissions, [])
            for row, page in enumerate(pages, 1):
                self.assertEqual(await page.evaluate("window.clicked"),
                                 [f"room{row}-21", f"room{row}-24"])
                self.assertFalse(await core.room_selection_is_open(page))
                self.assertEqual(await page.evaluate("window.finalClicks"), 0)
            self.advance(self.opened)
            await asyncio.wait_for(task, timeout=45)
        self.assertGreater(len(self.submissions), 0)
        self.assertTrue(all(moment >= self.opened for _, moment in self.submissions))
        self.assertTrue(registry[self.target.key].done.is_set())
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())

    async def test_half_past_start_and_end_follow_actual_selected_columns(self):
        target = core.Target(self.target.day, "10:30", "12:30", ("F5-1A",))
        page = await self.make_page(8, columns=(22, 25))
        room = core.RoomSpec("F5-1A", ROOMS[7], 8)
        self.assertTrue(await core.prepare_page(page, room, target))
        self.assertEqual(await page.evaluate("window.clicked"), ["room8-22", "room8-25"])
        self.assertFalse(await core.room_selection_is_open(page))
        self.assertEqual(self.submissions, [])

    async def test_half_hour_durations_reach_final_page_without_hour_truncation(self):
        cases = (("10:00", "10:30", (21,)),
                 ("10:00", "11:30", (21, 23)),
                 ("10:30", "13:00", (22, 26)))
        for start, end, selected in cases:
            with self.subTest(start=start, end=end):
                columns = (selected[0], selected[-1])
                page = await self.make_page(3, columns=columns)
                if len(selected) == 1:
                    # The user confirmed that one half-hour cell needs one click.
                    page.html = page.html.replace(
                        "selected.length === 2 && selected[0] === 21 && selected[1] === 21",
                        "selected.length === 1 && selected[0] === 21")
                room = core.RoomSpec("F5-1A", ROOMS[2], 3)
                target = core.Target(self.target.day, start, end, ("F5-1A",))
                # No duration dropdown exists in this local grid-driven form.
                # Production code must preserve the requested grid range instead
                # of attempting to set an integer-hour approximation.
                with patch.object(core, "select_duration", self.production_select_duration):
                    self.assertTrue(await core.prepare_page(page, room, target))
                self.assertEqual(await page.evaluate("window.clicked"),
                                 [f"room3-{column}" for column in selected])
                self.assertFalse(await core.room_selection_is_open(page))
                self.assertTrue(await page.get_by_role("button", name="预定", exact=True).is_enabled())
                self.assertEqual(await page.evaluate("window.finalClicks"), 0)
                self.assertEqual(self.submissions, [])

    async def test_occupied_before_opening_waits_then_refreshes_and_books_after_release(self):
        page = await self.make_page(confirm_mode="disabled")
        production_prepare = core.prepare_page
        calls = []
        outcomes = asyncio.Queue()

        async def prepare(*args, **kwargs):
            calls.append((args[1].name, self.clock))
            result = await production_prepare(*args, **kwargs)
            await outcomes.put(result)
            return result

        with patch.object(core, "prepare_page", prepare), self.assertLogs(level="INFO") as captured:
            task, pool, priority, registry = self.run_batch([page])
            self.assertIsNone(await asyncio.wait_for(outcomes.get(), timeout=30))
            # More than one retry interval passes while the release clock remains
            # before opening: an occupied page must stay parked, not re-prepare.
            await asyncio.sleep(core.RELEASE_RETRY_SECONDS * 4)
            self.assertEqual(len(calls), 1)
            self.assertFalse(task.done())
            self.assertTrue(pool.lock.locked())
            self.assertTrue(priority.is_set())
            self.assertFalse(registry[self.target.key].done.is_set())
            self.assertFalse(registry[self.target.key].uncertain.is_set())
            self.assertEqual(self.submissions, [])
            self.assertTrue(await core.room_selection_is_open(page))
            self.assertEqual(await page.evaluate("window.finalClicks"), 0)

            # Changing the navigation response leaves the current disabled DOM
            # untouched. Success therefore requires a real fresh preparation.
            page.html = pipeline_html(confirm_mode="close")
            self.advance(self.opened)
            await asyncio.wait_for(task, timeout=30)
        self.assertIs(await asyncio.wait_for(outcomes.get(), timeout=1), True)
        self.assertEqual(calls, [(ROOMS[0], self.opened - timedelta(minutes=5)),
                                 (ROOMS[0], self.opened)])
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())
        self.assertTrue(registry[self.target.key].done.is_set())
        self.assertFalse(registry[self.target.key].uncertain.is_set())
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        self.assertEqual(self.submissions, [(1, self.opened)])
        self.assertEqual(await page.evaluate("window.finalClicks"), 1)
        self.assertNotIn("提前准备重试", "\n".join(captured.output))

    async def test_occupied_after_opening_rechecks_until_deadline_without_final_click(self):
        page = await self.make_page(confirm_mode="disabled")
        production_prepare = core.prepare_page
        deadline = self.opened + timedelta(minutes=5)
        outcomes = []
        self.advance(self.opened)

        async def prepare(*args, **kwargs):
            result = await production_prepare(*args, **kwargs)
            outcomes.append(result)
            self.assertEqual(await page.evaluate("window.finalClicks"), 0)
            self.assertTrue(await core.room_selection_is_open(page))
            # Keep the target occupied through repeated real preparation, then
            # reach the configured cutoff before any further attempt can begin.
            if len(outcomes) == 2:
                self.advance(deadline)
            return result

        with patch.object(core, "prepare_page", prepare):
            task, pool, priority, registry = self.run_batch([page])
            await asyncio.wait_for(task, timeout=45)
        self.assertEqual(outcomes, [None, None])
        self.assertEqual(self.clock, deadline)
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())
        self.assertFalse(registry[self.target.key].done.is_set())
        self.assertFalse(registry[self.target.key].uncertain.is_set())
        self.assertEqual(core.GRID_SELECTOR_BLOCKED, set())
        self.assertEqual(self.submissions, [])
        self.assertEqual(await page.evaluate("window.finalClicks"), 0)

    async def test_occupied_room_does_not_stop_ready_peer_at_opening(self):
        occupied = await self.make_page(1, confirm_mode="disabled")
        available = await self.make_page(2)
        production_prepare = core.prepare_page
        completed = {}
        ready = asyncio.Event()

        async def prepare(page, room, *args, **kwargs):
            result = await production_prepare(page, room, *args, **kwargs)
            completed[room.row] = result
            if len(completed) == 2:
                ready.set()
            return result

        with patch.object(core, "prepare_page", prepare):
            task, pool, priority, registry = self.run_batch([occupied, available])
            await asyncio.wait_for(ready.wait(), timeout=35)
            self.assertEqual(completed, {1: None, 2: True})
            self.assertFalse(task.done())
            self.assertEqual(self.submissions, [])
            self.advance(self.opened)
            await asyncio.wait_for(task, timeout=30)
        self.assertEqual([row for row, _ in self.submissions], [2])
        self.assertTrue(registry[self.target.key].done.is_set())
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())

    async def test_disabled_and_noop_confirm_never_click_background_final_button(self):
        for mode in ("disabled", "noop"):
            with self.subTest(mode=mode):
                page = await self.make_page(confirm_mode=mode)
                room = core.RoomSpec("F5-1A", ROOMS[0], 1)
                # Keep normal geometry-click timeout; shorten only confirm wait.
                production_choose = core.choose_room_cells

                async def choose_and_shorten(*args):
                    await production_choose(*args)
                    core.ACTION_TIMEOUT_MS = 300

                with patch.object(core, "ACTION_TIMEOUT_MS", core.ACTION_TIMEOUT_MS), \
                     patch.object(core, "choose_room_cells", choose_and_shorten):
                    self.assertIsNone(await core.prepare_page(page, room, self.target))
                    job = core.BookingJob(self.target, asyncio.Event())
                    status = await core.confirm_and_book(page, room, job,
                                                         deadline=self.opened + timedelta(minutes=5))
                self.assertIn(status, ("not_open", "unavailable"))
                self.assertTrue(await core.room_selection_is_open(page))
                self.assertEqual(await page.evaluate("window.finalClicks"), 0)
        self.assertEqual(self.submissions, [])

    async def test_slow_preparation_does_not_block_a_ready_room_at_opening(self):
        gate = asyncio.Event()
        first = await self.make_page(1)
        second = await self.make_page(2, gate=gate)
        ready = asyncio.Event()
        production_prepare = core.prepare_page

        async def observed_prepare(page, room, target, **kwargs):
            result = await production_prepare(page, room, target, **kwargs)
            if room.row == 1 and result is True:
                ready.set()
            return result

        with patch.object(core, "prepare_page", observed_prepare):
            task, pool, priority, _ = self.run_batch([first, second])
            await asyncio.wait_for(ready.wait(), timeout=30)
            await asyncio.wait_for(second.navigation_started.wait(), timeout=5)
            self.assertFalse(gate.is_set())
            self.advance(self.opened)
            await first.page.get_by_text(core.SUCCESS_TEXT, exact=True).wait_for(timeout=15000)
            self.assertEqual([row for row, _ in self.submissions], [1])
            await asyncio.wait_for(task, timeout=3)
            self.assertTrue(second.navigation_cancelled.is_set())
        self.assertEqual([row for row, _ in self.submissions], [1])
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())

    async def test_stop_cancels_unsubmitted_preparation_and_releases_pool(self):
        gate = asyncio.Event()
        page = await self.make_page(gate=gate)
        task, pool, priority, _ = self.run_batch([page])
        await asyncio.wait_for(page.navigation_started.wait(), timeout=5)
        core.STOP_REQUESTED.set()
        self.clock_changed.set()
        await asyncio.wait_for(task, timeout=3)
        self.assertTrue(page.navigation_cancelled.is_set())
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())
        self.assertEqual(self.submissions, [])

    async def test_cutoff_cancels_unsubmitted_preparation_and_releases_pool(self):
        gate = asyncio.Event()
        page = await self.make_page(gate=gate)
        task, pool, priority, _ = self.run_batch([page])
        await asyncio.wait_for(page.navigation_started.wait(), timeout=5)
        self.advance(self.opened + timedelta(minutes=5))
        await asyncio.wait_for(task, timeout=3)
        self.assertTrue(page.navigation_cancelled.is_set())
        self.assertFalse(pool.lock.locked())
        self.assertFalse(priority.is_set())
        self.assertEqual(self.submissions, [])


if __name__ == "__main__":
    unittest.main()
