# -*- coding: utf-8 -*-
"""管理员维护的部署配置；构建时只把选中的一套写入程序。

此模块只供源码和构建工具使用。用户的运行配置不能新增区域、房号或行号。
每个区域的名单须完整覆盖 full_name_pattern 匹配到的房间，并按网页顺序编号。
只配置部分会议室时，完整房名规则必须严格限定这些房间；行号使用匹配后的
相对顺序（从1开始），不能填写它们在全部房间中的原始行号。
运行时按真实房名定位；账号只能看到部分房间时，不修改管理员名单及配置编号。
"""
from __future__ import annotations

from copy import deepcopy
from datetime import time
from pprint import pformat
import re


PROFILES = {
    "F5": {
        "id": "F5",
        "label": "F5 / F5-1A",
        "city": "深圳(Shenzhen)",
        "zone": "F",
        "full_name_pattern": r"CN[-_]ShenZhen[-_]F5[-_][A-Za-z0-9_-]+",
        "short_name_pattern": r"(?:^|[-_])(\d+F?[-_][A-Z]\d+R)$",
        "release_open_at": "08:30:00",
        "expected_counts": {"F5": 3, "F5-1A": 13},
        "room_catalog": {
            "F5": {
                "label": "科研楼F5", "advance_days": 7,
                "rooms": {"7F-A05R": 1, "7F-A28R": 2, "7F-B02R": 3},
            },
            "F5-1A": {
                "label": "科研楼F5-1A外来接待区", "advance_days": 3,
                "rooms": {
                    "1F-A10R": 1, "1F-A11R": 2, "1F-A12R": 3,
                    "1F-A13R": 4, "1F-A14R": 5, "1F-A15R": 6,
                    "1F-A16R": 7, "1F-A17R": 8, "1F-A18R": 9,
                    "1F-A20R": 10, "1F-A24R": 11, "1F-A25R": 12,
                    "1F-A26R": 13,
                },
            },
        },
    },
    "H7": {
        "id": "H7",
        "label": "H7",
        "city": "东莞(DongGuan)",
        "zone": "溪流背坡村(XLBPC)",
        "full_name_pattern": r"溪村[-_]H7[-_][A-Za-z0-9_-]+",
        "short_name_pattern": r"(?:^|[-_])(\d+F?[-_][A-Z]\d+R)$",
        "release_open_at": "08:45:00",
        "expected_counts": {"H7": 15},
        "room_catalog": {
            "H7": {
                "label": "H7", "advance_days": 7,
                # 用户提供的完整15间名单，行号按网页从上到下的顺序。
                "rooms": {
                    "2-A12R": 1, "2-A13R": 2, "2-A14R": 3,
                    "2-A15R": 4, "2-B12R": 5, "2-B13R": 6,
                    "2-B14R": 7, "3-A12R": 8, "3-A14R": 9,
                    "3-A15R": 10, "3-B13R": 11, "3-B14R": 12,
                    "4-A11R": 13, "4-A12R": 14, "4-A14R": 15,
                },
            },
        },
    },
    "B2a": {
        "id": "B2a",
        "label": "华电B2（指定4间）",
        "city": "深圳(Shenzhen)",
        "zone": "B",
        # 严格匹配指定4间，其他32间不进入候选名单。
        "full_name_pattern": r"^CN[-_]HQ[-_]B2a[-_]2[-_](?:A48R|A50R|B35R|B41R)$",
        "short_name_pattern": r"(?:^|[-_])(\d+F?[-_][A-Z]\d+R)$",
        "release_open_at": "08:30:00",
        "expected_counts": {"B2a": 4},
        "room_catalog": {
            "B2a": {
                "label": "华电B2", "advance_days": 7,
                # 网页原始第22~25行；这里只数上方规则匹配的4间，依次为1~4。
                # 选格依据核验后的完整房名所在位置，不会点击原始第1~4行。
                "rooms": {
                    "2-A48R": 1, "2-A50R": 2,
                    "2-B35R": 3, "2-B41R": 4,
                },
            },
        },
    },
}

_PROFILE_FIELDS = {
    "id", "label", "city", "zone", "full_name_pattern", "short_name_pattern",
    "release_open_at", "expected_counts", "room_catalog",
}
_SAFE_ID = re.compile(r"[A-Za-z][A-Za-z0-9_-]{0,31}\Z")


def _nonempty_text(value, label: str) -> str:
    if not isinstance(value, str) or not value.strip() or value != value.strip():
        raise ValueError(f"{label}必须为非空文字，不能带首尾空格")
    return value


def _safe_id(value, label: str) -> str:
    if not isinstance(value, str) or _SAFE_ID.fullmatch(value) is None:
        raise ValueError(f"{label}只能使用字母开头的英文字母、数字、下划线或短横线，不能包含路径")
    return value


def validate_profile(profile: dict) -> None:
    """验证单套配置，不导入运行核心，也不检查其他未选中的部署配置。"""
    if not isinstance(profile, dict):
        raise ValueError("部署配置必须为字典")
    missing = _PROFILE_FIELDS - profile.keys()
    extra = profile.keys() - _PROFILE_FIELDS
    if missing:
        raise ValueError("部署配置缺少字段：" + "、".join(sorted(missing)))
    if extra:
        raise ValueError("部署配置包含未支持的字段：" + "、".join(sorted(map(str, extra))))
    profile_id = _safe_id(profile["id"], "部署编号")
    for name, label in (("label", "部署名称"), ("city", "城市"), ("zone", "园区")):
        _nonempty_text(profile[name], f"{profile_id}的{label}")
    patterns = {}
    for name, label in (("full_name_pattern", "完整房名规则"),
                        ("short_name_pattern", "短房名规则")):
        pattern = _nonempty_text(profile[name], f"{profile_id}的{label}")
        try:
            patterns[name] = re.compile(pattern)
        except re.error as exc:
            raise ValueError(f"{profile_id}的{label}不是有效的正则表达式") from exc
    if patterns["short_name_pattern"].groups != 1:
        raise ValueError(f"{profile_id}的短房名规则必须恰好包含一个用于提取房号的分组")
    opening = profile["release_open_at"]
    if not isinstance(opening, str) or re.fullmatch(r"\d{2}:\d{2}:\d{2}", opening) is None:
        raise ValueError(f"{profile_id}的放号时间必须使用HH:MM:SS格式")
    try:
        # 显式检查每天的时钟范围，避免某些Python版本将24:00:00归一化成次日零点。
        time(*(int(part) for part in opening.split(":")))
    except ValueError as exc:
        raise ValueError(f"{profile_id}的放号时间无效：{opening}") from exc

    catalog, counts = profile["room_catalog"], profile["expected_counts"]
    if not isinstance(catalog, dict) or not catalog:
        raise ValueError(f"{profile_id}必须包含至少一个会议室区域")
    if not isinstance(counts, dict) or set(counts) != set(catalog):
        raise ValueError(f"{profile_id}的区域名单与预期房间数量配置必须一一对应")
    for area, config in catalog.items():
        _safe_id(area, "区域编号")
        if not isinstance(config, dict):
            raise ValueError(f"{profile_id}/{area}的区域配置必须为字典")
        _nonempty_text(config.get("label"), f"{profile_id}/{area}的网页区域名称")
        days = config.get("advance_days")
        if isinstance(days, bool) or not isinstance(days, int) or not 0 <= days <= 365:
            raise ValueError(f"{profile_id}/{area}的提前天数必须为0~365的整数")
        expected = counts[area]
        if isinstance(expected, bool) or not isinstance(expected, int) or not 1 <= expected <= 50:
            raise ValueError(f"{profile_id}/{area}的预期房间数量必须为1~50的整数")
        rooms = config.get("rooms")
        if not isinstance(rooms, dict):
            raise ValueError(f"{profile_id}/{area}的房号与行号必须为字典")
        if len(rooms) != expected:
            raise ValueError(
                f"{profile_id}/{area}需要完整的{expected}间会议室及网页顺序，"
                f"当前仅配置{len(rooms)}间；请补齐真实名单后再构建")
        for name, row in rooms.items():
            _nonempty_text(name, f"{profile_id}/{area}的房号")
            match = patterns["short_name_pattern"].fullmatch(name)
            if match is None or match.group(1) != name:
                raise ValueError(f"{profile_id}/{area}的短房号格式无效：{name!r}")
            if isinstance(row, bool) or not isinstance(row, int) or not 1 <= row <= expected:
                raise ValueError(f"{profile_id}/{area}:{name}的行号必须为1~{expected}的整数")
        if sorted(rooms.values()) != list(range(1, expected + 1)):
            raise ValueError(f"{profile_id}/{area}的行号必须唯一，并连续覆盖1~{expected}")


def _lookup_profile(profile_id: str) -> dict:
    _safe_id(profile_id, "部署编号")
    if profile_id not in PROFILES:
        raise ValueError(f"未知部署配置：{profile_id}；可选：{'、'.join(PROFILES)}")
    return PROFILES[profile_id]


def get_profile(profile_id: str) -> dict:
    """取得独立副本；缺少完整名单或有无效配置时拒绝使用。"""
    profile = deepcopy(_lookup_profile(profile_id))
    validate_profile(profile)
    if profile["id"] != profile_id:
        raise ValueError(f"部署配置索引{profile_id}与内部编号{profile['id']}不一致")
    return profile


def get_profile_label(profile_id: str) -> str:
    """菜单可显示尚未填完的配置；显示名称不代表已通过构建检查。"""
    return _nonempty_text(_lookup_profile(profile_id).get("label"), "部署名称")


def render_profile(profile: dict) -> str:
    """生成可按UTF-8保存的Python源码，只包含选中的PROFILE字面量。"""
    selected = deepcopy(profile)
    validate_profile(selected)
    return ("# -*- coding: utf-8 -*-\n"
            "# 由构建工具生成：本文件只包含本程序选定的部署配置。\n"
            "PROFILE = " + pformat(selected, sort_dicts=False, width=100) + "\n")
