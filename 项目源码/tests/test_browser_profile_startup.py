"""Browser profile contention checks using mocks and a fresh temporary profile.

The real Chrome test never opens the booking site or a user profile. All pages
stay at about:blank and all context requests are aborted. Some Windows Chrome
builds expose only exitCode=21 without ProcessSingleton stderr: that generic
failure must remain unclassified. The user's explicit Error 32 diagnostic is
covered by mocks; the real test verifies owner/data preservation and reopening.
Only its own TemporaryDirectory is cleaned up after both test contexts close.
"""
import asyncio
from contextlib import ExitStack
from pathlib import Path
from tempfile import TemporaryDirectory, gettempdir
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, patch

from playwright.async_api import Error as PlaywrightError, async_playwright

import booking_core as core


BUSY = 'Failed to create a ProcessSingleton for your profile directory. Error code: 32'


class BrowserProfileStartupTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        for name in ('STOP_REQUESTED', 'DRIVER_LOST', 'AUTH_BLOCKED', 'CONFIG_BLOCKED'):
            self.stack.enter_context(patch.object(core, name, asyncio.Event()))
        self.profile = Path(gettempdir()) / 'mock-only-meeting-profile-no-files-created'
        for name, value in (('PROFILE_DIR', self.profile), ('BROWSER_CHANNEL', 'chrome'),
                            ('HEADLESS', True), ('BROWSER_LAUNCH_ATTEMPTS', 3),
                            ('BROWSER_LAUNCH_RETRY_SECONDS', 0.001)):
            self.stack.enter_context(patch.object(core, name, value))
        self.launch = AsyncMock()
        self.playwright = SimpleNamespace(chromium=SimpleNamespace(
            launch_persistent_context=self.launch))

    def assert_launch_arguments_unchanged(self):
        for call in self.launch.await_args_list:
            self.assertEqual(call.args, ())
            self.assertEqual(call.kwargs, {
                'user_data_dir': str(self.profile), 'channel': 'chrome',
                'headless': True, 'accept_downloads': False,
                'viewport': {'width': 1536, 'height': 900},
            })
        self.assertEqual(core.PROFILE_DIR, self.profile)
        self.assertEqual(core.BROWSER_CHANNEL, 'chrome')

    async def test_temporary_contention_recovers_with_same_profile_and_channel(self):
        context = object()
        self.launch.side_effect = [RuntimeError(BUSY), context]
        self.assertIs(await core.launch_booking_browser(self.playwright), context)
        self.assertEqual(self.launch.await_count, 2)
        self.assert_launch_arguments_unchanged()

    async def test_three_busy_attempts_raise_actionable_error_without_raw_browser_trace(self):
        self.launch.side_effect = RuntimeError(BUSY)
        with self.assertRaisesRegex(RuntimeError, '登录资料目录仍被占用') as caught:
            await core.launch_booking_browser(self.playwright)
        self.assertEqual(self.launch.await_count, 3)
        message = str(caught.exception)
        self.assertIn(str(self.profile), message)
        self.assertIn('停止其他会议室程序', message)
        self.assertIn('不要删除登录资料目录', message)
        self.assertNotIn('ProcessSingleton', message)
        self.assertTrue(caught.exception.__suppress_context__)
        self.assert_launch_arguments_unchanged()

    async def test_unrelated_errors_are_not_retried_or_replaced(self):
        messages = (
            'Failed to create a ProcessSingleton for your profile directory. Error code: 5',
            'Failed to create a ProcessSingleton for your profile directory. Error code: 5; already in use',
            'Chromium distribution chrome is not found',
            'Executable does not exist at test-only-path',
            'Access is denied. Error code: 5',
            'Target page, context or browser has been closed; exitCode=21',
            'Page crashed unexpectedly',
        )
        for message in messages:
            with self.subTest(error=message):
                self.launch.reset_mock(side_effect=True)
                original = RuntimeError(message)
                self.launch.side_effect = original
                with self.assertRaises(RuntimeError) as caught:
                    await core.launch_booking_browser(self.playwright)
                self.assertIs(caught.exception, original)
                self.assertEqual(self.launch.await_count, 1)
                self.assert_launch_arguments_unchanged()

    async def test_profile_message_variants_are_recognized_without_generic_false_positives(self):
        for message in (BUSY, 'ProcessSingleton: profile already in use',
                        'The profile appears to be in use by another Chrome process'):
            self.assertTrue(core.is_browser_profile_busy(RuntimeError(message)), message)
        for message in ('Error code: 32', 'already in use', 'ProcessSingleton failed',
                        'profile access denied'):
            self.assertFalse(core.is_browser_profile_busy(RuntimeError(message)), message)

    async def test_stop_before_start_does_not_launch_browser(self):
        core.STOP_REQUESTED.set()
        with self.assertRaises(core.StopRequested):
            await core.launch_booking_browser(self.playwright)
        self.launch.assert_not_awaited()

    async def test_user_stop_interrupts_retry_delay_without_another_launch(self):
        self.launch.side_effect = RuntimeError(BUSY)
        entered = asyncio.Event()
        original_sleep = core.sleep_or_stop
        async def wait(seconds):
            entered.set()
            await original_sleep(seconds)
        with patch.object(core, 'BROWSER_LAUNCH_RETRY_SECONDS', 60), \
                patch.object(core, 'sleep_or_stop', wait):
            task = asyncio.create_task(core.launch_booking_browser(self.playwright))
            try:
                await asyncio.wait_for(entered.wait(), timeout=1)
                core.STOP_REQUESTED.set()
                with self.assertRaises(core.StopRequested):
                    await asyncio.wait_for(task, timeout=0.5)
            finally:
                if not task.done():
                    task.cancel()
                await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.launch.await_count, 1)

    async def test_external_cancellation_is_not_swallowed_as_retry(self):
        entered = asyncio.Event()
        async def pending_launch(**kwargs):
            entered.set()
            await asyncio.Event().wait()
        self.launch.side_effect = pending_launch
        task = asyncio.create_task(core.launch_booking_browser(self.playwright))
        try:
            await asyncio.wait_for(entered.wait(), timeout=1)
            task.cancel()
            with self.assertRaises(asyncio.CancelledError):
                await task
        finally:
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        self.assertEqual(self.launch.await_count, 1)


class RealChromeProfileStartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_busy_temporary_profile_preserves_owner_and_reopens_after_owner_closes(self):
        with ExitStack() as stack, TemporaryDirectory(prefix='meeting-profile-startup-') as temporary:
            profile = Path(temporary).resolve()
            # Cleanup is limited to this newly allocated direct child of the OS temp directory.
            self.assertEqual(profile.parent, Path(gettempdir()).resolve())
            sentinel = profile / 'test-only-profile-marker.txt'
            sentinel.write_text('temporary test profile; no real account data', encoding='utf-8')
            for name in ('STOP_REQUESTED', 'DRIVER_LOST', 'AUTH_BLOCKED', 'CONFIG_BLOCKED'):
                stack.enter_context(patch.object(core, name, asyncio.Event()))
            for name, value in (('PROFILE_DIR', profile), ('BROWSER_CHANNEL', 'chrome'),
                                ('HEADLESS', True), ('BROWSER_LAUNCH_ATTEMPTS', 3),
                                ('BROWSER_LAUNCH_RETRY_SECONDS', 0.01)):
                stack.enter_context(patch.object(core, name, value))
            requests = []
            async def block_network(route):
                requests.append(route.request.url)
                await route.abort()
            async with async_playwright() as playwright:
                owner = await playwright.chromium.launch_persistent_context(
                    user_data_dir=str(profile), channel='chrome', headless=True)
                recovered = None
                try:
                    await owner.route('**/*', block_network)
                    page = owner.pages[0]
                    self.assertEqual(page.url, 'about:blank')
                    await page.evaluate('window.testOwnerStillAlive = true')
                    with self.assertRaises((RuntimeError, PlaywrightError)) as caught:
                        await core.launch_booking_browser(playwright)
                    message = str(caught.exception)
                    self.assertIn(str(profile), message)
                    if isinstance(caught.exception, PlaywrightError):
                        # No explicit busy diagnosis means the helper must not
                        # infer profile corruption or kill the existing owner.
                        self.assertFalse(core.is_browser_profile_busy(caught.exception))
                        self.assertIn('exitCode=21', message)
                    else:
                        self.assertIn('登录资料目录仍被占用', message)
                    self.assertTrue(await page.evaluate('window.testOwnerStillAlive'))
                    self.assertFalse(page.is_closed())
                    self.assertEqual(sentinel.read_text(encoding='utf-8'),
                                     'temporary test profile; no real account data')
                    await owner.close()
                    recovered = await core.launch_booking_browser(playwright)
                    await recovered.route('**/*', block_network)
                    self.assertTrue(recovered.pages)
                    self.assertTrue(all(page.url == 'about:blank' for page in recovered.pages))
                    self.assertTrue(sentinel.exists())
                    self.assertEqual(core.PROFILE_DIR, profile)
                    self.assertEqual(core.BROWSER_CHANNEL, 'chrome')
                    self.assertEqual(requests, [])
                finally:
                    if recovered is not None:
                        await recovered.close()
                    await owner.close()


if __name__ == '__main__':
    unittest.main()
