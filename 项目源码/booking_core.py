# -*- coding: utf-8 -*-
"""会议室自动抢订（动态选房格子核验与重绘恢复；启动前核验Windows账号授权）

使用前：
  pip install playwright
  使用本机已安装的 Google Chrome（不需要 playwright install chromium）
  BOOKING_URL 已填写；请检查日期与时段参数，先 DRY_RUN=True 验证。
  python 会议室预定.py --mode release  # 每个RELEASE_TARGETS条目单独选areas，按对应区域放号日执行
  python 会议室预定.py --mode watch    # 人为指定的日期/时段候补
  python 会议室预定.py --mode both     # 两种模式复用工作标签，到点抢订优先
  python 会议室预定.py --mode list-rooms  # 查看当前源码配置的会议室列表，不会订房

管理员在 deployment_profiles.py 维护 F5/F5-1A、H7 两套资料，打包时选择。
源码运行默认F5；每个EXE只编译选中的配置。名单必须包含筛选后的全部房间。
筛选/选格子/确认均在同一个「添加会议室」弹窗；先复用已有筛选条件，只在需要时切换楼栋并点击查询。
F5-1A内部编码是F5-1F；H7完整房名使用“溪村-H7-”前缀。
房间编号和顺序来自用户提供的当前页面，可能变化；
正式预订前请运行 --mode list-rooms 核对。
不得绕过公司的频率限制、验证码或预约权限。最终提交无锁，多个页面可能在确认成功前同时提交，存在重复订房风险。
"""

from __future__ import annotations

import argparse
import os
import json
import asyncio
import logging
import sys
import time as monotonic_time
import re
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta, timezone
from pathlib import Path
from typing import AsyncIterator, Callable, Literal

from account_auth import AuthorizationError, require_authorized_account
from booking_profile import PROFILE as DEPLOYMENT_PROFILE
from date_picker import DatePickerStructureError, select_booking_date

from playwright.async_api import (
    BrowserContext, ElementHandle, Locator, Page, Playwright, TimeoutError as PlaywrightTimeout,
    async_playwright,
)

# ==================== 只需要优先修改这里 ====================
BOOKING_URL = "https://inner.welink.huawei.com/meetingroom/#/"  # 用户提供的会议室入口
DRY_RUN = True      # 首次验证保持 True；验证点击与状态无误后设置 False
COLOR_CONSOLE_LOGS = True  # 在PyCharm/支持ANSI颜色的终端，将WARNING显示为黄、ERROR显示为红
HEADLESS = False    # 公司 SSO/界面建议可见模式
BROWSER_CHANNEL = "chrome"  # 使用电脑本地安装的 Chrome，不再依赖 Playwright 下载的 Chromium
# 每次导航后自动激活并等待表单初始化；激活阶段串行，后续填表并行。
ACTIVATE_TAB_DURING_PREPARE = True
# 按每个页面环节统计耗时，方便验证是否仍存在后台标签导致的固定4秒等待。
LOG_PREPARE_STEP_TIMINGS = False
# False: 界面/控制台只显示关键节点、结果和异常；True: 同时显示详细步骤。
# 磁盘日志始终保留详细步骤与原始错误，便于排查。
VERBOSE_LOGS = False

# 管理员在 deployment_profiles.py 维护两套区域资料，在打包时选择。
# booking_profile.py 是构建器生成的单区域副本；EXE 不读取外置区域配置。
DEPLOYMENT_ID = DEPLOYMENT_PROFILE["id"]
DEPLOYMENT_LABEL = DEPLOYMENT_PROFILE["label"]
DEFAULT_RELEASE_OPEN_AT = DEPLOYMENT_PROFILE["release_open_at"]
ROOM_FULL_NAME_PATTERN = DEPLOYMENT_PROFILE["full_name_pattern"]
ROOM_SHORT_NAME_PATTERN = DEPLOYMENT_PROFILE["short_name_pattern"]
ROOM_CATALOG = DEPLOYMENT_PROFILE["room_catalog"]


def validate_room_catalog(catalog: dict) -> None:
    """尽早发现源码名单错误，避免带着错行配置构建和运行。"""
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError("ROOM_CATALOG 必须包含至少一个区域")
    for area, config in catalog.items():
        if not isinstance(area, str) or not area.strip() or not isinstance(config, dict):
            raise ValueError("ROOM_CATALOG 区域配置无效")
        if not isinstance(config.get("label"), str) or not config["label"].strip():
            raise ValueError(f"{area} 缺少网页区域名称 label")
        days = config.get("advance_days")
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 365:
            raise ValueError(f"{area} 的 advance_days 必须为0~365的整数")
        rows = config.get("rooms")
        if not isinstance(rows, dict) or not rows:
            raise ValueError(f"{area} 必须配置房号与行号")
        for name, row in rows.items():
            if not isinstance(name, str) or not name.strip() or name != name.strip():
                raise ValueError(f"{area} 房号必须为非空名称，不能带首尾空格")
            if isinstance(row, bool) or not isinstance(row, int) or not 1 <= row <= 50:
                raise ValueError(f"{area}:{name} 行号必须为1~50的整数")
        if len(set(rows.values())) != len(rows):
            raise ValueError(f"{area} 不同房号不能配置相同行号")


validate_room_catalog(ROOM_CATALOG)
AREA_ROOM_ROWS = {area: dict(config["rooms"]) for area, config in ROOM_CATALOG.items()}
AREA_LABELS = {area: config["label"] for area, config in ROOM_CATALOG.items()}
AREA_ADVANCE_DAYS = {area: config["advance_days"] for area, config in ROOM_CATALOG.items()}
AREA_EXPECTED_SHORT_NAMES = {area: tuple(rows) for area, rows in AREA_ROOM_ROWS.items()}
# 兼容已有诊断及测试引用；无需再维护第二份房号。
F5_ROOM_ROWS = AREA_ROOM_ROWS.get("F5", {})
F5_1A_ROOM_ROWS = AREA_ROOM_ROWS.get("F5-1A", {})


def default_room_selection() -> dict[str, list[str]]:
    return {area: list(rows) for area, rows in AREA_ROOM_ROWS.items()}


ROOM_SELECTION = default_room_selection()
# 当 F5-1A 通过行号选房时，先运行 list-rooms 并人工核实对应关系；
# 核实后才设为 True，避免正式预约时预订错误的房间。
F5_1A_ROW_MAPPING_VERIFIED = False
# 必须显式筛选“科研楼F5”，不能把默认页展示的13间F5-1A误当成F5。
F5_USE_FILTER = True
F5_FILTER_ROW_MAPPING_VERIFIED = False  # 保留兼容旧配置；由页面实际房名与行号校验，不再要求手动开启

@dataclass(frozen=True)
class RoomSpec:
    area: str
    name: str
    row: int


def resolve_rooms(selection: dict | None = None) -> list[RoomSpec]:
    """只允许勾选内置房号；外部配置没有自定义房号或按裸行号选房的入口。"""
    selection = ROOM_SELECTION if selection is None else selection
    if not isinstance(selection, dict):
        raise ValueError("会议室选择配置必须是对象")
    result: list[RoomSpec] = []
    for area, selected in selection.items():
        if area not in AREA_ROOM_ROWS:
            raise ValueError(f"未知会议室区域：{area}；仅支持 {list(AREA_ROOM_ROWS)}")
        if not isinstance(selected, list):
            raise ValueError(f"ROOM_SELECTION[{area}] 必须是列表")
        for item in selected:
            if not isinstance(item, str) or item not in AREA_ROOM_ROWS[area]:
                raise ValueError(f"{area} 只能勾选程序内置的会议室，不能新增房号或指定行号：{item!r}")
            room = RoomSpec(area, item, AREA_ROOM_ROWS[area][item])
            if room in result or any(r.area == room.area and r.row == room.row for r in result):
                raise ValueError(f"重复配置会议室或相同行号：{room}")
            result.append(room)
    if not result:
        raise ValueError("ROOM_SELECTION 没有选中任何会议室")
    return result


ROOMS = resolve_rooms()

# ========== 每个预约目标各自选择要抢的楼栋 ==========
# ROOM_SELECTION 是“全局候选房间池”，只规定各楼栋哪些具体房间可以参与；
# 每条 RELEASE_TARGETS / WATCH_TARGETS 另用 areas 指定本目标实际参与哪些楼栋。
# 注意：选择某个楼栋，必须先在上面的 ROOM_SELECTION 中为该楼栋选择至少一间房。
# 同一日期、同一时段只能写一条目标；要同时选两个楼栋，填写 areas=["F5", "F5-1A"]。
# 预约日期如果是4天后：F5的7天放号日已经过去，应加入下方候补；
# F5-1A的3天放号日尚未到，可加入到点抢订。到点抢订不自动改成候补。
RELEASE_TARGETS = []  # 在界面配置实际预约目标；不携带其他区域或已过期的示例。
# 每周固定预约：星期一=1 ... 星期日=7；每个时段分别指定区域。
# 不必每周修改日期；脚本保持运行，滚动规划未来的放号日。
# 与 RELEASE_TARGETS 可同时使用；相同时段会自动合并区域避免重复预订。
WEEKLY_RELEASE_RULES = [
    # {"weekday": 1, "start": "10:00", "end": "12:00", "areas": ["F5"]},
    # {"weekday": 3, "start": "14:00", "end": "16:00", "areas": ["F5", "F5-1A"]},
]
WEEKLY_LOOKAHEAD_DAYS = 21  # 滚动生成未来21天目标；至少大于最长提前放号天数

# 只维护一个绝对时刻，其他节点全部相对放号时间计算。
RELEASE_OPEN_AT = DEFAULT_RELEASE_OPEN_AT  # 所选区域每天放号基准时刻（北京时间）
RELEASE_PREPARE_BEFORE_SECONDS = 300   # 放号前300秒启动页面准备
RELEASE_STOP_AFTER_SECONDS = 300      # 放号后300秒停止抢订；提交前再次检查截止
RELEASE_RETRY_SECONDS = 0.9           # 页面操作失败后额外等待秒数

# 模式2：人工候补也是每个目标单独选区域；与到点抢订互不强制关联。
WATCH_TARGETS = []
WATCH_INTERVAL_SECONDS = 30         # 相邻两轮开始的目标间隔(秒)，包含页面加载和填表时间；不叠加执行
WATCH_DURATION_MINUTES = 120       # 候补总运行时长（分钟），从候补启动计时；设为 None 表示不限制总时长
# 候补时长包含刷新页面、筛选、等待，以及为08:30到点抢订让出工作页面的时间。
# 即使设置 None，WATCH_STOP_AT_MEETING_START=True 仍会在会议开始时结束该目标。
WATCH_PREP_GUARD_SECONDS = 120  # 到点抢订准备前2分钟暂停候补，让工作页优先用于到点抢订
WATCH_STOP_AT_MEETING_START = True  # 到会议开始时间自动停止候补

# 登录：独立、持久化的浏览器用户目录；首次可能需手动 SSO，后续尝试复用。
# 使用固定的本机用户数据目录，后续如打包EXE也能保留登录状态。
# 放在当前Windows用户的LocalAppData，更新EXE不会清除登录或日志。
APP_DATA_DIR = Path(os.environ.get("LOCALAPPDATA") or (Path.home() / ".local" / "share")) / "MeetingRoomAutoBooking"
# F5 保留历史配置；H7 独立保存任务、时间、日志及登录资料，避免两版相互覆盖。
if DEPLOYMENT_ID != "F5":
    APP_DATA_DIR = APP_DATA_DIR / DEPLOYMENT_ID
PROFILE_DIR = APP_DATA_DIR / "chrome_profile"
LOG_DIR = APP_DATA_DIR / "logs"
GUI_LOG_HANDLER: logging.Handler | None = None
STOP_REQUESTED = asyncio.Event()  # GUI中“停止”请求；不直接取消Playwright中的点击操作
NAVIGATION_TIMEOUT_MS = 15000
ACTION_TIMEOUT_MS = 5000  # 公司内网控件有时加载/重绘较慢，避免3.5秒误判
SUCCESS_TIMEOUT_MS = 10000  # 首次点击最终预定按钮后最多等待成功提示10秒
FINAL_BOOK_EXTRA_RETRIES = 3  # 首次提交超时后，若按钮仍可点击，当前页面最多再点3次
FINAL_BOOK_EXTRA_WAIT_SECONDS = 0.9  # 没有任何明确弹窗时，保留原版短重试的等待时间
POPUP_RETRY_SECONDS = 0.2         # 已关闭失败弹窗后、原页重复点击预定前的额外等待；GUI可配置
OTHER_FAILURE_EXTRA_RETRIES = 10  # 非提前天数错误：额外原页面重试10次（不含首次提交）
DROPDOWN_RETRY_TIMES = 2
DROPDOWN_LOAD_SECONDS = 4.5  # 下拉展开后等待异步选项加载，避免立即count()误判
AUTH_WAIT_SECONDS = 60  # 首个页面完成SSO认证的最长等待秒数
AUTH_STAGGER_SECONDS = 1.5  # 新标签首次认证访问错开秒数；认证完成后表单全部并发
# 首个标签先完成认证；其他新标签错峰完成首次认证访问，避免并发SSO触发403。
# 认证后各标签并发填写；表单操作没有全局串行锁。
# 最终提交不加互斥锁；每个工作页独立并发，只有真实成功后共享done停止其余页面。
AUTH_BLOCKED = asyncio.Event()
CONFIG_BLOCKED = asyncio.Event()  # 关键表单控件定位结构性失败时停止无效刷新
DRIVER_LOST = asyncio.Event()  # Chrome/Playwright 连接断开后立即停止，无效请求不再重试
SELECTION_DIAGNOSTICS_RECORDED: set[tuple[str, str, str]] = set()
GRID_SELECTOR_BLOCKED: set[tuple[str, str]] = set()  # (目标时段, 房间)：只跳过同一时段已确定失配的定位

# 录制中开始时间下拉框的入口，如网页版本变化，改这里。
START_TIME_DROPDOWN_RE = r"下拉框[,，]\s*当前值\s*\d{1,2}:"
# 根据已录制的时长框 id=ev_slt_10002_ev_se 推断：开始时间框可能是 10001。
# 仅在唯一且可见时使用；不确定时截图停止，避免错选。
START_TIME_DROPDOWN_CSS = "#ev_slt_10001_ev_se .ev_select_drop_icon"
DURATION_DROPDOWN = "#ev_slt_10002_ev_se > .ev_select_div > .ev_input > .ev_select_drop_icon"
CALENDAR_ICON = ".ev_datepicker_icon_calendar"
MONTH_TRIGGER = ".ev_calendar_trigger"
ADD_ROOM_BUTTON = "图标 添加会议室"
CONFIRM_BUTTON = "确认"
CANCEL_BUTTON = "取消"
FINAL_BOOK_BUTTON = "预定"
SUCCESS_TEXT = "会议室预定成功"
# 服务端未到放号时间时的明确业务拒绝；与“按钮不可点”区别处理。
ADVANCE_LIMIT_ERROR_RE = re.compile(r"超过\s*最大可提前预约天数")
ROOM_OCCUPIED_ERROR_RE = re.compile(r"(?:已被预[订定]|已被预约|已占用|已被占用|时段冲突|该时间段已被|会议室已满|时间段已满)")
OTHER_BOOKING_ERROR_RE = re.compile(
    r"(?:预[订定]失败|预约失败|提交失败|操作失败|请求失败|系统繁忙|预约异常|预订异常|重试|错误|稍后再试|请联系管理员|服务暂不可用|暂时无法|未成功)",
    re.IGNORECASE,
)
# 录制控件位于同一个选房弹窗；楼栋为主要切换条件，其他三个仅在楼栋缺选项时按需设置
FILTER_TYPE = "#ev_slt_10012_ev_se .ev_select_drop_icon"
FILTER_CITY = "#ev_slt_10013_ev_se .ev_select_drop_icon"
FILTER_ZONE = "#ev_slt_10014_ev_se .ev_select_drop_icon"
FILTER_BUILDING = "#ev_slt_10015_ev_se .ev_select_drop_icon"
GRID_WAIT_SECONDS = 3.5
FILTER_RENDER_TIMEOUT_MS = 7000  # 选房弹窗及区域筛选控件异步加载可能超过5秒
FILTER_STATE_POLL_SECONDS = 0.25  # 楼栋控件与默认房间名单可能不同步，目标区域不匹配时持续等待控件出现
FILTER_OPTION_TIMEOUT_MS = 10000  # 下拉选项出现的等待时长
AREA_RESULT_WAIT_SECONDS = 6.0

# ==================== 核心逻辑（通常无需修改） ====================
CN_MONTHS = ("一月", "二月", "三月", "四月", "五月", "六月", "七月", "八月", "九月", "十月", "十一月", "十二月")
CN_TZ = timezone(timedelta(hours=8))
LAST_DEBUG_CAPTURE: dict[str, datetime] = {}


def is_driver_disconnected(exc: BaseException) -> bool:
    """只识别明确的浏览器/驱动断连；普通页面超时不应触发停止。"""
    message = str(exc).lower()
    signals = (
        "connection closed while reading from the driver",
        "connection closed while writing to the driver",
        "target page, context or browser has been closed",
        "browser has been closed",
        "connection closed",
        "event loop is closed",
    )
    return any(key in message for key in signals)



def now_cn() -> datetime:
    return datetime.now(CN_TZ)


def at_cn(day: date, clock: str) -> datetime:
    return datetime.combine(day, time.fromisoformat(clock), tzinfo=CN_TZ)


def parse_clock(s: str) -> int:
    parts = s.split(":")
    if len(parts) != 2:
        raise ValueError(f"时间格式必须是 HH:MM：{s}")
    h, m = map(int, parts)
    if not 0 <= h < 24 or m not in (0, 30):
        raise ValueError(f"时间只能填写整点或半点：{s}")
    return 60 * h + m


def cell_columns(start: str, end: str) -> tuple[int, int]:
    """根据录制推导：09:00 对应 19，12:00 对应 25，14:00 对应 28（结束格包含）。"""
    s, e = parse_clock(start), parse_clock(end)
    if not (9 * 60 <= s < e <= 24 * 60):
        raise ValueError(f"时间必须在 09:00~24:00 且开始早于结束：{start}-{end}")
    first = 25 + (s - 12 * 60) // 30
    last = 25 + (e - 12 * 60) // 30 - 1
    return first, last


@dataclass(frozen=True)
class Target:
    day: date
    start: str
    end: str
    # None仅用于兼容旧版直接构造Target的内部调用；配置文件必须显式填写areas。
    areas: tuple[str, ...] | None = None

    def __post_init__(self):
        # 网页以半小时网格选取时段；开始、结束及最短时长由此统一校验。
        cell_columns(self.start, self.end)

    def includes_room(self, room: RoomSpec) -> bool:
        """只有本目标选择的区域才参与预约；不再与全局所有区域直接相乘。"""
        return self.areas is None or room.area in self.areas

    @property
    def key(self) -> str:
        return f"{self.day.isoformat()} {self.start}-{self.end}"

    @property
    def start_dt(self) -> datetime:
        return at_cn(self.day, self.start)



def parse_targets(raw: list[dict], label: str, *, rooms: list[RoomSpec] | None = None) -> list[Target]:
    """每个时段显式选择areas；校验房间池中确实存在对应楼栋的房间。"""
    if not isinstance(raw, list):
        raise ValueError(f"{label} 必须是列表")
    selected_rooms = ROOMS if rooms is None else rooms
    available_areas = {room.area for room in selected_rooms}
    seen: set[str] = set()
    result: list[Target] = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"{label} 中每个目标必须是字典：{item!r}")
        if "areas" not in item:
            raise ValueError(f"{label} 目标 {item!r} 缺少 areas，请从 {list(AREA_ROOM_ROWS)} 中明确选择参与区域")
        raw_areas = item["areas"]
        if not isinstance(raw_areas, list) or not raw_areas or any(not isinstance(a, str) for a in raw_areas):
            raise ValueError(f"{label} areas 必须是非空的区域名称列表：{item!r}")
        if len(set(raw_areas)) != len(raw_areas):
            raise ValueError(f"{label} areas 不可包含重复区域：{item!r}")
        unknown = set(raw_areas) - set(AREA_ADVANCE_DAYS)
        if unknown:
            raise ValueError(f"{label} 包含未知区域：{sorted(unknown)}；仅支持 {list(AREA_ADVANCE_DAYS)}")
        no_rooms = set(raw_areas) - available_areas
        if no_rooms:
            raise ValueError(f"{label} 目标 {item.get('date')} {item.get('start')}-{item.get('end')} 选中了 {sorted(no_rooms)}，但ROOM_SELECTION中没有这些区域的会议室")
        try:
            target = Target(date.fromisoformat(item["date"]), item["start"], item["end"], tuple(raw_areas))
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"{label} 配置错误：{item}：{exc}") from exc
        if target.key in seen:
            raise ValueError(f"{label} 重复配置了 {target.key}；同一目标需合并成一个条目，在 areas 中选择所有区域")
        seen.add(target.key)
        result.append(target)
    return result


def parse_weekly_rules(raw: list[dict], *, rooms: list[RoomSpec] | None = None) -> list[dict]:
    """校验周期规则。星期数遵循 ISO：1=周一、7=周日。"""
    if not isinstance(raw, list):
        raise ValueError("WEEKLY_RELEASE_RULES 必须是列表")
    selected_rooms = ROOMS if rooms is None else rooms
    available_areas = {r.area for r in selected_rooms}
    seen = set()
    result = []
    for item in raw:
        if not isinstance(item, dict):
            raise ValueError(f"每周规则须为字典：{item!r}")
        wd = item.get("weekday")
        if isinstance(wd, bool) or not isinstance(wd, int) or not 1 <= wd <= 7:
            raise ValueError(f"每周星期必须是整数1~7（周一到周日）：{item!r}")
        areas = item.get("areas")
        if not isinstance(areas, list) or not areas or len(areas) != len(set(areas)):
            raise ValueError(f"每周规则需要非空且不重复的 areas：{item!r}")
        if any(a not in AREA_ADVANCE_DAYS for a in areas):
            raise ValueError(f"每周规则使用了未知区域：{areas!r}")
        if set(areas) - available_areas:
            raise ValueError(f"每周规则选择了未配置具体会议室的区域：{set(areas)-available_areas}")
        start, end = item.get("start"), item.get("end")
        if not isinstance(start, str) or not isinstance(end, str):
            raise ValueError(f"每周规则需要开始和结束时间：{item!r}")
        Target(date(2026, 1, 5), start, end, tuple(areas))
        key = (wd, start, end)
        if key in seen:
            raise ValueError(f"星期{wd} {start}-{end} 重复，请合并为一条规则并选择所需区域")
        seen.add(key)
        result.append({"weekday": wd, "start": start, "end": end, "areas": list(areas)})
    return result


def expand_weekly_targets(rules: list[dict], from_day: date,
                          horizon_days: int = 21) -> list[Target]:
    """纯计算：从当天起滚动生成每周目标，放号仍由区域规则另行计算。"""
    if isinstance(horizon_days, bool) or not isinstance(horizon_days, int) or horizon_days < 8:
        raise ValueError("WEEKLY_LOOKAHEAD_DAYS 至少为8天")
    targets = []
    for offset in range(horizon_days + 1):
        day = from_day + timedelta(days=offset)
        for rule in rules:
            if day.isoweekday() == rule["weekday"]:
                targets.append(Target(day, rule["start"], rule["end"], tuple(rule["areas"])))
    return targets


def merge_release_targets(targets: list[Target]) -> list[Target]:
    """手动与每周计划同日期同时间时取区域并集，确保只建一份共享任务。"""
    merged: dict[str, Target] = {}
    for target in targets:
        old = merged.get(target.key)
        if old is None:
            merged[target.key] = target
        else:
            areas = tuple(dict.fromkeys((*old.areas, *target.areas)))
            merged[target.key] = Target(target.day, target.start, target.end, areas)
    return list(merged.values())


@dataclass(frozen=True)
class ReleaseGroup:
    """同一放号日、相同提前天数、同一区域的一组预约任务。"""
    release_day: date
    advance_days: int
    area: str
    tasks: tuple[tuple[Target, RoomSpec], ...]

    @property
    def target_count(self) -> int:
        return len({target.key for target, _ in self.tasks})

    @property
    def room_count(self) -> int:
        return len({(room.area, room.name) for _, room in self.tasks})


@dataclass(frozen=True)
class ReleaseDayBatch:
    """同一天真正开放的全部任务共用放号时钟，并发准备相应页面。"""
    release_day: date
    groups: tuple[ReleaseGroup, ...]

    @property
    def tasks(self) -> tuple[tuple[Target, RoomSpec], ...]:
        return tuple(pair for group in self.groups for pair in group.tasks)

    @property
    def max_pages(self) -> int:
        # 仅统计这个放号日的真实任务；已完成的目标在运行时还会进一步过滤。
        return len(self.tasks)

    @property
    def target_count(self) -> int:
        return len({target.key for target, _ in self.tasks})


def build_release_batches(targets: list[Target], rooms: list[RoomSpec],
                          advance_days: dict[str, int] | None = None) -> list[ReleaseDayBatch]:
    """放号计划：先按区域规则算实际放号日，再按(放号日, 提前天数, 区域)分组。

    同一个预约日期，在 F5 和 F5-1A 会落到不同的放号日；
    若不同预约日期恰巧在同一日放号，会合并到同一个批次，同时抢订。
    纯计算函数，不打开页面、不做网络请求，可供GUI预览与回归测试复用。
    """
    rules = AREA_ADVANCE_DAYS if advance_days is None else advance_days
    grouped: dict[tuple[date, int, str], list[tuple[Target, RoomSpec]]] = {}
    dedup: set[tuple[str, str, str]] = set()
    for target in targets:
        # 先按当前目标的 areas 过滤房间池，再按区域放号日分组。
        # 因此“多个日期×所有会议室”不会产生非本目标选择的任务。
        target_rooms = [room for room in rooms if target.includes_room(room)]
        if not target_rooms:
            raise ValueError(f"到点抢订目标 {target.key} 没有可参与的会议室，请检查 areas 和 ROOM_SELECTION")
        for room in target_rooms:
            if room.area not in rules:
                raise ValueError(f"会议室区域 {room.area!r} 没有配置提前开放天数")
            days = rules[room.area]
            if isinstance(days, bool) or not isinstance(days, int) or days < 0:
                raise ValueError(f"{room.area} 的提前开放天数必须是非负整数")
            pair_key = (target.key, room.area, room.name)
            if pair_key in dedup:
                continue
            dedup.add(pair_key)
            release_day = target.day - timedelta(days=days)
            grouped.setdefault((release_day, days, room.area), []).append((target, room))

    by_day: dict[date, list[ReleaseGroup]] = {}
    for (release_day, days, area), tasks in grouped.items():
        by_day.setdefault(release_day, []).append(
            ReleaseGroup(release_day, days, area, tuple(tasks)))
    result = []
    for day, groups in sorted(by_day.items()):
        # 提前7天与3天明确分栏，同天有多个组仍保持并发执行。
        sorted_groups = tuple(sorted(groups, key=lambda g: (-g.advance_days, g.area)))
        result.append(ReleaseDayBatch(day, sorted_groups))
    return result


def booking_result(mode: str, target: Target, room: RoomSpec | None,
                   status: str, *, detail: str = "") -> None:
    """预订/模拟预订的结果统一以WARNING记录；ERROR仍仅用于异常。"""
    labels = {
        "success": "预订成功", "dry_run": "模拟到达最终预定按钮（没有真实提交）",
        "unavailable": "未订到（不可确认或当前不可用）", "not_open": "尚未开放或服务端放号未生效",
        "expired": "超出截止时间",
        "uncertain": "提交结果待确认，已暂停此时段；请先在我的预订中核实",
        "occupied": "目标时段已被占用/当前不可选",
        "stopped": "本次尝试已停止", "blocked": "控件无法安全核验，本房间本时段已停止",
        "prepare_failed": "表单准备失败", "skipped": "检测到其他房间已成功，停止本页提交",
        "failed": "未订到", "no_rooms": "当天没有可参与的会议室",
    }
    room_text = f"{room.area}:{room.name}" if room is not None else "目标汇总"
    logging.warning("[预约结果][%s] %s｜%s｜%s%s", mode, room_text, target.key,
                    labels.get(status, status), f"｜{detail}" if detail else "")


def row_cell_selector(row: int, col: int) -> str:
    # 仅为旧录制选择器：第2行只录到10:00附近，第3行尚未录制。
    # 16:00以后的格子结构可能不同，定位失败时只诊断，不猜测点击。
    return f"div:nth-child({col})" if row == 1 else f"div:nth-child({row}) > div:nth-child({col})"


class ConciseLogFilter(logging.Filter):
    """界面保留进度和真正异常；磁盘日志不使用此过滤器。"""
    INFO_PREFIXES = (
        "日志：", "[启动]", "[账号验证]", "[登录] 单页认证成功",
        "[到点抢订]", "[候补] 开始监控", "[候补] 时段完成",
        "[候补] 已达到", "[候补] 所有时段", "[候补] 已停止", "[退出]",
    )

    def filter(self, record: logging.LogRecord) -> bool:
        if VERBOSE_LOGS or record.levelno >= logging.WARNING:
            return True
        msg = record.getMessage()
        return msg.startswith("[预约结果]") or msg.startswith(self.INFO_PREFIXES)


def prepare_error_message(exc: Exception) -> str:
    """面向界面解释常见错误；原始异常和堆栈另存完整日志。"""
    raw = str(exc).strip()
    if "not attached to the dom" in raw.lower() or "element was detached" in raw.lower():
        return "页面元素已失效（网页可能正在重绘）"
    if isinstance(exc, PlaywrightTimeout):
        return "等待页面或控件响应超时"
    return raw.splitlines()[0] if raw else type(exc).__name__


def setup_logging() -> None:
    """INFO正常色，WARNING黄字走stdout，ERROR红字走stderr；磁盘日志无ANSI转义符。"""
    LOG_DIR.mkdir(parents=True, exist_ok=True)
    log_path = LOG_DIR / (now_cn().strftime("抢订_%Y%m%d_%H%M%S") + ".log")
    fmt = "%(asctime)s | %(levelname)-7s | %(message)s"

    class LevelFilter(logging.Filter):
        def __init__(self, low: int, high: int):
            super().__init__()
            self.low, self.high = low, high

        def filter(self, record: logging.LogRecord) -> bool:
            return self.low <= record.levelno < self.high

    class ColorFormatter(logging.Formatter):
        def format(self, record: logging.LogRecord) -> str:
            msg = super().format(record)
            if not COLOR_CONSOLE_LOGS:
                return msg
            if record.levelno >= logging.ERROR:
                return f"\033[31m{msg}\033[0m"
            if record.levelno >= logging.WARNING:
                return f"\033[33m{msg}\033[0m"
            return msg

    # PyCharm将stderr统一标为红色，因此WARNING必须放到stdout。
    # PyCharm运行控制台和Windows Terminal通常都支持ANSI黄色；纯旧式控制台可能不显示颜色。
    file_handler = logging.FileHandler(log_path, encoding="utf-8")
    file_handler.setFormatter(logging.Formatter(fmt))
    display_fmt = fmt if VERBOSE_LOGS else "%(asctime)s | %(message)s"
    display_datefmt = None if VERBOSE_LOGS else "%H:%M:%S"
    handlers: list[logging.Handler] = [file_handler]
    # PyInstaller --windowed下stdout和stderr可能为None，不能直接创建StreamHandler。
    if sys.stdout is not None:
        normal_and_warning = logging.StreamHandler(sys.stdout)
        normal_and_warning.setLevel(logging.INFO)
        normal_and_warning.addFilter(LevelFilter(logging.INFO, logging.ERROR))
        normal_and_warning.addFilter(ConciseLogFilter())
        normal_and_warning.setFormatter(ColorFormatter(display_fmt, datefmt=display_datefmt))
        handlers.append(normal_and_warning)
    if sys.stderr is not None:
        errors = logging.StreamHandler(sys.stderr)
        errors.setLevel(logging.ERROR)
        errors.addFilter(ConciseLogFilter())
        errors.setFormatter(ColorFormatter(display_fmt, datefmt=display_datefmt))
        handlers.append(errors)
    if GUI_LOG_HANDLER is not None:
        GUI_LOG_HANDLER.addFilter(ConciseLogFilter())
        GUI_LOG_HANDLER.setFormatter(logging.Formatter(display_fmt, datefmt=display_datefmt))
        handlers.append(GUI_LOG_HANDLER)
    logging.basicConfig(level=logging.INFO, handlers=handlers, force=True)
    logging.info("日志：%s", log_path)

def run_stop_reason() -> str | None:
    """环境故障优先于用于唤醒另一模式的停止信号，不能误报为用户操作。"""
    for event, reason in ((DRIVER_LOST, "浏览器连接中断"), (AUTH_BLOCKED, "认证异常"),
                          (CONFIG_BLOCKED, "页面关键控件结构异常"),
                          (STOP_REQUESTED, "已响应停止请求")):
        if event.is_set():
            return reason
    return None


def raise_if_run_stopped() -> None:
    reason = run_stop_reason()
    if reason is not None:
        raise StopRequested(reason)


async def sleep_or_stop(seconds: float) -> None:
    """停止及环境故障均可立即唤醒等待，不取消已经提交的回执观察。"""
    raise_if_run_stopped()
    if seconds <= 0:
        return
    waits = [asyncio.create_task(event.wait()) for event in
             (STOP_REQUESTED, DRIVER_LOST, AUTH_BLOCKED, CONFIG_BLOCKED)]
    try:
        await asyncio.wait(waits, timeout=seconds, return_when=asyncio.FIRST_COMPLETED)
    finally:
        for task in waits:
            if not task.done():
                task.cancel()
        await asyncio.gather(*waits, return_exceptions=True)
    raise_if_run_stopped()


async def wait_until(moment: datetime) -> None:
    while True:
        raise_if_run_stopped()
        delta = (moment - now_cn()).total_seconds()
        if delta <= 0:
            return
        await sleep_or_stop(min(delta, 1.0))


async def save_debug(page: Page, tag: str) -> None:
    # 持续候补/重试时同类异常半小时最多留一张图，避免占满硬盘。
    last = LAST_DEBUG_CAPTURE.get(tag)
    if last is not None and (now_cn() - last).total_seconds() < 1800:
        return
    LAST_DEBUG_CAPTURE[tag] = now_cn()
    try:
        safe = re.sub(r"[^\w-]", "_", tag)
        file = LOG_DIR / f"{now_cn().strftime('%H%M%S_%f')}_{safe}.png"
        await page.screenshot(path=str(file), full_page=True, timeout=4500)
        logging.info("已保存排查截图：%s", file)
    except Exception as exc:
        logging.warning("保存截图失败：%s", exc)


async def select_date(page: Page, target: date) -> None:
    """在当前日历核验年月和日期，表单回读一致后才允许继续选房。"""
    raise_if_run_stopped()
    if target.year != now_cn().year:
        raise YearSelectionUnsupported("目标日期跨年；年份切换控件尚未录制。为避免选错年份，当前房间/目标暂停重试")
    try:
        logging.info("[日期] 开始选择并核验：%s", target.isoformat())
        await select_booking_date(
            page, target, timeout_ms=ACTION_TIMEOUT_MS, retries=DROPDOWN_RETRY_TIMES,
            diagnostic_dir=LOG_DIR, stop_check=raise_if_run_stopped)
    except DatePickerStructureError as exc:
        # 保留既有停止范围：确实无法辨认的日期属于共享表单结构异常。
        raise SelectorConfigurationError(str(exc)) from exc


async def select_start_time(page: Page, start: str) -> None:
    """优先通过独立的开始时间控件定位；其次使用用户录制的 role=option 入口。

    录制：get_by_role("option", name="下拉框,当前值11:").click()
          get_by_role("option", name="选项25,12:").click()

    由于「当前值」会变化，不可写死 11:；若候选不唯一就留截图停止，不乱点。
    """
    # 日期选择后页面可能重绘，等待最多约3秒再判断控件是否存在。
    entry = None
    for _ in range(6):
        by_id = page.locator(START_TIME_DROPDOWN_CSS)
        by_role = page.get_by_role("option", name=re.compile(START_TIME_DROPDOWN_RE))
        id_count = await by_id.count()
        role_count = await by_role.count()
        logging.info("[开始时间] 下拉入口候选：id=%d，录制role=%d", id_count, role_count)
        if id_count == 1 and await by_id.is_visible():
            entry = by_id
            logging.info("[开始时间] 使用专用控件定位：%s", START_TIME_DROPDOWN_CSS)
            break
        if role_count == 1 and await by_role.is_visible():
            entry = by_role
            logging.info("[开始时间] 使用录制role定位：%s", START_TIME_DROPDOWN_RE)
            break
        await asyncio.sleep(0.5)

    if entry is None:
        # 只记录日期/时间控件相关信息，避免日志包含整页业务或账号资料。
        related = page.locator('[id^="ev_slt_"]')
        details = []
        for i in range(min(await related.count(), 12)):
            el = related.nth(i)
            try:
                details.append({
                    "id": await el.get_attribute("id"),
                    "text": re.sub(r"\s+", " ", (await el.inner_text(timeout=800))[:75]),
                })
            except Exception:
                pass
        logging.error("[开始时间] 未能唯一定位入口。页面选择框信息=%s", details)
        raise SelectorConfigurationError("开始时间下拉入口不存在/不唯一；已输出控件信息并将截图")

    await entry.click(timeout=ACTION_TIMEOUT_MS)
    h, m = start.split(":")
    # 录制选项形如“选项25,12:”，不同版本也可能为“选项25,12:00”。
    pattern = rf"^选项\s*\d+\s*[,，]\s*{int(h):02d}:(?:00)?\s*$" if m == "00" else rf"^选项\s*\d+\s*[,，]\s*{int(h):02d}:{m}\s*$"
    value = page.get_by_role("option", name=re.compile(pattern))
    for _ in range(6):
        if await value.count() == 1 and await value.is_visible():
            await value.click(timeout=ACTION_TIMEOUT_MS)
            logging.info("[开始时间] 已选择 %s（匹配 %s）", start, pattern)
            return
        await asyncio.sleep(0.3)

    # 如所录页面的标签没有“选项N,”前缀，允许精确时刻文字的唯一可见 role=option。
    alt_pattern = rf"^\s*{int(h):02d}:{m}\s*$"
    alt_value = page.get_by_role("option", name=re.compile(alt_pattern))
    if await alt_value.count() == 1 and await alt_value.is_visible():
        await alt_value.click(timeout=ACTION_TIMEOUT_MS)
        logging.info("[开始时间] 已通过纯时刻选项选择 %s", start)
        return

    visible_opts = []
    opts = page.get_by_role("option")
    for i in range(min(await opts.count(), 120)):
        option = opts.nth(i)
        try:
            if await option.is_visible():
                # 不输出整页HTML；只摘取选项的精简文本/辅助名称。
                label = await option.get_attribute("aria-label")
                txt = (await option.inner_text(timeout=700))[:50]
                visible_opts.append((label or txt).strip())
                if len(visible_opts) >= 24:
                    break
        except Exception:
            continue
    logging.error("[开始时间] %s 选项不唯一或不存在。可见选项样例=%s", start, visible_opts)
    raise SelectorConfigurationError(f"开始时间 {start} 的选项无法唯一定位，需按本次诊断调整选择器")


async def select_duration(page: Page, target: Target) -> None:
    """整小时沿用已录制的下拉预填；非整小时交由后续时间网格准确选取。"""
    minutes = parse_clock(target.end) - parse_clock(target.start)
    hours, remaining_minutes = divmod(minutes, 60)
    if remaining_minutes:
        # 用户确认实际预约时段通过选房网格选择，每格30分钟。
        # 不把90分钟取整成1小时，也不猜测尚未录制的分钟下拉控件。
        logging.info("[时长] 目标%d分钟（%d个半小时格），由选房网格选择完整时段｜%s",
                     minutes, minutes // 30, target.key)
        return
    # aria-label可形如：选项3,02；容忍空格、中文逗号和“小时”后缀。
    pattern = re.compile(rf"^选项\s*\d+\s*[,，]\s*0?{hours}\s*(?:小时)?\s*$")
    fallback_pattern = re.compile(rf"^0?{hours}\s*(?:小时)?\s*$")
    dropdown = page.locator(DURATION_DROPDOWN)
    await dropdown.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
    if await dropdown.count() != 1:
        raise SelectorConfigurationError("会议时长下拉控件不唯一，停止点击")
    loop = asyncio.get_running_loop()

    for attempt in range(1, DROPDOWN_RETRY_TIMES + 1):
        if DRIVER_LOST.is_set():
            raise RuntimeError("浏览器已断连，停止设置会议时长")
        logging.info("[时长] 打开下拉：%d小时（尝试%d/%d）", hours, attempt, DROPDOWN_RETRY_TIMES)
        await dropdown.click(timeout=ACTION_TIMEOUT_MS)
        until = loop.time() + DROPDOWN_LOAD_SECONDS
        while loop.time() < until:
            candidates = []
            for selector in (page.get_by_role("option", name=pattern),
                             page.get_by_role("option", name=fallback_pattern)):
                for index in range(min(await selector.count(), 15)):
                    item = selector.nth(index)
                    if await item.is_visible():
                        candidates.append(item)
            if len(candidates) == 1:
                await candidates[0].click(timeout=ACTION_TIMEOUT_MS)
                logging.info("[时长] 已选择%d小时", hours)
                return
            if len(candidates) > 1:
                raise SelectorConfigurationError(f"{hours}小时时长选项出现多个可见匹配，避免误选")
            await asyncio.sleep(0.25)

        # 诊断只输出时长下拉可见选项，不含会议或账号资料。
        visible_opts = []
        options = page.get_by_role("option")
        for index in range(min(await options.count(), 100)):
            item = options.nth(index)
            try:
                if await item.is_visible():
                    label = await item.get_attribute("aria-label")
                    visible_opts.append((label or await item.inner_text(timeout=700)).strip()[:35])
                    if len(visible_opts) >= 20:
                        break
            except Exception:
                continue
        logging.warning("[时长] 第%d次未找到%d小时，可见选项=%s", attempt, hours, visible_opts)
        # 如果下拉仍展开，重试时再点一次可能把列表收起，先用Esc关闭。
        if attempt < DROPDOWN_RETRY_TIMES:
            await page.keyboard.press("Escape")
            await asyncio.sleep(0.6)

    raise RuntimeError(f"等待时长选项共{DROPDOWN_RETRY_TIMES}轮仍未找到{hours}小时，已输出可见选项")


async def save_area_filter_diagnostic(page: Page, area: str, stage: str) -> None:
    """失败时只保存筛选控件的结构/状态，不采集账号、会议主题、页面正文。"""
    try:
        result = await page.evaluate(r"""({area,stage}) => {
          const rect = e => {const r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}};
          const visible = e => {const r=e.getBoundingClientRect();return r.width>0 && r.height>0};
          const ids = ['ev_slt_10012_ev_se','ev_slt_10013_ev_se','ev_slt_10014_ev_se','ev_slt_10015_ev_se'];
          const filters = ids.map(id=>{const el=document.getElementById(id);const drop=el?.querySelector('.ev_select_drop_icon');
            return {id,exists:!!el,visible:!!el&&visible(el),rect:el?rect(el):null,
              dropdownExists:!!drop,dropdownVisible:!!drop&&visible(drop)};});
          const dialog = [...document.querySelectorAll('.ev_Dialog_Panel, .ev_Dialog_modal, [role="dialog"]')]
            .filter(visible).map(e=>({className:(typeof e.className==='string'?e.className:'').slice(0,90),rect:rect(e)}));
          const otherIds=[...document.querySelectorAll('[id^="ev_slt_"]')].filter(visible)
            .slice(0,30).map(e=>({id:e.id,rect:rect(e)}));
          return {area,stage,url:location.origin+location.pathname,filters,dialogs:dialog,visibleSelectIds:otherIds,
            frameCount:document.querySelectorAll('iframe').length};
        }""", {'area':area,'stage':stage})
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        path = LOG_DIR / (now_cn().strftime('%H%M%S_%f')+f'_区域筛选结构_{area}.json')
        path.write_text(json.dumps(result,ensure_ascii=False,indent=2),encoding='utf-8')
        logging.warning('[区域筛选] %s｜结构诊断=%s｜控件状态=%s', stage, path,
                        [(x['id'],x['exists'],x['visible']) for x in result['filters']])
    except Exception as exc:
        logging.warning('[区域筛选] 保存结构诊断失败：%s',str(exc).splitlines()[0])


async def select_filter_dropdown(page: Page, selector: str, label_pattern: str,
                                 title: str, area: str) -> None:
    """按真实录制顺序定位；每步记录日志，避免只有泛化的5000ms超时。"""
    entry = page.locator(selector)
    logging.info('[区域筛选] %s｜等待下拉入口：%s（最长%.1f秒）',
                 title, selector, FILTER_RENDER_TIMEOUT_MS/1000)
    try:
        await entry.wait_for(state='visible', timeout=FILTER_RENDER_TIMEOUT_MS)
    except Exception as exc:
        await save_area_filter_diagnostic(page,area,f'{title}:入口未出现')
        raise SelectorConfigurationError(
            f'{title} 下拉入口没有出现：{selector}；请看区域筛选结构_{area}.json') from exc
    count = await entry.count()
    if count != 1:
        await save_area_filter_diagnostic(page,area,f'{title}:入口数量错误')
        raise SelectorConfigurationError(f'{title} 下拉入口数量={count}（应为1）')
    option = page.get_by_role('option',name=re.compile(label_pattern))
    for attempt in range(1,DROPDOWN_RETRY_TIMES+1):
        logging.info('[区域筛选] %s｜点击下拉（%d/%d）',title,attempt,DROPDOWN_RETRY_TIMES)
        try:
            await entry.click(timeout=ACTION_TIMEOUT_MS)
            await option.first.wait_for(state='visible',timeout=FILTER_OPTION_TIMEOUT_MS)
            all_count = await option.count()
            visible = []
            for ix in range(all_count):
                candidate=option.nth(ix)
                if await candidate.is_visible():
                    visible.append(candidate)
            if len(visible)!=1:
                raise SelectorConfigurationError(f'{title} 匹配到{len(visible)}个可见选项（总数={all_count}），拒绝误选')
            await visible[0].click(timeout=ACTION_TIMEOUT_MS)
            logging.info('[区域筛选] %s｜选择成功',title)
            return
        except SelectorConfigurationError:
            await save_area_filter_diagnostic(page,area,f'{title}:选项数量不唯一')
            raise
        except Exception as exc:
            logging.warning('[区域筛选] %s｜第%d次失败：%s',title,attempt,str(exc).splitlines()[0])
            if attempt>=DROPDOWN_RETRY_TIMES:
                await save_area_filter_diagnostic(page,area,f'{title}:展开或选项超时')
                raise RuntimeError(f'{title} 选择失败（尝试{DROPDOWN_RETRY_TIMES}次），已保存控件诊断') from exc
            await page.keyboard.press('Escape')
            await asyncio.sleep(0.5)


async def read_visible_room_names(page: Page) -> list[dict]:
    """仅读取当前可见弹窗的会议室名称和纵向位置；不读取登录信息和会议主题。"""
    return await page.evaluate(r"""(pattern) => {
      const panel=[...document.querySelectorAll('.ev_Dialog_PanelContent')]
        .find(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0});
      if(!panel) return [];
      const out=[]; const seen=new Set();
      const rx=new RegExp(pattern, 'i');
      for (const e of panel.querySelectorAll('*')) {
        if(e.children.length!==0) continue;
        const r=e.getBoundingClientRect();
        if(!r.width||!r.height||getComputedStyle(e).visibility==='hidden') continue;
        const t=(e.textContent||'').trim();
        const match=t.match(rx);
        if(!match) continue;
        const name=match[0].replace(/[-_]+$/,'');
        const key=name+'@'+Math.round(r.y);
        if(seen.has(key)) continue;
        seen.add(key);
        out.push({name,y:Math.round(r.y),x:Math.round(r.x),center_y:r.y+r.height/2,height:r.height});
      }
      return out.sort((a,b)=>a.y-b.y||a.x-b.x);
    }""", ROOM_FULL_NAME_PATTERN)


def room_short_name(name: str) -> str:
    """从完整房号提取包含楼层的房间名，例如：
    CN-ShenZhen-F5-7F-A05R -> 7F-A05R
    CN-ShenZhen-F5-1F-A10R -> 1F-A10R
    切勿根据完整房号里的 F5-1F 推断它属于哪个筛选区域。
    """
    m = re.search(ROOM_SHORT_NAME_PATTERN, name.strip().upper())
    return m.group(1).replace("_", "-") if m else name.strip().upper()


def rooms_match_area(rooms: list[dict], area: str) -> bool:
    """用源码完整名单核验区域，不把旧楼层的固定数量作为判断依据。"""
    expected = AREA_EXPECTED_SHORT_NAMES.get(area)
    if not expected or len(rooms) != len(expected):
        return False
    observed = [room_short_name(str(r.get('name', ''))) for r in rooms]
    return len(set(observed)) == len(expected) and set(observed) == set(expected)


async def verify_room_identity(page: Page, room: RoomSpec) -> None:
    """避免筛选后列表顺序变化、仍按原固定行号点击到其他房间。"""
    visible_rooms = await read_visible_room_names(page)
    if not visible_rooms:
        if not DRY_RUN:
            raise SelectorConfigurationError(
                f'{room.area}:{room.name} 无法读取会议室名称，正式模式拒绝按第{room.row}行盲订')
        logging.warning('[房间核验] 暂未识别可见房间名称；模拟模式仅测试控件，不代表房间名称已核对')
        return
    if room.row > len(visible_rooms):
        raise SelectorConfigurationError(
            f'选择第{room.row}行，但可见会议室只有{len(visible_rooms)}间；停止错误点击')
    actual = visible_rooms[room.row-1]['name']
    # 无论使用名称还是行号，都先确认整个列表属于正确区域。
    correct = rooms_match_area(visible_rooms, room.area)
    correct = correct and room_short_name(actual) == room.name.upper()
    if not correct:
        logging.error('[房间核验] %s期望=%s第%d行，但实际第%d行=%s，阻止误订；请先运行list-rooms核实',
                      room.area,room.name,room.row,room.row,actual)
        raise SelectorConfigurationError(f'实际第{room.row}行={actual}，与配置{room.name}不一致')
    logging.info('[房间核验] %s第%d行匹配：%s', room.area,room.row,actual)


async def record_filter_ui_probe(page: Page, area: str, stage: str) -> None:
    """仅采集选房弹窗中与筛选有关的元素，不读用户凭据及会议表单内容。"""
    try:
        info = await page.evaluate(r"""(stage) => {
          const panel=[...document.querySelectorAll('.ev_Dialog_Panel')]
            .find(e=>{const r=e.getBoundingClientRect();return r.width>0&&r.height>0});
          const root=panel||document;
          const vis=e=>{let r=e.getBoundingClientRect();return r.width>0&&r.height>0};
          const rect=e=>{let r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}};
          const selectors=['ev_slt_10012_ev_se','ev_slt_10013_ev_se','ev_slt_10014_ev_se','ev_slt_10015_ev_se'];
          const expected=selectors.map(id=>{let e=document.getElementById(id);
            return {id,exists:!!e,visible:!!e&&vis(e),insidePanel:!!(e&&panel&&panel.contains(e)),r:e?rect(e):null};});
          const selectCandidates=[...root.querySelectorAll('[id^="ev_slt_"],.ev_select_drop_icon')].filter(vis).slice(0,40)
            .map(e=>({id:e.id||'',cls:typeof e.className==='string'?e.className.slice(0,80):'',r:rect(e)}));
          const advance=[...root.querySelectorAll('*')].filter(e=>e.children.length===0&&vis(e)&&
             (e.textContent||'').trim()==='查询').slice(0,8).map(e=>({tag:e.tagName,cls:typeof e.className==='string'?e.className.slice(0,100):'',r:rect(e),
             parentTag:e.parentElement?.tagName||'',parentCls:typeof e.parentElement?.className==='string'?e.parentElement.className.slice(0,130):'',
             grandparentCls:typeof e.parentElement?.parentElement?.className==='string'?e.parentElement.parentElement.className.slice(0,130):''}));
          const buttons=[...root.querySelectorAll('button,[role="button"]')].filter(vis).slice(0,30).map(e=>({text:(e.textContent||'').trim().slice(0,35),tag:e.tagName,r:rect(e)}));
          return {stage,expected,selectCandidates,queryLabels:advance,buttons,
             panelPresent:!!panel,dialogCount:document.querySelectorAll('.ev_Dialog_Panel').length,
             iframeCount:document.querySelectorAll('iframe').length};
        }""",stage)
        path=LOG_DIR / (now_cn().strftime('%H%M%S_%f')+f'_筛选入口诊断_{area}_{stage}.json')
        LOG_DIR.mkdir(parents=True,exist_ok=True)
        path.write_text(json.dumps(info,ensure_ascii=False,indent=2),encoding='utf-8')
        logging.info('[筛选诊断] %s｜%s',stage,path)
    except Exception as exc:
        logging.warning('[筛选诊断] 无法采集%s：%s',stage,str(exc).splitlines()[0])


async def filter_dropdown_status(page: Page, selector: str) -> str:
    """只读取当前筛选控件的可见文本，便于判断是否已经是正确的默认值。"""
    root = page.locator(selector).locator("xpath=ancestor::*[contains(@class, 'ev_select_div')][1]")
    try:
        if await root.count() == 1 and await root.is_visible():
            return (await root.inner_text(timeout=1000)).strip().replace("\n", " ")[:100]
    except Exception:
        pass
    return ""


def _rect_overlap_ratio(a: dict, b: dict) -> float:
    """计算两个可见控件矩形的重叠程度；用于识别网页重复渲染的同一楼栋控件。"""
    try:
        ax, ay, aw, ah = [float(a.get(k, 0)) for k in ("x", "y", "w", "h")]
        bx, by, bw, bh = [float(b.get(k, 0)) for k in ("x", "y", "w", "h")]
        if min(aw, ah, bw, bh) <= 0:
            return 0.0
        ix = max(0.0, min(ax + aw, bx + bw) - max(ax, bx))
        iy = max(0.0, min(ay + ah, by + bh) - max(ay, by))
        inter = ix * iy
        return inter / (aw * ah + bw * bh - inter)
    except Exception:
        return 0.0


def _collapse_duplicate_building_candidates(candidates: list[dict]) -> dict | None:
    """相同楼栋、几乎相同范围时，仅采用唯一实际接收指针的入口。

    DOM 身份重复在调用方去重；不同节点不能仅凭 class 或相同 HTML 合并。
    """
    if len(candidates) <= 1:
        return candidates[0] if candidates else None
    first = candidates[0]
    label = first.get("matchedLabel")
    if not label:
        return None
    for item in candidates[1:]:
        if item.get("matchedLabel") != label:
            return None
        for key in ("rect", "dropRect"):
            if _rect_overlap_ratio(first.get(key) or {}, item.get(key) or {}) < 0.90:
                return None
    clickable = [r for r in candidates if r.get("receivesPointer")]
    return clickable[0] if len(clickable) == 1 else None


async def _unique_dom_locators(locators: list[Locator]) -> list[Locator]:
    """按真实节点身份去重，不把 outerHTML 相同的不同控件当成同一个。"""
    unique = []
    handles = []
    try:
        for locator in locators:
            handle = await locator.element_handle(timeout=500)
            if handle is None:
                continue
            duplicate = False
            retained = False
            try:
                for existing in handles:
                    if await handle.evaluate("(el, other) => el === other", existing):
                        duplicate = True
                        break
                if not duplicate:
                    unique.append(locator)
                    handles.append(handle)
                    retained = True
            finally:
                if not retained:
                    await handle.dispose()
        return unique
    finally:
        for handle in handles:
            await handle.dispose()


async def find_building_dropdown(
    page: Page,
    current_area: str | None = None,
    *,
    log_ambiguity: bool = False,
) -> ElementHandle | None:
    """只定位“楼栋”下拉，不按下拉框顺序猜测。

    优先使用已验证的录制ID；如果录制ID不存在，则只从“当前选中值”
    精确识别楼栋下拉。current_area 来自当前实际会议室名单，用于在网页
    同时渲染多个候选容器时锁定当前楼栋，避免 F5 / F5-1A 串选。

    网页某些版本会短暂/长期同时渲染两个视觉上重叠的同一楼栋控件。
    同一实际下拉节点按身份去重；不同节点还需同值、同范围且唯一接收指针；
    两个真实不同位置的候选仍然拒绝猜测。
    """
    panel = page.locator('.ev_Dialog_Panel:visible')
    if await panel.count() != 1:
        return None
    async def pin(locator: Locator, expected_label: str | None = None) -> ElementHandle | None:
        # 固定实际节点；后续插入其它下拉框不能让 nth 定位器改指城市或区域。
        handle = await locator.element_handle(timeout=500)
        if handle is None:
            return None
        try:
            valid = await handle.evaluate(r'''(el, expected) => {
                const panels=[...document.querySelectorAll('.ev_Dialog_Panel')].filter(e=>{
                    const r=e.getBoundingClientRect();
                    return r.width>0 && r.height>0 && getComputedStyle(e).visibility!=='hidden';
                });
                if (!el.isConnected || panels.length!==1 || !panels[0].contains(el)) return false;
                if (!expected) return true;
                const root=el.closest('.ev_select_div');
                if (!root) return false;
                const values=[root.innerText];
                for (const node of root.querySelectorAll('.ev_input,input,[role="combobox"]')) {
                    values.push(node.value, node.innerText, node.textContent, node.getAttribute('title'));
                }
                return values.some(v=>String(v??'').replace(/\s+/g,' ').trim()===expected);
            }''', expected_label)
            if valid:
                return handle
        except Exception:
            await handle.dispose()
            raise
        await handle.dispose()
        return None

    direct = panel.locator(FILTER_BUILDING).filter(visible=True)
    if await direct.count() == 1:
        return await pin(direct)

    def norm(value: str) -> str:
        return re.sub(r'\s+', ' ', value or '').strip()

    preferred_label = AREA_LABELS.get(current_area) if current_area else None
    area_labels = tuple(AREA_LABELS.values())
    preferred: list[dict] = []
    exact_current: list[dict] = []
    structural: list[dict] = []

    try:
        containers = panel.locator('.ev_select_div:visible')
        count = await containers.count()
    except Exception:
        count = 0

    for index in range(min(count, 40)):
        container = containers.nth(index)
        try:
            if not await container.is_visible():
                continue
            drop = container.locator('.ev_select_drop_icon:visible')
            if await drop.count() != 1:
                continue

            info = await container.evaluate(r"""el => {
                const visible = e => {
                    if (!e) return false;
                    const r = e.getBoundingClientRect();
                    return r.width > 0 && r.height > 0;
                };
                const rect = e => {
                    const r = e.getBoundingClientRect();
                    return {
                        x: Math.round(r.x), y: Math.round(r.y),
                        w: Math.round(r.width), h: Math.round(r.height)
                    };
                };
                const add = (arr, v) => {
                    const s = String(v ?? '').replace(/\s+/g, ' ').trim();
                    if (s && !arr.includes(s)) arr.push(s);
                };

                const values = [];
                for (const e of el.querySelectorAll('.ev_input')) {
                    if (!visible(e)) continue;
                    add(values, e.innerText);
                    add(values, e.textContent);
                    add(values, e.getAttribute('aria-label'));
                    add(values, e.getAttribute('title'));
                    add(values, e.getAttribute('data-label'));
                    add(values, e.getAttribute('data-name'));
                    for (const inp of e.querySelectorAll('input')) {
                        add(values, inp.value);
                        add(values, inp.getAttribute('aria-label'));
                        add(values, inp.getAttribute('title'));
                        add(values, inp.getAttribute('placeholder'));
                    }
                }
                for (const e of el.querySelectorAll('input, [role="combobox"]')) {
                    if (!visible(e)) continue;
                    add(values, e.value);
                    add(values, e.getAttribute('aria-label'));
                    add(values, e.getAttribute('title'));
                    add(values, e.getAttribute('placeholder'));
                }

                const drop = [...el.querySelectorAll('.ev_select_drop_icon')].find(visible);
                const dr = drop?.getBoundingClientRect();
                const hit = dr ? document.elementFromPoint(dr.x + dr.width/2, dr.y + dr.height/2) : null;
                const receivesPointer = !!(drop && hit && (drop === hit || drop.contains(hit)));
                const className = typeof el.className === 'string' ? el.className : '';
                const dropClass = drop && typeof drop.className === 'string' ? drop.className : '';

                let fieldHint = '';
                let p = el;
                for (let depth = 0; p && depth < 4; depth++, p = p.parentElement) {
                    if (!visible(p)) continue;
                    const t = (p.innerText || '').replace(/\s+/g, ' ').trim();
                    if (t.includes('楼栋') && t.length <= 180) {
                        fieldHint = t;
                        break;
                    }
                }

                return {
                    values,
                    rect: rect(el),
                    dropRect: drop ? rect(drop) : {},
                    receivesPointer,
                    className,
                    dropClass,
                    fieldHint,
                };
            }""")

            values = [norm(v) for v in (info.get('values') or []) if norm(v)]
            value_text = ' / '.join(values[:6])
            matched_label = next(
                (label for label in area_labels
                 if any(norm(v) == label for v in values)),
                None,
            )
            record = {
                "locator": drop.first,
                "values": values,
                "matchedLabel": matched_label,
                "value_text": value_text,
                "rect": info.get("rect") or {},
                "dropRect": info.get("dropRect") or {},
                "receivesPointer": bool(info.get("receivesPointer")),
                "className": info.get("className") or "",
                "dropClass": info.get("dropClass") or "",
                "fieldHint": norm(info.get("fieldHint", "")),
            }

            if preferred_label and matched_label == preferred_label:
                preferred.append(record)
                continue

            if matched_label is not None:
                exact_current.append(record)
                continue

            parent_text = norm(info.get('fieldHint', ''))
            if '楼栋' in parent_text and not re.search(r'城市|区域|类型', parent_text):
                structural.append(record)
        except Exception:
            continue

    async def resolve(records: list[dict], title: str) -> Locator | None:
        try:
            unique = await _unique_dom_locators([r["locator"] for r in records])
        except Exception:
            return None  # 渲染交接中节点已替换，交给外层轮询重新采样。
        records = [r for r in records if any(r["locator"] is loc for loc in unique)]
        if len(records) == 1:
            return records[0]["locator"]
        if not records:
            return None

        chosen = _collapse_duplicate_building_candidates(records)
        if chosen is not None:
            logging.info(
                '[区域筛选] 检测到%d个重叠的重复楼栋下拉入口，已核验同值/范围及实际点击入口：%s',
                len(records), chosen["value_text"] or title
            )
            return chosen["locator"]

        if log_ambiguity:
            logging.warning(
                '[区域筛选] %s候选=%d，无法安全唯一确定，拒绝猜测｜候选=%s',
                title, len(records),
                [
                    {
                        "value": r["value_text"],
                        "rect": r["rect"],
                        "field": r["fieldHint"][:80],
                    }
                    for r in records[:6]
                ],
            )
        return None

    if preferred_label:
        resolved = await resolve(preferred, f'当前楼栋值“{preferred_label}”')
        if resolved is not None:
            logging.info('[区域筛选] 通过当前楼栋值定位下拉入口：%s', preferred_label)
            return await pin(resolved, preferred_label)
        if preferred:
            # 已有明确目标楼栋值，但存在多个无法消歧的候选；不要退回到
            # 更宽松的“其他楼栋文字”匹配。
            return None

    resolved = await resolve(exact_current, '当前楼栋下拉')
    if resolved is not None:
        logging.info('[区域筛选] 通过当前楼栋值定位下拉入口（录制ID在此页面未出现）')
        expected = next(r["matchedLabel"] for r in exact_current if r["locator"] is resolved)
        return await pin(resolved, expected)
    if exact_current:
        return None

    resolved = await resolve(structural, '带“楼栋”结构标签的下拉')
    if resolved is not None:
        logging.info('[区域筛选] 通过“楼栋”结构标签定位下拉入口（录制ID在此页面未出现）')
        return await pin(resolved)
    if structural:
        return None

    # 文字反查只能回到当前弹窗内最近的下拉容器，并汇总全部标签消歧。
    candidates: list[Locator] = []
    labels: list[str] = []
    for label in area_labels:
        try:
            value = panel.get_by_text(label, exact=True)
            for index in range(min(await value.count(), 20)):
                node = value.nth(index)
                if not await node.is_visible():
                    continue
                ancestor = node.locator(
                    "xpath=ancestor-or-self::*[contains(concat(' ', normalize-space(@class), ' '), ' ev_select_div ')][1]")
                if await ancestor.count() != 1 or not await ancestor.is_visible():
                    continue
                if not await ancestor.evaluate(
                        "el => !!el.closest('.ev_Dialog_Panel')"):
                    continue
                drop = ancestor.locator('.ev_select_drop_icon:visible')
                if await drop.count() == 1:
                    candidates.append(drop)
                    labels.append(label)
        except Exception:
            continue
    unique = await _unique_dom_locators(candidates)
    if len(unique) == 1:
        candidate = unique[0]
        label = next(label for loc, label in zip(candidates, labels) if loc is candidate)
        logging.info('[区域筛选] 通过精确楼栋文字反查唯一弹窗内下拉入口')
        return await pin(candidate, label)
    return None


async def select_building(page: Page, area: str, entry: Locator | ElementHandle) -> None:
    """在当前选房弹窗内切换楼栋，不关闭或另开页面。"""
    try:
        label = AREA_LABELS[area]
        opt = page.get_by_role('option', name=re.compile(
            rf'^选项\s*\d+\s*[,，]\s*{re.escape(label)}$'))
        await entry.click(timeout=ACTION_TIMEOUT_MS)
        try:
            await opt.first.wait_for(state='visible', timeout=FILTER_OPTION_TIMEOUT_MS)
        except Exception as exc:
            # 不选择相似楼栋，不将F5误选为F5-1A。
            await save_area_filter_diagnostic(page, area, '楼栋选项未出现')
            raise SelectorConfigurationError(f'已展开楼栋下拉，但找不到精确选项：{label}') from exc
        count = await opt.count()
        matches = [opt.nth(i) for i in range(count) if await opt.nth(i).is_visible()]
        if len(matches) != 1:
            raise SelectorConfigurationError(f'楼栋 {label} 匹配可见选项数={len(matches)}，拒绝误选')
        await matches[0].click(timeout=ACTION_TIMEOUT_MS)
        logging.info('[区域筛选] 已选择楼栋：%s', label)
    finally:
        if isinstance(entry, ElementHandle):
            await entry.dispose()


async def complete_parent_filters_if_present(page: Page, area: str) -> bool:
    """仅当楼栋入口未显示时，尝试按录制步骤补足未选中的上游条件。

    用户并不需要每次四级重选；页面若根本没有这些控件，不继续乱点。
    """
    configs = (
        (FILTER_TYPE, r'^选项\s*1\s*[,，]\s*\[object Object\]$', '类型', None),
        (FILTER_CITY, rf'^选项\s*\d+\s*[,，]\s*{re.escape(DEPLOYMENT_PROFILE["city"])}$',
         '城市', re.compile(re.escape(DEPLOYMENT_PROFILE["city"]), re.I)),
        (FILTER_ZONE, rf'^选项\s*\d+\s*[,，]\s*{re.escape(DEPLOYMENT_PROFILE["zone"])}$',
         '区域', re.compile(r'^' + re.escape(DEPLOYMENT_PROFILE["zone"]) + r'$', re.I)),
    )
    changed = False
    for selector, pattern, label, desired in configs:
        entry = page.locator(selector)
        if await entry.count() != 1 or not await entry.is_visible():
            continue
        display = await filter_dropdown_status(page, selector)
        if desired is not None and desired.search(display):
            logging.info('[区域筛选] %s已有正确默认值：%s，跳过',label,display)
            continue
        # 类型如显示了具体值（而非请选择）也尽量保持；只在为空或占位时补充。
        if label == '类型' and display and not re.search(r'请选择|选择|全部|^\s*$',display):
            logging.info('[区域筛选] 类型已有值：%s，跳过',display)
            continue
        logging.info('[区域筛选] %s未就绪，按原录制补选',label)
        await select_filter_dropdown(page, selector, pattern, label, area)
        changed = True
    return changed


async def query_area_rooms(page: Page, area: str) -> None:
    button = page.locator('.ev_Dialog_Panel:visible').get_by_role('button', name='查询', exact=True)
    # 兼容部分站点将查询按钮渲染在弹窗外层portal中。
    if await button.count() != 1:
        button = page.get_by_role('button', name='查询', exact=True)
    if await button.count() != 1 or not await button.is_visible():
        await record_filter_ui_probe(page,area,'查询按钮不可定位')
        raise SelectorConfigurationError(f'{area} 找不到唯一可见的「查询」按钮')
    await button.click(timeout=ACTION_TIMEOUT_MS)
    logging.info('[区域筛选] %s｜点击查询（仍在同一个选房弹窗）',area)
    for _ in range(12):
        await asyncio.sleep(AREA_RESULT_WAIT_SECONDS/12)
        names = await read_visible_room_names(page)
        if rooms_match_area(names,area):
            logging.info('[区域筛选] %s｜查询后核验房间%d间',area,len(names))
            return
    names = await read_visible_room_names(page)
    raise SelectorConfigurationError(
        f'{area} 查询后房间名单不匹配，当前={[x["name"] for x in names[:15]]}')


async def select_area(page: Page, area: str) -> str:
    """同一弹窗完成：打开→按需切换楼栋→查询→核验，随后选格子与确认。

    不会打开“高级搜索”或其他页面，也不强制每次重选四个下拉框。
    """
    if area not in AREA_LABELS:
        raise ValueError(f'未知区域：{area}')
    logging.info('[区域筛选] 打开同一个选房弹窗，目标=%s（%s）',area,AREA_LABELS[area])
    await page.get_by_role('button',name=ADD_ROOM_BUTTON).click(timeout=ACTION_TIMEOUT_MS)
    panel = page.locator('.ev_Dialog_Panel:visible').first
    await panel.wait_for(state='visible',timeout=FILTER_RENDER_TIMEOUT_MS)

    # 弹窗里的默认会议室名单和楼栋下拉控件并不是同时渲染。
    # 尤其是默认先显示F5-1A时，不能因为“已经读到13间房”就提前结束等待，
    # 否则F5目标会在楼栋控件尚未出现时被误判为“无法筛选”。
    # 如果当前默认名单已经就是目标区域，则无需寻找楼栋控件，避免被网页
    # 同时渲染的重复下拉入口干扰。
    current = []
    current_area = None
    entry = None
    wait_deadline = monotonic_time.monotonic() + FILTER_RENDER_TIMEOUT_MS / 1000
    while monotonic_time.monotonic() < wait_deadline:
        current = await read_visible_room_names(page)
        initial_matches = rooms_match_area(current, area)
        if initial_matches:
            current_area = area
            break
        current_area = next((candidate_area for candidate_area in AREA_LABELS
                             if rooms_match_area(current, candidate_area)), None)
        entry = await find_building_dropdown(page, current_area=current_area, log_ambiguity=False)
        if entry is not None:
            break
        await asyncio.sleep(FILTER_STATE_POLL_SECONDS)

    initial_matches = rooms_match_area(current,area)
    current_area = next((candidate_area for candidate_area in AREA_LABELS
                         if rooms_match_area(current, candidate_area)), None)
    logging.info('[区域筛选] 弹窗当前显示%d间；是否已经是%s：%s；当前楼栋=%s；楼栋入口=%s',
                 len(current),area,initial_matches,current_area or '未知',
                 '有' if entry is not None else '未找到')

    # 已经是目标楼栋就保留当前筛选结果：无需重复展开下拉、点击查询。
    # 这是名单核验成功，并不是声称本次执行了楼栋筛选。
    if initial_matches:
        logging.info('[区域筛选] %s｜当前已经显示目标%d间会议室，保留现有筛选，在同一弹窗直接选时间格子',
                     area, len(current))
        return 'matched_current'

    # 当前楼栋不匹配时，才尝试切换楼栋再查询。
    if entry is not None:
        try:
            await select_building(page,area,entry)
        except SelectorConfigurationError:
            # 可能是城市/区域尚未初始化，按需要补上游条件并重新选择。
            if not await complete_parent_filters_if_present(page,area):
                if initial_matches:
                    logging.warning('[区域筛选] 目标名单本就匹配，但楼栋选项未出现；按已显示名单使用，未声称完成筛选')
                    return 'matched_current'
                raise
            entry = await find_building_dropdown(page, current_area=current_area, log_ambiguity=False)
            if entry is None:
                raise SelectorConfigurationError(f'{area} 补全上游筛选条件后仍找不到楼栋')
            await select_building(page,area,entry)
        await query_area_rooms(page,area)
        return 'explicit'

    # 当前名单不匹配但楼栋框缺失：先补全已出现的上游条件，再给楼栋控件
    # 一个完整的异步渲染窗口；不能在控件刚开始创建时立即判定为结构异常。
    await complete_parent_filters_if_present(page,area)
    wait_deadline = monotonic_time.monotonic() + FILTER_RENDER_TIMEOUT_MS / 1000
    while monotonic_time.monotonic() < wait_deadline:
        entry = await find_building_dropdown(page, current_area=current_area, log_ambiguity=False)
        if entry is not None:
            await select_building(page,area,entry)
            await query_area_rooms(page,area)
            return 'explicit'
        await asyncio.sleep(FILTER_STATE_POLL_SECONDS)

    # 最终只记录一次无法消歧的候选信息；轮询阶段不重复刷 WARNING。
    await find_building_dropdown(page, current_area=current_area, log_ambiguity=True)
    await record_filter_ui_probe(page,area,'弹窗内无法定位楼栋控件')
    await save_area_filter_diagnostic(page,area,'弹窗内无法定位楼栋控件')
    raise SelectorConfigurationError(
        f'{area} 当前显示{[r["name"] for r in current[:13]]}，但弹窗里未找到楼栋下拉框。'
        '已保存结构诊断；不会错误地把F5-1A名单当成F5。')


async def open_room_list(page: Page, area: str) -> str:
    """同一个选房弹窗中核验区域（可复用正确默认值），不另开页面。"""
    return await select_area(page, area)


async def list_area_rooms(page: Page, area: str) -> None:
    """探查过滤后弹窗中的会议室条目。只读取文本/布局，不点击任何时间格子。"""
    await page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
    await page.get_by_role("button", name=ADD_ROOM_BUTTON).wait_for(timeout=NAVIGATION_TIMEOUT_MS)
    # 即使当前页面看起来已展示了会议室，也核验清单必须属于目标区域。
    filter_mode = await open_room_list(page,area)
    await page.locator('.ev_Dialog_PanelContent:visible').first.wait_for(
        state='visible',timeout=FILTER_RENDER_TIMEOUT_MS)
    await asyncio.sleep(1.0)
    # 弹窗中的房间名称和结构事先未知，不能猜行号。存储本机供用户核对。
    data = await page.evaluate(r"""(area) => {
        const panel = document.querySelector('.ev_Dialog_PanelContent');
        if(!panel) return {area, error:'选房弹窗正文不存在'};
        const visible = e => {let r=e.getBoundingClientRect(); return r.width>0 && r.height>0};
        const rect = e => {let r=e.getBoundingClientRect();return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}};
        const leaves = [...panel.querySelectorAll('*')].filter(e=>e.children.length===0 && visible(e))
            .map(e=>({text:(e.textContent||'').trim().slice(0,90),...rect(e)}))
            .filter(e=>e.text && e.w>0 && e.h>0);
        // 多数会议室名称在表格左侧；只把布局相同的候选元素聚合到行供人工核对。
        const left = panel.getBoundingClientRect().left;
        const likelyNames = leaves.filter(e=>e.x < left+420 && e.text.length<=45 &&
             !/^(?:\d{1,2}:\d{2}|查询|取消|确认|深圳|F|选项\d+)/.test(e.text));
        const unique=[]; const seen=new Set();
        for (const item of likelyNames) {let key=item.text+'@'+item.y;
           if(!seen.has(key)) {seen.add(key);unique.push(item)} }
        return {area, url:location.origin+location.pathname,
          namesAndRowsToVerify:unique.slice(0,80),
          // 只保存选房弹窗文本，方便核对房间名称；不抓账号信息/全站页面。
          panelText:(panel.innerText||'').slice(0,5000)};
    }""", area)
    data['roomCandidates'] = await read_visible_room_names(page)
    # 下方仅按页面可见房间名称与坐标排序；真正格子行号仍需人工确认。
    data['allVisibleRoomsMatchTargetArea'] = rooms_match_area(data['roomCandidates'],area)
    data['filterExplicitlyApplied'] = (filter_mode == 'explicit')
    data['filterMode'] = filter_mode
    data['expectedRoomCount'] = len(AREA_EXPECTED_SHORT_NAMES[area])
    data['roomCandidates'] = [dict(candidate, visibleOrder=i+1)
                              for i,candidate in enumerate(data['roomCandidates'])]
    path = LOG_DIR / (now_cn().strftime("%H%M%S_%f") + f"_会议室清单_{area}.json")
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
    logging.info("[会议室清单] %s 已保存：%s；筛选实际执行=%s（模式=%s）", area, path,filter_mode=="explicit",filter_mode)
    logging.info("[会议室清单] %s：识别到%d个房间名称；名单与目标吻合=%s（不代表实际操作过筛选）",area,
                 len(data['roomCandidates']),data['allVisibleRoomsMatchTargetArea'])
    for candidate in data['roomCandidates'][:50]:
        logging.info('[会议室清单] 可见顺序%d | %s',candidate['visibleOrder'],candidate['name'])
    expected=set(AREA_ROOM_ROWS[area])
    current={room_short_name(candidate['name']) for candidate in data['roomCandidates']}
    if expected-current:
        logging.warning('[会议室清单] 默认列表未包含预设房间%s；正式选房不应依赖固定行号，应先核实筛选结果',sorted(expected-current))
    for item in data.get("namesAndRowsToVerify", [])[:35]:
        logging.info("[会议室清单] x=%s,y=%s｜%s", item['x'], item['y'], item['text'])
    logging.info("[会议室清单] 请以页面实际房间顺序为准；左侧名称可能包含筛选条件文字")


async def run_room_discovery(page: Page) -> None:
    """不依赖ROOM_SELECTION，两个区域均浏览一次，记录可选房间。"""
    for area in AREA_LABELS:
        try:
            await list_area_rooms(page, area)
        except Exception as exc:
            logging.warning("[会议室清单] %s 查看失败：%s", area, str(exc).splitlines()[0])
            await save_debug(page, f"清单失败_{area}")


async def save_grid_diagnostic(page: Page, room_name: str, row: int, start_col: int, end_col: int, *,
                               target: Target | None = None, stage: str = "") -> None:
    """在本机保存选房弹窗的结构摘要，避免猜测 CSS；不记录会议主题/人员等正文。"""
    try:
        js = r"""({roomName, row, startCol, endCol, target, stage}) => {
          const $ = (sel, root=document) => Array.from(root.querySelectorAll(sel));
          const visible = el => { const r=el.getBoundingClientRect(); return r.width>0 && r.height>0; };
          const rect = el => { const r=el.getBoundingClientRect();
            return {x:Math.round(r.x),y:Math.round(r.y),w:Math.round(r.width),h:Math.round(r.height)}; };
          const cls = el => typeof el.className==='string' ? el.className.slice(0,120) : '';
          const info = el => ({tag:el.tagName,cls:cls(el),children:el.children.length,rect:rect(el)});
          const dialog = $('.ev_Dialog_Panel').find(visible) ||
                         $('[role="dialog"], .ev_Dialog_modal').find(visible) || document.body;
          const sampleIndices = Array.from(new Set([1,2,3,19,20,21,24,25,28,startCol,endCol]));
          const isSelectedText = t => (t||'').trim().includes(roomName) ||
               /^(?:[01]?\d|2[0-3]):[0-5]\d$/.test((t||'').trim());
          const candidates = $('div',dialog).filter(el=>{
            if(!visible(el) || el.children.length < 8) return false;
            const r=el.getBoundingClientRect();
            return r.width>180 && r.height>4;
          }).slice(0,65).map(el => {
            const selected = sampleIndices.filter(i=>i<=el.children.length).map(i=>{
              const ch=el.children[i-1];
              const t=ch.textContent?.trim()||'';
              return {index:i,...info(ch),label:isSelectedText(t) && t.length<30 ? t : ''};
            });
            return {...info(el),scrollWidth:el.scrollWidth,clientWidth:el.clientWidth,
              scrollLeft:el.scrollLeft,overflowX:getComputedStyle(el).overflowX,
              preview:selected};
          });
          // Only exact known room names or time strings are allowed in output.
          const roomMentions = $('*',dialog).filter(el=>el.children.length===0 &&
            (el.textContent||'').trim()===roomName).slice(0,10).map(el=>{
              const path=[]; let n=el;
              for(let i=0;i<7 && n;i++,n=n.parentElement) path.push(info(n));
              return path;
            });
          const timeLabels = $('*',dialog).filter(el=>el.children.length===0 && visible(el) &&
            /^(?:[01]?\d|2[0-3]):[0-5]\d$/.test((el.textContent||'').trim()))
            .slice(0,45).map(el=>({time:el.textContent.trim(),...info(el)}));
          const scrollables=$('*',dialog).filter(el=>visible(el) &&
            (el.scrollWidth>el.clientWidth+25 || el.scrollHeight>el.clientHeight+25))
            .slice(0,25).map(el=>({...info(el),scrollWidth:el.scrollWidth,
              clientWidth:el.clientWidth,scrollLeft:el.scrollLeft}));
          const selector = c => row===1 ? `div:nth-child(${c})`:
             `div:nth-child(${row}) > div:nth-child(${c})`;
          const ancestors = el => {
            const out=[]; let e=el;
            for(let i=0;e && i<6;i++,e=e.parentElement) out.push({...info(e),id:e.id||''});
            return out;
          };
          const state = e => ({...info(e),id:e.id||'',role:e.getAttribute('role')||'',
            disabled:typeof e.disabled==='boolean'?e.disabled:null,
            ariaDisabled:e.getAttribute('aria-disabled'),ariaSelected:e.getAttribute('aria-selected'),
            ariaChecked:e.getAttribute('aria-checked'),pointerEvents:getComputedStyle(e).pointerEvents,
            background:getComputedStyle(e).backgroundColor});
          const confirmCandidates=$('button,[role="button"],a,span,div',dialog)
            .filter(e=>visible(e)&&(e.innerText||e.textContent||'').replace(/\s+/g,' ').trim()==='确认')
            .slice(0,25).map(e=>({...state(e),ancestors:ancestors(e)}));
          const selectedCandidates=$('[aria-selected="true"],[aria-checked="true"],[class*="selected"],[class*="checked"]',dialog)
            .filter(visible).slice(0,60).map(state);
          const matchedCells=[startCol,endCol].map(c=>({column:c,
            nodes:$(selector(c)).slice(0,20).map(e=>({...state(e),ancestors:ancestors(e)}))}));
          return {url:location.origin+location.pathname,roomName,row,startCol,endCol,target,stage,
            dialog:info(dialog),confirmCandidates,selectedCandidates,matchedCells,
            selectors:[startCol,endCol].map(c=>({selector:selector(c),matches:$(selector(c)).length})),
            roomMentions,timeLabels,scrollables,candidates};
        }"""
        details = await page.evaluate(js, {"roomName": room_name, "row": row,
                                          "startCol": start_col, "endCol": end_col,
                                          "target": ({"date": target.day.isoformat(), "start": target.start,
                                                      "end": target.end} if target else None), "stage": stage})
        details["globalConfirmRoleCount"] = await page.get_by_role(
            "button", name=CONFIRM_BUTTON, exact=True).count()
        details["dialogConfirmRoleCount"] = await room_selection_dialogs(page).get_by_role(
            "button", name=CONFIRM_BUTTON, exact=True).count()
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        filename = LOG_DIR / (now_cn().strftime("%H%M%S_%f") + f"_格子结构_{room_name}.json")
        filename.write_text(json.dumps(details, ensure_ascii=False, indent=2), encoding="utf-8")
        logging.info("[格子诊断] 已保存增强结构：%s｜候选行%d组，横向滚动容器%d个",
                        filename, len(details.get('candidates', [])), len(details.get('scrollables', [])))
    except Exception as exc:
        logging.warning("[格子诊断] 采集失败：%s", str(exc).splitlines()[0])


async def locate_room_cells(page: Page, room_name: str, room_row: int, start_col: int,
                            end_col: int) -> list[ElementHandle] | None:
    """在唯一选房弹窗内按实际房名、行中心和同父格子核验；所有行使用同一规则。"""
    panels = page.locator(".ev_Dialog_Panel:visible")
    panel_count = await panels.count()
    if panel_count == 0:
        return None  # 异步刷新期间的短暂消失，不永久封禁此房间。
    if panel_count != 1:
        raise SelectorConfigurationError("无法唯一确定当前选房弹窗，拒绝时间格子点击")
    result = await panels.evaluate_handle(r"""(panel, args) => {
        const {startCol,endCol,roomName,roomRow,fullNamePattern,shortNamePattern}=args;
        const fullNameRx=new RegExp(fullNamePattern,'i');
        // 房名和格子在同一次DOM读取中取坐标，避免自动滚动/重绘发生在两次读取之间。
        const rooms=[],seen=new Set();
        const content=panel.querySelector('.ev_Dialog_PanelContent')||panel;
        for(const e of content.querySelectorAll('*')) {
            if(e.children.length) continue;
            const r=e.getBoundingClientRect();
            if(!r.width||!r.height||getComputedStyle(e).visibility==='hidden') continue;
            const m=(e.textContent||'').trim().match(fullNameRx);
            if(!m) continue;
            const name=m[0].replace(/[-_]+$/,''),key=name+'@'+Math.round(r.y);
            if(seen.has(key)) continue;
            seen.add(key); rooms.push({name,r});
        }
        rooms.sort((a,b)=>a.r.y-b.r.y||a.r.x-b.r.x);
        if(rooms.length<roomRow) return {count:0};
        const actual=rooms[roomRow-1];
        const short=actual.name.toUpperCase().match(new RegExp(shortNamePattern));
        if(!roomName.startsWith('F5-1A/第') &&
           (short?short[1].replaceAll('_','-'):actual.name.toUpperCase())!==roomName.toUpperCase())
            return {count:0,identityMismatch:actual.name};
        const centerY=actual.r.y+actual.r.height/2;
        const gaps=rooms.map(x=>Math.abs(x.r.y+x.r.height/2-centerY)).filter(x=>x>1);
        const tolerance=Math.min(Math.max(3,actual.r.height*.6),gaps.length?Math.min(...gaps)*.4:Infinity);
        const valid=[];
        const timeLabel = e => {
            for(const value of [e.getAttribute('data-time'),e.getAttribute('title'),
                                e.getAttribute('aria-label'),e.textContent]) {
                const match=(value||'').trim().match(/^(\d{1,2}):([0-5]\d)$/);
                if(match) return Number(match[1])*60+Number(match[2]);
            }
            return null;
        };
        for(const start of panel.querySelectorAll('div:nth-child('+startCol+')')) {
            const end=start.parentElement?.children[endCol-1];
            if(!end || end.tagName!=='DIV') continue;
            const a=start.getBoundingClientRect(),b=end.getBoundingClientRect();
            if(Math.min(a.width,a.height,b.width,b.height)<=0) continue;
            if([start,end].some(e=>getComputedStyle(e).visibility==='hidden')) continue;
            if(Math.abs(a.y+a.height/2-centerY)>tolerance ||
               Math.abs(b.y+b.height/2-centerY)>tolerance) continue;
            if(Math.abs(a.width-b.width)>Math.max(2,a.width*0.15) ||
               Math.abs(a.height-b.height)>2) continue;
            const difference=endCol-startCol;
            if(Math.abs((b.x-a.x)-difference*a.width)>
               Math.max(3,difference*Math.max(1,a.width*0.05))) continue;
            // 有明确时间时必须符合目标；无标签时只接受完整48个半小时格子。
            // 防止局部时轴（例如从12:00开始）被误当成从00:00开始的整天。
            const startTime=timeLabel(start),endTime=timeLabel(end);
            if((startTime!==null && startTime!==(startCol-1)*30) ||
               (endTime!==null && endTime!==(endCol-1)*30)) continue;
            if(startTime===null || endTime===null) {
                const children=[...start.parentElement.children];
                if(children.length!==48 || children.some((e,i)=>{
                    const r=e.getBoundingClientRect();
                    return e.tagName!=='DIV' || Math.abs(r.width-a.width)>Math.max(2,a.width*.15) ||
                        Math.abs(r.height-a.height)>2 || Math.abs(r.y-a.y)>2 ||
                        Math.abs((r.x-a.x)-(i+1-startCol)*a.width)>Math.max(3,Math.abs(i+1-startCol)*1.5);
                })) continue;
            }
            valid.push({start,end});
        }
        return {count:valid.length,start:valid[0]?.start||null,end:valid[0]?.end||null};
    }""", {"startCol": start_col, "endCol": end_col,
            "roomName": room_name, "roomRow": room_row,
            "fullNamePattern": ROOM_FULL_NAME_PATTERN,
            "shortNamePattern": ROOM_SHORT_NAME_PATTERN}, timeout=ACTION_TIMEOUT_MS)
    kept: list[ElementHandle] = []
    try:
        mismatch = await result.evaluate("value => value.identityMismatch || ''")
        if mismatch:
            raise SelectorConfigurationError(f"第{room_row}行实际房名{mismatch}与目标{room_name}不一致")
        count = await result.evaluate("value => value.count")
        if count > 1:
            raise SelectorConfigurationError(f"第{room_row}行有{count}组不同的时间格子候选，拒绝按first猜选")
        if count == 0:
            return None
        for key in ("start", "end"):
            handle = await result.get_property(key)
            element = handle.as_element()
            if element is None:
                await handle.dispose()
                raise GridNotReadyError("时间格子在核验时消失，需重新读取当前页面")
            kept.append(element)
        return kept
    except BaseException:
        for handle in kept:
            await handle.dispose()
        raise
    finally:
        await result.dispose()


async def locate_first_room_cells(page: Page, room_name: str, start_col: int,
                                  end_col: int) -> list[ElementHandle] | None:
    """兼容旧诊断入口；实际定位规则覆盖全部会议室。"""
    return await locate_room_cells(page, room_name, 1, start_col, end_col)


class GridNotReadyError(RuntimeError):
    """网格仍在重绘或选格动作未完成；允许当前放号窗口内重新准备。"""


async def choose_room_cells(page: Page, room_name: str, room_row: int, target: Target) -> None:
    """逐端点重新定位；单个半小时格只点一次，其余时段分别点起点和终点。"""
    start_col, end_col = cell_columns(target.start, target.end)
    # 用户确认30分钟只需单击；重复点击同一格会改变已选状态。
    columns = (start_col,) if start_col == end_col else (start_col, end_col)
    for endpoint, column in enumerate(columns):
        expires = monotonic_time.monotonic() + max(GRID_WAIT_SECONDS, ACTION_TIMEOUT_MS / 1000) * 2
        last_error = None
        while monotonic_time.monotonic() < expires:
            raise_if_run_stopped()
            pair = None
            try:
                pair = await locate_room_cells(page, room_name, room_row, start_col, end_col)
                if pair is not None:
                    # 正常 Playwright 点击会等待稳定、滚动和遮挡检查；不使用force或JS代点。
                    # 13页同时重绘时，DOM核验本身也耗时；给实际点击完整等待窗口，
                    # 不将剩余预算挤成1ms后立即误报。上层仍按抢订截止/停止取消准备。
                    await pair[endpoint].click(timeout=ACTION_TIMEOUT_MS,
                                               no_wait_after=True)
                    logging.info("[格子核验] [%s] 第%d行%s已点击｜%s-%s｜列%d",
                                 room_name, room_row, "起点" if endpoint == 0 else "终点",
                                 target.start, target.end, column)
                    break
            except SelectorConfigurationError:
                await save_grid_diagnostic(page, room_name, room_row, start_col, end_col,
                                           target=target, stage="时间格子存在歧义或房名不符")
                raise
            except Exception as exc:
                if is_driver_disconnected(exc):
                    raise
                if not (isinstance(exc, (GridNotReadyError, PlaywrightTimeout)) or
                        "not attached" in str(exc).lower() or "detached" in str(exc).lower()):
                    raise
                last_error = exc
            finally:
                if pair:
                    for cell in pair:
                        await cell.dispose()
            await asyncio.sleep(0.1)
        else:
            raise GridNotReadyError(
                f"{room_name}第{room_row}行第{column}列尚未稳定或无法核验时间；本轮未完成，继续重试") from last_error


class StopRequested(RuntimeError):
    """用户停止或环境故障；不中断已提交请求的回执观察。"""
    pass


class AuthBlocked(RuntimeError):
    pass


class SelectorConfigurationError(RuntimeError):
    """页面控件与已录制选择器不匹配；停止持续刷新并保存诊断。"""
    pass


class YearSelectionUnsupported(SelectorConfigurationError):
    """跨年年份控件未录制：仅阻止当前房间/时段，不让其他目标也停摆。"""
    pass


async def is_403_page(page: Page) -> bool:
    """403 检测只在失败时使用，不将任何页面业务文本当成成功认证。"""
    try:
        title = await page.title()
        body = (await page.locator("body").inner_text(timeout=1800))[:1800]
        return bool(re.search(r"\b403\b|forbidden|访问被拒绝|无权限访问", title + "\n" + body, re.I))
    except Exception:
        return False


class PreparationInterrupted(Exception):
    """准备尚未提交，可安全终止以让路或遵守截止时间。"""


async def interruptible_preparation(operation, interrupted: Callable[[], bool]):
    """只用于认证/填表，绝不包裹最终提交及其回执观察。"""
    task = asyncio.create_task(operation)
    try:
        while not task.done():
            if interrupted():
                raise PreparationInterrupted()
            await asyncio.wait({task}, timeout=0.1)
        return await task
    finally:
        if not task.done():
            task.cancel()
        await asyncio.gather(task, return_exceptions=True)


async def activate_booking_page(page: Page, *, wait_for_form: bool = True) -> None:
    """替用户切换工作标签；确认日历已加载后再放行其他标签的初始化。"""
    context = page.context
    if not hasattr(context, "_booking_activation_lock"):
        context._booking_activation_lock = asyncio.Lock()
    async with context._booking_activation_lock:
        if ACTIVATE_TAB_DURING_PREPARE:
            await page.bring_to_front()
        if wait_for_form:
            await page.locator(CALENDAR_ICON).first.wait_for(
                state="visible", timeout=NAVIGATION_TIMEOUT_MS)


async def auth_preflight(page: Page) -> bool:
    """复用Chrome启动时的第一个标签完成SSO，不额外新建登录标签。"""
    logging.info("[登录] 复用首个标签页进行认证，不创建独立登录页")
    try:
        await activate_booking_page(page, wait_for_form=False)
        await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        await activate_booking_page(page, wait_for_form=False)
        until = asyncio.get_running_loop().time() + AUTH_WAIT_SECONDS
        while asyncio.get_running_loop().time() < until:
            if STOP_REQUESTED.is_set():
                raise StopRequested("用户停止登录")
            if await is_403_page(page):
                AUTH_BLOCKED.set()
                logging.error("[登录] 预订页面返回403，停止脚本；请确认账户权限与认证状态")
                await save_debug(page, "登录403")
                return False
            try:
                if await page.get_by_role("button", name=ADD_ROOM_BUTTON).is_visible():
                    logging.info("[登录] 单页认证成功，保留首个标签作为首间会议室的工作页面")
                    return True
            except Exception:
                pass
            await asyncio.sleep(1)
        logging.error("[登录] %s秒内未进入预订表单，可能仍在SSO/二次验证页面", AUTH_WAIT_SECONDS)
        await save_debug(page, "登录未完成")
        return False
    except StopRequested:
        raise
    except Exception as exc:
        logging.error("[登录] 打开预订页失败：%s", str(exc).splitlines()[0])
        await save_debug(page, "登录打开失败")
        return False


class PagePool:
    """只串行各新标签的首次认证访问；后续各页面可并发填表。"""

    def __init__(self, context: BrowserContext, first_page: Page):
        self.context = context
        self._pages = [first_page]
        # 此锁保护工作页池在到点抢订/候补之间的共享，不锁各页面填表。
        self.lock = asyncio.Lock()
        self._cleanup_tasks = set()

    async def _close_unused_page(self, page: Page) -> None:
        try:
            await asyncio.wait_for(page.close(), timeout=4)
        except Exception:
            pass  # 浏览器关闭时也可能已经销毁此页。

    def _discard_late_page(self, creation: asyncio.Task) -> None:
        """new_page请求已发给浏览器时不能假定取消Python等待就取消了创建。"""
        if creation.cancelled() or creation.exception() is not None:
            return
        cleanup = asyncio.create_task(self._close_unused_page(creation.result()))
        self._cleanup_tasks.add(cleanup)
        cleanup.add_done_callback(self._cleanup_tasks.discard)

    async def _new_authenticated_page(self, index: int) -> Page:
        if not hasattr(self.context, "_booking_activation_lock"):
            self.context._booking_activation_lock = asyncio.Lock()
        async with self.context._booking_activation_lock:
            creation = asyncio.create_task(self.context.new_page())
            try:
                page = await asyncio.shield(creation)
            except asyncio.CancelledError:
                creation.add_done_callback(self._discard_late_page)
                raise
        try:
            await self._authenticate_new_page(page, index)
        except BaseException:
            await self._close_unused_page(page)
            raise
        return page

    async def _authenticate_new_page(self, page: Page, index: int) -> None:
        """错峰访问SSO入口；每个新标签需确认已进入预订页后才能并发填表。"""
        logging.info("[认证错峰] 工作标签%d：开始首次认证访问", index)
        try:
            await activate_booking_page(page, wait_for_form=False)
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
            await activate_booking_page(page, wait_for_form=False)
            deadline = monotonic_time.monotonic() + AUTH_WAIT_SECONDS
            while monotonic_time.monotonic() < deadline:
                if STOP_REQUESTED.is_set():
                    raise StopRequested("用户停止登录准备")
                if await is_403_page(page):
                    AUTH_BLOCKED.set()
                    raise AuthBlocked(f"工作标签{index}首次认证遇到403")
                button = page.get_by_role("button", name=ADD_ROOM_BUTTON)
                if await button.count() >= 1 and await button.first.is_visible():
                    logging.info("[认证错峰] 工作标签%d：认证完成，可参加并行填表", index)
                    return
                await asyncio.sleep(0.4)
            raise PlaywrightTimeout(f"工作标签{index}认证等待超时：{AUTH_WAIT_SECONDS}秒内未出现添加会议室")
        except StopRequested:
            raise
        except (PlaywrightTimeout, TimeoutError):
            # 单个标签加载慢不能撤销其他标签已经通过的认证，也不能阻止其到点提交。
            # 此页尚未交付；调用方会关闭它，再按本模式的时序重试。
            raise
        except Exception as exc:
            if isinstance(exc, AuthBlocked):
                AUTH_BLOCKED.set()
            elif is_driver_disconnected(exc):
                DRIVER_LOST.set()
            logging.error("[认证错峰] 工作标签%d：认证未通过：%s", index, str(exc).splitlines()[0])
            if not DRIVER_LOST.is_set():
                await save_debug(page, f"工作标签{index}_认证失败")
            raise

    async def iter_ready(self, needed: int, *, interrupted: Callable[[], bool] | None = None,
                         retry_authentication: bool = False
                         ) -> AsyncIterator[Page]:
        """逐张交付可用页面；新页认证不阻塞已交付页面的准备和抢订。

        候补认证超时结束本轮分配，下轮再试；抢订可在提供截止条件后持续重试。
        未完成认证的页面从不交付给填表/提交任务。
        """
        def should_interrupt() -> bool:
            return (STOP_REQUESTED.is_set() or DRIVER_LOST.is_set()
                    or AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set()
                    or bool(interrupted and interrupted()))

        for page in self._pages[:needed]:
            if should_interrupt():
                raise PreparationInterrupted()
            yield page
        while len(self._pages) < needed:
            if should_interrupt():
                raise PreparationInterrupted()
            index = len(self._pages) + 1
            # 等待激活锁、创建页面、认证整个阶段都可让路；最终提交不在此处。
            try:
                page = await interruptible_preparation(
                    self._new_authenticated_page(index), should_interrupt)
            except (PlaywrightTimeout, TimeoutError) as exc:
                logging.warning("[认证错峰] 工作标签%d：加载/认证等待超时，已关闭未就绪页；其他工作页继续｜%s",
                                index, str(exc).splitlines()[0])
                if not retry_authentication or interrupted is None:
                    logging.info("[认证错峰] 本轮停止新增标签，下轮重新认证")
                    return
                await interruptible_preparation(
                    sleep_or_stop(max(RELEASE_RETRY_SECONDS, AUTH_STAGGER_SECONDS)),
                    should_interrupt)
                continue
            self._pages.append(page)
            logging.info("[页面] 工作标签%d/%d已就绪；后续表单可并发执行", index, needed)
            yield page
            if len(self._pages) < needed and AUTH_STAGGER_SECONDS > 0:
                logging.info("[认证错峰] 下一个标签首次认证前间隔%.1f秒", AUTH_STAGGER_SECONDS)
                await interruptible_preparation(sleep_or_stop(AUTH_STAGGER_SECONDS),
                                                should_interrupt)

    async def get(self, needed: int, *, interrupted=None) -> list[Page]:
        return [page async for page in self.iter_ready(needed, interrupted=interrupted)]


def opening_datetime(target: Target, area: str) -> datetime:
    """使用当前打包区域的提前天数及界面设置的放号时间计算。"""
    return at_cn(target.day - timedelta(days=AREA_ADVANCE_DAYS[area]), RELEASE_OPEN_AT)


def confirmation_unavailable_reason(target: Target, area: str) -> str:
    current = now_cn()
    days = AREA_ADVANCE_DAYS[area]
    opened = opening_datetime(target, area)
    if current < opened:
        return (f"{area}尚未开放（提前{days}天、{RELEASE_OPEN_AT}放号；"
                f"预计开放={opened.strftime('%Y-%m-%d %H:%M:%S')}）")
    if current.date() == target.day - timedelta(days=days) and current < opened + timedelta(minutes=5):
        return f"{area}放号时间附近：可能尚未同步开放，也可能已被预订；不能仅凭按钮判定"
    return f"{area}已到开放时间，但选房尚未确认；按钮状态不能证明房间已被占用"


def room_selection_dialogs(page: Page) -> Locator:
    """按确认文字识别可见面板；按钮角色缺失也不能视为选房已经关闭。"""
    return page.locator(".ev_Dialog_Panel:visible").filter(
        has=page.get_by_text(CONFIRM_BUTTON, exact=True))


async def room_confirmation_control(page: Page) -> Locator | None:
    """只使用当前唯一弹窗内的确认入口，兼容网页自定义按钮缺少button角色。"""
    dialogs = room_selection_dialogs(page)
    if await dialogs.count() != 1:
        return None
    buttons = dialogs.get_by_role("button", name=CONFIRM_BUTTON, exact=True)
    count = await buttons.count()
    if count == 1:
        return buttons
    if count > 1:
        return None
    # 只接受弹窗内唯一可见的精确“确认”文字；动作后仍必须验证弹窗关闭。
    text = dialogs.get_by_text(CONFIRM_BUTTON, exact=True).filter(visible=True)
    return text if await text.count() == 1 else None


async def room_selection_is_open(page: Page) -> bool:
    return await room_selection_dialogs(page).count() > 0


async def wait_selection_confirmation_ready(page: Page) -> bool:
    """给选时段后的异步确认状态一个有界更新窗口；不靠固定短延时猜完成。"""
    deadline = monotonic_time.monotonic() + ACTION_TIMEOUT_MS / 1000
    while True:
        if STOP_REQUESTED.is_set() or DRIVER_LOST.is_set():
            return False
        if await is_confirm_ready(page):
            return not (STOP_REQUESTED.is_set() or DRIVER_LOST.is_set())
        if monotonic_time.monotonic() >= deadline:
            return False
        await asyncio.sleep(min(0.1, max(0.0, deadline - monotonic_time.monotonic())))


async def save_selection_diagnostic_once(page: Page, room: RoomSpec, target: Target, stage: str) -> None:
    """同一目标房间和阶段只保存一次，避免每轮候补重复生成相同诊断。"""
    key = (target.key, f"{room.area}:{room.name}", stage)
    if key in SELECTION_DIAGNOSTICS_RECORDED:
        return
    SELECTION_DIAGNOSTICS_RECORDED.add(key)
    start_col, end_col = cell_columns(target.start, target.end)
    await save_grid_diagnostic(page, room.name, room.row, start_col, end_col,
                               target=target, stage=stage)


async def confirm_room_selection(page: Page) -> bool:
    """确认后必须观察当前选房弹窗关闭；旧节点被替换并不等于选房完成。"""
    dialogs = room_selection_dialogs(page)
    if await dialogs.count() != 1:
        raise RuntimeError("找不到唯一选房弹窗，不能确认选房")
    panel = await dialogs.element_handle(timeout=ACTION_TIMEOUT_MS)
    if panel is None:
        raise RuntimeError("选房弹窗在确认前消失，不能证明选房已完成")
    try:
        control = await room_confirmation_control(page)
        if control is None:
            raise RuntimeError("当前选房弹窗内无法唯一识别确认入口")
        if STOP_REQUESTED.is_set() or DRIVER_LOST.is_set():
            return False
        await control.click(timeout=ACTION_TIMEOUT_MS)
        try:
            # 捕获实际节点，防止重新渲染的同类弹窗让关闭判断漂移。
            await panel.wait_for_element_state("hidden", timeout=ACTION_TIMEOUT_MS)
        except PlaywrightTimeout:
            logging.info("[选房确认] 点击确认后弹窗仍未关闭，当前选房未完成；禁止进入最终预定")
            return False
        if await room_selection_is_open(page):
            logging.info("[选房确认] 原弹窗已隐藏，但仍有选房弹窗显示；禁止进入最终预定")
            return False
        return True
    finally:
        await panel.dispose()


async def prepare_page(page: Page, room: RoomSpec, target: Target, *,
                       watch_mode: bool = False) -> bool | None:
    """并发准备工作页。

    返回 True=已准备好最终预订页面；False=真实准备异常；
    None=完成选格后目标时段仍不可选：确认禁用，或确认后选房弹窗仍未关闭。
    到点抢订在放号前暂停准备，放号后持续重试；空闲候补在下一轮重新检查。
    到点抢订仍以 RELEASE_STOP_AFTER_SECONDS 作为本次窗口的最终截止。
    """
    name, row = room.name, room.row
    if run_stop_reason() or (target.key, name) in GRID_SELECTOR_BLOCKED:
        return False
    step = "激活工作标签"
    started = monotonic_time.monotonic()
    mark = started
    timing_parts: list[tuple[str, float]] = []

    def record_timing(label: str) -> None:
        nonlocal mark
        now = monotonic_time.monotonic()
        spent = now - mark
        timing_parts.append((label, spent))
        if LOG_PREPARE_STEP_TIMINGS:
            logging.info("[表单耗时] [%s] %s：%.2f秒", name, label, spent)
        mark = now

    try:
        logging.info("[页面] [%s] 独立标签并发填写（不限制表单任务并发数）", name)
        record_timing("准备任务启动")
        step = "打开预订页面"
        logging.info("[%s] 准备：%s｜%s", name, step, target.key)
        await activate_booking_page(page, wait_for_form=False)
        # 单页应用的 /#/ 路由相同，goto(相同URL) 未必销毁弹窗/表单状态。
        # 候补第二时段、下一轮查询时必须 reload，避免上轮选房遮挡日历。
        if page.url.split("#", 1)[0] == BOOKING_URL.split("#", 1)[0] and page.url != "about:blank":
            logging.info("[%s] 已在会议室页面，强制刷新以清除上轮弹窗和表单", name)
            await page.reload(wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        else:
            await page.goto(BOOKING_URL, wait_until="domcontentloaded", timeout=NAVIGATION_TIMEOUT_MS)
        await activate_booking_page(page, wait_for_form=False)
        if await is_403_page(page):
            AUTH_BLOCKED.set()
            raise AuthBlocked("页面返回403；停止全部任务，不继续刷新重试")
        step = "激活标签并等待日期控件"
        await activate_booking_page(page)
        step = "等待添加会议室按钮"
        await page.get_by_role("button", name=ADD_ROOM_BUTTON).wait_for(timeout=NAVIGATION_TIMEOUT_MS)
        record_timing("页面加载与可操作等待")
        step = "选择日期"
        await select_date(page, target.day)
        record_timing("选择日期")
        step = "设置开始时间"
        logging.info("[%s] %s: %s", name, step, target.start)
        await select_start_time(page, target.start)
        record_timing("选择开始时间")
        step = "设置会议时长"
        logging.info("[%s] %s", name, step)
        await select_duration(page, target)
        record_timing("选择会议时长")
        step = "筛选区域并打开会议室列表"
        logging.info("[%s] %s：%s", name, step, room.area)
        await open_room_list(page, room.area)
        record_timing("打开并筛选会议室列表")
        step = "核验会议室名称与行号"
        await verify_room_identity(page,room)
        record_timing("核验会议室名称")
        step = "选择会议室时间格子"
        logging.info("[%s] %s: %s第%d行，%s-%s", name, step, room.area, row, target.start, target.end)
        await choose_room_cells(page, name, row, target)
        record_timing("选择时间格子")
        # 真实网页：8:30前选房弹窗中的「确认」可点击；真正的放号校验在主页面「预定」时发生。
        # 因此提前准备时就确认选房，停在最终「预定」按钮前，不向服务端提前提交。
        step = "确认选房并进入最终预定页"
        if not await wait_selection_confirmation_ready(page):
            await save_selection_diagnostic_once(page, room, target, "确认按钮未就绪")
            # 已核验目标房名并完成首尾格点击，且已等待确认状态稳定。
            # 按用户确认的网页规则，这时仍不可确认即按时段占用处理。
            if await watch_confirm_disabled(page):
                logging.info("[%s] 目标时段不可选：完成选格后确认按钮仍禁用｜%s",
                             name, target.key)
                return None
            # 按钮不存在、弹窗结构异常仍属于真正的准备失败；保留诊断。
            raise RuntimeError("选房确认按钮不存在、不可见或无法点击，无法进入最终预定页")
        if not await confirm_room_selection(page):
            await save_selection_diagnostic_once(page, room, target, "点击确认后弹窗未关闭")
            logging.info("[%s] 目标时段不可选：点击确认后选房弹窗仍未关闭｜%s",
                         name, target.key)
            return None
        final_button = page.get_by_role("button", name=FINAL_BOOK_BUTTON, exact=True)
        await final_button.wait_for(state="visible", timeout=ACTION_TIMEOUT_MS)
        if not await is_final_book_button_clickable(final_button):
            logging.info("[%s] 选房弹窗已关闭，但最终预定按钮尚不可点击；等待重新准备｜%s",
                         name, target.key)
            return False
        record_timing("确认选房并进入最终预定页")
        logging.info("[%s] 提前准备就绪，已确认选房、等待放号后点击预定：%s", name, target.key)
        logging.info("[表单耗时汇总] [%s] 墙钟耗时%.2f秒（含浏览器等待/后台节流，并非人工填表操作时长）｜%s", name,
                     monotonic_time.monotonic() - started,
                     "；".join(f"{label}={secs:.1f}s" for label, secs in timing_parts))
        return True
    except StopRequested:
        raise  # 停止信号不是填表失败，不再读取页面或截图。
    except Exception as exc:
        logging.info("[表单耗时汇总] [%s] 未完成｜卡在%s｜已耗时%.2f秒", name, step,
                        monotonic_time.monotonic() - started)
        logging.info("[准备异常详情] %s:%s｜%s｜步骤=%s", room.area, name, target.key,
                     step, exc_info=True)
        # Playwright 驱动已断连时不能再查询页面或截图，否则会将原始错误淹没。
        if is_driver_disconnected(exc):
            DRIVER_LOST.set()
            logging.error("[浏览器连接中断] [%s] 步骤=%s｜%s。停止重试；请检查 Chrome 是否被关闭或崩溃。",
                          name, step, str(exc).splitlines()[0])
            return False
        if await is_403_page(page):
            AUTH_BLOCKED.set()
        if isinstance(exc, GridNotReadyError):
            await save_selection_diagnostic_once(page, room, target, "网格暂未稳定")
        if isinstance(exc, YearSelectionUnsupported):
            GRID_SELECTOR_BLOCKED.add((target.key, name))
            logging.error("[跨年限制] %s:%s｜%s｜当前网页缺少已验证的年份选择操作，暂停此房间/时段；其他任务继续。",
                          room.area, name, target.key)
        elif isinstance(exc, SelectorConfigurationError):
            if step in ("设置开始时间", "设置会议时长", "选择日期"):
                CONFIG_BLOCKED.set()
                logging.error("[停止] 关键表单控件定位结构性失败，停止全局重复刷新")
            else:
                GRID_SELECTOR_BLOCKED.add((target.key, name))
                logging.warning("[%s] 网格定位未验证；只暂停此房间在当前目标时段的重复尝试；其他时段会继续尝试。", name)
        action = ("｜下一轮重新检查" if watch_mode and not (
            AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set()
            or (target.key, name) in GRID_SELECTOR_BLOCKED) else "")
        logging.warning("[准备异常] %s:%s｜%s｜步骤=%s｜耗时%.1f秒｜原因=%s%s",
                        room.area, name, target.key, step,
                        monotonic_time.monotonic() - started, prepare_error_message(exc), action)
        await save_debug(page, f"准备失败_{name}_{step}")
        return False

async def watch_confirm_disabled(page: Page) -> bool:
    """识别当前不可选状态：须找到唯一、可见但不能点击的确认按钮。

    禁用本身不证明占用；定位不到确认按钮、页面未渲染仍属于准备异常。
    """
    try:
        button = await room_confirmation_control(page)
        if button is None or not await button.is_visible():
            return False
        return not await is_confirm_ready(page)
    except Exception as exc:
        if is_driver_disconnected(exc):
            raise
        return False


async def is_confirm_ready(page: Page) -> bool:
    try:
        btn = await room_confirmation_control(page)
        if btn is None or not await btn.is_visible():
            return False
        if not await btn.is_enabled():
            return False
        aria = (await btn.get_attribute("aria-disabled") or "").lower()
        cls = (await btn.get_attribute("class") or "").lower()
        if aria == "true" or re.search(r"(?:^|[\s_-])disabled(?:$|[\s_-])", cls):
            return False
        return not await btn.evaluate(r"""el => {
            for(let node=el;node;node=node.parentElement) {
                const classes=typeof node.className==='string'?node.className:'';
                if(node.disabled || node.getAttribute('aria-disabled')==='true' ||
                   /(?:^|[\s_-])disabled(?:$|[\s_-])/i.test(classes) ||
                   getComputedStyle(node).pointerEvents==='none') return true;
                if(node.classList.contains('ev_Dialog_Panel')) break;
            }
            return false;
        }""")
    except Exception as exc:
        if is_driver_disconnected(exc):
            DRIVER_LOST.set()
        return False


@dataclass
class BookingJob:
    target: Target
    done: asyncio.Event
    uncertain: asyncio.Event = field(default_factory=asyncio.Event)


def shared_booking_job(registry: dict[str, BookingJob], target: Target) -> BookingJob:
    """共享相同日期时段的成功/待确认状态，不共享区域，也不对最终提交加锁。

    到点抢订和空闲候补各自保留areas；任一页面确认真实成功后设置done。
    无回执的提交标记为待确认，暂停该目标的新提交；在途请求仍观察真实成功。
    在服务器回执到达前，多个页面可能都已经发出提交，因此不能保证只订到一间。
    """
    shared = registry.get(target.key)
    if shared is None:
        shared = BookingJob(target, asyncio.Event())
        registry[target.key] = shared
    return BookingJob(target, shared.done, shared.uncertain)


def may_submit(target: Target, deadline: datetime | None = None) -> bool:
    """最终提交前再次检查截止时刻，不能只在本轮开始时检查。"""
    if STOP_REQUESTED.is_set():
        return False
    if deadline is not None and now_cn() >= deadline:
        return False
    if WATCH_STOP_AT_MEETING_START and now_cn() >= target.start_dt:
        return False
    return True


async def is_final_book_button_clickable(button: Locator) -> bool:
    """仅在页面仍显示一个可用的最终预定按钮时，才允许当前页面重复点击。"""
    try:
        if await button.count() != 1 or not await button.is_visible() or not await button.is_enabled():
            return False
        if (await button.get_attribute("aria-disabled") or "").lower() == "true":
            return False
        cls = (await button.get_attribute("class") or "").lower()
        return not re.search(r"(?:^|[\s_-])disabled(?:$|[\s_-])", cls)
    except Exception:
        return False


async def is_room_occupied_state(page: Page, room: RoomSpec, target: Target,
                                  final_btn: Locator | None = None) -> bool:
    """只有网页明确的占用提示才证明占用；控件禁用只表示尚不可提交。"""
    try:
        tip = page.get_by_text(ROOM_OCCUPIED_ERROR_RE).filter(visible=True).first
        return await tip.is_visible()
    except Exception as exc:
        if is_driver_disconnected(exc):
            raise
    return False


async def booking_feedback(page: Page, timeout_ms: int) -> str:
    """并行观察真实回执；明确拒绝立即返回，超时不能当成失败。

    保留驱动异常，调用方据此停止任务；不得把断连吞成可重试的普通超时。
    """
    if timeout_ms <= 0:
        return "timeout"
    signals = {
        "success": page.get_by_text(SUCCESS_TEXT, exact=False).filter(visible=True).first,
        "not_open": page.get_by_text(ADVANCE_LIMIT_ERROR_RE).filter(visible=True).first,
        "failure": page.get_by_text(OTHER_BOOKING_ERROR_RE).filter(visible=True).first,
        "occupied": page.get_by_text(ROOM_OCCUPIED_ERROR_RE).filter(visible=True).first,
    }
    tasks = {
        key: asyncio.create_task(locator.wait_for(state="visible", timeout=timeout_ms))
        for key, locator in signals.items()
    }
    all_tasks = tuple(tasks.values())
    try:
        while tasks:
            completed, pending = await asyncio.wait(tasks.values(), return_when=asyncio.FIRST_COMPLETED)
            for key in ("success", "not_open", "occupied", "failure"):
                task = tasks.get(key)
                if task in completed and not task.cancelled() and task.exception() is None:
                    # 同一提示可匹配多种模式，成功及明确业务原因优先。
                    if key == "failure":
                        for specific in ("success", "not_open", "occupied"):
                            if await signals[specific].is_visible():
                                return specific
                    return key
            for task in completed:
                if task.cancelled():
                    continue
                error = task.exception()
                if error is not None and not isinstance(error, (PlaywrightTimeout, asyncio.TimeoutError)):
                    raise error
            tasks = {key: task for key, task in tasks.items() if task in pending}
        return "timeout"
    finally:
        for task in all_tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*all_tasks, return_exceptions=True)


async def booking_failure_dialog(page: Page) -> tuple[str, str]:
    """读取当前可见的预约结果弹窗；返回分类与网页实际文本原文。

    不能对原文 strip/折叠空白/截断，也不能因为连续弹窗相同而省略。
    注意：Playwright inner_text 是页面可见文本，而不是原始 HTML。
    """
    # 优先整块弹窗正文，避免只取其中“预约失败”4个字而丢失房间/详细原因。
    for selector in (".ev_Dialog_Panel", "[role='alertdialog']", "[role='alert']", ".ev_Message"):
        try:
            panels = page.locator(selector)
            for i in range(min(await panels.count(), 12)):
                panel = panels.nth(i)
                if not await panel.is_visible():
                    continue
                raw = await panel.inner_text(timeout=1000)
                if not raw.strip():
                    continue
                if SUCCESS_TEXT in raw:
                    return "success", raw
                if ADVANCE_LIMIT_ERROR_RE.search(raw):
                    return "not_open", raw
                if ROOM_OCCUPIED_ERROR_RE.search(raw):
                    return "occupied", raw
                if OTHER_BOOKING_ERROR_RE.search(raw):
                    return "failure", raw
                # 未匹配任何明确结果文字的弹窗不能直接判成“预订失败”。
                # 原文仍须记录；调用方会区分未知提示，不误触“超过天数”或“已占用”分支。
                return "unknown", raw
        except Exception as exc:
            if is_driver_disconnected(exc):
                raise
    # toast / 单行提示可能没有模态弹窗：仍然保留网页返回的原始可见文本。
    for kind, pattern in (("success", SUCCESS_TEXT),
                          ("not_open", ADVANCE_LIMIT_ERROR_RE),
                          ("occupied", ROOM_OCCUPIED_ERROR_RE),
                          ("failure", OTHER_BOOKING_ERROR_RE)):
        try:
            tip = (page.get_by_text(pattern, exact=False) if kind == "success" else page.get_by_text(pattern)).filter(visible=True).first
            if await tip.is_visible():
                raw = await tip.inner_text(timeout=1000)
                if raw.strip():
                    return kind, raw
        except Exception as exc:
            if is_driver_disconnected(exc):
                raise
    return "", ""


def log_booking_popup_raw(room: str, attempt: int, raw: str) -> None:
    """逐次打印弹窗原文；保留空格和换行，不截断、不去重。"""
    if raw:
        logging.warning("[弹窗原文][%s][第%d次提交]\n%s", room, attempt, raw)


async def dismiss_booking_failure_dialog(page: Page, *, allow_unknown: bool = False,
                                         verify_cleared: bool = False) -> bool:
    """关闭当前预订结果弹窗。

    默认只关闭已识别的预约失败/占用弹窗；allow_unknown=True时用于最终提交阶段，
    对未识别的异常弹窗也尝试关闭，但若检测到仍是“添加会议室”选房弹窗则绝不误关。
    返回False表示旧结果仍未消失，应重新准备，不能把旧失败当成下一次提交的回执。
    重试间隔由调用方统一等待一次，本函数只确认旧提示已经隐藏。
    verify_cleared仅在已收到失败回执后检查toast，避免增加首次提交的页面查询。
    """
    for selector in (".ev_Dialog_Panel", "[role='alertdialog']"):
        try:
            panels = page.locator(selector)
            for i in range(min(await panels.count(), 8)):
                panel = panels.nth(i)
                if not await panel.is_visible():
                    continue
                content = await panel.inner_text(timeout=700)
                if SUCCESS_TEXT in content:
                    continue

                known_failure = bool(
                    ADVANCE_LIMIT_ERROR_RE.search(content)
                    or ROOM_OCCUPIED_ERROR_RE.search(content)
                    or OTHER_BOOKING_ERROR_RE.search(content)
                )
                if not known_failure:
                    if not allow_unknown:
                        continue
                    # 最终提交阶段才允许处理未知弹窗；若里面仍有会议室名称，
                    # 说明可能还是选房弹窗，宁可不碰也不能误关。
                    try:
                        if await read_visible_room_names(page):
                            continue
                    except Exception:
                        pass

                # 已进入最终预订页；已识别失败弹窗和允许处理的未知异常弹窗
                # 均只关闭当前结果弹窗，不触碰其他页面。
                close = panel.locator(".ev_Dialog_closeIcon")
                if await close.count() and await close.first.is_visible():
                    await close.first.click(timeout=1200)
                    try:
                        await panel.wait_for(state="hidden", timeout=1200)
                    except PlaywrightTimeout:
                        return False
                    return True
                # 对没有关闭图标的结果弹窗，仅使用其内部明确的关闭/确定按钮。
                for label in ("关闭", "确定", "知道了"):
                    button = panel.get_by_role("button", name=label, exact=True)
                    if await button.count() == 1 and await button.is_visible():
                        await button.click(timeout=1200)
                        try:
                            await panel.wait_for(state="hidden", timeout=1200)
                        except PlaywrightTimeout:
                            return False
                        return True
                logging.warning("[失败弹窗] 已识别/允许处理的异常弹窗，但未找到关闭控件，转入常规重试")
                return False
        except Exception as exc:
            if is_driver_disconnected(exc):
                raise
    if not verify_cleared:
        return True
    # Toast没有关闭按钮，也必须等旧失败消失，防止下一次等待读到旧回执。
    for pattern in (ADVANCE_LIMIT_ERROR_RE, ROOM_OCCUPIED_ERROR_RE, OTHER_BOOKING_ERROR_RE):
        tip = page.get_by_text(pattern).filter(visible=True).first
        if await tip.is_visible():
            try:
                await tip.wait_for(state="hidden", timeout=ACTION_TIMEOUT_MS)
            except PlaywrightTimeout:
                logging.warning("[失败提示] 旧失败提示未消失，重新准备页面后再试")
                return False
    return True


async def dismiss_advance_limit_dialog(page: Page) -> None:
    """兼容旧的提前天数错误处理入口。"""
    await dismiss_booking_failure_dialog(page)


async def confirm_and_book(page: Page, room: RoomSpec, job: BookingJob, *,
                           deadline: datetime | None = None,
                           yield_requested: asyncio.Event | None = None) -> str:
    """各房间并行提交，成功即广播；明确失败才允许原页重试。

    每次点击都独立观察完整回执窗口；失败提示出现即返回，无需等满窗口。
    无回执或未知弹窗表示结果待确认，暂停该目标的新提交，避免重复预约。
    截止时间只阻止新提交，已发出的请求仍须观察回执。
    """
    room_name = room.name

    def halted_status() -> str | None:
        # 同步检查后直接点击，中间不得再插入页面 await。
        if job.done.is_set():
            return "skipped"
        if job.uncertain.is_set():
            return "uncertain"
        if yield_requested is not None and yield_requested.is_set():
            return "yielded"
        if (STOP_REQUESTED.is_set() or DRIVER_LOST.is_set()
                or AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set()):
            return "unavailable"
        if not may_submit(job.target, deadline):
            return "expired"
        return None

    def unresolved(reason: str) -> str:
        if job.done.is_set():
            return "skipped"
        job.uncertain.set()
        logging.warning("[结果待确认] [%s] %s｜%s；暂停该时段的新提交，请核对实际预约记录后再启动",
                        room_name, job.target.key, reason)
        return "uncertain"

    halted = halted_status()
    if halted is not None:
        return halted
    final_btn = page.get_by_role("button", name=FINAL_BOOK_BUTTON, exact=True)
    try:
        selection_open = await room_selection_is_open(page)
        final_visible = (not selection_open and await final_btn.count() == 1
                         and await final_btn.is_visible())
    except Exception as exc:
        if is_driver_disconnected(exc):
            DRIVER_LOST.set()
            return "unavailable"
        final_visible = False

    if not final_visible:
        if not await wait_selection_confirmation_ready(page):
            await save_selection_diagnostic_once(page, room, job.target, "提交前确认按钮未就绪")
            if await is_room_occupied_state(page, room, job.target, final_btn):
                logging.info("[%s] 网页明确提示房间已被预订｜%s", room_name, job.target.key)
                return "occupied"
            why = confirmation_unavailable_reason(job.target, room.area)
            logging.info("[%s] 确认不可点击：%s｜%s", room_name, why, job.target.key)
            return "not_open" if now_cn() < opening_datetime(job.target, room.area) else "unavailable"
        try:
            if not await confirm_room_selection(page):
                await save_selection_diagnostic_once(page, room, job.target, "提交前选房弹窗未关闭")
                logging.info("[%s] 选房弹窗未关闭，当前选房未完成；不进入最终预定", room_name)
                return "unavailable"
            await final_btn.wait_for(state="visible", timeout=2500)
        except Exception as exc:
            if is_driver_disconnected(exc):
                DRIVER_LOST.set()
            logging.warning("[%s] 选房确认或进入最终预定页失败：%s", room_name, str(exc).splitlines()[0])
            return "unavailable"

    if await room_selection_is_open(page):
        return "unavailable"
    if not await is_final_book_button_clickable(final_btn):
        if await is_room_occupied_state(page, room, job.target, final_btn):
            logging.info("[%s] 网页明确提示房间已被预订｜%s", room_name, job.target.key)
            return "occupied"
        return "not_open" if now_cn() < opening_datetime(job.target, room.area) else "unavailable"
    if await room_selection_is_open(page):
        return "unavailable"
    halted = halted_status()
    if halted is not None:
        return halted
    if room.area == "F5-1A" and room.name.startswith("F5-1A/第") and not F5_1A_ROW_MAPPING_VERIFIED and not DRY_RUN:
        logging.error("[%s] F5-1A行号尚未核对，拒绝正式提交", room_name)
        return "unavailable"
    if DRY_RUN:
        logging.info("[模拟] [%s] 已到最终预定按钮，未提交：%s", room_name, job.target.key)
        return "dry_run"

    success_tip = page.get_by_text(SUCCESS_TEXT, exact=False).filter(visible=True).first
    attempts = 0
    other_failure_extra_count = 0
    advance_error_count = 0
    while True:
        halted = halted_status()
        if halted is not None:
            return halted
        try:
            if await success_tip.is_visible():
                job.done.set()
                kind, raw = await booking_failure_dialog(page)
                log_booking_popup_raw(room_name, attempts, raw)
                logging.warning("[成功] [%s] %s｜提交第%d次后收到成功提示", room_name, job.target.key, attempts)
                return "success"
        except Exception as exc:
            if job.done.is_set():
                return "success"
            if is_driver_disconnected(exc):
                DRIVER_LOST.set()
                return "unavailable"

        # 选房弹窗重现时交上层重新准备，不得把它当失败回执关闭后点击背景按钮。
        if await room_selection_is_open(page):
            logging.info("[%s] 选房弹窗重新出现，停止本页最终提交并重新准备", room_name)
            return "unavailable"

        if not await dismiss_booking_failure_dialog(page):
            return "failed"
        if not await is_final_book_button_clickable(final_btn):
            if await is_room_occupied_state(page, room, job.target, final_btn):
                logging.info("[%s] 预定按钮已不可点击，网页提示已被预订；后续检查将刷新页面", room_name)
                return "occupied"
            return "failed"

        if await room_selection_is_open(page):
            return "unavailable"
        halted = halted_status()
        if halted is not None:
            return halted
        click_timeout_ms = ACTION_TIMEOUT_MS
        stop_at = deadline
        if WATCH_STOP_AT_MEETING_START:
            stop_at = min(stop_at, job.target.start_dt) if stop_at is not None else job.target.start_dt
        if stop_at is not None:
            click_timeout_ms = min(click_timeout_ms, max(0, int((stop_at - now_cn()).total_seconds() * 1000)))
            if click_timeout_ms <= 0:
                return "expired"
        attempts += 1
        try:
            await final_btn.click(timeout=click_timeout_ms)
        except Exception as exc:
            if is_driver_disconnected(exc):
                DRIVER_LOST.set()
                return unresolved("点击预定时浏览器断连，无法确认请求结果")
            # 点击可能已送出而在等待页面响应时超时，仍先观察回执。
            logging.warning("[%s] 第%d次点击预定异常：%s", room_name, attempts, str(exc).splitlines()[0])

        try:
            feedback = await booking_feedback(page, SUCCESS_TIMEOUT_MS)
        except Exception as exc:
            if is_driver_disconnected(exc):
                DRIVER_LOST.set()
                return unresolved("等待预订回执时浏览器断连")
            feedback = "timeout"
            logging.warning("[%s] 读取预订回执异常：%s", room_name, str(exc).splitlines()[0])
        if feedback == "success":
            # 先通知其他页面停止新点击，再读取可能较慢的原文或关闭弹窗。
            job.done.set()
        try:
            popup_type, popup_text = await booking_failure_dialog(page)
        except Exception as exc:
            if is_driver_disconnected(exc):
                DRIVER_LOST.set()
            logging.warning("[%s] 读取弹窗原文失败：%s", room_name, str(exc).splitlines()[0])
            popup_type, popup_text = "", ""
        log_booking_popup_raw(room_name, attempts, popup_text)
        if feedback == "success" or popup_type == "success":
            job.done.set()
            logging.warning("[成功] [%s] %s｜提交第%d次收到成功回执", room_name, job.target.key, attempts)
            try:
                close = page.locator(".ev_Dialog_closeIcon")
                if await close.count() == 1:
                    await close.click(timeout=1200)
            except Exception:
                pass
            return "success"
        if job.done.is_set():
            return "skipped"
        if popup_type in ("not_open", "occupied", "failure"):
            feedback = popup_type
        if feedback not in ("not_open", "occupied", "failure"):
            return unresolved("提交后未收到明确成功/失败回执" if popup_type != "unknown" else "提交后出现未识别的结果提示")

        # 此次请求已经有明确拒绝，才允许关闭结果并重试。
        halted = halted_status()
        if halted is not None:
            return halted
        if not popup_text:
            logging.warning("[弹窗原文][%s][第%d次提交] 未能读取到可见的弹窗文字", room_name, attempts)
        if feedback == "occupied":
            # 已明确拒绝，下一次将整页刷新重新选房；不等待关闭动画，
            # 也不能因关闭失败把已知占用改成普通失败。
            logging.info("[%s] 收到已被预订/占用回执；后续检查将刷新页面", room_name)
            return "occupied"
        if not await dismiss_booking_failure_dialog(page, verify_cleared=True):
            return "failed"
        if feedback == "not_open":
            advance_error_count += 1
            if advance_error_count % 20 == 0:
                logging.warning("[%s] 提前天数限制已重试%d次；仍在截止时间内复用表单", room_name, advance_error_count)
        else:
            if other_failure_extra_count >= OTHER_FAILURE_EXTRA_RETRIES:
                logging.warning("[%s] 其他失败弹窗原页面额外重试已达%d次，转常规重新填表", room_name, OTHER_FAILURE_EXTRA_RETRIES)
                return "failed"
            other_failure_extra_count += 1
        # 先等关闭动画/按钮恢复，再判断可点击，不能把恢复中的禁用态当作占用。
        await sleep_or_stop(POPUP_RETRY_SECONDS)
        if not await is_final_book_button_clickable(final_btn):
            if await is_room_occupied_state(page, room, job.target, final_btn):
                return "occupied"
            return "failed"



async def release_worker(page: Page, room: RoomSpec, job: BookingJob,
                         opened: datetime, deadline: datetime,
                         prepared: bool | None) -> str:
    """单个会议室在同一放号窗口内持续抢订。

    放号前占用只暂停提前准备，放号后占用与技术异常均持续重试至截止。
    每间独立执行，其他房间的慢页面不阻碍就绪页面到点提交。
    """
    if job.done.is_set():
        return "skipped"
    if job.uncertain.is_set():
        return "uncertain"
    raise_if_run_stopped()
    if prepared is None and now_cn() < opened:
        logging.info("[到点抢订] 提前准备暂停｜%s:%s｜%s｜目标时段占用/不可选；不重复提前准备，%s放号后恢复重试至%s",
                     room.area, room.name, job.target.key, opened.strftime('%H:%M:%S'),
                     deadline.strftime('%H:%M:%S'))
    await wait_until(opened)
    logging.info("[到点抢订] 单页进入抢订阶段｜%s:%s｜%s｜表单就绪=%s｜计划开放=%s｜当前=%s",
                 room.area, room.name, job.target.key, prepared,
                 opened.strftime('%H:%M:%S'), now_cn().strftime('%H:%M:%S.%f')[:-3])
    had_attempt = False
    last_status = "occupied" if prepared is None else ("prepare_failed" if not prepared else "unavailable")

    while (now_cn() < deadline and not STOP_REQUESTED.is_set() and not DRIVER_LOST.is_set()
           and not AUTH_BLOCKED.is_set() and not CONFIG_BLOCKED.is_set()
           and (job.target.key, room.name) not in GRID_SELECTOR_BLOCKED
           and not job.done.is_set() and not job.uncertain.is_set()):

        selection_unavailable = False
        if not prepared:
            try:
                prepared = await interruptible_preparation(
                    prepare_page(page, room, job.target),
                    lambda: now_cn() >= deadline or bool(run_stop_reason())
                    or job.done.is_set() or job.uncertain.is_set())
            except PreparationInterrupted:
                break
            selection_unavailable = prepared is None
        if job.done.is_set():
            return "skipped"
        if job.uncertain.is_set():
            return "uncertain"
        if run_stop_reason() or (job.target.key, room.name) in GRID_SELECTOR_BLOCKED:
            break
        if prepared:
            status = await confirm_and_book(page, room, job, deadline=deadline)
            had_attempt = True
        else:
            status = "occupied" if selection_unavailable else "prepare_failed"
        last_status = status

        if status in ("success", "dry_run", "expired", "skipped", "uncertain"):
            booking_result("到点抢订", job.target, room, status)
            return status
        if run_stop_reason() or (job.target.key, room.name) in GRID_SELECTOR_BLOCKED:
            break
        if status not in ("occupied", "prepare_failed"):
            booking_result("到点抢订", job.target, room, status)

        if prepared and status == "not_open":
            btn = page.get_by_role("button", name=FINAL_BOOK_BUTTON, exact=True)
            await dismiss_advance_limit_dialog(page)
            prepared = await is_final_book_button_clickable(btn)
            if prepared:
                logging.info("[%s] 放号尚未生效，复用已确认表单，不重新填写", room.name)
        else:
            prepared = False

        remaining = max(0.0, (deadline - now_cn()).total_seconds())
        if (remaining <= 0 or run_stop_reason() or job.done.is_set() or job.uncertain.is_set()
                or (job.target.key, room.name) in GRID_SELECTOR_BLOCKED):
            break
        if status == "occupied":
            evidence = "完成选格后仍不可确认，按占用处理" if selection_unavailable else "网页明确提示已被预订"
            logging.info("[到点抢订] 占用重试｜%s:%s｜%s｜%s；%.1f秒后刷新重试，截止=%s",
                         room.area, room.name, job.target.key, evidence,
                         min(RELEASE_RETRY_SECONDS, remaining), deadline.strftime('%H:%M:%S'))
        else:
            logging.info("[%s] 本次未订到（状态=%s），%.1f秒后重试", room.name, status,
                         min(RELEASE_RETRY_SECONDS, remaining))
        await sleep_or_stop(min(RELEASE_RETRY_SECONDS, remaining))

    if job.done.is_set():
        if not had_attempt:
            booking_result("到点抢订", job.target, room, "skipped")
    elif job.uncertain.is_set():
        booking_result("到点抢订", job.target, room, "uncertain")
        return "uncertain"
    elif reason := run_stop_reason():
        booking_result("到点抢订", job.target, room, "stopped", detail=reason)
        return "stopped"
    elif (job.target.key, room.name) in GRID_SELECTOR_BLOCKED:
        booking_result("到点抢订", job.target, room, "blocked")
        return "blocked"
    elif now_cn() >= deadline:
        booking_result("到点抢订", job.target, room, "expired",
                       detail="本房间本次抢订结束；最后状态=" + last_status)
        return "expired"
    elif not had_attempt:
        booking_result("到点抢订", job.target, room, last_status)
    return "skipped" if job.done.is_set() else last_status


async def prepare_until_release(page: Page, room: RoomSpec, target: Target,
                                opened: datetime, deadline: datetime) -> bool | None:
    """提前准备期间每间独立重试，已准备成功的页面原样保留，等待放号。

    明确不可选交给 release_worker 暂停准备并等放号；技术异常在放号前持续重试。
    放号后由 release_worker 接管所有未就绪页面；不重新准备成功页面。
    """
    attempt = 0
    last_result = False
    while (now_cn() < opened and now_cn() < deadline
           and not STOP_REQUESTED.is_set() and not DRIVER_LOST.is_set() and not AUTH_BLOCKED.is_set()
           and not CONFIG_BLOCKED.is_set()
           and (target.key, room.name) not in GRID_SELECTOR_BLOCKED):
        attempt += 1
        try:
            ok = await interruptible_preparation(
                prepare_page(page, room, target),
                lambda: now_cn() >= deadline or bool(run_stop_reason()))
        except PreparationInterrupted:
            return False
        last_result = ok
        if ok is None:
            return None
        if ok:
            if attempt > 1:
                logging.warning("[提前准备恢复] %s:%s｜%s｜第%d次尝试成功，等待放号",
                                room.area, room.name, target.key, attempt)
            return True
        if (run_stop_reason()
                or (target.key, room.name) in GRID_SELECTOR_BLOCKED):
            break
        remaining = (opened - now_cn()).total_seconds()
        if remaining <= 0:
            break
        logging.warning("[提前准备重试] %s:%s｜%s｜第%d次未完成，%.1f秒后重试",
                        room.area, room.name, target.key, attempt,
                        min(RELEASE_RETRY_SECONDS, remaining))
        await sleep_or_stop(min(RELEASE_RETRY_SECONDS, remaining))
    return last_result


async def run_release_batch(pool: PagePool, release_priority: asyncio.Event,
                            registry: dict[str, BookingJob], batch: ReleaseDayBatch) -> None:
    """只处理本放号日开放的任务：首次认证错峰，其余并发。"""
    if STOP_REQUESTED.is_set():
        return
    booking_day = batch.release_day
    pairs = batch.tasks
    opened = at_cn(booking_day, RELEASE_OPEN_AT)
    deadline = opened + timedelta(seconds=RELEASE_STOP_AFTER_SECONDS)
    if now_cn() >= deadline:
        logging.warning("[到点抢订] %s 的放号窗口已经结束，不再执行", booking_day)
        for target, room in pairs:
            booking_result("到点抢订", target, room, "expired")
        return
    # 相同目标跨模式共享状态；成功或结果待核实都不能再次准备提交。
    def is_pending(target: Target) -> bool:
        job = shared_booking_job(registry, target)
        return not job.done.is_set() and not job.uncertain.is_set()

    for target in dict.fromkeys(t for t, _ in pairs):
        job = shared_booking_job(registry, target)
        if job.uncertain.is_set() and not job.done.is_set():
            logging.warning("[到点抢订] %s 上次提交结果待核实，暂停此目标；其他目标继续", target.key)
    active_pairs = [(t, r) for t, r in pairs if is_pending(t)]
    if not active_pairs:
        return
    # 开始准备和截止时刻均由同一个放号时间计算。
    prepare_at = opened - timedelta(seconds=RELEASE_PREPARE_BEFORE_SECONDS)
    groups_label = ", ".join(f"提前{g.advance_days}天:{g.area}({len(g.tasks)}页)"
                             for g in batch.groups)
    logging.info("[到点抢订] 放号日=%s｜%s｜目标%d个、实际需准备页面%d个｜计划准备=%s｜开放=%s｜截止=%s",
                 booking_day, groups_label, len({t.key for t, _ in active_pairs}), len(active_pairs),
                 prepare_at.strftime('%Y-%m-%d %H:%M:%S'),
                 opened.strftime('%H:%M:%S'), deadline.strftime('%H:%M:%S'))
    started_at = now_cn()
    if prepare_at < started_at < opened:
        logging.warning("[到点抢订] 启动晚于计划准备%.0f秒｜距放号仅%.1f秒｜需准备%d页；立即开始，已就绪页到点独立提交",
                        (started_at - prepare_at).total_seconds(),
                        (opened - started_at).total_seconds(), len(active_pairs))
    elif started_at >= opened:
        logging.warning("[到点抢订] 已进入放号窗口｜剩余%.1f秒｜立即准备并尝试提交",
                        (deadline - started_at).total_seconds())
    guard = prepare_at - timedelta(seconds=WATCH_PREP_GUARD_SECONDS)
    if now_cn() < guard:
        await wait_until(guard)
    release_priority.set()
    logging.info("[到点抢订] 开始接管工作页面；候补停止新提交，在途请求收齐回执后让路")
    # 开点通知独立于页面准备/页面池等待，不能让慢页把“到点”日志拖迟。
    # 这是当前准备状态快照，并不表示已向服务器提交。
    page_phases = {(target.key, room.area, room.name): "等待页面" for target, room in active_pairs}

    async def observe_opening() -> None:
        await wait_until(opened)
        if run_stop_reason():
            return
        counts = {phase: sum(value == phase for value in page_phases.values())
                  for phase in ("就绪", "准备中", "等待页面", "准备异常", "占用待重试")}
        logging.info("[到点抢订] 已到计划开放时刻｜计划=%s｜当前=%s｜表单就绪%d页｜准备中%d页｜等待页面%d页｜准备异常%d页｜占用待重试%d页；就绪页独立提交，未就绪及占用页继续重试至%s（此条不是提交回执）",
                     opened.strftime('%H:%M:%S'), now_cn().strftime('%H:%M:%S.%f')[:-3],
                     counts["就绪"], counts["准备中"], counts["等待页面"],
                     counts["准备异常"], counts["占用待重试"], deadline.strftime('%H:%M:%S'))

    opening_observer = asyncio.create_task(observe_opening())
    try:
        async with pool.lock:
            active_pairs = [(t, r) for t, r in active_pairs if is_pending(t)]
            if not active_pairs:
                return
            if STOP_REQUESTED.is_set():
                return
            if now_cn() >= deadline:
                for target, room in active_pairs:
                    booking_result("到点抢订", target, room, "expired")
                return
            async def prepare_and_run(page: Page, room: RoomSpec, target: Target) -> str:
                job = shared_booking_job(registry, target)
                await wait_until(prepare_at)
                if job.done.is_set():
                    return "skipped"
                if job.uncertain.is_set():
                    return "uncertain"
                phase_key = (target.key, room.area, room.name)
                page_phases[phase_key] = "准备中"
                if now_cn() >= opened:
                    logging.info("[到点抢订] 已在放号窗口，直接开始抢订准备｜%s:%s｜%s｜截止=%s",
                                 room.area, room.name, target.key, deadline.strftime('%H:%M:%S'))
                    return await release_worker(page, room, job, opened, deadline, False)
                logging.info("[到点抢订] 开始准备｜%s:%s｜%s｜计划=%s｜实际=%s", room.area,
                             room.name, target.key, prepare_at.strftime('%H:%M:%S'),
                             now_cn().strftime('%H:%M:%S.%f')[:-3])
                try:
                    ready = await interruptible_preparation(
                        prepare_until_release(page, room, target, opened, deadline),
                        lambda: job.done.is_set() or job.uncertain.is_set()
                        or bool(run_stop_reason()) or now_cn() >= deadline)
                except PreparationInterrupted:
                    return ("skipped" if job.done.is_set() else "uncertain" if job.uncertain.is_set()
                            else "stopped" if run_stop_reason() else "expired")
                if run_stop_reason():
                    return "stopped"
                page_phases[phase_key] = "就绪" if ready else ("占用待重试" if ready is None else "准备异常")
                logging.info("[到点抢订] 单页准备结果｜%s｜%s:%s｜就绪=%s",
                             target.key, room.area, room.name, ready)
                # 每间准备好后独立等待放号，不等其它慢页的导航或控件超时。
                if job.done.is_set():
                    return "skipped"
                if job.uncertain.is_set():
                    return "uncertain"
                return await release_worker(page, room, job, opened, deadline, ready)

            workers = []
            allocation_error = None
            try:
                try:
                    async for page in pool.iter_ready(
                            len(active_pairs), retry_authentication=True,
                            interrupted=lambda: now_cn() >= deadline
                            or STOP_REQUESTED.is_set() or DRIVER_LOST.is_set()
                            or AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set()
                            or not any(is_pending(t) for t, _ in active_pairs)):
                        target, room = active_pairs[len(workers)]
                        workers.append(asyncio.create_task(prepare_and_run(page, room, target)))
                except PreparationInterrupted:
                    logging.info("[到点抢订] 停止新增页面：任务结束、已停止或已到截止时间")
                except Exception as exc:
                    allocation_error = exc
                # 一个页面异常不抢先释放页面池，其他在途预约仍需正常收尾。
                results = await asyncio.gather(*workers, return_exceptions=True)
            finally:
                # 外部取消时也要收齐子任务，不能让旧任务操作已经交给候补的页面。
                for worker in workers:
                    if not worker.done():
                        worker.cancel()
                await asyncio.gather(*workers, return_exceptions=True)
            for target in dict.fromkeys(t for t, _ in active_pairs):
                job = shared_booking_job(registry, target)
                if job.done.is_set():
                    booking_result("到点抢订汇总", target, None, "success")
                elif job.uncertain.is_set():
                    booking_result("到点抢订汇总", target, None, "uncertain")
                elif DRY_RUN and any(st == "dry_run" for (candidate, _), st in zip(active_pairs, results)
                                     if candidate.key == target.key):
                    booking_result("到点抢订汇总", target, None, "dry_run")
                elif reason := run_stop_reason():
                    booking_result("到点抢订汇总", target, None, "stopped", detail=reason)
                else:
                    booking_result("到点抢订汇总", target, None, "failed")
            errors = [result for result in results if isinstance(result, BaseException)]
            if allocation_error is not None:
                errors.append(allocation_error)
            if errors:
                raise errors[0]
    finally:
        if not opening_observer.done():
            opening_observer.cancel()
        await asyncio.gather(opening_observer, return_exceptions=True)
        release_priority.clear()
        logging.info("[到点抢订] 已释放工作标签页")


async def run_release(pool: PagePool, release_priority: asyncio.Event,
                      registry: dict[str, BookingJob]) -> None:
    """手动目标执行一次；每周目标按实际放号日滚动排程，已错过的周期批次自动跳过。"""
    manual = parse_targets(RELEASE_TARGETS, "到点抢订")
    weekly = parse_weekly_rules(WEEKLY_RELEASE_RULES)
    if not manual and not weekly:
        logging.warning("[到点抢订] 没有配置手动或每周规则")
        return
    manual_keys = {t.key for t in manual}
    processed_days: set[date] = set()
    last_plan_signature = None
    while not (STOP_REQUESTED.is_set() or DRIVER_LOST.is_set()
               or AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set()):
        now = now_cn()
        # 每轮只生成未来一段时间。周期规则先生成预约日期，再由区域提前天数换算实际放号日。
        generated = expand_weekly_targets(weekly, now.date(), WEEKLY_LOOKAHEAD_DAYS)
        future_manual = [t for t in manual if t.day >= now.date()]
        targets = merge_release_targets(future_manual + generated)
        batches = build_release_batches(targets, ROOMS) if targets else []

        # 关键规则：
        # 1) 周期规则如果对应的实际放号窗口已经过去，这一周已经错过，不应继续作为“待执行任务”反复提示。
        # 2) 手动目标仍保留明确的“放号窗口已过”告警，方便发现配置过晚。
        # 3) 当前放号日尚未开放或仍在300秒窗口内，必须保留为pending并正常等待/抢订。
        pending = []
        for batch in batches:
            if batch.release_day in processed_days:
                continue
            close_at = at_cn(batch.release_day, RELEASE_OPEN_AT) + timedelta(seconds=RELEASE_STOP_AFTER_SECONDS)
            if now >= close_at:
                processed_days.add(batch.release_day)
                expired_manual_targets = sorted({
                    target.key for target, _room in batch.tasks if target.key in manual_keys
                })
                if expired_manual_targets:
                    logging.warning(
                        "[到点抢订] 手动目标放号窗口已过｜放号日=%s｜目标=%s｜需要捡漏请配候补",
                        batch.release_day, "、".join(expired_manual_targets)
                    )
                continue
            pending.append(batch)

        signature = tuple((b.release_day, b.max_pages) for b in pending)
        if signature != last_plan_signature:
            preview = "; ".join(f"{b.release_day}({b.max_pages}页)" for b in pending[:7])
            logging.info("[循环放号计划] 下批放号日：%s%s", preview or "暂无", " ..." if len(pending)>7 else "")
            last_plan_signature = signature

        if not pending:
            if not weekly:
                return
            # 没有临近任务时，保持低频唤醒，不频繁刷新页面。
            await sleep_or_stop(3600)
            continue

        batch = pending[0]
        await run_release_batch(pool, release_priority, registry, batch)
        processed_days.add(batch.release_day)
        # 清理旧任务状态，长期挂起时防止无限增长。
        cutoff = now_cn().date() - timedelta(days=30)
        for key, job in list(registry.items()):
            if job.target.day < cutoff:
                registry.pop(key, None)
        processed_days = {d for d in processed_days if d >= cutoff}


async def watch_cycle(pages: list[Page] | AsyncIterator[Page], job: BookingJob, active_rooms: list[RoomSpec],
                      *, deadline: datetime | None = None,
                      yield_requested: asyncio.Event | None = None) -> None:
    """候补每轮并行检查：已占用是正常状态，不是准备失败，也无需截图。"""
    cycle_started = monotonic_time.monotonic()
    def should_yield() -> bool:
        return yield_requested is not None and yield_requested.is_set()

    def preparation_finished() -> bool:
        return (should_yield() or STOP_REQUESTED.is_set() or job.done.is_set()
                or job.uncertain.is_set() or DRIVER_LOST.is_set() or AUTH_BLOCKED.is_set()
                or CONFIG_BLOCKED.is_set() or not may_submit(job.target, deadline))

    async def single(page: Page, room: RoomSpec) -> str:
        if should_yield():
            return "yielded"
        if STOP_REQUESTED.is_set() or job.done.is_set():
            return "skipped"
        if job.uncertain.is_set():
            return "uncertain"
        try:
            ready = await interruptible_preparation(
                prepare_page(page, room, job.target, watch_mode=True), preparation_finished)
        except PreparationInterrupted:
            return "yielded" if should_yield() else "skipped"
        if should_yield():
            return "yielded"
        if STOP_REQUESTED.is_set() or job.done.is_set():
            return "skipped"
        if job.uncertain.is_set():
            return "uncertain"
        if ready is None:
            # 页面已加载、目标房间已选中，但确认按钮禁用；继续定时捡漏。
            return "occupied"
        if ready is False:
            # prepare_page 已打印带原因、阶段和耗时的一条异常，不再重复泛化结果。
            return "prepare_failed"
        if DRIVER_LOST.is_set() or AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set():
            booking_result("空闲候补", job.target, room, "skipped", detail="环境异常")
            return "skipped"
        if job.done.is_set():
            return "skipped"
        if job.uncertain.is_set():
            return "uncertain"
        status = await confirm_and_book(page, room, job, deadline=deadline,
                                        yield_requested=yield_requested)
        if status == "unavailable":
            # 检查时可能刚被其他人订走；依然算当前不可选，不算脚本异常。
            return "occupied"
        if status not in ("skipped", "yielded"):
            # 只有确实尝试过预订的结果（成功/提交失败/模拟等）才单独打印WARNING。
            booking_result("空闲候补", job.target, room, status)
        return status

    # 准备与提交按房间串联，空闲房间无需等待另一间完成筛选。
    async def ready_pages():
        if hasattr(pages, "__aiter__"):
            async for page in pages:
                yield page
        else:
            for page in pages[:len(active_rooms)]:
                yield page

    workers = []
    allocation_error = None
    try:
        try:
            async for page in ready_pages():
                if preparation_finished():
                    break
                room = active_rooms[len(workers)]
                workers.append(asyncio.create_task(single(page, room)))
        except PreparationInterrupted:
            pass
        except Exception as exc:
            allocation_error = exc
        results = await asyncio.gather(*workers, return_exceptions=True)
    finally:
        for worker in workers:
            if not worker.done():
                worker.cancel()
        await asyncio.gather(*workers, return_exceptions=True)
    if should_yield():
        logging.info("[候补] 已停止填表和新提交，在途回执已收齐，交还工作页面给到点抢订")
    occupied = results.count("occupied")
    failed_preparation = results.count("prepare_failed")
    submitted = sum(result in ("success", "failed", "dry_run", "expired", "not_open")
                    for result in results)
    success = results.count("success")
    logging.info("[候补] 时段完成 %s｜检查%d间｜已占用/不可选%d间｜提交/模拟%d间｜准备异常%d间｜成功%d间｜耗时%.1f秒%s",
                 job.target.key, len(workers), occupied, submitted,
                 failed_preparation, success, monotonic_time.monotonic()-cycle_started,
                 ("｜提交结果待核实，暂停该时段" if job.uncertain.is_set() and not job.done.is_set()
                  else ""))

    errors = [result for result in results if isinstance(result, BaseException)]
    if allocation_error is not None:
        errors.append(allocation_error)
    if errors:
        raise errors[0]


async def run_watch(pool: PagePool, release_priority: asyncio.Event,
                    registry: dict[str, BookingJob]) -> None:
    if not WATCH_TARGETS:
        logging.warning("[候补] WATCH_TARGETS 为空，不执行")
        return
    if isinstance(WATCH_INTERVAL_SECONDS, bool) or not isinstance(WATCH_INTERVAL_SECONDS, (int, float)) or WATCH_INTERVAL_SECONDS < 1:
        raise ValueError("WATCH_INTERVAL_SECONDS 必须为 >=1 的秒数，请遵守系统查询频率限制")
    if WATCH_DURATION_MINUTES is not None and (
            isinstance(WATCH_DURATION_MINUTES, bool)
            or not isinstance(WATCH_DURATION_MINUTES, (int, float))
            or WATCH_DURATION_MINUTES <= 0):
        raise ValueError("WATCH_DURATION_MINUTES 必须为大于0的分钟数，或填 None（不限制）")
    jobs = [shared_booking_job(registry, target)
            for target in parse_targets(WATCH_TARGETS, "候补")]
    if not jobs:
        logging.warning("[候补] 没有有效候补目标")
        return

    # 所有工作标签共用唯一的候补轮询时钟：整轮开始时记时，全部标签并发填表；
    # 下一轮绝不与上一轮重叠，轮间等待=max(0, 周期-本轮实际耗时)。
    watch_started_at = now_cn()
    watch_end_at = (watch_started_at + timedelta(minutes=WATCH_DURATION_MINUTES)
                    if WATCH_DURATION_MINUTES is not None else None)
    watch_deadline_monotonic = (monotonic_time.monotonic() + WATCH_DURATION_MINUTES * 60
                                if WATCH_DURATION_MINUTES is not None else None)

    def duration_ended() -> bool:
        return (watch_deadline_monotonic is not None
                and monotonic_time.monotonic() >= watch_deadline_monotonic)

    logging.info("[候补] 开始监控｜%s｜每%s秒一轮（含页面操作）｜持续=%s｜截止=%s",
                 "；".join(j.target.key for j in jobs), f"{WATCH_INTERVAL_SECONDS:g}",
                 f"{WATCH_DURATION_MINUTES}分钟" if WATCH_DURATION_MINUTES is not None else "不限",
                 watch_end_at.strftime("%Y-%m-%d %H:%M:%S") if watch_end_at is not None else "未设置")
    reported_unopened: set[str] = set()
    reported_uncertain: set[str] = set()
    reported_started: set[str] = set()
    stopped_due_to_duration = False
    while (not STOP_REQUESTED.is_set() and not DRIVER_LOST.is_set()
           and not AUTH_BLOCKED.is_set() and not CONFIG_BLOCKED.is_set()):
        if duration_ended():
            stopped_due_to_duration = True
            break
        for job in jobs:
            if (job.uncertain.is_set() and not job.done.is_set()
                    and job.target.key not in reported_uncertain):
                reported_uncertain.add(job.target.key)
                logging.warning("[候补] %s 提交结果待核实，暂停此目标；其他目标继续", job.target.key)
            if (WATCH_STOP_AT_MEETING_START and not job.done.is_set() and not job.uncertain.is_set()
                    and now_cn() >= job.target.start_dt and job.target.key not in reported_started):
                reported_started.add(job.target.key)
                logging.info("[候补] 已停止此目标｜%s｜已到会议开始时间%s，不再尝试预订",
                             job.target.key, job.target.start_dt.strftime('%Y-%m-%d %H:%M:%S'))
        remaining = [j for j in jobs if not j.done.is_set() and not j.uncertain.is_set()
                     and (not WATCH_STOP_AT_MEETING_START or now_cn() < j.target.start_dt)]
        if not remaining:
            success_count = sum(j.done.is_set() for j in jobs)
            uncertain_count = sum(j.uncertain.is_set() and not j.done.is_set() for j in jobs)
            logging.info("[候补] 所有时段已结束｜成功%d个｜结果待核实%d个｜已到会议开始时间%d个",
                         success_count, uncertain_count, len(jobs) - success_count - uncertain_count)
            break
        if release_priority.is_set():
            # 到点抢订进行中让出工作页面；计时不会暂停。
            wait_seconds = 1 if watch_deadline_monotonic is None else max(
                0.0, min(1.0, watch_deadline_monotonic - monotonic_time.monotonic()))
            if wait_seconds > 0:
                await sleep_or_stop(wait_seconds)
            continue
        round_started = monotonic_time.monotonic()  # 单一轮询时钟，绝不为每个房间创建独立定时器
        for job in remaining:
            if (STOP_REQUESTED.is_set() or duration_ended() or DRIVER_LOST.is_set() or AUTH_BLOCKED.is_set()
                    or CONFIG_BLOCKED.is_set() or release_priority.is_set()):
                break
            if job.done.is_set() or job.uncertain.is_set():
                continue
            if WATCH_STOP_AT_MEETING_START and now_cn() >= job.target.start_dt:
                if job.target.key not in reported_started:
                    reported_started.add(job.target.key)
                    logging.info("[候补] 已停止此目标｜%s｜已到会议开始时间%s，不再尝试预订",
                                 job.target.key, job.target.start_dt.strftime('%Y-%m-%d %H:%M:%S'))
                continue
            active_rooms: list[RoomSpec] = []
            for room in ROOMS:
                if not job.target.includes_room(room):
                    continue  # 本候补时段没有选择这个区域，不能擅自尝试。
                if (job.target.key, room.name) in GRID_SELECTOR_BLOCKED:
                    continue  # 已确定该时段控件无法安全定位，不继续刷同一错误。
                room_open = opening_datetime(job.target, room.area)
                if now_cn() < room_open:
                    report_key = f"{job.target.key}|{room.area}"
                    if report_key not in reported_unopened:
                        logging.warning("[候补结果] %s｜%s提前%d天放号，尚未开放（预计%s），暂跳过此区域",
                                     job.target.key, room.area, AREA_ADVANCE_DAYS[room.area],
                                     room_open.strftime('%Y-%m-%d %H:%M'))
                        reported_unopened.add(report_key)
                    continue
                active_rooms.append(room)
            if not active_rooms:
                continue
            async with pool.lock:
                if release_priority.is_set() or duration_ended():
                    break
                if job.done.is_set() or job.uncertain.is_set():
                    continue
                pages = pool.iter_ready(len(active_rooms), interrupted=lambda:
                                        release_priority.is_set() or duration_ended()
                                        or STOP_REQUESTED.is_set() or job.done.is_set()
                                        or job.uncertain.is_set())
                logging.info("[候补] 正在查询 %s｜已开放房间=%s", job.target.key,
                             [f"{room.area}:{room.name}" for room in active_rooms])
                # 设置绝对截止时间，阻止超出候补时长后继续点击最终预定。
                await watch_cycle(pages, job, active_rooms, deadline=watch_end_at,
                                  yield_requested=release_priority)
        if STOP_REQUESTED.is_set() or DRIVER_LOST.is_set() or AUTH_BLOCKED.is_set() or CONFIG_BLOCKED.is_set():
            break
        if duration_ended():
            stopped_due_to_duration = True
            break
        round_elapsed = monotonic_time.monotonic() - round_started
        sleep_for = max(0.0, WATCH_INTERVAL_SECONDS - round_elapsed)  # 填表时间已经包含在round_elapsed
        logging.info("[候补] 本轮耗时%.1f秒；设定轮询间隔%s秒（含填表）；下一轮额外等待%.1f秒%s",
                     round_elapsed, WATCH_INTERVAL_SECONDS, sleep_for,
                     "（本轮超时，立即开始下一轮）" if sleep_for == 0 else "")
        if watch_deadline_monotonic is not None:
            sleep_for = min(sleep_for, max(0.0, watch_deadline_monotonic - monotonic_time.monotonic()))
        if sleep_for > 0:
            await sleep_or_stop(sleep_for)
    if stopped_due_to_duration:
        logging.info("[候补] 已达到设定持续时间%s分钟，停止候补检查（不影响独立的到点抢订）",
                     WATCH_DURATION_MINUTES)
    if DRIVER_LOST.is_set():
        logging.error("[候补] Chrome/Playwright 连接断开，候补任务已停止；未确认任何新预订")
    if CONFIG_BLOCKED.is_set():
        logging.error("[候补] 控件定位有误，已暂停；请看截图与日志")

async def run_modes_safely(*operations) -> None:
    """某模式异常时通知另一模式安全收尾，避免它仍等待下一放号日。"""
    async def guarded(operation):
        try:
            return await operation
        except Exception:
            STOP_REQUESTED.set()
            raise

    outcomes = await asyncio.gather(*(guarded(operation) for operation in operations),
                                    return_exceptions=True)
    for outcome in outcomes:
        if isinstance(outcome, BaseException) and not isinstance(outcome, StopRequested):
            raise outcome


async def main(mode: Literal["release", "watch", "both", "list-rooms"]) -> None:
    setup_logging()
    try:
        account = require_authorized_account()
    except AuthorizationError as exc:
        logging.error("[账号验证] %s", exc)
        raise
    logging.info("[账号验证] Windows账号 %s 已获授权", account)
    if not BOOKING_URL.startswith(("http://", "https://")):
        raise SystemExit("请填写 BOOKING_URL（不含临时登录token）")
    logging.info("[启动] %s｜%s｜浏览器=%s｜候选会议室%d间",
                 {"release": "仅到点抢订", "watch": "仅空闲候补", "both": "到点抢订 + 空闲候补",
                  "list-rooms": "查看会议室列表"}[mode],
                 "模拟运行，不提交预订" if DRY_RUN else "正式预订", BROWSER_CHANNEL, len(ROOMS))
    logging.info("会议室分组：%s", {area: selected for area, selected in ROOM_SELECTION.items()})
    logging.info("提前放号天数：%s；统一放号时间：%s", AREA_ADVANCE_DAYS, RELEASE_OPEN_AT)
    logging.info("到点抢订目标：%s；每周循环规则：%s", RELEASE_TARGETS, WEEKLY_RELEASE_RULES)
    logging.info("页面调度：首次认证错峰%.1f秒；表单及最终预订均并发（无最终提交锁）；一间成功后其他页面停止", AUTH_STAGGER_SECONDS)
    logging.info("候补目标：%s", WATCH_TARGETS)
    if mode in ("release", "both"):
        parse_targets(RELEASE_TARGETS, "到点抢订")
        parse_weekly_rules(WEEKLY_RELEASE_RULES)
    if mode in ("watch", "both"):
        parse_targets(WATCH_TARGETS, "候补")
    if any(room.area == "F5-1A" and room.name.startswith("F5-1A/第") for room in ROOMS) and not F5_1A_ROW_MAPPING_VERIFIED:
        logging.warning("F5-1A按纯行号选房尚未人工核实；建议用会议室名称选房")
    if STOP_REQUESTED.is_set():
        logging.warning("[运行] 启动前已请求停止")
        return
    PROFILE_DIR.mkdir(parents=True, exist_ok=True)
    async with async_playwright() as p:
        context = await p.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), channel=BROWSER_CHANNEL,
            headless=HEADLESS, accept_downloads=False,
            viewport={"width": 1536, "height": 900},
        )
        context.set_default_timeout(ACTION_TIMEOUT_MS)
        lifecycle = {"closing": False}

        def on_unexpected_context_close(_context) -> None:
            if not lifecycle["closing"]:
                DRIVER_LOST.set()
                logging.error("[浏览器连接中断] Chrome 浏览器上下文意外关闭；检查是否手动关闭浏览器、公司策略终止进程或 Chrome 崩溃")

        def watch_page_crash(page: Page) -> None:
            def on_page_crash(_page) -> None:
                DRIVER_LOST.set()
                logging.error("[浏览器页面崩溃] %s；停止抢订和候补，避免结果不明时重复提交", page.url)
            page.on("crash", on_page_crash)

        context.on("close", on_unexpected_context_close)
        context.on("page", watch_page_crash)
        for existing_page in context.pages:
            watch_page_crash(existing_page)
        try:
            # 持久化Chrome通常自带一个about:blank页，直接使用，不再多开登录标签。
            if context.pages:
                first_page = context.pages[0]
                logging.info("[页面] 复用Chrome初始标签，当前标签数量=%d", len(context.pages))
            else:
                first_page = await context.new_page()
                logging.info("[页面] Chrome没有初始标签，新建首个标签")
            if not await auth_preflight(first_page):
                logging.error("[停止] 登录检查未通过，不启动抢订/候补")
                return
            # 关闭之前自动化遗留的多余空白页；绝不关闭认证/会议室页面。
            for extra in list(context.pages):
                if extra != first_page and not extra.is_closed() and extra.url == "about:blank":
                    await extra.close()
                    logging.info("[页面] 已清理多余的about:blank标签")
            if mode == "list-rooms":
                await run_room_discovery(first_page)
                return
            pool = PagePool(context, first_page)
            priority = asyncio.Event()
            registry: dict[str, BookingJob] = {}
            if mode == "release":
                await run_release(pool, priority, registry)
            elif mode == "watch":
                await run_watch(pool, priority, registry)
            else:
                # 停止时允许两个并行模式均到达安全检查点，再统一关闭Chrome。
                # 否则release等待期间先抛StopRequested可能使watch尚未收尾时就关闭浏览器。
                await run_modes_safely(
                    run_release(pool, priority, registry),
                    run_watch(pool, priority, registry),
                )
            if reason := run_stop_reason():
                logging.warning("[运行] %s；本次任务已停止", reason)
        except StopRequested as exc:
            logging.warning("[运行] %s；正在安全关闭浏览器", run_stop_reason() or str(exc))
            return
        except Exception:
            logging.exception("[主流程异常] 运行中发生未处理异常，下面保留原始堆栈供排查")
            raise
        finally:
            lifecycle["closing"] = True
            try:
                # 如果驱动已经断连，context.close() 本身还会抛异常。
                # 关闭失败只记警告，不能覆盖导致退出的原始异常。
                if not DRIVER_LOST.is_set():
                    await asyncio.wait_for(context.close(), timeout=8)
            except Exception as close_exc:
                logging.warning("[退出清理] Chrome 关闭请求失败（不代表预订失败）：%s",
                                str(close_exc).splitlines()[0])
            logging.info("[退出] 主流程结束；Chrome 连接状态=%s",
                         "已断开" if DRIVER_LOST.is_set() else "未检测到异常断连")


def apply_gui_settings(settings: dict) -> None:
    """由GUI调用，应用并校验一份配置；运行期间不得修改。"""
    global ROOM_SELECTION, ROOMS, RELEASE_TARGETS, WATCH_TARGETS, WEEKLY_RELEASE_RULES, DRY_RUN
    global RELEASE_OPEN_AT, RELEASE_PREPARE_BEFORE_SECONDS, RELEASE_STOP_AFTER_SECONDS
    global RELEASE_RETRY_SECONDS, POPUP_RETRY_SECONDS, WATCH_INTERVAL_SECONDS, WATCH_DURATION_MINUTES
    global WATCH_PREP_GUARD_SECONDS, WATCH_STOP_AT_MEETING_START, AUTH_STAGGER_SECONDS
    global VERBOSE_LOGS, LOG_PREPARE_STEP_TIMINGS, AUTH_BLOCKED, CONFIG_BLOCKED
    global DRIVER_LOST, GRID_SELECTOR_BLOCKED, STOP_REQUESTED, LAST_DEBUG_CAPTURE, SELECTION_DIAGNOSTICS_RECORDED
    if not isinstance(settings, dict):
        raise ValueError("配置必须是JSON对象")
    if settings.get("deployment_id", DEPLOYMENT_ID) != DEPLOYMENT_ID:
        raise ValueError("该配置属于其他区域，不能用于当前程序")
    rooms = settings.get("rooms", {})
    if not isinstance(rooms, dict):
        raise ValueError("会议室选择配置无效")
    # 必须先校验原始输入，不能先过滤未知区域或把字符串拆成列表。
    # 行号一律取编译进程序的名单，不读取用户JSON中的任何行号配置。
    resolved_rooms = resolve_rooms(rooms)
    ROOM_SELECTION = {key: list(rooms.get(key, [])) for key in AREA_ROOM_ROWS}
    ROOMS = resolved_rooms
    release = settings.get("release_targets", [])
    watch = settings.get("watch_targets", [])
    weekly = settings.get("weekly_release_rules", [])
    parse_weekly_rules(weekly)
    parse_targets(release, "到点抢订")
    parse_targets(watch, "空闲候补")
    RELEASE_TARGETS = release
    WATCH_TARGETS = watch
    WEEKLY_RELEASE_RULES = weekly
    DRY_RUN = bool(settings.get("dry_run", True))
    RELEASE_OPEN_AT = settings.get("release_open_at", DEFAULT_RELEASE_OPEN_AT)
    if not isinstance(RELEASE_OPEN_AT, str) or len(RELEASE_OPEN_AT) != 8:
        raise ValueError("放号时间必须填写 HH:MM:SS")
    time.fromisoformat(RELEASE_OPEN_AT)
    RELEASE_PREPARE_BEFORE_SECONDS = int(settings.get("prepare_before_seconds", 300))
    RELEASE_STOP_AFTER_SECONDS = int(settings.get("stop_after_seconds", 300))
    RELEASE_RETRY_SECONDS = float(settings.get("retry_seconds", 0.9))
    POPUP_RETRY_SECONDS = float(settings.get("popup_retry_seconds", 0.2))
    WATCH_INTERVAL_SECONDS = float(settings.get("watch_interval_seconds", 30))
    duration = settings.get("watch_duration_minutes", 120)
    WATCH_DURATION_MINUTES = None if duration is None else float(duration)
    WATCH_PREP_GUARD_SECONDS = int(settings.get("watch_guard_seconds", 120))
    WATCH_STOP_AT_MEETING_START = bool(settings.get("stop_at_meeting_start", True))
    AUTH_STAGGER_SECONDS = float(settings.get("auth_stagger_seconds", 1.5))
    VERBOSE_LOGS = bool(settings.get("verbose_logs", False))
    LOG_PREPARE_STEP_TIMINGS = bool(settings.get("step_timing_logs", False))
    if not 0 <= RELEASE_PREPARE_BEFORE_SECONDS <= 86400:
        raise ValueError("提前准备秒数应在0~86400范围内")
    if not 1 <= RELEASE_STOP_AFTER_SECONDS <= 86400:
        raise ValueError("抢订持续时间应在1~86400秒范围内")
    if not 0.5 <= RELEASE_RETRY_SECONDS <= 3600:
        raise ValueError("抢订重试间隔应在0.5~3600秒范围内")
    if not 0.1 <= POPUP_RETRY_SECONDS <= 60:
        raise ValueError("失败弹窗原页重试间隔应在0.1~60秒范围内")
    if not 1 <= WATCH_INTERVAL_SECONDS <= 86400:
        raise ValueError("候补轮询间隔应在1~86400秒范围内")
    if WATCH_DURATION_MINUTES is not None and not 0 < WATCH_DURATION_MINUTES <= 525600:
        raise ValueError("候补持续时间应为正数或留空")
    if not 0 <= WATCH_PREP_GUARD_SECONDS <= 3600:
        raise ValueError("候补让出页面时间应在0~3600秒范围内")
    if not 0 <= AUTH_STAGGER_SECONDS <= 120:
        raise ValueError("错峰认证时间应在0~120秒范围内")
    # 每次启动必须清除上次运行的停止、403或控件结构失败状态。
    AUTH_BLOCKED = asyncio.Event()
    CONFIG_BLOCKED = asyncio.Event()
    DRIVER_LOST = asyncio.Event()
    STOP_REQUESTED = asyncio.Event()
    GRID_SELECTOR_BLOCKED = set()
    SELECTION_DIAGNOSTICS_RECORDED = set()
    LAST_DEBUG_CAPTURE = {}


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="会议室 08:30 多日期/时段到点抢订 + 多日期/时段空闲候补")
    ap.add_argument("--mode", choices=("release", "watch", "both", "list-rooms"), default="both")
    args = ap.parse_args()
    try:
        asyncio.run(main(args.mode))
    except KeyboardInterrupt:
        print("已手动停止")
