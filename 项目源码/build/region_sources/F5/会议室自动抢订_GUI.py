# -*- coding: utf-8 -*-
"""会议室自动抢订 - 由Tk统一管理的粉色无边框窗口、Windows账号授权。

同一页管理单次/每周到点抢订；核心逻辑来自独立 booking_core.py。
桌面配置保存于 %LOCALAPPDATA%/MeetingRoomAutoBooking/config.json。
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import queue
import sys
import threading
import tkinter as tk
from datetime import date, datetime, timedelta
from pathlib import Path
from tkinter import messagebox, ttk
from tkinter import font as tkfont

import booking_core as core
from window_chrome import BorderlessWindow as WindowsWindowFrame

try:
    from build_info import CORE_SHA256
except (ImportError, AttributeError):
    CORE_SHA256 = "source"

CORE_COMPAT_REQUIRED = ("apply_gui_settings", "parse_weekly_rules", "expand_weekly_targets",
                        "build_release_batches", "merge_release_targets", "parse_targets",
                        "AREA_ROOM_ROWS", "default_room_selection", "resolve_rooms",
                        "DEPLOYMENT_ID", "DEPLOYMENT_LABEL", "DEFAULT_RELEASE_OPEN_AT",
                        "log_booking_popup_raw", "booking_failure_dialog", "confirm_and_book",
                        "watch_confirm_disabled")
for _attr in CORE_COMPAT_REQUIRED:
    if not hasattr(core, _attr):
        raise RuntimeError(f"当前预约核心缺少 {_attr}，请检查工程内会议室预定.py是否包含所需功能")

CONFIG_FILE = core.APP_DATA_DIR / "config.json"
MODE_OPTIONS = {"到点抢订 + 空闲候补": "both", "仅到点抢订": "release",
                "仅空闲候补": "watch", "查看会议室列表": "list-rooms"}
MODE_LABELS = {v: k for k, v in MODE_OPTIONS.items()}
AREAS = tuple(core.AREA_ROOM_ROWS)
WEEKDAYS = ("星期一", "星期二", "星期三", "星期四", "星期五", "星期六", "星期日")
TIME_SLOTS = tuple(f"{h:02d}:{m:02d}" for h in range(9, 24) for m in (0, 30))

# 嫩粉少女风：背景更轻、更偏粉；主色降低灰度，文字保持深色以保证清晰度。
BG = "#FFF7FA"
WHITE = "#FFFFFF"
TEXT = "#3B3338"
MUTED = "#8A7A82"
LINE = "#F1E0E7"
ACCENT = "#F4A1BF"
ACCENT_TEXT = "#B84F78"
DARK_RED = "#E67D9F"
YELLOW = "#8A6A3A"
FONT = "Microsoft YaHei UI" if sys.platform == "win32" else "Noto Sans CJK SC"

DEFAULT_CONFIG = {
    "mode": "both", "dry_run": True,
    "rooms": core.default_room_selection(),
    "release_targets": [],
    "weekly_release_rules": [],
    "watch_targets": [],
    "release_open_at": core.DEFAULT_RELEASE_OPEN_AT,
    "deployment_id": core.DEPLOYMENT_ID,
    "prepare_before_seconds": 300,
    "stop_after_seconds": 300,
    "retry_seconds": 0.9,
    "popup_retry_seconds": 0.2,
    "watch_interval_seconds": 30,
    "watch_duration_minutes": 120,
    "watch_guard_seconds": 120,
    "stop_at_meeting_start": True,
    "auth_stagger_seconds": 1.5,
    "verbose_logs": False,
    "step_timing_logs": False,
}


def startup_summary(cfg: dict) -> str:
    """仅统计当前模式实际参与的任务，避免停用任务造成误解。"""
    mode = cfg["mode"]
    tasks = []
    if mode in ("release", "both"):
        tasks.extend((f"单次{len(cfg['release_targets'])}条",
                      f"每周{len(cfg['weekly_release_rules'])}条"))
    if mode in ("watch", "both"):
        tasks.append(f"候补{len(cfg['watch_targets'])}条")
    rooms = sum(len(selected) for selected in cfg["rooms"].values())
    return f"启动：{MODE_LABELS[mode]} · 候选会议室{rooms}间" + (
        " · " + " / ".join(tasks) if tasks else "")


def draw_rounded_rectangle(canvas, x1, y1, x2, y2, radius, *, fill, outline="", width=1, tag="shape"):
    """纯 Tk 绘制圆角矩形，不引入图片/Pillow等额外打包依赖。"""
    r = max(2, min(radius, (x2 - x1) / 2, (y2 - y1) / 2))
    points = [x1+r, y1, x2-r, y1, x2, y1, x2, y1+r,
              x2, y2-r, x2, y2, x2-r, y2, x1+r, y2,
              x1, y2, x1, y2-r, x1, y1+r, x1, y1]
    return canvas.create_polygon(points, smooth=True, splinesteps=18,
                                 fill=fill, outline=outline, width=width, tags=tag)


def compute_release_preview(cfg: dict, *, now: datetime | None = None):
    """预览未来21天的实际放号日期，而不是未来21天的预约日期。"""
    current = core.now_cn() if now is None else now
    today = current.date()
    lookahead = 21
    horizon_end = today + timedelta(days=lookahead)
    specs = core.resolve_rooms(cfg["rooms"])
    manual = core.parse_targets(cfg["release_targets"], "到点抢订", rooms=specs)
    weekly = core.parse_weekly_rules(cfg["weekly_release_rules"], rooms=specs)

    # 生成的预约目标范围需要额外覆盖当前名单的最大提前天数。
    max_advance = max((core.AREA_ADVANCE_DAYS[area] for area in AREAS
                       if cfg["rooms"].get(area)), default=0)
    generated = core.expand_weekly_targets(weekly, today, lookahead + max_advance) if weekly else []
    batches = core.build_release_batches(core.merge_release_targets(manual + generated), specs)
    filtered = []
    for batch in batches:
        if not today <= batch.release_day <= horizon_end:
            continue
        opened = core.at_cn(batch.release_day, cfg["release_open_at"])
        deadline = opened + timedelta(seconds=cfg["stop_after_seconds"])
        status = "已截止" if current >= deadline else ("抢订中" if current >= opened else "待开放")
        filtered.append((batch, status))
    return today, horizon_end, filtered


class ChoiceChip(tk.Canvas):
    """单选/多选采用相同的圆角卡片＋方框勾选外观，不再混用圆点与叉号。"""
    def __init__(self, parent, *, text, variable, value=None, width=126, bg=WHITE, command=None):
        super().__init__(parent, width=width, height=27, bg=bg, bd=0,
                         relief="flat", highlightthickness=0, cursor="hand2", takefocus=True)
        self.title = text
        self.variable = variable
        self.value = value
        self.surface = bg
        self.command = command
        self.hover = False
        self.variable.trace_add("write", self._changed)
        self.bind("<Button-1>", self._toggle)
        self.bind("<Return>", self._toggle)
        self.bind("<space>", self._toggle)
        self.bind("<Enter>", self._enter)
        self.bind("<Leave>", self._leave)
        self._render()

    def _selected(self):
        return self.variable.get() == self.value if self.value is not None else bool(self.variable.get())

    def _changed(self, *_):
        if self.winfo_exists():
            self._render()

    def _enter(self, _):
        self.hover = True
        self._render()

    def _leave(self, _):
        self.hover = False
        self._render()

    def _toggle(self, _=None):
        if self.value is None:
            self.variable.set(not self.variable.get())
        else:
            self.variable.set(self.value)
        if self.command:
            self.command()
        self.focus_set()
        return "break"

    def _render(self):
        self.delete("all")
        w = int(self.cget("width"))
        selected = self._selected()
        fill = "#FFEAF2" if selected else ("#FFF3F7" if self.hover else self.surface)
        border = "#EFC1D2" if selected else LINE
        draw_rounded_rectangle(self, 1, 1, w-1, 26, 13, fill=fill, outline=border)
        draw_rounded_rectangle(self, 10, 8, 22, 20, 4,
                               fill=ACCENT if selected else WHITE,
                               outline=ACCENT if selected else "#C9AEB9")
        if selected:
            self.create_line(13, 14, 15.5, 16.5, 19, 11.5, fill=WHITE,
                             width=1.8, joinstyle="round", capstyle="round")
        self.create_text(29, 14, text=self.title, anchor="w", font=(FONT, 9),
                         fill=ACCENT_TEXT if selected else TEXT)


class RoundedActionButton(tk.Canvas):
    """统一轻圆角操作按钮，可直接接受 configure(state=...)。"""
    def __init__(self, parent, *, text, command=None, variant="normal", width=105, height=29, state="normal", bg=WHITE):
        super().__init__(parent, width=width, height=height, bg=bg, bd=0,
                         relief="flat", highlightthickness=0, cursor="hand2", takefocus=True)
        self.title = text
        self.command = command
        self.variant = variant
        self.selected = False
        self.nav_text_color = MUTED
        self.is_enabled = state != "disabled"
        self.hover = False
        self.bind("<Button-1>", self._click)
        self.bind("<Return>", self._click)
        self.bind("<space>", self._click)
        self.bind("<Enter>", lambda _: self._hover(True))
        self.bind("<Leave>", lambda _: self._hover(False))
        self._render()

    def configure(self, cnf=None, **kwargs):
        if "state" in kwargs:
            self.is_enabled = kwargs.pop("state") != "disabled"
            self._render()
        if "text" in kwargs:
            self.title = kwargs.pop("text")
            self._render()
        if "selected" in kwargs:
            self.selected = kwargs.pop("selected")
            self._render()
        if "text_color" in kwargs:
            self.nav_text_color = kwargs.pop("text_color")
            self._render()
        if cnf or kwargs:
            return super().configure(cnf, **kwargs)

    config = configure

    def _click(self, _=None):
        if self.is_enabled and self.command:
            self.command()
        self.focus_set()
        return "break"

    def _hover(self, is_hover):
        self.hover = is_hover
        self._render()

    def _render(self):
        self.delete("all")
        w, h = int(self.cget("width")), int(self.cget("height"))
        if self.variant == "nav":
            bg = WHITE if self.selected else ("#FFF0F5" if self.hover else "#FFF4F8")
            fg, border = self.nav_text_color, ("#F2E3E9" if self.selected else bg)
        elif self.variant == "primary":
            bg = (DARK_RED if self.hover else ACCENT) if self.is_enabled else "#F8D9E5"
            fg, border = "#6E2F48", bg
        elif self.variant == "danger":
            bg = "#FFF4F8" if self.hover else WHITE
            fg, border = ACCENT_TEXT if self.is_enabled else MUTED, "#EFC6D7"
        else:
            bg = "#FFF5F8" if self.hover else WHITE
            fg, border = TEXT if self.is_enabled else MUTED, LINE
        draw_rounded_rectangle(self, 1, 1, w-1, h-1, 14, fill=bg, outline=border)
        self.create_text(w/2, h/2, text=self.title, fill=fg, font=(FONT, 9, "bold") if self.variant == "primary" else (FONT, 9))


class RoundedSurface(tk.Canvas):
    """普通卡片随内容高度变化；日志卡片可填满剩余高度。"""
    def __init__(self, parent, *, pad_x=12, pad_y=6, bg=BG, stretch_height=False):
        super().__init__(parent, bg=bg, bd=0, highlightthickness=0,
                         height=135 if stretch_height else 50)
        self.pad_x, self.pad_y = pad_x, pad_y
        self.stretch_height = stretch_height
        self.inner = ttk.Frame(self, style="Card.TFrame")
        self.window_id = self.create_window((pad_x, pad_y), window=self.inner, anchor="nw")
        self.inner.bind("<Configure>", self._resize_to_content)
        self.bind("<Configure>", self._resize_width)

    def _resize_width(self, event):
        options = {"width": max(20, event.width - 2*self.pad_x)}
        if self.stretch_height:
            options["height"] = max(24, event.height - 2*self.pad_y)
        self.itemconfigure(self.window_id, **options)
        self._paint()

    def _resize_to_content(self, _=None):
        if self.stretch_height:
            return
        wanted = max(24, self.inner.winfo_reqheight() + 2*self.pad_y)
        if int(self.cget("height")) != wanted:
            super().configure(height=wanted)

    def _paint(self):
        self.delete("surface")
        draw_rounded_rectangle(self, 1, 1, max(12, self.winfo_width()-1),
                               max(12, self.winfo_height()-1), 18,
                               fill=WHITE, outline="#EDE5EB", tag="surface")
        self.tag_lower("surface")


class QueueHandler(logging.Handler):
    def __init__(self, out_queue: queue.Queue):
        super().__init__(logging.INFO)
        self.out_queue = out_queue

    def emit(self, record):
        try:
            self.out_queue.put(("log", record.levelno, self.format(record)))
        except Exception:
            pass


def configured_room_selection(cfg: dict) -> dict[str, list[str]]:
    """旧勾选与编译名单取交集；新增候选房间由用户主动勾选。"""
    if "rooms" not in cfg:
        return core.default_room_selection()
    selected = cfg["rooms"]
    if not isinstance(selected, dict):
        raise ValueError("已保存的会议室选项格式无效，请重新勾选")
    result = {}
    for area, room_map in core.AREA_ROOM_ROWS.items():
        names = selected.get(area, [])
        if not isinstance(names, list) or any(not isinstance(name, str) for name in names):
            raise ValueError("已保存的会议室选项格式无效，请重新勾选")
        result[area] = [name for name in room_map if name in names]
    return result


def release_rule_summary() -> str:
    return "，".join(f"{area}提前{core.AREA_ADVANCE_DAYS[area]}天" for area in AREAS)


def load_config() -> dict:
    values = copy.deepcopy(DEFAULT_CONFIG)
    if not CONFIG_FILE.exists():
        return values
    data = json.loads(CONFIG_FILE.read_text(encoding="utf-8-sig"))
    if not isinstance(data, dict):
        raise ValueError("配置文件必须是JSON对象")
    if data.get("deployment_id", core.DEPLOYMENT_ID) != core.DEPLOYMENT_ID:
        raise ValueError("该配置属于其他区域，不能用于当前程序；请重新设置预约任务")
    # 保留已有配置，不重置用户勾选的房间、目标和运行参数。
    values.update(data)
    values["rooms"] = configured_room_selection(data)
    return values


def save_config(settings: dict):
    CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
    tmp = CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(json.dumps(settings, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(CONFIG_FILE)


def validate_areas(areas: list[str]):
    if not areas or len(areas) != len(set(areas)) or any(a not in AREAS for a in areas):
        raise ValueError(f"每个时段必须选择至少一个有效区域：{' / '.join(AREAS)}")


def number(text, label: str, *, integral=False, nullable=False):
    value = str(text).strip()
    if not value and nullable:
        return None
    try:
        val = int(value) if integral else float(value)
    except ValueError as exc:
        raise ValueError(f"{label}请输入数字") from exc
    return val


class CompactPanel(ttk.Frame):
    """仅一层内容区域：页面自身不滚动，列表超出时由列表本身滚动。"""
    def __init__(self, parent):
        super().__init__(parent, style="Background.TFrame")
        self.content = self


class WindowControlButton(RoundedActionButton):
    """标题栏沿用主题按钮的键盘操作，用线条绘制窗口图标。"""
    def __init__(self, parent, *, text, command):
        super().__init__(parent, text=text, command=command, width=42, height=30, bg=BG)
        self.bind("<FocusIn>", lambda _: self._render())
        self.bind("<FocusOut>", lambda _: self._render())

    def _click(self, _=None):
        if self.is_enabled:
            # 关闭命令可能销毁整个Tk窗口，聚焦必须发生在命令之前。
            self.focus_set()
            if self.command:
                self.command()
        return "break"

    def _render(self):
        self.delete("all")
        fill = "#F8D9E5" if self.hover else BG
        border = ACCENT_TEXT if self.focus_get() is self else fill
        draw_rounded_rectangle(self, 1, 1, 41, 29, 8, fill=fill, outline=border)
        if self.title == "最小化":
            self.create_line(16, 17, 26, 17, fill=TEXT, width=1.4)
        elif self.title == "最大化":
            self.create_rectangle(16, 10, 26, 20, outline=TEXT, width=1.2)
        elif self.title == "还原":
            self.create_rectangle(18, 9, 27, 18, outline=TEXT, width=1.2)
            self.create_rectangle(15, 12, 24, 21, outline=TEXT, fill=fill, width=1.2)
        else:
            self.create_line(16, 10, 26, 20, fill=TEXT, width=1.4)
            self.create_line(16, 20, 26, 10, fill=TEXT, width=1.4)


class BookingGUI:
    def __init__(self, root: tk.Tk, *, config_override: dict | None = None):
        self.root = root
        root.title("会议室自动抢订")
        # 使用 Tk 的逻辑像素，尊重 Windows 125% / 150% DPI 缩放。
        # 16 寸笔记本优先约 1160×750，窄屏和低分屏使用滚动区域。
        screen_w, screen_h = root.winfo_screenwidth(), root.winfo_screenheight()
        width = min(1110, max(790, screen_w - 56))
        height = min(655, max(630, screen_h - 76))
        root.geometry(f"{width}x{height}")
        root.minsize(min(940, screen_w - 30), min(640, screen_h - 30))
        root.configure(bg=BG)
        root.protocol("WM_DELETE_WINDOW", self.on_close)

        self.queue: queue.Queue = queue.Queue()
        self.thread: threading.Thread | None = None
        self.worker_loop: asyncio.AbstractEventLoop | None = None
        self.stop_event = threading.Event()
        self.close_after_stop = False
        self.rooms: dict[tuple[str, str], tk.BooleanVar] = {}
        self.release_data: dict[str, dict] = {}
        self.watch_data: dict[str, dict] = {}
        self.setting_vars: dict[str, tk.StringVar] = {}
        self.mode_var = tk.StringVar(value=MODE_LABELS["both"])
        self.dry_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value="就绪")
        self.verbose_var = tk.BooleanVar(value=False)
        self.timing_var = tk.BooleanVar(value=False)
        self.watch_stop_var = tk.BooleanVar(value=True)
        self.release_type = tk.StringVar(value="单次")
        self.release_date = tk.StringVar(value=date.today().isoformat())
        self.release_weekday = tk.StringVar(value=WEEKDAYS[0])
        self.release_start = tk.StringVar(value="14:00")
        self.release_end = tk.StringVar(value="16:00")
        self.watch_date = tk.StringVar(value=date.today().isoformat())
        self.watch_start = tk.StringVar(value="14:00")
        self.watch_end = tk.StringVar(value="16:00")
        self.release_areas = {a: tk.BooleanVar(value=a == AREAS[0]) for a in AREAS}
        self.watch_areas = {a: tk.BooleanVar(value=a == AREAS[0]) for a in AREAS}
        self._style()
        self._build()
        self._window_frame = None
        self._frame_update_pending = False
        self._custom_frame_disabled = False
        self._window_drag = None
        if sys.platform == "win32":
            try:
                self._window_frame = WindowsWindowFrame(root)
            except (OSError, AttributeError, tk.TclError) as exc:
                logging.warning("窗口样式初始化失败，改用系统窗口：%s", exc)
                root.overrideredirect(False)
                self._custom_frame_disabled = True
                self.titlebar.pack_forget()
                self._resize_grip.place_forget()
        root.bind("<Map>", self._schedule_window_frame, add="+")
        self._schedule_window_frame()
        try:
            self.set_config(config_override if config_override is not None else load_config())
        except Exception as exc:
            messagebox.showwarning("读取配置失败", f"将使用初始配置。\n{exc}", parent=self.root)
            self.set_config(DEFAULT_CONFIG)
        self.root.after(140, self._drain_queue)

    def _schedule_window_frame(self, event=None):
        if self._custom_frame_disabled:
            return
        if event is not None and event.widget is not self.root:
            return
        if not self._frame_update_pending:
            self._frame_update_pending = True
            self.root.after_idle(self._apply_window_frame)

    def _apply_window_frame(self):
        self._frame_update_pending = False
        if self._custom_frame_disabled:
            return
        if sys.platform != "win32":
            self._custom_frame_disabled = True
            self.titlebar.pack_forget()
            return
        try:
            self._window_frame.enable()
            self.maximize_button.configure(text="还原" if self._is_maximized() else "最大化")
            if self._is_maximized():
                self._resize_grip.place_forget()
            else:
                self._resize_grip.place(relx=1, rely=1, anchor="se")
        except (OSError, AttributeError, tk.TclError) as exc:
            logging.warning("标题栏样式设置失败，保留可用的窗口操作：%s", exc)
            self.root.overrideredirect(False)
            self._custom_frame_disabled = True
            self.titlebar.pack_forget()
            self._resize_grip.place_forget()

    def _is_maximized(self):
        frame = getattr(self, "_window_frame", None)
        return (frame.maximized if frame is not None and not self._custom_frame_disabled
                else self.root.state() == "zoomed")

    def _minimize_window(self):
        self._window_drag = None
        if self._window_frame is not None and not self._custom_frame_disabled:
            self._window_frame.minimize()
        else:
            self.root.iconify()

    def _start_window_drag(self, event):
        # 移动全过程由 Tk 处理；不要在 Tk 回调内进入 Win32 的嵌套拖动循环。
        self._window_drag = {
            "pointer_x": int(event.x_root), "pointer_y": int(event.y_root),
            "x": self.root.winfo_x(), "y": self.root.winfo_y(),
            "width": self.root.winfo_width(), "height": self.root.winfo_height(),
            "zoomed": self._is_maximized(), "moving": False,
            "ratio": min(1.0, max(0.0, (event.x_root - self.root.winfo_rootx())
                                     / max(1, self.root.winfo_width()))),
            "title_offset": min(32, max(0, event.y_root - self.root.winfo_rooty())),
        }
        return "break"

    def _drag_window(self, event):
        drag = self._window_drag
        if drag is None:
            return "break"
        dx = int(event.x_root) - drag["pointer_x"]
        dy = int(event.y_root) - drag["pointer_y"]
        if not drag["moving"] and max(abs(dx), abs(dy)) < 3:
            return "break"
        if drag["zoomed"]:
            frame = getattr(self, "_window_frame", None)
            if frame is not None and not getattr(self, "_custom_frame_disabled", False):
                frame.restore()
            else:
                self.root.state("normal")
            self.root.update_idletasks()
            drag["width"], drag["height"] = self.root.winfo_width(), self.root.winfo_height()
            border_x = self.root.winfo_rootx() - self.root.winfo_x()
            border_y = self.root.winfo_rooty() - self.root.winfo_y()
            x = round(event.x_root - drag["ratio"] * drag["width"] - border_x)
            y = int(event.y_root - drag["title_offset"] - border_y)
            # 后续移动始终从还原后的同一基准计算，避免连续移动时累加误差。
            drag.update(x=x, y=y, pointer_x=int(event.x_root),
                        pointer_y=int(event.y_root), zoomed=False)
            self._schedule_window_frame()
        else:
            x, y = drag["x"] + dx, drag["y"] + dy
        drag["moving"] = True
        # Tk 将分隔符后的负数解释为绝对坐标，支持主屏左侧/上方的显示器。
        # 不重复写入尺寸，避免系统边框与 Tk 客户区尺寸差异在拖动时累积。
        self.root.geometry(f"+{x}+{y}")
        self.root.update_idletasks()
        return "break"

    def _end_window_drag(self, _event=None):
        self._window_drag = None
        return "break"

    def _toggle_maximize(self, _event=None):
        self._window_drag = None
        frame = getattr(self, "_window_frame", None)
        if frame is not None and not getattr(self, "_custom_frame_disabled", False):
            frame.toggle_maximize()
        else:
            self.root.state("normal" if self.root.state() == "zoomed" else "zoomed")
        self._schedule_window_frame()
        return "break"

    def _style(self):
        # 使用紧凑字号与柔和粉色配色。
        defaults = tkfont.nametofont("TkDefaultFont")
        defaults.configure(family=FONT, size=9)
        tkfont.nametofont("TkTextFont").configure(family=FONT, size=9)
        self.style = ttk.Style()
        if "clam" in self.style.theme_names():
            self.style.theme_use("clam")
        self.style.configure("Background.TFrame", background=BG)
        self.style.configure("Card.TFrame", background=WHITE)
        self.style.configure("TLabel", font=(FONT, 9), foreground=TEXT, background=BG)
        self.style.configure("Card.TLabel", font=(FONT, 9), foreground=TEXT, background=WHITE)
        self.style.configure("Muted.TLabel", font=(FONT, 8), foreground=MUTED, background=BG)
        self.style.configure("CardMuted.TLabel", font=(FONT, 8), foreground=MUTED, background=WHITE)
        self.style.configure("Header.TLabel", font=(FONT, 13, "bold"), foreground=TEXT, background=BG)
        self.style.configure("Section.TLabel", font=(FONT, 9, "bold"), foreground=TEXT, background=WHITE)
        self.style.configure("TButton", font=(FONT, 9), padding=(8, 3),
                             background=WHITE, foreground=TEXT, borderwidth=1, bordercolor=LINE)
        self.style.map("TButton", background=[("active", "#FFF4F8"), ("disabled", "#F8F3F5")])
        self.style.configure("Primary.TButton", font=(FONT, 9, "bold"), padding=(10, 4),
                             foreground="#6E2F48", background=ACCENT, bordercolor=ACCENT)
        self.style.map("Primary.TButton", background=[("active", DARK_RED), ("disabled", "#F7D4E1")])
        self.style.configure("Danger.TButton", font=(FONT, 9), padding=(8, 3),
                             foreground=ACCENT, background=WHITE, bordercolor="#EFC6D7")
        self.style.configure("TEntry", font=(FONT, 9), padding=2,
                             fieldbackground=WHITE, background=WHITE, bordercolor=LINE)
        # 禁用 readonly 下拉框的灰底；当前运行模式和所有时间选择均为白底。
        self.style.configure("White.TCombobox", font=(FONT, 9), padding=(5, 2),
                             fieldbackground=WHITE, background=WHITE,
                             foreground=TEXT, selectbackground=WHITE, selectforeground=TEXT,
                             bordercolor=LINE, lightcolor=WHITE, darkcolor=WHITE)
        self.style.map("White.TCombobox",
                       fieldbackground=[("readonly", WHITE), ("!disabled", WHITE)],
                       background=[("readonly", WHITE), ("active", WHITE)],
                       foreground=[("readonly", TEXT)],
                       selectbackground=[("readonly", WHITE)],
                       selectforeground=[("readonly", TEXT)])
        self.style.configure("Treeview", font=(FONT, 9), rowheight=25, background=WHITE,
                             fieldbackground=WHITE, foreground=TEXT, borderwidth=0,
                             bordercolor="#F2E5EA", lightcolor=WHITE, darkcolor=WHITE, relief="flat")
        self.style.configure("Treeview.Heading", font=(FONT, 9, "bold"), padding=(6, 4),
                             background="#FFF0F5", foreground="#594B52", relief="flat")
        self.style.map("Treeview", background=[("selected", "#FFEAF2")], foreground=[("selected", TEXT)])
        self.style.configure("TScrollbar", background="#E9CBD7", troughcolor=BG, bordercolor=BG, lightcolor="#F4D9E3", darkcolor="#E9CBD7", arrowcolor=ACCENT_TEXT, arrowsize=11)

    def _select_tab(self, key: str):
        """仅切换颜色和下划线，不改变标签文字大小、padding及按钮尺寸。"""
        if key == getattr(self, "_active_tab", None):
            return
        for panel in getattr(self, "tab_panels", {}).values():
            panel.pack_forget()
        self.tab_panels[key].pack(fill="both", expand=True)
        self._active_tab = key
        for name, (button, underline) in self.tab_buttons.items():
            active = name == key
            button.configure(text_color=ACCENT_TEXT if active else MUTED,
                             selected=active)
            underline.configure(bg=ACCENT_TEXT if active else BG)

    def _build_tab_navigation(self, parent):
        """圆角导航标签固定尺寸，选中/未选中尺寸、字体始终相同。"""
        nav = tk.Frame(parent, bg=BG, height=35)
        nav.pack(fill="x")
        nav.pack_propagate(False)
        self.tab_buttons = {}
        captions = (("release", "到点抢订"), ("watch", "空闲候补"),
                    ("rooms", "会议室选择"), ("advanced", "高级设置"))
        for key, caption in captions:
            cell = tk.Frame(nav, bg=BG, width=118, height=32)
            cell.pack(side="left", padx=(0, 6))
            cell.pack_propagate(False)
            underline = tk.Frame(cell, bg=BG, height=2)
            underline.pack(side="bottom", fill="x", padx=10)
            button = RoundedActionButton(cell, text=caption, variant="nav", width=118, height=30,
                                         bg=BG, command=lambda choice=key: self._select_tab(choice))
            button.pack(side="top", fill="both", expand=True)
            self.tab_buttons[key] = (button, underline)
        tk.Frame(parent, bg=LINE, height=1).pack(fill="x", pady=(0, 2))

    def card(self, parent, title: str, hint: str | None = None) -> ttk.Frame:
        outer_card = RoundedSurface(parent, pad_x=11, pad_y=6)
        outer_card.pack(fill="x", padx=2, pady=(0, 1))
        card = outer_card.inner
        headline = ttk.Frame(card, style="Card.TFrame")
        headline.pack(fill="x", pady=(0, 5))
        ttk.Label(headline, text=title, style="Section.TLabel").pack(side="left", padx=(0, 13))
        if hint:
            ttk.Label(headline, text=hint, style="CardMuted.TLabel").pack(side="left")
        return card

    def _build(self):
        outer = ttk.Frame(self.root, style="Background.TFrame", padding=(11, 8))
        outer.pack(fill="both", expand=True)
        self.titlebar = tk.Frame(outer, bg=BG, height=32)
        self.titlebar.pack(fill="x", pady=(0, 5))
        title = tk.Label(self.titlebar, text="会议室自动抢订", bg=BG, fg=ACCENT_TEXT,
                         font=(FONT, 13, "bold"))
        title.pack(side="left")
        WindowControlButton(self.titlebar, text="关闭", command=self.on_close).pack(side="right")
        self.maximize_button = WindowControlButton(self.titlebar, text="最大化", command=self._toggle_maximize)
        self.maximize_button.pack(side="right")
        WindowControlButton(self.titlebar, text="最小化", command=self._minimize_window).pack(side="right")
        for surface in (self.titlebar, title):
            surface.bind("<ButtonPress-1>", self._start_window_drag)
            surface.bind("<B1-Motion>", self._drag_window)
            surface.bind("<ButtonRelease-1>", self._end_window_drag)
            surface.bind("<Double-Button-1>", self._toggle_maximize)
        self.style.configure("Pink.TSizegrip", background=BG)
        self._resize_grip = ttk.Sizegrip(self.root, style="Pink.TSizegrip")
        self._resize_grip.place(relx=1, rely=1, anchor="se")
        self.root.bind("<Alt-F4>", lambda _event: (self.on_close(), "break")[1])
        controls = ttk.Frame(outer, style="Background.TFrame")
        controls.pack(fill="x", pady=(0, 5))
        ttk.Label(controls, text="运行模式").pack(side="left", padx=(0, 9))
        self.mode_combo = ttk.Combobox(controls, textvariable=self.mode_var,
                                       values=list(MODE_OPTIONS), width=22, state="readonly", style="White.TCombobox")
        self.mode_combo.pack(side="left", padx=(0, 14))
        simulation = ttk.Frame(controls, style="Background.TFrame")
        simulation.pack(side="left")
        ChoiceChip(simulation, text="模拟运行（不提交预订）", variable=self.dry_var,
                   width=188, bg=BG).pack(side="left")
        self.status_label = tk.Label(controls, textvariable=self.status_var, font=(FONT, 9, "bold"),
                                     bg="#FFEAF2", fg=TEXT, padx=9, pady=3)
        self.status_label.pack(side="right", padx=(15, 0))
        self.room_count = ttk.Label(controls, text="", style="Muted.TLabel")
        self.room_count.pack(side="right")

        # 自定义顶部导航：选中与未选中标签的几何尺寸完全一致。
        self._build_tab_navigation(outer)
        self.tab_host = ttk.Frame(outer, style="Background.TFrame")
        # 关键：内容按自然高度占位，不再把空白全部撑在操作栏上方。
        # 剩余高度交给下方可扩展的运行日志窗口。
        self.tab_host.pack(fill="x")
        self.tab_release = CompactPanel(self.tab_host)
        self.tab_watch = CompactPanel(self.tab_host)
        self.tab_rooms = CompactPanel(self.tab_host)
        self.tab_advanced = CompactPanel(self.tab_host)
        self.tab_panels = {
            "release": self.tab_release, "watch": self.tab_watch,
            "rooms": self.tab_rooms, "advanced": self.tab_advanced,
        }
        self._active_tab = None
        self._build_release()
        self._build_watch()
        self._build_rooms()
        self._build_advanced()
        self._select_tab("release")

        foot = ttk.Frame(outer, style="Background.TFrame")
        foot.pack(fill="x", pady=(3, 3))
        self.start_button = RoundedActionButton(foot, text="▶  保存并开始", variant="primary", width=129, height=30,
                                                bg=BG, command=self.start_run)
        self.start_button.pack(side="left")
        self.stop_button = RoundedActionButton(foot, text="■  停止任务", variant="danger", width=108, height=30,
                                               bg=BG, command=self.stop_run, state="disabled")
        self.stop_button.pack(side="left", padx=9)
        RoundedActionButton(foot, text="保存配置", width=84, height=30, bg=BG, command=self.save_current).pack(side="left", padx=(11, 0))
        RoundedActionButton(foot, text="打开日志文件夹", width=112, height=30, bg=BG,
                            command=lambda: self._open_folder(core.LOG_DIR)).pack(side="right")
        RoundedActionButton(foot, text="查看放号计划", width=112, height=30, bg=BG,
                            command=self.show_release_plan).pack(side="right", padx=(0, 8))

        rounded_log = RoundedSurface(outer, pad_x=10, pad_y=6, stretch_height=True)
        rounded_log.pack(fill="both", expand=True)
        log_card = rounded_log.inner
        log_top = ttk.Frame(log_card, style="Card.TFrame")
        log_top.pack(fill="x", pady=(0, 2))
        ttk.Label(log_top, text="运行日志", style="Card.TLabel").pack(side="left")
        ttk.Label(log_top, text="黄色：预约结果 / 需关注     红色：错误", style="CardMuted.TLabel").pack(side="right")
        log_body = ttk.Frame(log_card, style="Card.TFrame")
        log_body.pack(fill="both", expand=True)
        self.log_text = tk.Text(log_body, height=6, bg="#FFFFFF", fg=TEXT, font=(FONT, 9),
                                relief="flat", wrap="word", padx=8, pady=3, state="disabled",
                                insertbackground=TEXT, highlightthickness=0)
        self.log_text.pack(side="left", fill="both", expand=True)
        self.log_scroll = ttk.Scrollbar(log_body, orient="vertical", command=self.log_text.yview)
        # 运行日志只有内容溢出时才展示滚动条，避免常驻的灰色长轨道。
        self.log_text.configure(yscrollcommand=lambda first, last: self._autohide_list_scroll(
            self.log_scroll, log_body, first, last))
        self.log_text.tag_configure("warning", foreground=YELLOW)
        self.log_text.tag_configure("error", foreground="#B24E6D")
        self.log_text.tag_configure("system", foreground=MUTED)

    def _new_tree(self, parent, columns, headings, widths, height=4):
        row = ttk.Frame(parent, style="Card.TFrame")
        row.pack(fill="x")
        tree = ttk.Treeview(row, columns=columns, show="headings", height=height, selectmode="browse")
        for key, name, width in zip(columns, headings, widths):
            tree.heading(key, text=name)
            tree.column(key, width=width, minwidth=85, anchor="center", stretch=True)
        ybar = ttk.Scrollbar(row, orient="vertical", command=tree.yview)
        tree.configure(yscrollcommand=lambda first, last, bar=ybar, area=row:
                       self._autohide_list_scroll(bar, area, first, last))
        tree.pack(side="left", fill="x", expand=True)
        tree._scrollbar = ybar  # 内部引用，方便调试
        return tree

    @staticmethod
    def _autohide_list_scroll(bar, area, first, last):
        try:
            if float(first) <= 0.0 and float(last) >= 0.999:
                if bar.winfo_manager():
                    bar.pack_forget()
            elif not bar.winfo_manager():
                bar.pack(side="right", fill="y")
            bar.set(first, last)
        except tk.TclError:
            pass

    def _label_entry(self, parent, text, variable, width=15):
        field = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(field, text=text, style="CardMuted.TLabel").pack(anchor="w", pady=(0, 2))
        entry = ttk.Entry(field, textvariable=variable, width=width)
        entry.pack(fill="x")
        return field, entry

    def _label_combo(self, parent, text, variable, values, width=12):
        field = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(field, text=text, style="CardMuted.TLabel").pack(anchor="w", pady=(0, 2))
        entry = ttk.Combobox(field, textvariable=variable, values=values, width=width,
                             state="readonly", style="White.TCombobox")
        entry.pack(fill="x")
        return field, entry

    def _area_checkboxes(self, parent, values):
        frame = ttk.Frame(parent, style="Card.TFrame")
        ttk.Label(frame, text="本时段预约区域", style="CardMuted.TLabel").pack(side="left", padx=(0, 12))
        label_font = tkfont.Font(family=FONT, size=9)
        for a in AREAS:
            label = core.AREA_LABELS[a]
            ChoiceChip(frame, text=label, variable=values[a],
                       width=max(115, label_font.measure(label) + 40)).pack(side="left", padx=(0, 8))
        return frame

    def _build_release(self):
        tab = self.tab_release.content
        card = self.card(tab, "预约计划", "单次日期与每周固定预约放在同一张表中。每一条任务都能单独选择参与区域。")
        self.release_tree = self._new_tree(card, ("kind", "when", "time", "areas"),
                                           ("任务类型", "日期 / 星期", "预订时段", "参与区域"),
                                           (128, 230, 200, 265), 3)
        self.release_tree.bind("<<TreeviewSelect>>", self._release_select)
        editor = ttk.Frame(card, style="Card.TFrame")
        editor.pack(fill="x", pady=(6, 0))
        row1 = ttk.Frame(editor, style="Card.TFrame")
        row1.pack(fill="x")
        kind = ttk.Frame(row1, style="Card.TFrame")
        kind.pack(side="left", padx=(0, 14))
        ttk.Label(kind, text="预约类型", style="CardMuted.TLabel").pack(anchor="w", pady=(0, 2))
        ChoiceChip(kind, text="单次日期", value="单次", variable=self.release_type,
                   width=112, command=self._type_changed).pack(side="left", padx=(0, 7))
        ChoiceChip(kind, text="每周固定", value="每周", variable=self.release_type,
                   width=112, command=self._type_changed).pack(side="left")
        date_field, _ = self._label_entry(row1, "预约日期（YYYY-MM-DD）", self.release_date, 18)
        self.release_date_field = date_field
        week_field, _ = self._label_combo(row1, "每周星期", self.release_weekday, WEEKDAYS, 15)
        self.release_week_field = week_field
        for field in (date_field, week_field):
            field.pack(side="left", padx=(0, 14))
        self.release_kind_field = kind
        self._type_changed()
        start_field, _ = self._label_combo(row1, "开始", self.release_start, TIME_SLOTS, 10)
        start_field.pack(side="left", padx=(0, 15))
        end_field, _ = self._label_combo(row1, "结束", self.release_end, TIME_SLOTS, 10)
        end_field.pack(side="left")
        row2 = ttk.Frame(editor, style="Card.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        self._area_checkboxes(row2, self.release_areas).pack(side="left")
        buttons = ttk.Frame(row2, style="Card.TFrame")
        buttons.pack(side="right")
        RoundedActionButton(buttons, text="新增任务", variant="primary", width=90, command=self.release_add).pack(side="left")
        RoundedActionButton(buttons, text="修改选中", width=90, command=self.release_update).pack(side="left", padx=7)
        RoundedActionButton(buttons, text="删除", width=67, command=self.release_delete).pack(side="left")
        ttk.Label(card, text="选中上表任一行即可回填修改。每周固定任务会自动滚动安排未来放号，不需要每周修改日期。",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(4, 0))

        timings = self.card(tab, "到点抢订设置", "只填写一个绝对放号时间，其余按相对秒数计算；" + release_rule_summary() + "开放。")
        row = ttk.Frame(timings, style="Card.TFrame")
        row.pack(fill="x")
        for idx, (key, title, default) in enumerate((
            ("release_open_at", "每天放号时间", core.DEFAULT_RELEASE_OPEN_AT),
            ("prepare_before_seconds", "提前准备（秒）", "300"),
            ("stop_after_seconds", "到点抢订放号后截止（秒）", "300"),
            ("retry_seconds", "失败额外等待（秒）", "0.9"),
        )):
            variable = tk.StringVar(value=default)
            self.setting_vars[key] = variable
            field, _ = self._label_entry(row, title, variable, 15)
            field.pack(side="left", fill="x", expand=True, padx=(0, 12 if idx < 3 else 0))

    def _type_changed(self):
        active = self.release_week_field if self.release_type.get() == "每周" else self.release_date_field
        inactive = self.release_date_field if self.release_type.get() == "每周" else self.release_week_field
        inactive.pack_forget()
        active.pack(side="left", padx=(0, 14), after=self.release_kind_field)

    def _release_values(self) -> dict:
        areas = [a for a in AREAS if self.release_areas[a].get()]
        validate_areas(areas)
        start, end = self.release_start.get(), self.release_end.get()
        if self.release_type.get() == "单次":
            when = self.release_date.get().strip()
            core.Target(date.fromisoformat(when), start, end, tuple(areas))
            return {"kind": "single", "date": when, "start": start, "end": end, "areas": areas}
        weekday = WEEKDAYS.index(self.release_weekday.get()) + 1
        core.Target(date(2026, 1, 5), start, end, tuple(areas))
        return {"kind": "weekly", "weekday": weekday, "start": start, "end": end, "areas": areas}

    def _release_label(self, item: dict):
        return ("单次" if item["kind"] == "single" else "每周固定",
                item["date"] if item["kind"] == "single" else WEEKDAYS[item["weekday"] - 1],
                f"{item['start']}–{item['end']}", " / ".join(item["areas"]))

    @staticmethod
    def _release_key(item: dict):
        return (item["kind"], item["date"] if item["kind"] == "single" else item["weekday"],
                item["start"], item["end"])

    def release_add(self):
        try:
            item = self._release_values()
            if any(self._release_key(d) == self._release_key(item) for d in self.release_data.values()):
                raise ValueError("相同日期 / 星期和时段已存在，请选中原记录修改预约区域")
            iid = self.release_tree.insert("", "end", values=self._release_label(item))
            self.release_data[iid] = item
        except Exception as exc:
            messagebox.showerror("新增任务失败", str(exc), parent=self.root)

    def release_update(self):
        chosen = self.release_tree.selection()
        if not chosen:
            messagebox.showinfo("请选择一行", "先在预约计划中选择要修改的记录", parent=self.root)
            return
        try:
            item = self._release_values()
            if any(self._release_key(d) == self._release_key(item) for k, d in self.release_data.items() if k != chosen[0]):
                raise ValueError("修改后的时段与现有记录重复")
            self.release_data[chosen[0]] = item
            self.release_tree.item(chosen[0], values=self._release_label(item))
        except Exception as exc:
            messagebox.showerror("修改任务失败", str(exc), parent=self.root)

    def release_delete(self):
        for key in self.release_tree.selection():
            self.release_data.pop(key, None)
            self.release_tree.delete(key)

    def _release_select(self, _=None):
        chosen = self.release_tree.selection()
        if not chosen:
            return
        item = self.release_data.get(chosen[0])
        if not item:
            return
        self.release_type.set("单次" if item["kind"] == "single" else "每周")
        if item["kind"] == "single":
            self.release_date.set(item["date"])
        else:
            self.release_weekday.set(WEEKDAYS[item["weekday"] - 1])
        self.release_start.set(item["start"])
        self.release_end.set(item["end"])
        for area in AREAS:
            self.release_areas[area].set(area in item["areas"])
        self._type_changed()

    def _build_watch(self):
        tab = self.tab_watch.content
        card = self.card(tab, "候补监控时段", "自选具体日期与时段；只有该区域已开放时才进行轮询。多个目标共享一个轮询时钟。")
        self.watch_tree = self._new_tree(card, ("date", "time", "areas"),
                                         ("预约日期", "预订时段", "参与区域"), (240, 270, 300), 3)
        self.watch_tree.bind("<<TreeviewSelect>>", self._watch_select)
        editor = ttk.Frame(card, style="Card.TFrame")
        editor.pack(fill="x", pady=(6, 0))
        for field in (
            self._label_entry(editor, "会议日期（YYYY-MM-DD）", self.watch_date, 19)[0],
            self._label_combo(editor, "开始", self.watch_start, TIME_SLOTS, 11)[0],
            self._label_combo(editor, "结束", self.watch_end, TIME_SLOTS, 11)[0],
        ):
            field.pack(side="left", padx=(0, 14))
        row2 = ttk.Frame(card, style="Card.TFrame")
        row2.pack(fill="x", pady=(6, 0))
        self._area_checkboxes(row2, self.watch_areas).pack(side="left")
        buttons = ttk.Frame(row2, style="Card.TFrame")
        buttons.pack(side="right")
        RoundedActionButton(buttons, text="新增时段", variant="primary", width=90, command=self.watch_add).pack(side="left")
        RoundedActionButton(buttons, text="修改选中", width=90, command=self.watch_update).pack(side="left", padx=7)
        RoundedActionButton(buttons, text="删除", width=67, command=self.watch_delete).pack(side="left")

        settings = self.card(tab, "候补轮询设置", "轮询间隔包含本轮刷新、填表及结果检查；上一轮未结束不会启动下一轮。")
        row = ttk.Frame(settings, style="Card.TFrame")
        row.pack(fill="x")
        for idx, (key, text, default) in enumerate((
            ("watch_interval_seconds", "轮询间隔（秒）", "30"),
            ("watch_duration_minutes", "持续时间（分钟，可留空）", "120"),
        )):
            variable = tk.StringVar(value=default)
            self.setting_vars[key] = variable
            field, _ = self._label_entry(row, text, variable, 22)
            field.pack(side="left", padx=(0, 24 if idx == 0 else 0))
        ChoiceChip(settings, text="会议开始后停止候补", variable=self.watch_stop_var,
                   width=177).pack(anchor="w", pady=(9, 1))

    def _watch_values(self):
        areas = [a for a in AREAS if self.watch_areas[a].get()]
        validate_areas(areas)
        d, s, e = self.watch_date.get().strip(), self.watch_start.get(), self.watch_end.get()
        core.Target(date.fromisoformat(d), s, e, tuple(areas))
        return {"date": d, "start": s, "end": e, "areas": areas}

    @staticmethod
    def _watch_key(item):
        return item["date"], item["start"], item["end"]

    def _watch_label(self, item):
        return item["date"], f"{item['start']}–{item['end']}", " / ".join(item["areas"])

    def watch_add(self):
        try:
            item = self._watch_values()
            if any(self._watch_key(d) == self._watch_key(item) for d in self.watch_data.values()):
                raise ValueError("相同日期时段已存在，请选择后修改预约区域")
            iid = self.watch_tree.insert("", "end", values=self._watch_label(item))
            self.watch_data[iid] = item
        except Exception as exc:
            messagebox.showerror("添加候补时段失败", str(exc), parent=self.root)

    def watch_update(self):
        chosen = self.watch_tree.selection()
        if not chosen:
            messagebox.showinfo("请选择一行", "请在列表中先选中要修改的时段", parent=self.root)
            return
        try:
            item = self._watch_values()
            if any(self._watch_key(d) == self._watch_key(item) for k, d in self.watch_data.items() if k != chosen[0]):
                raise ValueError("该日期时段与现有记录重复")
            self.watch_data[chosen[0]] = item
            self.watch_tree.item(chosen[0], values=self._watch_label(item))
        except Exception as exc:
            messagebox.showerror("修改候补时段失败", str(exc), parent=self.root)

    def watch_delete(self):
        for key in self.watch_tree.selection():
            self.watch_data.pop(key, None)
            self.watch_tree.delete(key)

    def _watch_select(self, _=None):
        chosen = self.watch_tree.selection()
        if not chosen:
            return
        item = self.watch_data.get(chosen[0])
        if not item:
            return
        self.watch_date.set(item["date"])
        self.watch_start.set(item["start"])
        self.watch_end.set(item["end"])
        for area in AREAS:
            self.watch_areas[area].set(area in item["areas"])

    def _build_rooms(self):
        tab = self.tab_rooms.content
        tips = self.card(tab, "候选会议室", "名单由管理员维护，可勾选参与房间；每个预约时段仍需单独勾选参与区域。")
        tools = ttk.Frame(tips, style="Card.TFrame")
        tools.pack(fill="x", pady=(1, 0))
        total_rooms = sum(len(room_map) for room_map in core.AREA_ROOM_ROWS.values())
        RoundedActionButton(tools, text=f"全选{total_rooms}间", variant="primary", width=85, height=28,
                            command=lambda: [self.select_area(a, True) for a in AREAS]).pack(side="left")
        ttk.Label(tools, text="首次使用默认全选；保留已有勾选，新加入的会议室需自行勾选。",
                  style="CardMuted.TLabel").pack(side="left", padx=12)
        for area, room_map in core.AREA_ROOM_ROWS.items():
            note = f"提前{core.AREA_ADVANCE_DAYS[area]}天开放 · {len(room_map)}间会议室"
            group = self.card(tab, core.AREA_LABELS[area], note)
            header = ttk.Frame(group, style="Card.TFrame")
            header.pack(fill="x", pady=(0, 5))
            RoundedActionButton(header, text="全选", width=65,
                                command=lambda a=area: self.select_area(a, True)).pack(side="left")
            RoundedActionButton(header, text="清空", width=65,
                                command=lambda a=area: self.select_area(a, False)).pack(side="left", padx=8)
            frame = ttk.Frame(group, style="Card.TFrame")
            frame.pack(fill="x")
            for i, name in enumerate(room_map):
                variable = tk.BooleanVar(value=False)
                variable.trace_add("write", lambda *_: self.refresh_room_count())
                self.rooms[(area, name)] = variable
                # 五列排列候选会议室，为运行日志留出空间。
                ChoiceChip(frame, text=name, variable=variable, width=121).grid(
                    row=i//5, column=i%5, padx=(0, 8), pady=2, sticky="w")
        ttk.Label(tips, text="选房以网页实际结果为准；若页面提示失败，程序会保留弹窗原文并按配置处理。",
                  style="CardMuted.TLabel").pack(anchor="w")

    def select_area(self, area: str, selected: bool):
        for (a, _), variable in self.rooms.items():
            if a == area:
                variable.set(selected)
        self.refresh_room_count()

    def refresh_room_count(self):
        if not hasattr(self, "room_count"):
            return
        count = sum(int(v.get()) for v in self.rooms.values())
        self.room_count.configure(text=f"已选 {count} 间会议室")

    def _build_advanced(self):
        tab = self.tab_advanced.content
        conn = self.card(tab, "浏览器与认证", "先依次完成新标签认证，之后所有会议室并行填表；不限制同时准备的房间数量。")
        grid = ttk.Frame(conn, style="Card.TFrame")
        grid.pack(fill="x")
        for i, (key, text, default) in enumerate((
            ("auth_stagger_seconds", "新标签认证错峰（秒）", "1.5"),
            ("watch_guard_seconds", "放号准备前让出页面（秒）", "120"),
            ("popup_retry_seconds", "失败弹窗关闭后重试（秒）", "0.2"),
        )):
            variable = tk.StringVar(value=default)
            self.setting_vars[key] = variable
            field, _ = self._label_entry(grid, text, variable, 22)
            field.grid(row=0, column=i, sticky="w", padx=(0, 18))
        logging_card = self.card(tab, "日志和诊断")
        ChoiceChip(logging_card, text="显示详细网页日志", variable=self.verbose_var,
                   width=166).pack(anchor="w")
        ChoiceChip(logging_card, text="记录逐步骤耗时", variable=self.timing_var,
                   width=166).pack(anchor="w", pady=(6, 0))
        paths = self.card(tab, "文件保存位置")
        ttk.Label(paths, text=f"区域：{core.DEPLOYMENT_LABEL}    业务核心识别码：{CORE_SHA256[:12]}",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(0, 2))
        for txt in (f"配置：{CONFIG_FILE}", f"日志：{core.LOG_DIR}", f"Chrome登录状态：{core.PROFILE_DIR}"):
            ttk.Label(paths, text=txt, style="CardMuted.TLabel", wraplength=920).pack(anchor="w", pady=(2, 7))
        ttk.Label(paths, text="本机登录信息仅保存在当前用户账户。不要把 Chrome 登录数据、个人配置和日志发给其他人。",
                  style="CardMuted.TLabel").pack(anchor="w", pady=(6, 0))

    def set_config(self, cfg):
        self.mode_var.set(MODE_LABELS.get(cfg.get("mode", "both"), MODE_LABELS["both"]))
        self.dry_var.set(bool(cfg.get("dry_run", True)))
        self.release_tree.delete(*self.release_tree.get_children())
        self.release_data.clear()
        for t in cfg.get("release_targets", []):
            item = {"kind": "single", "date": t["date"], "start": t["start"],
                    "end": t["end"], "areas": list(t.get("areas", []))}
            iid = self.release_tree.insert("", "end", values=self._release_label(item))
            self.release_data[iid] = item
        for rule in cfg.get("weekly_release_rules", []):
            item = {"kind": "weekly", "weekday": rule["weekday"], "start": rule["start"],
                    "end": rule["end"], "areas": list(rule.get("areas", []))}
            iid = self.release_tree.insert("", "end", values=self._release_label(item))
            self.release_data[iid] = item
        self.watch_tree.delete(*self.watch_tree.get_children())
        self.watch_data.clear()
        for item in cfg.get("watch_targets", []):
            row = {"date": item["date"], "start": item["start"], "end": item["end"],
                   "areas": list(item.get("areas", []))}
            iid = self.watch_tree.insert("", "end", values=self._watch_label(row))
            self.watch_data[iid] = row
        room_selection = configured_room_selection(cfg)
        for (area, name), variable in self.rooms.items():
            variable.set(name in room_selection[area])
        for key, variable in self.setting_vars.items():
            val = cfg.get(key, DEFAULT_CONFIG.get(key, ""))
            variable.set("" if val is None else str(val))
        self.watch_stop_var.set(bool(cfg.get("stop_at_meeting_start", True)))
        self.verbose_var.set(bool(cfg.get("verbose_logs", False)))
        self.timing_var.set(bool(cfg.get("step_timing_logs", False)))
        self.refresh_room_count()

    def get_config(self) -> dict:
        rooms = {a: [name for (area, name), var in self.rooms.items() if area == a and var.get()] for a in AREAS}
        releases = [dict(date=t["date"], start=t["start"], end=t["end"], areas=t["areas"])
                    for t in self.release_data.values() if t["kind"] == "single"]
        weekly = [dict(weekday=t["weekday"], start=t["start"], end=t["end"], areas=t["areas"])
                  for t in self.release_data.values() if t["kind"] == "weekly"]
        watches = [copy.deepcopy(t) for t in self.watch_data.values()]
        cfg = {
            "deployment_id": core.DEPLOYMENT_ID,
            "mode": MODE_OPTIONS[self.mode_var.get()], "dry_run": self.dry_var.get(), "rooms": rooms,
            "release_targets": releases, "weekly_release_rules": weekly, "watch_targets": watches,
            "release_open_at": self.setting_vars["release_open_at"].get().strip(),
            "prepare_before_seconds": number(self.setting_vars["prepare_before_seconds"].get(), "提前准备", integral=True),
            "stop_after_seconds": number(self.setting_vars["stop_after_seconds"].get(), "到点抢订截止", integral=True),
            "retry_seconds": number(self.setting_vars["retry_seconds"].get(), "失败重试间隔"),
            "popup_retry_seconds": number(self.setting_vars["popup_retry_seconds"].get(), "失败弹窗原页重试间隔"),
            "watch_interval_seconds": number(self.setting_vars["watch_interval_seconds"].get(), "候补间隔"),
            "watch_duration_minutes": number(self.setting_vars["watch_duration_minutes"].get(), "候补持续时长", nullable=True),
            "watch_guard_seconds": number(self.setting_vars["watch_guard_seconds"].get(), "候补让出页面时间", integral=True),
            "stop_at_meeting_start": self.watch_stop_var.get(),
            "auth_stagger_seconds": number(self.setting_vars["auth_stagger_seconds"].get(), "认证错峰间隔"),
            "verbose_logs": self.verbose_var.get(),
            "step_timing_logs": self.timing_var.get(),
        }
        # 运行前不修改全局配置；静态校验使用GUI当前勾选的真实会议室。
        if not any(rooms.values()):
            raise ValueError("请先到『会议室选择』勾选至少一间会议室")
        specs = core.resolve_rooms(rooms)
        core.parse_targets(releases, "到点抢订", rooms=specs)
        core.parse_targets(watches, "候补", rooms=specs)
        core.parse_weekly_rules(weekly, rooms=specs)
        if not cfg["release_open_at"] or len(cfg["release_open_at"]) != 8:
            raise ValueError("放号时间格式应为 HH:MM:SS")
        from datetime import time as _time
        _time.fromisoformat(cfg["release_open_at"])
        if not (0 <= cfg["prepare_before_seconds"] <= 86400):
            raise ValueError("提前准备秒数必须在 0～86400 范围内")
        if not (1 <= cfg["stop_after_seconds"] <= 86400):
            raise ValueError("抢订截止秒数必须在 1～86400 范围内")
        if not (0.5 <= cfg["retry_seconds"] <= 3600):
            raise ValueError("失败额外等待必须在 0.5～3600 秒内")
        if not (0.1 <= cfg["popup_retry_seconds"] <= 60):
            raise ValueError("失败弹窗原页重试间隔必须在 0.1～60 秒内")
        if not (1 <= cfg["watch_interval_seconds"] <= 86400):
            raise ValueError("候补间隔必须在 1～86400 秒内")
        if cfg["watch_duration_minutes"] is not None and not (0 < cfg["watch_duration_minutes"] <= 525600):
            raise ValueError("候补持续时间必须为正数或留空")
        if not (0 <= cfg["watch_guard_seconds"] <= 3600):
            raise ValueError("候补让出页面时间必须在 0～3600 秒内")
        if not (0 <= cfg["auth_stagger_seconds"] <= 120):
            raise ValueError("认证错峰间隔必须在 0～120 秒内")
        return cfg

    def show_release_plan(self):
        try:
            cfg = self.get_config()
            today, horizon_end, groups = compute_release_preview(cfg)
        except Exception as exc:
            messagebox.showerror("无法生成放号计划", str(exc), parent=self.root)
            return
        top = tk.Toplevel(self.root)
        top.title("放号计划预览")
        top.geometry("1120x620")
        top.minsize(980, 500)
        top.transient(self.root)
        top.configure(bg=BG)
        frame = ttk.Frame(top, style="Background.TFrame", padding=14)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="未来放号计划", style="Header.TLabel").pack(anchor="w", pady=(0, 2))
        ttk.Label(
            frame,
            text=(f"放号日期范围：{today.isoformat()} ～ {horizon_end.isoformat()}（未来21天）"
                  f"；{release_rule_summary()}；今日已过期批次会标为已截止。"),
            style="Muted.TLabel",
        ).pack(anchor="w", pady=(0, 10))

        # 一行对应一个“放号日 × 区域 × 实际预约目标”。
        # 不再只展示“任务概览”，直接把“哪天放号 → 抢哪天、哪个时段”列出来，
        # 同一放号日存在多个预约目标时分别展示，避免用户需要反推目标日期。
        tree = self._new_tree(
            frame,
            ("release_date", "booking_date", "time", "area", "rooms", "status"),
            ("放号日期", "抢订日期", "抢订时段", "区域", "会议室", "状态"),
            (125, 125, 120, 95, 500, 95),
            height=15,
        )

        for batch, status in groups:
            for group in batch.groups:
                # 同一 group 可能包含多个实际预约日期/时段；按目标分别列行。
                target_map = {}
                for target, room in group.tasks:
                    target_map.setdefault(target.key, {"target": target, "rooms": []})["rooms"].append(room.name)

                for item in sorted(target_map.values(), key=lambda x: (x["target"].day, x["target"].start)):
                    target = item["target"]
                    room_names = "、".join(item["rooms"])
                    tree.insert(
                        "",
                        "end",
                        values=(
                            batch.release_day.isoformat(),
                            target.day.isoformat(),
                            f"{target.start}–{target.end}",
                            group.area,
                            room_names,
                            status,
                        ),
                    )

        if not groups:
            ttk.Label(
                frame,
                text="未来21天没有待放号的任务。请检查单次和每周任务配置。",
                style="Muted.TLabel",
            ).pack(anchor="w", pady=10)

    def save_current(self):
        try:
            cfg = self.get_config()
            save_config(cfg)
            self.append_log("配置已保存。" + (" 正在运行的任务继续使用启动时的配置。" if self.is_running() else ""), 0)
        except Exception as exc:
            messagebox.showerror("无法保存配置", str(exc), parent=self.root)

    def start_run(self):
        if self.is_running():
            return
        try:
            core.require_authorized_account()
            cfg = self.get_config()
            mode = cfg["mode"]
            if mode == "release" and not (cfg["release_targets"] or cfg["weekly_release_rules"]):
                raise ValueError("到点抢订尚未配置任务，请在同一页添加单次或每周规则")
            if mode == "watch" and not cfg["watch_targets"]:
                raise ValueError("空闲候补没有配置日期和时段")
            if mode == "both" and not (cfg["release_targets"] or cfg["weekly_release_rules"] or cfg["watch_targets"]):
                raise ValueError("没有任何预约任务，请先增加单次、每周或候补目标")
            if not cfg["dry_run"] and mode != "list-rooms":
                if not messagebox.askyesno("确认真实提交", "已关闭模拟模式。\n程序将以你的账号正式提交会议室预订。\n\n请确认已核对预约日期、时段、楼栋和具体房间。是否继续？", icon="warning", parent=self.root):
                    return
            core.apply_gui_settings(cfg)
            save_config(cfg)
        except Exception as exc:
            messagebox.showerror("启动前检查失败", str(exc), parent=self.root)
            return
        self.stop_event.clear()
        self.worker_loop = None
        self.start_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.status_var.set("运行中 · 模拟" if cfg["dry_run"] else "运行中 · 正式")
        self.append_log(startup_summary(cfg), 0)
        self.thread = threading.Thread(target=self._run_worker, args=(mode,), daemon=True, name="meeting-booker")
        self.thread.start()

    def _run_worker(self, mode):
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        self.worker_loop = loop
        core.GUI_LOG_HANDLER = QueueHandler(self.queue)
        try:
            if self.stop_event.is_set():
                core.STOP_REQUESTED.set()
            loop.run_until_complete(core.main(mode))
        except Exception as exc:
            self.queue.put(("log", logging.ERROR, f"任务异常：{type(exc).__name__}: {exc}"))
        finally:
            try:
                loop.run_until_complete(loop.shutdown_asyncgens())
            except Exception:
                pass
            self.worker_loop = None
            loop.close()
            core.GUI_LOG_HANDLER = None
            self.queue.put(("done", 0, ""))

    def stop_run(self):
        if not self.is_running():
            return
        self.stop_event.set()
        self.status_var.set("正在安全停止")
        self.stop_button.configure(state="disabled")
        loop = self.worker_loop
        if loop is not None and not loop.is_closed():
            try:
                loop.call_soon_threadsafe(core.STOP_REQUESTED.set)
            except RuntimeError:
                pass
        self.append_log("已请求停止，将在安全检查点结束当前操作。", 0)

    def is_running(self):
        return self.thread is not None and self.thread.is_alive()

    def _drain_queue(self):
        try:
            while True:
                kind, level, text = self.queue.get_nowait()
                if kind == "done":
                    self.status_var.set("已停止 / 已完成")
                    self.start_button.configure(state="normal")
                    self.stop_button.configure(state="disabled")
                    self.append_log("本次任务已结束。", 0)
                    if self.close_after_stop:
                        self.root.destroy()
                        return
                else:
                    self.append_log(text, level)
        except queue.Empty:
            pass
        self.root.after(140, self._drain_queue)

    def append_log(self, text: str, level: int):
        tag = "error" if level >= logging.ERROR else "warning" if level >= logging.WARNING else "system" if level == 0 else None
        self.log_text.configure(state="normal")
        self.log_text.insert("end", text + "\n", (tag,) if tag else ())
        if int(self.log_text.index("end-1c").split(".")[0]) > 1000:
            self.log_text.delete("1.0", "180.0")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def _open_folder(self, path: Path):
        path.mkdir(parents=True, exist_ok=True)
        try:
            if sys.platform == "win32":
                os.startfile(str(path))
            else:
                messagebox.showinfo("目录", str(path), parent=self.root)
        except Exception as exc:
            messagebox.showerror("打开失败", str(exc), parent=self.root)

    def on_close(self):
        if self.is_running():
            if not messagebox.askyesno("任务正在运行", "当前任务仍在运行。\n是否先安全停止，再关闭程序？", parent=self.root):
                return
            self.close_after_stop = True
            self.stop_run()
        else:
            self.root.destroy()


def run_window_drag_self_test(app):
    """通过真实 Tk 事件覆盖拖动、释放、负坐标和最大化还原，不启动预约。"""
    root, surface = app.root, app.titlebar
    root.deiconify()
    root.state("normal")
    root.geometry("+120+100")
    root.update()

    event_time = 10000

    def emit(kind, pointer):
        nonlocal event_time
        # 每组事件独立计时，防止快速合成测试被 Tk 识别为双击。
        event_time += 1000
        surface.event_generate(kind, x=32, y=14, rootx=pointer[0], rooty=pointer[1],
                               state=0x100 if kind == "<B1-Motion>" else 0, time=event_time)
        root.update()

    def start():
        pointer = (surface.winfo_rootx() + 32, surface.winfo_rooty() + 14)
        origin = (root.winfo_x(), root.winfo_y())
        emit("<ButtonPress-1>", pointer)
        return pointer, origin

    pointer, origin = start()
    initial_size = (root.winfo_width(), root.winfo_height())
    emit("<ButtonRelease-1>", pointer)
    assert (root.winfo_x(), root.winfo_y()) == origin, "普通点击改变了窗口位置"
    for _ in range(3):
        pointer, origin = start()
        emit("<B1-Motion>", (pointer[0] + 45, pointer[1] + 32))
        assert (root.winfo_x(), root.winfo_y()) == (origin[0] + 45, origin[1] + 32), "拖动偏移不正确"
        assert (root.winfo_width(), root.winfo_height()) == initial_size, "首次或重复拖动改变了窗口尺寸"
        emit("<B1-Motion>", (pointer[0] + 65, pointer[1] + 40))
        target = (origin[0] + 65, origin[1] + 40)
        assert (root.winfo_x(), root.winfo_y()) == target, "连续拖动发生累计偏移"
        emit("<ButtonRelease-1>", (pointer[0] + 65, pointer[1] + 40))
        assert app._window_drag is None, "释放鼠标后拖动状态未清除"
        emit("<B1-Motion>", (pointer[0] + 100, pointer[1] + 100))
        assert (root.winfo_x(), root.winfo_y()) == target, "释放鼠标后仍在移动"
    pointer, origin = start()
    target = (-120, 80)
    moved = (pointer[0] + target[0] - origin[0], pointer[1] + target[1] - origin[1])
    emit("<B1-Motion>", moved)
    assert (root.winfo_x(), root.winfo_y()) == target, "左侧副屏负坐标被解释成右侧边距"
    emit("<ButtonRelease-1>", moved)
    root.geometry("+120+100")
    root.update()
    if sys.platform == "win32":
        normal_size = (root.winfo_width(), root.winfo_height())
        app._toggle_maximize()
        root.update()
        assert app._is_maximized(), "最大化按钮未生效"
        pointer, _ = start()
        assert app._is_maximized(), "最大化窗口点击后提前还原"
        moved = (pointer[0] + 35, pointer[1] + 45)
        emit("<B1-Motion>", moved)
        assert not app._is_maximized(), "最大化拖动未还原"
        assert (root.winfo_width(), root.winfo_height()) == normal_size, "还原后窗口尺寸丢失"
        assert abs((moved[0] - root.winfo_rootx()) / root.winfo_width()
                   - app._window_drag["ratio"]) < 0.01, "还原后鼠标横向位置跳动"
        emit("<ButtonRelease-1>", moved)
        # 自绘窗口的缩放、任务栏恢复和最大化恢复也必须保持同一套尺寸。
        grip = app._resize_grip
        before_resize = (root.winfo_width(), root.winfo_height())
        grip_pointer = (grip.winfo_rootx() + 5, grip.winfo_rooty() + 5)
        for kind, offset in (("<ButtonPress-1>", (0, 0)),
                             ("<B1-Motion>", (32, 24)),
                             ("<ButtonRelease-1>", (32, 24))):
            event_time += 1000
            grip.event_generate(kind, x=5 + offset[0], y=5 + offset[1],
                                rootx=grip_pointer[0] + offset[0],
                                rooty=grip_pointer[1] + offset[1],
                                state=0x100 if kind == "<B1-Motion>" else 0,
                                time=event_time)
            root.update()
        resized = (root.winfo_width(), root.winfo_height())
        assert resized == (before_resize[0] + 32, before_resize[1] + 24), "右下角缩放手柄未生效"
        for maximized in (False, True):
            if maximized:
                app._toggle_maximize()
                root.update()
            wanted = (root.winfo_width(), root.winfo_height())
            app._minimize_window()
            root.update()
            assert root.state() == "iconic", "最小化未生效"
            root.deiconify()
            root.update()
            assert root.overrideredirect(), "恢复后出现重复系统标题栏"
            assert (root.winfo_width(), root.winfo_height()) == wanted, "最小化恢复后尺寸改变"
            assert app._is_maximized() == maximized, "恢复后最大化状态丢失"
        app._toggle_maximize()
        root.update()
        assert (root.winfo_width(), root.winfo_height()) == resized, "恢复后丢失手柄调整的尺寸"
        # 无系统标题栏时也必须能正常聚焦并输入日期。
        def descendants(widget):
            for child in widget.winfo_children():
                yield child
                yield from descendants(child)
        entry = next(widget for widget in descendants(root)
                     if isinstance(widget, ttk.Entry)
                     and str(widget.cget("textvariable")) == str(app.release_date))
        saved_date = app.release_date.get()
        entry.focus_force()
        entry.selection_range(0, "end")
        entry.event_generate("<KeyPress>", keysym="1")
        entry.event_generate("<KeyRelease>", keysym="1")
        root.update()
        assert entry.get() == "1", "窗口恢复后日期输入框无法接收键盘输入"
        app.release_date.set(saved_date)


def run_self_test(marker: Path):
    # 仅验证配套签名及加密依赖是否进入程序，不授予当前Windows用户使用权。
    # 正常入口、开始按钮和核心入口仍各自执行OS账号门禁。
    import account_auth
    raw = (account_auth.application_directory() / account_auth.LICENSE_FILENAME).read_bytes()
    assert 0 < len(raw) <= account_auth.MAX_LICENSE_BYTES, "配套授权文件大小异常"
    document = json.loads(raw.decode("utf-8"))
    first_account = document["payload"]["accounts"][0]
    assert account_auth.verify_document(document, first_account) == first_account, "配套授权验签失败"
    """Windows 打包后做真实GUI与核心参数一致性自检，不进入公司网络。"""
    test_config = copy.deepcopy(DEFAULT_CONFIG)
    test_config["rooms"] = {area: [next(iter(room_map))]
                            for area, room_map in core.AREA_ROOM_ROWS.items()}
    primary_area, weekly_area = AREAS[0], AREAS[-1]
    test_config["release_targets"] = [{"date": "2026-10-21", "start": "14:00", "end": "16:00", "areas": [primary_area]}]
    test_config["weekly_release_rules"] = [{"weekday": 3, "start": "10:00", "end": "12:00", "areas": [weekly_area]}]
    test_config["watch_targets"] = [{"date": "2026-10-19", "start": "10:00", "end": "12:00", "areas": [primary_area]}]
    test_config.update({"release_open_at": "09:15:00", "prepare_before_seconds": 215,
                        "stop_after_seconds": 185, "retry_seconds": 3.5,
                        "popup_retry_seconds": 0.45,
                        "watch_interval_seconds": 42, "watch_duration_minutes": 60,
                        "watch_guard_seconds": 95, "auth_stagger_seconds": 1.8,
                        "verbose_logs": True, "step_timing_logs": True,
                        "stop_at_meeting_start": False})
    root = tk.Tk()
    root.withdraw()
    app = BookingGUI(root, config_override=test_config)
    root.update_idletasks()
    # 保证四个标签的文字字号和导航占位尺寸切换前后相同。
    nav_before = {k: (button.title, button.winfo_reqwidth(), button.winfo_reqheight())
                  for k, (button, _) in app.tab_buttons.items()}
    for key in ("watch", "rooms", "advanced", "release"):
        app._select_tab(key)
        root.update_idletasks()
        assert nav_before == {k: (button.title, button.winfo_reqwidth(), button.winfo_reqheight())
                              for k, (button, _) in app.tab_buttons.items()}, "标签切换导致尺寸变化"
    got = app.get_config()
    assert got["release_targets"] == test_config["release_targets"]
    assert got["weekly_release_rules"] == test_config["weekly_release_rules"]
    assert got["watch_targets"] == test_config["watch_targets"]
    assert got["rooms"] == test_config["rooms"]
    for key in ("release_open_at", "prepare_before_seconds", "stop_after_seconds", "retry_seconds",
                "popup_retry_seconds", "watch_interval_seconds", "watch_duration_minutes", "watch_guard_seconds",
                "auth_stagger_seconds", "verbose_logs", "step_timing_logs", "stop_at_meeting_start"):
        assert got[key] == test_config[key], f"界面与核心参数未同步：{key}"
    specs = core.resolve_rooms(got["rooms"])
    assert {(room.area, room.name, room.row) for room in specs} == {
        (area, name, core.AREA_ROOM_ROWS[area][name])
        for area, names in test_config["rooms"].items() for name in names
    }, "界面勾选未按管理员会议室名单解析"
    assert core.expand_weekly_targets(
        core.parse_weekly_rules(got["weekly_release_rules"], rooms=specs), date(2026, 10, 15), 21)
    core.apply_gui_settings(got)
    assert core.ROOM_SELECTION == got["rooms"]
    assert core.RELEASE_TARGETS == got["release_targets"]
    assert core.WEEKLY_RELEASE_RULES == got["weekly_release_rules"]
    assert core.WATCH_TARGETS == got["watch_targets"]
    for attr, key in (("RELEASE_OPEN_AT", "release_open_at"),
                      ("RELEASE_PREPARE_BEFORE_SECONDS", "prepare_before_seconds"),
                      ("RELEASE_STOP_AFTER_SECONDS", "stop_after_seconds"),
                      ("RELEASE_RETRY_SECONDS", "retry_seconds"),
                      ("POPUP_RETRY_SECONDS", "popup_retry_seconds"),
                      ("WATCH_INTERVAL_SECONDS", "watch_interval_seconds"),
                      ("WATCH_DURATION_MINUTES", "watch_duration_minutes"),
                      ("WATCH_PREP_GUARD_SECONDS", "watch_guard_seconds"),
                      ("WATCH_STOP_AT_MEETING_START", "stop_at_meeting_start"),
                      ("AUTH_STAGGER_SECONDS", "auth_stagger_seconds"),
                      ("VERBOSE_LOGS", "verbose_logs"),
                      ("LOG_PREPARE_STEP_TIMINGS", "step_timing_logs")):
        assert getattr(core, attr) == got[key], f"核心参数未同步：{attr}"
    run_window_drag_self_test(app)
    root.destroy()
    marker.write_text("gui-core-config-ok", encoding="ascii")


def check_account_to_file(marker: Path) -> int:
    """离线排查授权的入口；仅检查，不创建工作窗口或启动预约。"""
    try:
        account = core.require_authorized_account()
        result = {"authorized": True, "account": account}
        status = 0
    except core.AuthorizationError as exc:
        result = {"authorized": False, "error": str(exc)}
        status = 2
    marker.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return status


def main():
    if sys.platform == "win32":
        asyncio.set_event_loop_policy(asyncio.WindowsProactorEventLoopPolicy())
    if len(sys.argv) == 3 and sys.argv[1] == "--self-test":
        run_self_test(Path(sys.argv[2]))
        return 0
    if len(sys.argv) == 3 and sys.argv[1] == "--check-authorization":
        return check_account_to_file(Path(sys.argv[2]))
    from gui_diagnostics import GuiDiagnostics
    diagnostics = None
    try:
        try:
            diagnostics = GuiDiagnostics(core.LOG_DIR)
        except OSError:
            pass  # 诊断目录不可写不妨碍启动时显示授权错误。
        root = tk.Tk()
        root.withdraw()
        if diagnostics is not None:
            diagnostics.attach(root)
        try:
            core.require_authorized_account()
        except core.AuthorizationError as exc:
            messagebox.showerror("账号未获授权", str(exc), parent=root)
            root.destroy()
            return 2
        BookingGUI(root)
        root.deiconify()
        root.mainloop()
        return 0
    except Exception:
        if diagnostics is not None:
            diagnostics.report(*sys.exc_info())
            return 1
        raise
    finally:
        if diagnostics is not None:
            diagnostics.close()


if __name__ == "__main__":
    raise SystemExit(main())
