"""保留窗口回调异常和原生崩溃信息；导入本模块不创建文件或窗口。"""
from __future__ import annotations

from datetime import datetime
import faulthandler
import os
from pathlib import Path
import sys
import traceback


class GuiDiagnostics:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.path = folder / f"界面诊断_{datetime.now():%Y%m%d_%H%M%S}_{os.getpid()}.log"
        self.file = self.path.open("a", encoding="utf-8", buffering=1)
        self._old_hook = sys.excepthook
        self._owns_fault_handler = not faulthandler.is_enabled()
        if self._owns_fault_handler:
            faulthandler.enable(file=self.file, all_threads=True)
        self._notified = False
        self._closed = False
        self.file.write("界面诊断已启用。正常退出时此文件可能没有异常内容。\n")
        sys.excepthook = self.report

    def report(self, error_type, error, tb):
        traceback.print_exception(error_type, error, tb, file=self.file)
        self.file.flush()
        if self._notified:
            return
        self._notified = True
        try:
            from tkinter import messagebox
            messagebox.showerror("程序异常", f"程序遇到错误：{error}\n\n排查记录已保存：\n{self.path}")
        except Exception:
            pass

    def attach(self, root):
        root.report_callback_exception = self.report

    def close(self):
        if self._closed:
            return
        self._closed = True
        sys.excepthook = self._old_hook
        if self._owns_fault_handler:
            faulthandler.disable()
        self.file.close()
