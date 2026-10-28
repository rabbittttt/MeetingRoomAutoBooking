"""本地Chrome登录夹具；所有请求均被拦截，不连接W3，不使用真实凭据。"""
import asyncio
from contextlib import ExitStack
import logging
from pathlib import Path
from types import SimpleNamespace
import sys
import unittest
from unittest.mock import AsyncMock, Mock, patch

from playwright.async_api import async_playwright

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import w3_login
from w3_credentials import CredentialError


LOGIN_URL = 'https://login.huawei.com/login1/?redirect=fixture'
READY_URL = 'https://booking.invalid/ready'
# 仅测试用虚构值，不能登录任何真实服务。
DUMMY = SimpleNamespace(username='fixture_user', password='fictional-password-仅测试',
                        login_origin=w3_login.W3_LOGIN_ORIGIN)


def login_html(*, action='', extra='', duplicate=False, mode='remain', text_button=False, override=''):
    button = '<div onclick="submitLogin(event)">登录</div>' if text_button else f'<button type="submit" {override}>登录</button>'
    return f'''<!doctype html><meta charset="utf-8"><title>本地登录夹具</title>
    <form action="{action}" method="post" onsubmit="submitLogin(event)">
    <input placeholder="W3账号"><input placeholder="密码" type="password">
    {button}{extra}</form>
    {'<input placeholder="W3账号">' if duplicate else ''}
    <script>window.loginClicks=0;
    function submitLogin(e){{e.preventDefault();window.loginClicks++;
    if({mode!r}==='success')location.href={READY_URL!r};}}
    </script>'''


class W3LoginBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(channel='chrome', headless=True)
        self.context = await self.browser.new_context()
        self.html = login_html()
        self.requests = []

        async def route(request_route):
            self.requests.append(request_route.request.url)
            if request_route.request.url == READY_URL:
                await request_route.fulfill(body='<meta charset="utf-8"><button>图标 添加会议室</button>', content_type='text/html')
            else:
                await request_route.fulfill(body=self.html, content_type='text/html')

        await self.context.route('**/*', route)
        self.page = await self.context.new_page()
        self.loader = Mock(return_value=DUMMY)
        self.login = w3_login.W3LoginFallback(credentials_loader=self.loader, grace_seconds=0)

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def open_login(self, **kwargs):
        self.html = login_html(**kwargs)
        await self.page.goto(LOGIN_URL)

    async def assert_empty(self, page=None):
        page = page or self.page
        self.assertEqual(await page.get_by_placeholder('W3账号', exact=True).first.input_value(), '')
        self.assertEqual(await page.get_by_placeholder('密码', exact=True).input_value(), '')
        self.assertEqual(await page.evaluate('window.loginClicks'), 0)

    async def test_saved_credentials_fill_and_submit_once_then_real_form_is_ready(self):
        await self.open_login(mode='success')
        self.assertEqual(await self.login.maybe_login(self.page), 'submitted')
        await self.page.wait_for_url(READY_URL)
        self.assertTrue(await self.page.get_by_role('button', name='图标 添加会议室').is_visible())
        self.loader.assert_called_once_with()
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')

    async def test_already_logged_in_does_not_read_credentials(self):
        await self.page.goto(READY_URL)
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        self.loader.assert_not_called()

    async def test_failed_login_does_not_loop_or_repeat_after_reload(self):
        await self.open_login()
        await self.login.maybe_login(self.page)
        for _ in range(3):
            self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        self.assertEqual(await self.page.evaluate('window.loginClicks'), 1)
        await self.page.reload()
        await self.login.maybe_login(self.page)
        await self.assert_empty()

    async def test_untrusted_similar_domain_never_reads_or_fills_credentials(self):
        await self.page.goto('https://login.huawei.com.evil.invalid/login1/')
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        self.loader.assert_not_called()
        await self.assert_empty()

    async def test_cross_origin_form_action_does_not_fill(self):
        await self.open_login(action='https://other.invalid/post')
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        await self.assert_empty()

    async def test_cross_origin_button_action_override_does_not_fill(self):
        await self.open_login(override='formaction="https://other.invalid/post"')
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        await self.assert_empty()

    async def test_native_get_form_is_rejected_without_putting_password_in_url(self):
        await self.open_login()
        await self.page.locator('form').evaluate("e => e.method='get'")
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        await self.assert_empty()
        self.assertNotIn(DUMMY.password, self.page.url)

    async def test_button_get_method_override_is_rejected(self):
        await self.open_login(override='formmethod="get"')
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        await self.assert_empty()

    async def test_action_changed_by_fill_is_rechecked_before_click(self):
        await self.open_login(override='id="login-button"')
        await self.page.get_by_placeholder('密码', exact=True).evaluate('''e => e.addEventListener('input', () => {
          if (e.value) document.getElementById('login-button').formAction='https://other.invalid/post';
        })''')
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        self.assertEqual(await self.page.evaluate('window.loginClicks'), 0)
        self.assertEqual(await self.page.get_by_placeholder('密码', exact=True).input_value(), '')

    async def test_other_saved_origin_is_rejected(self):
        self.loader.return_value = SimpleNamespace(**{**vars(DUMMY), 'login_origin': 'https://other.invalid'})
        await self.open_login()
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        await self.assert_empty()

    async def test_grace_waits_for_existing_automatic_login(self):
        tick = [0.0]
        self.login = w3_login.W3LoginFallback(credentials_loader=self.loader, clock=lambda: tick[0], grace_seconds=3)
        await self.open_login()
        self.assertEqual(await self.login.maybe_login(self.page), 'waiting')
        self.loader.assert_not_called()
        tick[0] = 2.9
        self.assertEqual(await self.login.maybe_login(self.page), 'waiting')
        tick[0] = 3
        self.assertEqual(await self.login.maybe_login(self.page), 'submitted')

    async def test_duplicate_account_fields_are_not_guessed(self):
        await self.open_login(duplicate=True)
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        self.loader.assert_not_called()
        await self.assert_empty()

    async def test_existing_manual_input_is_preserved(self):
        await self.open_login()
        await self.page.get_by_placeholder('W3账号', exact=True).fill('manual_fixture')
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        self.loader.assert_not_called()
        self.assertEqual(await self.page.get_by_placeholder('W3账号', exact=True).input_value(), 'manual_fixture')
        self.assertEqual(await self.page.evaluate('window.loginClicks'), 0)

    async def test_visible_challenge_requires_manual_login(self):
        await self.open_login(extra='<label>验证码<input name="captcha"></label>')
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        self.loader.assert_not_called()
        await self.assert_empty()

    async def test_missing_credentials_keeps_manual_login_available(self):
        self.loader.return_value = None
        await self.open_login()
        self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        await self.assert_empty()

    async def test_corrupt_credentials_are_not_exposed_in_logs(self):
        self.loader.side_effect = CredentialError(DUMMY.password)
        await self.open_login()
        with self.assertLogs(level='WARNING') as logs:
            self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        self.assertNotIn(DUMMY.password, '\n'.join(logs.output))
        await self.assert_empty()

    async def test_fill_exception_cannot_leak_password_or_retry(self):
        await self.open_login()
        # 用locator类注入异常，模拟Playwright错误中含fill输入值的情况。
        from playwright.async_api import Locator
        original = Locator.fill

        async def fill(locator, value, **kwargs):
            if value == DUMMY.password:
                raise RuntimeError('fill(' + DUMMY.password + ') failed')
            return await original(locator, value, **kwargs)

        with patch.object(Locator, 'fill', fill), self.assertLogs(level='WARNING') as logs:
            self.assertEqual(await self.login.maybe_login(self.page), 'manual')
        self.assertNotIn(DUMMY.password, '\n'.join(logs.output))
        self.assertEqual(await self.login.maybe_login(self.page), 'skipped')
        self.assertEqual(await self.page.evaluate('window.loginClicks'), 0)
        self.assertEqual(await self.page.get_by_placeholder('密码', exact=True).input_value(), '')

    async def test_context_shared_quota_prevents_parallel_tab_password_submissions(self):
        await self.open_login()
        other = await self.context.new_page()
        await other.goto(LOGIN_URL)
        self.context._booking_w3_fallback = self.login
        results = await asyncio.gather(w3_login.try_w3_backup_login(self.page), w3_login.try_w3_backup_login(other))
        self.assertEqual(results.count('submitted'), 1)
        self.assertEqual(sum([await p.evaluate('window.loginClicks') for p in (self.page, other)]), 1)

    async def test_stop_does_not_read_fill_or_submit(self):
        await self.open_login()
        self.assertEqual(await self.login.maybe_login(self.page, stopped=lambda: True), 'skipped')
        self.loader.assert_not_called()
        await self.assert_empty()

    async def test_non_button_login_control_uses_unique_visible_exact_text(self):
        await self.open_login(text_button=True)
        self.assertEqual(await self.login.maybe_login(self.page), 'submitted')
        self.assertEqual(await self.page.evaluate('window.loginClicks'), 1)

    async def test_first_page_auth_wait_uses_fallback_and_requires_real_booking_form(self):
        import booking_core as core
        self.html = login_html(mode='success')
        self.context._booking_w3_fallback = self.login
        with patch.object(core, 'BOOKING_URL', LOGIN_URL), \
                patch.object(core, 'activate_booking_page', AsyncMock()), \
                patch.object(core, 'AUTH_WAIT_SECONDS', 4), \
                patch.object(core, 'STOP_REQUESTED', asyncio.Event()):
            self.assertTrue(await core.auth_preflight(self.page))
        self.assertTrue(self.login.attempted)

    async def test_new_page_authentication_uses_the_same_fallback(self):
        import booking_core as core
        self.html = login_html(mode='success')
        self.context._booking_w3_fallback = self.login
        with patch.object(core, 'BOOKING_URL', LOGIN_URL), \
                patch.object(core, 'activate_booking_page', AsyncMock()), \
                patch.object(core, 'AUTH_WAIT_SECONDS', 4), \
                patch.object(core, 'STOP_REQUESTED', asyncio.Event()):
            await core.PagePool(self.context, self.page)._authenticate_new_page(self.page, 2)
        self.assertTrue(self.login.attempted)


class OriginTests(unittest.TestCase):
    def test_origin_boundary_is_exact_https(self):
        self.assertTrue(w3_login.is_w3_login_url(LOGIN_URL))
        self.assertTrue(w3_login.is_w3_login_url('https://login.huawei.com:443/login1/'))
        for url in ('http://login.huawei.com/', 'https://login.huawei.com.evil.invalid/',
                    'https://evil.invalid/login.huawei.com/', 'https://user:pass@login.huawei.com/',
                    'https://login.huawei.com:444/', 'about:blank', None):
            with self.subTest(url=url):
                self.assertFalse(w3_login.is_w3_login_url(url))


if __name__ == '__main__':
    unittest.main()
