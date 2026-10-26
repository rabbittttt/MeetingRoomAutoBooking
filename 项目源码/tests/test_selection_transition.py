"""Real-Chrome selection transition regressions. All network is blocked."""
import asyncio
from contextlib import ExitStack
from datetime import datetime, timedelta
import os
import tempfile
from pathlib import Path
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import async_playwright

import booking_core as core


class SelectionTransitionBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.page = await self.browser.new_page()
        await self.page.route("**/*", lambda route: route.abort())
        self.log_dir = Path(self.stack.enter_context(tempfile.TemporaryDirectory(prefix="booking_selection_test_")))
        self.stack.enter_context(patch.object(core, "LOG_DIR", self.log_dir))
        self.stack.enter_context(patch.object(core, "SELECTION_DIAGNOSTICS_RECORDED", set()))
        self.current = datetime(2030, 1, 1, 10, 0, tzinfo=core.CN_TZ)
        self.target = core.Target(self.current.date() + timedelta(days=2),
                                  "14:00", "16:00", ("F5",))
        self.room = core.RoomSpec("F5", "7F-A28R", 2)
        self.job = core.BookingJob(self.target, asyncio.Event())
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.stack.enter_context(patch.object(core, "GRID_SELECTOR_BLOCKED", set()))
        self.stack.enter_context(patch.object(core, "now_cn", lambda: self.current))
        self.stack.enter_context(patch.object(core, "DRY_RUN", True))
        self.stack.enter_context(patch.object(core, "ACTION_TIMEOUT_MS", 3000))
        self.stack.enter_context(patch.object(self.page, "goto", AsyncMock()))
        for name in ("activate_booking_page", "select_date", "select_start_time", "select_duration",
                     "open_room_list", "verify_room_identity", "choose_room_cells", "save_debug"):
            self.stack.enter_context(patch.object(core, name, AsyncMock()))
        self.stack.enter_context(patch.object(core, "is_403_page", AsyncMock(return_value=False)))

    async def asyncTearDown(self):
        self.stack.close()
        await self.browser.close()
        await self.pw.stop()

    async def load_selection(self, confirm_js="window.confirmClicks++", *, disabled=False):
        button_disabled = "disabled" if disabled else ""
        await self.page.set_content("""
            <button aria-label="图标 添加会议室">添加会议室</button>
            <button id="final" onclick="window.finalClicks++">预定</button>
            <div class="ev_Dialog_Panel" id="selection">
              <div class="ev_Dialog_Title">添加会议室</div>
              <div class="ev_select_div">科研楼F5</div>
              <div class="ev_Dialog_PanelContent">CN-ShenZhen-F5-7F-A28R</div>
              <button id="confirm" """ + button_disabled + """>确认</button>
            </div>
            <script>window.confirmClicks=0; window.finalClicks=0;</script>
        """)
        await self.page.locator("#confirm").evaluate(
            "(button, code) => button.onclick = new Function(code)", confirm_js)

    async def assert_background_untouched(self):
        self.assertEqual(await self.page.evaluate("window.finalClicks"), 0)
        self.assertFalse(self.job.done.is_set())
        self.assertFalse(self.job.uncertain.is_set())

    async def test_prepare_does_not_accept_background_final_while_selection_stays_open(self):
        await self.load_selection()
        result = await core.prepare_page(self.page, self.room, self.target, watch_mode=True)
        self.assertIsNone(result)
        self.assertEqual(await self.page.evaluate("window.confirmClicks"), 1)
        await self.assert_background_untouched()

    async def test_dry_run_does_not_accept_background_final_while_selection_stays_open(self):
        await self.load_selection()
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertNotEqual(result, "dry_run")
        await self.assert_background_untouched()

    async def test_real_submit_does_not_click_background_final_while_selection_stays_open(self):
        await self.load_selection()
        with patch.object(core, "DRY_RUN", False), \
             patch.object(core, "booking_feedback", AsyncMock(return_value="timeout")):
            result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertNotEqual(result, "success")
        await self.assert_background_untouched()

    async def test_prepare_waits_for_actual_async_selection_close(self):
        await self.load_selection("window.confirmClicks++; setTimeout(() => {"
                                  "document.getElementById('selection').style.display='none';"
                                  "window.closedSelection=true;}, 130)")
        result = await core.prepare_page(self.page, self.room, self.target, watch_mode=True)
        self.assertTrue(result)
        self.assertTrue(await self.page.evaluate("window.closedSelection === true"))
        self.assertEqual(await core.confirm_and_book(self.page, self.room, self.job), "dry_run")
        await self.assert_background_untouched()

    async def test_direct_confirmation_waits_for_actual_async_selection_close(self):
        await self.load_selection("window.confirmClicks++; setTimeout(() => {"
                                  "document.getElementById('selection').remove();"
                                  "window.closedSelection=true;}, 130)")
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertEqual(result, "dry_run")
        self.assertTrue(await self.page.evaluate("window.closedSelection === true"))
        await self.assert_background_untouched()

    async def test_replacing_selection_dom_does_not_count_as_closing(self):
        await self.load_selection("window.confirmClicks++; const old = document.getElementById('selection');"
                                  "const replacement = old.cloneNode(true); old.replaceWith(replacement);")
        result = await core.prepare_page(self.page, self.room, self.target, watch_mode=True)
        self.assertIsNone(result)
        self.assertTrue(await self.page.locator("#selection").is_visible())
        self.assertNotEqual(await core.confirm_and_book(self.page, self.room, self.job), "dry_run")
        await self.assert_background_untouched()

    async def test_old_selection_closing_with_new_selection_open_is_not_ready(self):
        await self.load_selection("window.confirmClicks++; const old = document.getElementById('selection');"
                                  "const replacement = old.cloneNode(true); replacement.id='new-selection';"
                                  "old.style.display='none'; document.body.append(replacement);")
        result = await core.prepare_page(self.page, self.room, self.target, watch_mode=True)
        self.assertIsNone(result)
        self.assertTrue(await self.page.locator("#new-selection").is_visible())
        self.assertNotEqual(await core.confirm_and_book(self.page, self.room, self.job), "dry_run")
        await self.assert_background_untouched()

    async def test_disabled_selection_confirm_blocks_visible_background_final(self):
        await self.load_selection(disabled=True)
        self.assertIsNone(await core.prepare_page(self.page, self.room, self.target, watch_mode=True))
        self.assertNotEqual(await core.confirm_and_book(self.page, self.room, self.job), "dry_run")
        self.assertEqual(await self.page.evaluate("window.confirmClicks"), 0)
        await self.assert_background_untouched()


    async def test_selection_reappearing_during_real_retry_is_never_dismissed_or_submitted(self):
        await self.load_selection()
        await self.page.evaluate(r"""() => {
            const selection = document.getElementById('selection');
            selection.style.display = 'none';
            selection.insertAdjacentHTML('beforeend', '<p>该会议室已被预订</p>' +
                '<span class="ev_Dialog_closeIcon" onclick="window.selectionCloseClicks++;' +
                'document.getElementById(\'selection\').remove()">关闭选房</span>');
            window.selectionCloseClicks = 0;
            document.getElementById('final').onclick = () => {
                window.finalClicks++;
                document.body.insertAdjacentHTML('beforeend', '<div class="ev_Dialog_Panel" id="result">' +
                    '<p>超过最大可提前预约天数</p>' +
                    '<button onclick="document.getElementById(\'result\').remove()">关闭</button></div>');
            };
        }""")
        async def selection_returns(_seconds):
            await self.page.locator('#selection').evaluate("e => e.style.display='block'")
        with patch.object(core, 'DRY_RUN', False), \
             patch.object(core, 'booking_feedback', AsyncMock(side_effect=['not_open', 'occupied'])), \
             patch.object(core, 'sleep_or_stop', selection_returns):
            result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertNotEqual(result, 'success')
        self.assertEqual(await self.page.evaluate('window.finalClicks'), 1)
        self.assertEqual(await self.page.evaluate('window.selectionCloseClicks'), 0)
        self.assertTrue(await self.page.locator('#selection').is_visible())



    async def test_visible_confirm_without_button_role_still_blocks_background(self):
        await self.load_selection()
        await self.page.locator('#confirm').evaluate(
            "e => e.outerHTML='<div id=\"confirm\">确认</div>'")
        self.assertTrue(await core.room_selection_is_open(self.page))
        self.assertTrue(await core.is_confirm_ready(self.page))
        result = await core.confirm_and_book(self.page, self.room, self.job)
        self.assertNotEqual(result, 'dry_run')
        await self.assert_background_untouched()
        files = list(self.log_dir.glob('*_格子结构_*.json'))
        self.assertEqual(len(files), 1)
        import json
        diagnostic = json.loads(files[0].read_text(encoding='utf-8'))
        self.assertEqual(diagnostic['target']['start'], self.target.start)
        self.assertEqual(diagnostic['globalConfirmRoleCount'], 0)
        self.assertEqual(diagnostic['dialogConfirmRoleCount'], 0)
        self.assertTrue(any(n['tag'] == 'DIV' for n in diagnostic['confirmCandidates']))

    async def test_confirm_button_outside_selection_does_not_make_inner_button_ambiguous(self):
        await self.load_selection(
            "window.confirmClicks++; document.getElementById('selection').style.display='none'")
        await self.page.evaluate(
            "document.body.insertAdjacentHTML('beforeend','<button>确认</button>')")
        self.assertEqual(await self.page.get_by_role('button', name='确认', exact=True).count(), 2)
        self.assertTrue(await core.is_confirm_ready(self.page))
        self.assertTrue(await core.prepare_page(self.page, self.room, self.target, watch_mode=True))
        self.assertEqual(await self.page.evaluate('window.confirmClicks'), 1)
        await self.assert_background_untouched()

    async def test_selection_confirmation_can_enable_after_grid_update(self):
        await self.load_selection(
            "window.confirmClicks++; document.getElementById('selection').style.display='none'",
            disabled=True)
        await self.page.evaluate(
            "setTimeout(()=>document.getElementById('confirm').disabled=false,130)")
        self.assertTrue(await core.prepare_page(self.page, self.room, self.target, watch_mode=True))
        self.assertEqual(await self.page.evaluate('window.confirmClicks'), 1)
        await self.assert_background_untouched()


    async def test_custom_confirm_text_can_close_selection_without_button_role(self):
        await self.load_selection()
        await self.page.locator('#confirm').evaluate(
            '''e => e.outerHTML='<div id="confirm">确认</div>' ''')
        await self.page.locator('#confirm').evaluate('''e => e.onclick=()=>{
            window.confirmClicks++;
            document.getElementById('selection').style.display='none';
        }''')
        self.assertEqual(await self.page.get_by_role('button', name='确认', exact=True).count(), 0)
        self.assertTrue(await core.prepare_page(self.page, self.room, self.target, watch_mode=True))
        self.assertFalse(await core.room_selection_is_open(self.page))
        self.assertEqual(await self.page.evaluate('window.confirmClicks'), 1)
        await self.assert_background_untouched()



    async def test_disabled_custom_confirm_parent_is_not_treated_as_ready(self):
        await self.load_selection()
        await self.page.locator('#confirm').evaluate(
            '''e => e.outerHTML='<div class="custom disabled"><span id="confirm">确认</span></div>' ''')
        await self.page.locator('#confirm').evaluate('''e => e.onclick=()=>{
            window.confirmClicks++;
            document.getElementById('selection').style.display='none';
        }''')
        self.assertFalse(await core.is_confirm_ready(self.page))
        self.assertIsNone(await core.prepare_page(self.page, self.room, self.target, watch_mode=True))
        self.assertEqual(await self.page.evaluate('window.confirmClicks'), 0)
        await self.assert_background_untouched()


if __name__ == "__main__":
    unittest.main()
