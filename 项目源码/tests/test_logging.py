"""Offline log-display regressions; no GUI window, browser, or booking is opened."""
import asyncio
from contextlib import ExitStack, contextmanager
import copy
from datetime import date
import logging
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

import booking_core as core
import 会议室自动抢订_GUI as gui


class _BufferHandler(logging.Handler):
    def __init__(self):
        super().__init__(logging.INFO)
        self.messages = []

    def emit(self, record):
        self.messages.append(self.format(record))


@contextmanager
def _isolated_logging(*, verbose=False):
    """Keep basicConfig(force=True) from closing the caller's handlers."""
    root = logging.getLogger()
    original_handlers = root.handlers[:]
    original_level = root.level
    original_disabled = root.disabled
    original_global_disable = root.manager.disable
    root.handlers = []
    root.disabled = False
    logging.disable(logging.NOTSET)
    try:
        with tempfile.TemporaryDirectory(prefix="booking_log_") as temp_dir:
            displayed = _BufferHandler()
            with patch.object(core, "LOG_DIR", Path(temp_dir)), \
                 patch.object(core, "GUI_LOG_HANDLER", displayed), \
                 patch.object(core, "VERBOSE_LOGS", verbose), \
                 patch.object(core.sys, "stdout", None), \
                 patch.object(core.sys, "stderr", None):
                try:
                    core.setup_logging()
                    yield displayed, Path(temp_dir)
                finally:
                    for handler in root.handlers[:]:
                        root.removeHandler(handler)
                        handler.close()
    finally:
        root.handlers = original_handlers
        root.setLevel(original_level)
        root.disabled = original_disabled
        logging.disable(original_global_disable)


class ConciseLogFilterTests(unittest.TestCase):
    def record(self, text, level=logging.INFO, exc_info=None):
        return logging.LogRecord("booking-test", level, __file__, 1, text, (), exc_info)

    def test_default_hides_detail_but_keeps_progress(self):
        hidden = (
            "[格子诊断] 已保存增强结构：example.json",
            "已保存排查截图：example.png",
            "[候补] 正在查询 2030-01-02 14:00-16:00｜已开放房间=['F5:7F-A05R']",
            "[候补] 本轮耗时21.8秒；下一轮额外等待8.2秒",
            "[表单耗时汇总] [7F-A05R] 未完成｜已耗时18秒",
            "[准备异常详情] raw exception and traceback",
        )
        with patch.object(core, "VERBOSE_LOGS", False):
            current = core.ConciseLogFilter()
            for message in hidden:
                with self.subTest(message=message):
                    self.assertFalse(current.filter(self.record(message)))
            self.assertTrue(current.filter(self.record("[候补] 时段完成 2030-01-02｜检查6间")))
            self.assertTrue(current.filter(self.record("[登录] 单页认证成功")))

    def test_default_keeps_every_warning_error_and_booking_result(self):
        with patch.object(core, "VERBOSE_LOGS", False):
            current = core.ConciseLogFilter()
            for level in (logging.WARNING, logging.ERROR, logging.CRITICAL):
                with self.subTest(level=level):
                    self.assertTrue(current.filter(self.record("unexpected diagnostic failure", level)))
            self.assertTrue(current.filter(self.record("[预约结果][空闲候补] 预订成功")))

    def test_error_traceback_is_not_filtered(self):
        try:
            raise RuntimeError("receipt marker")
        except RuntimeError:
            import sys
            record = self.record("unhandled receipt error", logging.ERROR, sys.exc_info())
        with patch.object(core, "VERBOSE_LOGS", False):
            self.assertTrue(core.ConciseLogFilter().filter(record))
        self.assertIn("RuntimeError: receipt marker", logging.Formatter().format(record))

    def test_verbose_mode_keeps_all_detail(self):
        with patch.object(core, "VERBOSE_LOGS", True):
            self.assertTrue(core.ConciseLogFilter().filter(self.record("[格子诊断] 已保存增强结构")))
            self.assertTrue(core.ConciseLogFilter().filter(self.record("arbitrary step detail")))


class LoggingOutputTests(unittest.TestCase):
    def test_file_keeps_details_and_tracebacks_while_gui_is_concise(self):
        with _isolated_logging() as (displayed, log_dir):
            logging.info("[格子诊断] 已保存增强结构：diagnostic.json")
            logging.info("已保存排查截图：screenshot.png")
            logging.info("[候补] 正在查询 detail-marker")
            logging.info("[候补] 本轮耗时 waiting-marker")
            logging.info("[候补] 时段完成 progress-marker｜检查6间")
            try:
                raise RuntimeError("original detached marker")
            except RuntimeError:
                logging.info("[准备异常详情] detail-stack-marker", exc_info=True)
            logging.warning("[准备异常] one-clear-warning")
            try:
                raise ValueError("critical receipt marker")
            except ValueError:
                logging.exception("[主流程异常] error-stack-marker")
            core.booking_result("空闲候补", core.Target(date(2030, 1, 2), "14:00", "16:00"),
                                core.RoomSpec("F5", "7F-A05R", 1), "uncertain")
            for handler in logging.getLogger().handlers:
                handler.flush()
            files = list(log_dir.glob("*.log"))
            self.assertEqual(len(files), 1)
            file_text = files[0].read_text(encoding="utf-8")
            gui_text = "\n".join(displayed.messages)
            for marker in ("diagnostic.json", "screenshot.png", "detail-marker", "waiting-marker",
                           "detail-stack-marker", "RuntimeError: original detached marker",
                           "progress-marker", "one-clear-warning", "error-stack-marker",
                           "ValueError: critical receipt marker", "提交结果待确认"):
                with self.subTest(marker=marker):
                    self.assertIn(marker, file_text)
            for marker in ("diagnostic.json", "screenshot.png", "detail-marker", "waiting-marker",
                           "detail-stack-marker", "original detached marker"):
                self.assertNotIn(marker, gui_text)
            for marker in ("progress-marker", "one-clear-warning", "error-stack-marker",
                           "ValueError: critical receipt marker", "提交结果待确认"):
                self.assertIn(marker, gui_text)
            progress = next(text for text in displayed.messages if "progress-marker" in text)
            self.assertRegex(progress, r"^\d{2}:\d{2}:\d{2} \| \[候补\]")
            self.assertNotIn("INFO", progress)
            self.assertRegex(file_text, r"\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \| INFO")
            self.assertNotIn("\x1b[", file_text)

    def test_verbose_gui_displays_details_and_full_format(self):
        with _isolated_logging(verbose=True) as (displayed, _):
            logging.info("[格子诊断] 已保存增强结构：verbose-marker.json")
            text = next(message for message in displayed.messages if "verbose-marker" in message)
            self.assertRegex(text, r"^\d{4}-\d{2}-\d{2} \d{2}:\d{2}:\d{2},\d{3} \| INFO")


class StartupSummaryTests(unittest.TestCase):
    def config(self, mode):
        config = copy.deepcopy(gui.DEFAULT_CONFIG)
        config.update(mode=mode, rooms={"F5": ["7F-A05R", "7F-A28R"], "F5-1A": ["1F-A10R"]},
                      release_targets=[{}, {}], weekly_release_rules=[{}, {}, {}], watch_targets=[{}])
        return config

    def test_watch_only_shows_watch_count(self):
        text = gui.startup_summary(self.config("watch"))
        self.assertIn("仅空闲候补", text)
        self.assertIn("候选会议室3间", text)
        self.assertIn("候补1条", text)
        self.assertNotIn("单次", text)
        self.assertNotIn("每周", text)

    def test_release_only_shows_release_counts(self):
        text = gui.startup_summary(self.config("release"))
        self.assertIn("单次2条 / 每周3条", text)
        self.assertNotIn("候补", text)

    def test_both_shows_all_counts(self):
        self.assertIn("单次2条 / 每周3条 / 候补1条", gui.startup_summary(self.config("both")))

    def test_discovery_shows_no_task_counts(self):
        text = gui.startup_summary(self.config("list-rooms"))
        self.assertIn("查看会议室列表", text)
        self.assertNotRegex(text, r"(?:单次|每周|候补)\d+条")


class PrepareErrorMessageTests(unittest.TestCase):
    def test_detached_variants_are_readable(self):
        for raw in (": Element is not attached to the DOM", "Element was detached from the DOM"):
            with self.subTest(raw=raw):
                self.assertIn("页面元素已失效", core.prepare_error_message(RuntimeError(raw)))

    def test_empty_exception_is_safe(self):
        self.assertEqual(core.prepare_error_message(RuntimeError()), "RuntimeError")

    def test_timeout_is_readable_and_unknown_error_keeps_first_line(self):
        self.assertIn("超时", core.prepare_error_message(core.PlaywrightTimeout("Timeout 5000ms")))
        self.assertEqual(core.prepare_error_message(ValueError("room mismatch\nstack detail")), "room mismatch")


class WatchLogTests(unittest.IsolatedAsyncioTestCase):
    async def test_prepare_failure_has_one_main_warning_and_summary_count(self):
        target = core.Target(date(2030, 1, 2), "14:00", "16:00", ("F5",))
        room = core.RoomSpec("F5", "7F-A05R", 1)
        job = core.BookingJob(target, asyncio.Event())
        async def failed_preparation(*args, **kwargs):
            logging.warning("[准备异常] F5:7F-A05R｜步骤=选格子｜原因=页面元素已失效")
            return False
        with ExitStack() as stack:
            for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
                stack.enter_context(patch.object(core, name, asyncio.Event()))
            stack.enter_context(patch.object(core, "may_submit", return_value=True))
            stack.enter_context(patch.object(core, "prepare_page", side_effect=failed_preparation))
            submit = stack.enter_context(patch.object(core, "confirm_and_book", AsyncMock()))
            with self.assertLogs(level=logging.INFO) as captured:
                await core.watch_cycle([object()], job, [room])
        messages = [record.getMessage() for record in captured.records]
        warnings = [record for record in captured.records if record.levelno >= logging.WARNING]
        self.assertEqual(len(warnings), 1)
        self.assertTrue(warnings[0].getMessage().startswith("[准备异常]"))
        self.assertFalse(any(message.startswith("[预约结果]") for message in messages))
        summary = next(message for message in messages if message.startswith("[候补] 时段完成"))
        self.assertIn("检查1间", summary)
        self.assertIn("准备异常1间", summary)
        self.assertIn("提交/模拟0间", summary)
        submit.assert_not_awaited()
        self.assertFalse(job.done.is_set())
        self.assertFalse(job.uncertain.is_set())


if __name__ == "__main__":
    unittest.main()
