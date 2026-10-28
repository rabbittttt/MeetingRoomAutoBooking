"""认证诊断截图遮罩回归；不写截图、不启动浏览器、不读取本机凭据。"""
import asyncio
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock, patch

import booking_core as core


class AuthScreenshotPrivacyTests(unittest.IsolatedAsyncioTestCase):
    async def check_capture(self, url, **kwargs):
        marker = object()
        page = SimpleNamespace(url=url, locator=Mock(return_value=marker), screenshot=AsyncMock())
        with patch.object(core, 'LAST_DEBUG_CAPTURE', {}), patch.object(core, 'LOG_DIR', Path('unused-fixture')):
            await core.save_debug(page, 'fixture', **kwargs)
        return page, marker

    async def test_auth_error_masks_inputs_even_on_a_different_auth_origin(self):
        page, marker = await self.check_capture('https://other-auth.invalid/', mask_inputs=True)
        self.assertEqual(page.screenshot.call_args.kwargs['mask'], [marker])

    async def test_session_expiry_redirect_masks_login_even_from_preparation_diagnostics(self):
        page, marker = await self.check_capture('https://login.huawei.com/login1/')
        self.assertEqual(page.screenshot.call_args.kwargs['mask'], [marker])

    async def test_business_grid_capture_keeps_existing_detail(self):
        page, _ = await self.check_capture('https://booking.invalid/meetingroom/')
        self.assertNotIn('mask', page.screenshot.call_args.kwargs)


if __name__ == '__main__':
    unittest.main()
