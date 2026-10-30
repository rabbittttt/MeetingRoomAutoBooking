"""W3 自动登录的本机备用流程；仅在用户确认的登录源提交一次。"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from urllib.parse import urlsplit

from w3_credentials import CredentialError, load_credentials, normalize_login_origin


W3_LOGIN_ORIGIN = "https://login.huawei.com"
AUTO_LOGIN_GRACE_SECONDS = 3.0
CONTROL_TIMEOUT_MS = 2500
_LOGIN_TEXT = re.compile(r"^\s*登\s*录\s*$")
_CHALLENGE_TEXT = re.compile(r"验证码|动态口令|短信验证|二次验证|安全验证|一次性密码")


def is_w3_login_url(url: str) -> bool:
    """不接受同名子域、用户信息、HTTP或其他端口；从不记录查询参数。"""
    try:
        parsed = urlsplit(url)
        return (parsed.scheme.lower() == 'https'
                and parsed.hostname == 'login.huawei.com'
                and parsed.port in (None, 443)
                and parsed.username is None and parsed.password is None)
    except (TypeError, ValueError):
        return False


async def _unique_visible(locator):
    count = await locator.count()
    if count > 8:
        return None
    matches = [locator.nth(i) for i in range(count)
               if await locator.nth(i).is_visible()]
    return matches[0] if len(matches) == 1 else None


async def _trusted_actions(*controls):
    for control in controls:
        info = await control.evaluate('''(e) => {
            const submit = (e.tagName === 'BUTTON' && e.type === 'submit') ||
                (e.tagName === 'INPUT' && ['submit','image'].includes(e.type));
            return {action: e.hasAttribute('formaction') ? e.formAction : (e.form ? e.form.action : null),
                method: submit && e.form ? (e.hasAttribute('formmethod') ? e.formMethod : e.form.method) : null};
        }''')
        action = info['action']
        if action and not is_w3_login_url(action):
            return False
        # 原生GET表单会把密码放入URL；不依赖页面JS必定阻止这个默认提交。
        if info['method'] and info['method'].lower() != 'post':
            return False
    return True


class W3LoginFallback:
    """同一浏览器运行共享一次尝试额度，避免多个工作标签重复提交密码。"""

    def __init__(self, *, credentials_loader=None, grace_seconds=AUTO_LOGIN_GRACE_SECONDS,
                 clock=time.monotonic):
        self._load = credentials_loader or load_credentials
        self._clock = clock
        self._grace = grace_seconds
        self._lock = asyncio.Lock()
        self._first_seen = {}
        self._notices = set()
        self.attempted = False
        self.disabled = False

    def _notice(self, key, message):
        if key not in self._notices:
            self._notices.add(key)
            logging.warning('[W3备用登录] %s', message)

    async def maybe_login(self, page, *, stopped=lambda: False) -> str:
        if (stopped() or self.disabled or self.attempted
                or not is_w3_login_url(getattr(page, 'url', ''))):
            return 'skipped'
        async with self._lock:
            if stopped() or self.disabled or self.attempted:
                return 'skipped'
            password = None
            credentials = None
            try:
                # 选择器来自用户提供的账号登录页；结构不符时留给人工处理。
                username = await _unique_visible(page.get_by_placeholder('W3账号', exact=True))
                password = await _unique_visible(page.get_by_placeholder('密码', exact=True))
                if username is None or password is None:
                    return 'skipped'
                if ((await password.get_attribute('type') or '').lower() != 'password'
                        or not await username.is_editable() or not await password.is_editable()):
                    return 'skipped'
                button = await _unique_visible(page.get_by_role('button', name=_LOGIN_TEXT))
                if button is None:
                    button = await _unique_visible(page.get_by_text(_LOGIN_TEXT, exact=True))
                # 登录按钮可能在填完整账号密码后才启用，不能在填写前要求可点击。
                if button is None:
                    return 'skipped'
                password_element = await password.element_handle()
                if (not await username.evaluate('(e, p) => e.form === p.form', password_element)
                        or not await button.evaluate('(e, p) => !e.form || e.form === p.form', password_element)):
                    return 'skipped'
                first_seen = self._first_seen.setdefault(id(page), self._clock())
                if self._clock() - first_seen < self._grace:
                    return 'waiting'
                if _CHALLENGE_TEXT.search(await page.locator('body').inner_text(timeout=CONTROL_TIMEOUT_MS)):
                    self._notice('challenge', '页面要求验证码或额外验证，请在浏览器手动完成。')
                    return 'manual'
                credentials = self._load()
                if credentials is None:
                    self.disabled = True
                    self._notice('missing', '尚未设置本机备用凭据；可在高级设置中保存，当前请手动登录。')
                    return 'manual'
                if normalize_login_origin(credentials.login_origin) != W3_LOGIN_ORIGIN:
                    self.disabled = True
                    self._notice('origin', '保存的登录域名不匹配，请在高级设置中重新保存。')
                    return 'manual'
                # 原生表单若指向另一个源，不能把凭据发送过去。
                if not await _trusted_actions(username, password, button):
                    self.disabled = True
                    self._notice('action', '登录表单提交地址无法核验，请手动登录。')
                    return 'manual'
                if stopped() or not is_w3_login_url(page.url):
                    return 'skipped'
                # 即使控件重绘/提交失败也不再自动尝试，避免错误密码反复触发锁定。
                self.attempted = True
                # 使用已保存的凭据覆盖预填内容；先清旧密码，fill会替换原账号。
                await password.fill('', timeout=CONTROL_TIMEOUT_MS)
                if stopped() or not is_w3_login_url(page.url):
                    return 'skipped'
                await username.fill(credentials.username, timeout=CONTROL_TIMEOUT_MS)
                if stopped() or not is_w3_login_url(page.url):
                    return 'skipped'
                await password.fill(credentials.password, timeout=CONTROL_TIMEOUT_MS)
                if (stopped() or not is_w3_login_url(page.url)
                        or not await _trusted_actions(username, password, button)):
                    await password.fill('', timeout=CONTROL_TIMEOUT_MS)
                    return 'skipped'
                logging.info('[W3备用登录] 已填写保存的账号密码，等待登录按钮可用。')
                await button.click(timeout=CONTROL_TIMEOUT_MS)
                logging.info('[W3备用登录] 已填写并提交一次登录，等待认证；本次运行不重复提交密码。')
                return 'submitted'
            except CredentialError:
                self.disabled = True
                self._notice('credentials', '本机备用凭据无法读取或解密，请在高级设置中重新保存。')
                return 'manual'
            except Exception:
                # Playwright.fill的异常可能含输入值，禁止记录原异常/调用堆栈。
                if self.attempted:
                    self.disabled = True
                    if password is not None and is_w3_login_url(getattr(page, 'url', '')):
                        try:
                            await password.fill('', timeout=CONTROL_TIMEOUT_MS)
                        except Exception:
                            pass
                    self._notice('controls', '备用登录未完成，请手动登录；本次运行不再自动提交。')
                return 'manual'
            finally:
                credentials = None


async def try_w3_backup_login(page, *, stopped=lambda: False) -> str:
    if not is_w3_login_url(getattr(page, 'url', '')):
        return 'skipped'
    context = page.context
    fallback = getattr(context, '_booking_w3_fallback', None)
    if fallback is None:
        fallback = W3LoginFallback()
        context._booking_w3_fallback = fallback
    return await fallback.maybe_login(page, stopped=stopped)
