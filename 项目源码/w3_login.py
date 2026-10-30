"""W3 本机备用登录；仅在确认的登录源进行有间隔、有上限的重试。"""
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
MAX_LOGIN_ATTEMPTS = 3
LOGIN_RETRY_SECONDS = 3.0
LOGIN_RESULT_WAIT_SECONDS = 10.0
LOGIN_OWNER_SECONDS = 60.0
_LOGIN_TEXT = re.compile(r"^\s*登\s*录\s*$")
_CHALLENGE_TEXT = re.compile(r"验证码|动态口令|短信验证|二次验证|安全验证|一次性密码")
_REJECTED_TEXT = re.compile(
    r"(?:账号|帐号|账户|用户名|密码).{0,12}(?:错误|不正确)|"
    r"(?:账号|帐号|账户).{0,12}(?:锁定|冻结)|密码已过期|password\s+(?:is\s+)?incorrect",
    re.IGNORECASE)


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
    """一次认证的所有标签共享重试额度；真实认证成功后允许下次掉线恢复。"""

    def __init__(self, *, credentials_loader=None, grace_seconds=AUTO_LOGIN_GRACE_SECONDS,
                 clock=time.monotonic):
        self._load = credentials_loader or load_credentials
        self._clock = clock
        self._grace = grace_seconds
        self._lock = asyncio.Lock()
        self._first_seen = {}
        self._notices = set()
        self.attempts = 0
        self.attempted = False
        self.disabled = False
        self._next_attempt_at = 0.0
        self._owner = None
        self._owner_until = 0.0

    def authenticated(self, page) -> None:
        # 其他已登录工作页不能清零正在认证页面的额度，避免并发无限重试。
        if self._lock.locked() or self._owner is None:
            return
        if self._owner is not page and not self._owner.is_closed():
            return
        self.attempts = 0
        self._next_attempt_at = 0.0
        self._owner = None
        self._owner_until = 0.0
        self._first_seen.clear()
        self._notices.clear()
        logging.info('[W3备用登录] 已核验进入会议室页面；后续若掉线，可重新尝试登录。')

    def _waiting_for_peer(self, page) -> bool:
        return (self._owner is not None and self._owner is not page
                and not self._owner.is_closed() and self._clock() < self._owner_until)

    def _notice(self, key, message):
        if key not in self._notices:
            self._notices.add(key)
            logging.warning('[W3备用登录] %s', message)

    async def maybe_login(self, page, *, stopped=lambda: False) -> str:
        if (stopped() or self.disabled
                or not is_w3_login_url(getattr(page, 'url', ''))):
            return 'skipped'
        async with self._lock:
            if stopped() or self.disabled or not is_w3_login_url(getattr(page, 'url', '')):
                return 'skipped'
            if self._clock() < self._next_attempt_at or self._waiting_for_peer(page):
                return 'skipped'
            password = None
            credentials = None
            started = False
            clicking = False
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
                try:
                    same_form = (await username.evaluate('(e, p) => e.form === p.form', password_element)
                                 and await button.evaluate('(e, p) => !e.form || e.form === p.form', password_element))
                finally:
                    if password_element is not None:
                        await password_element.dispose()
                if not same_form:
                    return 'skipped'
                first_seen = self._first_seen.setdefault(id(page), self._clock())
                if self._clock() - first_seen < self._grace:
                    return 'waiting'
                body = await page.locator('body').inner_text(timeout=CONTROL_TIMEOUT_MS)
                if _CHALLENGE_TEXT.search(body):
                    self._notice('challenge', '页面要求验证码或额外验证，请在浏览器手动完成。')
                    return 'manual'
                if _REJECTED_TEXT.search(body):
                    self.disabled = True
                    self._notice('rejected', '页面提示账号密码错误、过期或账号锁定，停止自动提交，请手动处理。')
                    return 'manual'
                if self.attempts >= MAX_LOGIN_ATTEMPTS:
                    self._notice('limit', f'本次认证已自动尝试{MAX_LOGIN_ATTEMPTS}次，仍未恢复；请手动完成登录。')
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
                # 连续失败次数跨刷新/新标签共享，直到实际预订表单认证成功才清零。
                self.attempts += 1
                self.attempted = True
                started = True
                self._owner = page
                self._owner_until = self._clock() + LOGIN_OWNER_SECONDS
                self._next_attempt_at = self._clock() + LOGIN_RETRY_SECONDS
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
                    if is_w3_login_url(page.url):
                        await password.fill('', timeout=CONTROL_TIMEOUT_MS)
                    return 'skipped'
                logging.info('[W3备用登录] 第%d/%d次｜已填写保存的账号密码，等待登录按钮可用。',
                             self.attempts, MAX_LOGIN_ATTEMPTS)
                # click超时不代表请求没发出，也要先等待结果再允许补点。
                clicking = True
                self._next_attempt_at = self._clock() + LOGIN_RESULT_WAIT_SECONDS
                await button.click(timeout=CONTROL_TIMEOUT_MS)
                self._next_attempt_at = self._clock() + LOGIN_RESULT_WAIT_SECONDS
                logging.info('[W3备用登录] 第%d/%d次｜已点击登录，等待认证；%s秒后仍在登录页则重试。',
                             self.attempts, MAX_LOGIN_ATTEMPTS, LOGIN_RESULT_WAIT_SECONDS)
                return 'submitted'
            except asyncio.CancelledError:
                if started and self._owner is page:
                    # 页面准备被抢订截止/让路取消时，不长期占住其他页的认证资格。
                    # 已开始click的等待期和总次数仍保留，不能立即补交未知结果。
                    self._owner_until = self._clock()
                raise
            except CredentialError:
                self.disabled = True
                self._notice('credentials', '本机备用凭据无法读取或解密，请在高级设置中重新保存。')
                return 'manual'
            except Exception:
                # Playwright.fill的异常可能含输入值，禁止记录原异常/调用堆栈。
                if started:
                    delay = LOGIN_RESULT_WAIT_SECONDS if clicking else LOGIN_RETRY_SECONDS
                    self._next_attempt_at = self._clock() + delay
                    if password is not None and is_w3_login_url(getattr(page, 'url', '')):
                        try:
                            await password.fill('', timeout=CONTROL_TIMEOUT_MS)
                        except Exception:
                            pass
                    logging.warning('[W3备用登录] 第%d/%d次｜填表或登录按钮操作未完成；%s秒后重新检查登录状态。',
                                    self.attempts, MAX_LOGIN_ATTEMPTS, delay)
                return 'retrying'
            finally:
                credentials = None


def notify_w3_authenticated(page) -> None:
    """只由已验证预订表单就绪的调用方通知，不把URL跳转或click返回当登录成功。"""
    fallback = getattr(getattr(page, 'context', None), '_booking_w3_fallback', None)
    if fallback is not None:
        fallback.authenticated(page)


async def try_w3_backup_login(page, *, stopped=lambda: False) -> str:
    if not is_w3_login_url(getattr(page, 'url', '')):
        return 'skipped'
    context = page.context
    fallback = getattr(context, '_booking_w3_fallback', None)
    if fallback is None:
        fallback = W3LoginFallback()
        context._booking_w3_fallback = fallback
    return await fallback.maybe_login(page, stopped=stopped)
