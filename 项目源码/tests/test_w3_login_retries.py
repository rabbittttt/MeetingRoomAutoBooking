"""本地Chrome验证W3有限重试；使用虚构凭据，拦截全部网络请求。"""
import asyncio
import os
import unittest
from unittest.mock import Mock, patch

from playwright.async_api import Locator, async_playwright

import w3_login
from tests.test_w3_login import DUMMY, LOGIN_URL, READY_URL, login_html


class W3LoginRetryBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.context = await self.browser.new_context()
        self.html = login_html()
        self.requests = []

        async def local_only(route):
            url = route.request.url
            self.requests.append(url)
            if url == READY_URL:
                await route.fulfill(body='<meta charset="utf-8"><button>图标 添加会议室</button>',
                                    content_type="text/html")
            elif url == LOGIN_URL:
                await route.fulfill(body=self.html, content_type="text/html")
            else:
                await route.abort()

        await self.context.route("**/*", local_only)
        self.page = await self.context.new_page()
        self.tick = 0.0
        self.loader = Mock(return_value=DUMMY)
        self.login = w3_login.W3LoginFallback(
            credentials_loader=self.loader, clock=lambda: self.tick, grace_seconds=0)
        self.context._booking_w3_fallback = self.login

    async def asyncTearDown(self):
        try:
            await self.browser.close()
            await self.pw.stop()
        finally:
            self.assertTrue(all(url in (LOGIN_URL, READY_URL) for url in self.requests))

    async def open_login(self, page=None, *, mode="remain"):
        self.html = login_html(mode=mode)
        await (page or self.page).goto(LOGIN_URL)

    async def new_login_page(self):
        page = await self.context.new_page()
        await self.open_login(page)
        return page

    async def clicks(self, page=None):
        return await (page or self.page).evaluate("window.loginClicks")

    async def notify_real_form(self, page):
        self.assertEqual(page.url, READY_URL)
        self.assertTrue(await page.get_by_role("button", name="图标 添加会议室").is_visible())
        w3_login.notify_w3_authenticated(page)

    async def test_fill_failure_retries_at_three_seconds_without_leaking_inputs(self):
        await self.open_login()
        original = Locator.fill

        async def fail_saved_password(locator, value, **kwargs):
            if value == DUMMY.password:
                raise RuntimeError(f"fill({DUMMY.username}, {DUMMY.password}) failed")
            return await original(locator, value, **kwargs)

        with patch.object(Locator, "fill", fail_saved_password), self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await self.login.maybe_login(self.page), "retrying")
        self.assertNotIn(DUMMY.password, "\n".join(logs.output))
        self.assertNotIn(DUMMY.username, "\n".join(logs.output))
        self.assertEqual(await self.clicks(), 0)
        self.assertEqual(await self.page.get_by_placeholder("密码", exact=True).input_value(), "")
        self.assertEqual(self.login.attempts, 1)
        self.tick = 2.999
        self.assertEqual(await self.login.maybe_login(self.page), "skipped")
        self.loader.assert_called_once_with()
        self.tick = 3.0
        self.assertEqual(await self.login.maybe_login(self.page), "submitted")
        self.assertEqual(await self.clicks(), 1)
        self.assertEqual(self.login.attempts, 2)

    async def test_dispatched_click_exception_waits_ten_seconds_before_another_click(self):
        await self.open_login()
        original = Locator.click

        async def dispatch_then_fail(locator, **kwargs):
            await original(locator, **kwargs)
            raise RuntimeError("操作完成后的异常 " + DUMMY.password)

        with patch.object(Locator, "click", dispatch_then_fail), self.assertLogs(level="WARNING") as logs:
            self.assertEqual(await self.login.maybe_login(self.page), "retrying")
        self.assertEqual(await self.clicks(), 1)
        self.assertNotIn(DUMMY.password, "\n".join(logs.output))
        for moment in (3.0, 9.999):
            self.tick = moment
            self.assertEqual(await self.login.maybe_login(self.page), "skipped")
            self.assertEqual(await self.clicks(), 1)
        self.tick = 10.0
        self.assertEqual(await self.login.maybe_login(self.page), "submitted")
        self.assertEqual(await self.clicks(), 2)
        self.assertEqual(self.login.attempts, 2)

    async def test_three_attempt_quota_survives_reload_and_new_pages(self):
        await self.open_login()
        self.assertEqual(await w3_login.try_w3_backup_login(self.page), "submitted")
        self.assertEqual(await self.clicks(), 1)
        await self.page.reload()
        self.assertEqual(await self.clicks(), 0)
        self.tick = 10.0
        self.assertEqual(await w3_login.try_w3_backup_login(self.page), "submitted")
        self.assertEqual(await self.clicks(), 1)
        other = await self.new_login_page()
        self.tick = 20.0
        self.assertEqual(await w3_login.try_w3_backup_login(other), "skipped")
        self.assertEqual(await self.clicks(other), 0)
        await self.page.close()
        self.assertEqual(await w3_login.try_w3_backup_login(other), "submitted")
        self.assertEqual(await self.clicks(other), 1)
        self.assertEqual(self.login.attempts, 3)
        await other.close()
        final_page = await self.new_login_page()
        self.tick = 30.0
        self.assertEqual(await w3_login.try_w3_backup_login(final_page), "manual")
        self.tick = 300.0
        await final_page.reload()
        self.assertEqual(await w3_login.try_w3_backup_login(final_page), "manual")
        self.assertEqual(await self.clicks(final_page), 0)
        self.assertEqual(self.loader.call_count, 3)

    async def test_concurrent_pages_share_one_inflight_attempt(self):
        await self.open_login()
        other = await self.new_login_page()
        results = await asyncio.gather(w3_login.try_w3_backup_login(self.page),
                                       w3_login.try_w3_backup_login(other))
        self.assertEqual(results.count("submitted"), 1)
        self.assertEqual(await self.clicks() + await self.clicks(other), 1)
        self.assertEqual(self.login.attempts, 1)
        self.loader.assert_called_once_with()

    async def test_closed_owner_allows_takeover_only_after_existing_cooldown(self):
        await self.open_login()
        await self.login.maybe_login(self.page)
        self.assertEqual(await self.clicks(), 1)
        other = await self.new_login_page()
        await self.page.close()
        for moment in (0.0, 9.999):
            self.tick = moment
            self.assertEqual(await self.login.maybe_login(other), "skipped")
            self.assertEqual(await self.clicks(other), 0)
        self.tick = 10.0
        self.assertEqual(await self.login.maybe_login(other), "submitted")
        self.assertEqual(await self.clicks(other), 1)
        self.assertEqual(self.login.attempts, 2)

    async def test_live_owner_lease_expires_at_sixty_seconds_without_resetting_quota(self):
        await self.open_login()
        await self.login.maybe_login(self.page)
        other = await self.new_login_page()
        for moment in (10.0, 59.999):
            self.tick = moment
            self.assertEqual(await self.login.maybe_login(other), "skipped")
            self.assertEqual(await self.clicks(other), 0)
        self.tick = 60.0
        self.assertEqual(await self.login.maybe_login(other), "submitted")
        self.assertEqual(await self.clicks(other), 1)
        self.assertEqual(self.login.attempts, 2)
        self.assertFalse(self.page.is_closed())
        self.assertEqual(await self.login.maybe_login(self.page), "skipped")

    async def test_nonowner_ready_notification_does_not_clear_live_owner_quota(self):
        await self.open_login()
        await self.login.maybe_login(self.page)
        other = await self.context.new_page()
        await other.goto(READY_URL)
        await self.notify_real_form(other)
        self.assertEqual(self.login.attempts, 1)
        self.assertEqual(await self.login.maybe_login(self.page), "skipped")
        self.tick = 10.0
        self.assertEqual(await self.login.maybe_login(self.page), "submitted")
        self.assertEqual(self.login.attempts, 2)

    async def test_owner_verified_success_resets_quota_for_later_logout(self):
        await self.open_login(mode="success")
        self.assertEqual(await self.login.maybe_login(self.page), "submitted")
        await self.page.wait_for_url(READY_URL)
        self.assertEqual(self.login.attempts, 1)
        # 仅click返回或跳转还不能清零，调用方须实际看见预订表单。
        await self.notify_real_form(self.page)
        self.assertEqual(self.login.attempts, 0)
        await self.open_login()
        self.assertEqual(await self.login.maybe_login(self.page), "submitted")
        self.assertEqual(await self.clicks(), 1)
        self.assertEqual(self.login.attempts, 1)

    async def test_peer_authentication_after_owner_close_resets_for_next_logout(self):
        await self.open_login()
        await self.login.maybe_login(self.page)
        other = await self.new_login_page()
        self.assertEqual(await self.login.maybe_login(other), "skipped")
        await self.page.close()
        # 模拟该轮另一登录页经共享认证/人工登录真正回到表单。
        await other.goto(READY_URL)
        await self.notify_real_form(other)
        self.assertEqual(self.login.attempts, 0)
        await self.open_login(other)
        self.assertEqual(await self.login.maybe_login(other), "submitted")
        self.assertEqual(await self.clicks(other), 1)
        self.assertEqual(self.login.attempts, 1)

    async def test_explicit_password_error_lock_or_challenge_stops_further_submissions(self):
        for text in ("账号或密码错误", "账号已锁定", "请输入验证码"):
            with self.subTest(message=text):
                self.tick = 0.0
                self.login = w3_login.W3LoginFallback(
                    credentials_loader=self.loader, clock=lambda: self.tick, grace_seconds=0)
                await self.open_login()
                await self.login.maybe_login(self.page)
                await self.page.evaluate("""text => {
                    const alert = document.createElement('div');
                    alert.setAttribute('role', 'alert'); alert.textContent = text;
                    document.body.appendChild(alert);
                }""", text)
                self.tick = 10.0
                self.assertEqual(await self.login.maybe_login(self.page), "manual")
                for moment in (20.0, 100.0):
                    self.tick = moment
                    self.assertIn(await self.login.maybe_login(self.page), ("manual", "skipped"))
                    self.assertEqual(await self.clicks(), 1)
                self.assertEqual(self.login.attempts, 1)

    async def test_stop_before_attempt_never_reads_or_clicks(self):
        await self.open_login()
        self.assertEqual(await self.login.maybe_login(self.page, stopped=lambda: True), "skipped")
        self.loader.assert_not_called()
        self.assertEqual(await self.clicks(), 0)

    async def test_stop_after_filling_password_clears_it_without_clicking(self):
        await self.open_login()
        original = Locator.fill
        stopped = False

        async def stop_after_fill(locator, value, **kwargs):
            nonlocal stopped
            await original(locator, value, **kwargs)
            if value == DUMMY.password:
                stopped = True

        with patch.object(Locator, "fill", stop_after_fill):
            self.assertEqual(await self.login.maybe_login(self.page, stopped=lambda: stopped), "skipped")
        self.assertEqual(await self.clicks(), 0)
        self.assertEqual(await self.page.get_by_placeholder("密码", exact=True).input_value(), "")
        self.tick = 100.0
        self.assertEqual(await self.login.maybe_login(self.page, stopped=lambda: stopped), "skipped")
        self.assertEqual(await self.clicks(), 0)

    async def test_cancel_after_dispatched_click_releases_lock_without_extra_click(self):
        await self.open_login()
        original = Locator.click
        dispatched = asyncio.Event()

        async def click_then_wait(locator, **kwargs):
            await original(locator, **kwargs)
            dispatched.set()
            await asyncio.Event().wait()

        with patch.object(Locator, "click", click_then_wait):
            task = asyncio.create_task(self.login.maybe_login(self.page))
            try:
                await asyncio.wait_for(dispatched.wait(), 3)
                self.assertEqual(await self.clicks(), 1)
                task.cancel()
                with self.assertRaises(asyncio.CancelledError):
                    await task
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        other = await self.new_login_page()
        # 关闭被取消操作所属页后，另一页仍须遵守原有10秒冷却。
        await self.page.close()
        self.tick = 9.999
        self.assertEqual(await asyncio.wait_for(self.login.maybe_login(other), 1), "skipped")
        self.assertEqual(await self.clicks(other), 0)
        self.tick = 10.0
        self.assertEqual(await self.login.maybe_login(other, stopped=lambda: True), "skipped")
        self.assertEqual(await self.clicks(other), 0)


if __name__ == "__main__":
    unittest.main()
