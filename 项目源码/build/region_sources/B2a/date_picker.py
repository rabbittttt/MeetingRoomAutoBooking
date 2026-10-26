"""Select a date only when the calendar and the resulting form value agree."""
from __future__ import annotations

import asyncio
import calendar
from datetime import date, datetime
import json
import logging
from pathlib import Path
import re
import time

from playwright.async_api import Error as PlaywrightError


class DatePickerStructureError(RuntimeError):
    """The observed controls do not provide an unambiguous date."""


class _NotReady(RuntimeError):
    """A valid date control has not become usable yet."""


_MONTHS = ("一月", "二月", "三月", "四月", "五月", "六月",
           "七月", "八月", "九月", "十月", "十一月", "十二月")
_DATE = re.compile(r"^\s*(\d{4})\s*[-/.年]\s*(\d{1,2})\s*[-/.月]\s*(\d{1,2})\s*日?\s*$")
_DIAGNOSTIC_TIMES = {}
_DIAGNOSTIC_INTERVAL = 30 * 60


def _full_date(value):
    match = _DATE.fullmatch(value or "")
    if not match:
        return None
    try:
        return date(*(int(part) for part in match.groups()))
    except ValueError:
        return None


def _full_date_index(cells, target):
    """Return the unique exact date without depending on month-title wording."""
    attributed = []
    for index, cell in enumerate(cells):
        dates = {_full_date(value) for value in cell.get("dates", [])} - {None}
        if len(dates) > 1:
            raise DatePickerStructureError("日期格的完整日期属性互相冲突")
        if target in dates:
            if cell.get("day") != target.day:
                raise DatePickerStructureError("日期格文字与完整日期属性不一致")
            attributed.append(index)
    if len(attributed) == 1:
        return attributed[0]
    if len(attributed) > 1:
        raise DatePickerStructureError("当前日历中目标完整日期仍有多个候选")
    return None


def _day_index(cells, year, month, target):
    """Use full date attributes, or prove one complete contiguous month."""
    exact = _full_date_index(cells, target)
    if exact is not None:
        return exact

    days = [cell.get("day") for cell in cells]
    length = calendar.monthrange(year, month)[1]
    sequence = list(range(1, length + 1))
    starts = [start for start in range(len(days) - length + 1)
              if days[start:start + length] == sequence]
    if len(starts) != 1 or not 28 <= len(days) <= 42:
        raise DatePickerStructureError("当前日历不能确认唯一完整的本月日期序列")
    if all("x" in cell and "y" in cell for cell in cells):
        _check_grid_order(cells)
    start = starts[0]
    prefix, suffix = days[:start], days[start + length:]
    previous = date(year, month, 1).toordinal() - 1
    previous_length = date.fromordinal(previous).day
    if (len(prefix) > 6 or len(suffix) > 14
            or prefix != list(range(previous_length - len(prefix) + 1, previous_length + 1))
            or suffix != list(range(1, len(suffix) + 1))):
        raise DatePickerStructureError("日期网格的相邻月份不连续，不能安全定位本月")
    index = start + target.day - 1
    cell_dates = {_full_date(value) for value in cells[index].get("dates", [])} - {None}
    if cell_dates and cell_dates != {target}:
        raise DatePickerStructureError("本月日期序列与日期格完整日期属性冲突")
    return index


# Every snapshot is rebuilt after a UI transition. Paths describe observed nodes;
# they are never fixed positional guesses about which of two day labels is right.
_DOM = r"""
() => {
  const visible = e => {
    const r=e.getBoundingClientRect(), s=getComputedStyle(e);
    return e.isConnected && r.width>0 && r.height>0 &&
      s.display!=='none' && s.visibility!=='hidden' && s.visibility!=='collapse';
  };
  const path = e => {
    const parts=[];
    for(let n=e;n&&n.nodeType===1;n=n.parentElement) {
      let i=1; for(let p=n.previousElementSibling;p;p=p.previousElementSibling)
        if(p.tagName===n.tagName)i++;
      parts.unshift(n.tagName.toLowerCase()+'['+i+']');
    }
    return '/'+parts.join('/');
  };
  const dateText = value => /^\s*\d{4}\s*[-/.年]\s*\d{1,2}\s*[-/.月]\s*\d{1,2}\s*日?\s*$/.test(value||'');
  const value = e => 'value' in e ? String(e.value) : (e.textContent||'').trim();
  const icons=[...document.querySelectorAll('.ev_datepicker_icon_calendar')].filter(visible);
  let control=null, controlError='', controlPending=false;
  if(icons.length===1) {
    let owner=null;
    for(let p=icons[0].parentElement;p&&p!==document.body;p=p.parentElement)
      if([...p.classList].some(c=>/^ev_datepicker(?:$|_)/.test(c)&&!/icon|calendar/.test(c))) {
        owner=p;break;
      }
    for(let p=icons[0].parentElement;p&&p!==document.body;p=p.parentElement) {
      if(owner&&!owner.contains(p))break;
      // Never collect a second date picker or unrelated form-wide date fields.
      if([...p.querySelectorAll('.ev_datepicker_icon_calendar')].filter(visible).length!==1)break;
      if([...p.querySelectorAll('button,select,textarea,[role="combobox"]')]
          .some(e=>visible(e)&&!e.contains(icons[0])))break;
      let inputs=[...p.querySelectorAll('input,.ev_input')]
        .filter(e=>visible(e)&&(!e.contains(icons[0])||e.matches('.ev_input')));
      inputs=inputs.filter(e=>!inputs.some(other=>other!==e&&e.contains(other)));
      if(inputs.length===1&&!dateText(value(inputs[0]))) {
        controlPending=true;controlError='日期输入尚未加载完整日期';break;
      }
      let fields=[...p.querySelectorAll('input,.ev_input,span,div')]
        .filter(e=>visible(e)&&(!e.contains(icons[0])||e.matches('.ev_input'))&&dateText(value(e)));
      fields=fields.filter(e=>!fields.some(other=>other!==e&&e.contains(other)));
      if(fields.length>1){controlError='同一日期控件包含多个完整日期值';break;}
      if(fields.length===1){control={path:path(fields[0]),value:value(fields[0])};break;}
    }
    if(!control&&!controlError)controlError='日历按钮所属控件缺少可核验的完整日期值';
  }
  const rawCells = root => {
    let cells=[...root.querySelectorAll('td,[role="cell"],[role="gridcell"]')]
      .filter(e=>visible(e)&&/^(?:[1-9]|[12]\d|3[01])$/.test((e.textContent||'').trim()));
    return cells.filter(e=>!cells.some(other=>other!==e&&other.contains(e)));
  };
  const roots=[];
  for(const trigger of document.querySelectorAll('.ev_calendar_trigger')) {
    if(!visible(trigger))continue;
    for(let p=trigger.parentElement;p&&p!==document.body;p=p.parentElement) {
      if(visible(p)&&rawCells(p).length>=7){if(!roots.includes(p))roots.push(p);break;}
    }
  }
  for(const root of document.querySelectorAll('.ev_calendar,[role="dialog"],[role="grid"]')) {
    if(!visible(root)||rawCells(root).length<7)continue;
    const cells=rawCells(root);
    if(!roots.some(other=>cells.some(cell=>other.contains(cell))))roots.push(root);
  }
  const describe = root => {
    const cells=rawCells(root);
    // On the company calendar the trigger is an empty arrow span. Its owned
    // dropdown header contains the year/month label and receives the click.
    const controls=[...new Set([...root.querySelectorAll('.ev_calendar_trigger')].filter(visible)
      .map(e=>{
        const header=e.closest('.ev_calendar_dropdown_header');
        return header&&root.contains(header)&&visible(header)?header:e;
      }))];
    const triggers=controls.map(e=>({path:path(e),text:(e.textContent||'').trim().slice(0,80)}));
    const titles=[...new Set(triggers.map(t=>t.text).concat(
      [...root.querySelectorAll('[role="heading"],[class*="calendar_header"],[class*="calendar_title"]')]
        .filter(visible).map(e=>(e.textContent||'').trim().slice(0,120))))];
    const yearLabels=[...new Set(((root.innerText||'').match(/(?<!\d)(?:19|20|21)\d{2}\s*年/g)||[])
      .map(s=>Number(s.match(/\d{4}/)[0])))];
    return {path:path(root),titles,triggers,yearLabels,cells:cells.map((e,index)=>{
      const r=e.getBoundingClientRect();
      const attrs=[];
      for(const n of [e,...e.querySelectorAll('[data-date],[data-value],[datetime],[title],[aria-label]')])
        for(const key of ['data-date','data-value','datetime','title','aria-label']){
          const val=n.getAttribute(key); if(dateText(val))attrs.push(val.trim());
        }
      const tr=e.closest('tr,[role="row"]');
      const disabled=!!e.closest('[disabled],[aria-disabled="true"],[data-disabled="true"]') ||
        [...e.classList].some(c=>/(?:^|[_-])(?:disabled|disable)(?:$|[_-])/.test(c));
      return {path:path(e),day:Number(e.textContent.trim()),dates:[...new Set(attrs)],
        disabled,row:tr?path(tr):null,x:r.x,y:r.y,w:r.width,h:r.height,index};
    })};
  };
  return {icons:icons.map(e=>path(e)),control,controlError,controlPending,calendars:roots.map(describe)};
}
"""

_OPTIONS = r"""
(names) => {
  const visible=e=>{const r=e.getBoundingClientRect(),s=getComputedStyle(e);
    return r.width>0&&r.height>0&&s.display!=='none'&&s.visibility!=='hidden';};
  const path=e=>{const a=[];for(let n=e;n&&n.nodeType===1;n=n.parentElement){
    let i=1;for(let p=n.previousElementSibling;p;p=p.previousElementSibling)if(p.tagName===n.tagName)i++;
    a.unshift(n.tagName.toLowerCase()+'['+i+']');}return '/'+a.join('/');};
  let nodes=[...document.querySelectorAll('li,[role="option"],button,span,div,td')]
    .filter(e=>visible(e)&&names.includes((e.textContent||'').trim())&&
      !e.closest('.ev_calendar_trigger,.ev_calendar_dropdown_header')&&
      !e.closest('[disabled],[aria-disabled="true"],[data-disabled="true"],.ev_calendar_disabled'));
  nodes=nodes.filter(e=>!nodes.some(n=>n!==e&&e.contains(n)));
  return [...new Set(nodes.map(e=>path(e.closest('[role="option"],li')||e)))];
}
"""


def _month(text):
    text = text.strip()
    # Observed dropdown header: "2026 十月"; other supported controls display
    # "十月", "10月" or "2026年10月". Do not parse arbitrary surrounding text.
    text = re.sub(r"^\d{4}(?:\s*年\s*|\s+)", "", text)
    if text in _MONTHS:
        return _MONTHS.index(text) + 1
    match = re.fullmatch(r"(\d{1,2})\s*月", text)
    if match and 1 <= int(match[1]) <= 12:
        return int(match[1])
    return None


def _year_month(scope, anchor, anchored):
    months = {_month(item["text"]) for item in scope["triggers"]} - {None}
    if len(months) != 1:
        raise DatePickerStructureError("当前日历月份控件不唯一或月份文字无法核验")
    month = months.pop()
    years = set(scope["yearLabels"])
    for title in scope["titles"]:
        if re.fullmatch(r"\s*(?:19|20|21)\d{2}\s*年?\s*", title):
            years.add(int(re.search(r"\d{4}", title)[0]))
        match = re.match(r"^\s*(\d{4})(?:\s*年\s*|\s+)", title)
        if match and _month(title) is not None:
            years.add(int(match[1]))
    if len(years) > 1:
        raise DatePickerStructureError("当前日历出现多个年份，不能确定目标月份")
    if years:
        return years.pop(), month, anchored
    if anchored or month == anchor.month:
        return anchor.year, month, True
    raise DatePickerStructureError("日历只有月份，且与所属日期输入的年月无法对应")


def _check_grid_order(cells):
    """Fallback evidence must be a complete seven-column calendar grid."""
    rows = []
    row = []
    for previous, current in zip(cells, cells[1:]):
        same_row = abs(previous["y"] - current["y"]) < min(previous["h"], current["h"]) / 2
        if same_row:
            if current["x"] <= previous["x"]:
                raise DatePickerStructureError("日期格的可见顺序与网格顺序不一致")
        elif current["y"] <= previous["y"]:
            raise DatePickerStructureError("日期网格包含重复或倒序的可见行")
    for cell in cells:
        if row and abs(row[0]["y"] - cell["y"]) >= min(row[0]["h"], cell["h"]) / 2:
            rows.append(row)
            row = []
        row.append(cell)
    if row:
        rows.append(row)
    if not 4 <= len(rows) <= 6 or any(len(row) != 7 for row in rows):
        raise DatePickerStructureError("日期网格不是完整的七列日历，不能按日号推断")
    columns = [cell["x"] for cell in rows[0]]
    if any(abs(cell["x"] - columns[index]) > 2 for row in rows
           for index, cell in enumerate(row)):
        raise DatePickerStructureError("日期网格列未对齐，不能按日号推断")


def _diagnostic(snapshot, target, directory, error):
    if not directory or not snapshot:
        return
    try:
        folder = Path(directory)
        key = (str(folder.resolve()), target.isoformat(), type(error).__name__,
               str(error).splitlines()[0][:160])
        now = time.monotonic()
        if now - _DIAGNOSTIC_TIMES.get(key, -float("inf")) < _DIAGNOSTIC_INTERVAL:
            return
        control = snapshot.get("control")
        data = {
            "target": target.isoformat(), "errorType": type(error).__name__,
            "controlDate": (_full_date(control["value"]).isoformat()
                            if control and _full_date(control["value"]) else None),
            "calendars": [
                {"titles": scope["titles"], "cells": [
                    {key: cell[key] for key in ("day", "dates", "disabled", "row", "x", "y", "index")}
                    for cell in scope["cells"][:62]]}
                for scope in snapshot.get("calendars", [])[:3]
            ],
        }
        folder.mkdir(parents=True, exist_ok=True)
        output = folder / f"{datetime.now():%H%M%S_%f}_日期结构_{target.isoformat()}.json"
        output.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")
        for old_key, saved_at in list(_DIAGNOSTIC_TIMES.items()):
            if now - saved_at >= _DIAGNOSTIC_INTERVAL:
                del _DIAGNOSTIC_TIMES[old_key]
        _DIAGNOSTIC_TIMES[key] = now
        logging.warning("[日期] 已保存日期控件诊断：%s", output)
    except OSError:
        logging.warning("[日期] 无法保存日期控件诊断", exc_info=True)


async def select_booking_date(page, target, *, timeout_ms=5000, retries=2,
                              diagnostic_dir=None, stop_check=None):
    """Click a verified date and require the same date field to reflect it."""
    snapshot = None
    last_calendars = []

    def check_stop():
        if stop_check is not None:
            stop_check()

    async def read():
        nonlocal snapshot, last_calendars
        check_stop()
        snapshot = await page.evaluate(_DOM)
        if snapshot["calendars"]:
            last_calendars = snapshot["calendars"]
        return snapshot

    async def pause():
        check_stop()
        await asyncio.sleep(0.06)

    async def click(path):
        check_stop()
        await page.locator("xpath=" + path).click(timeout=timeout_ms)

    try:
        initial_deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            initial = await read()
            if len(initial["icons"]) == 1 and (initial["control"] or
                                     initial["controlError"] == "同一日期控件包含多个完整日期值"):
                break
            if asyncio.get_running_loop().time() >= initial_deadline:
                if len(initial["icons"]) > 1:
                    raise DatePickerStructureError("可见日历入口不唯一")
                if initial["icons"] and not initial["controlPending"]:
                    raise DatePickerStructureError(initial["controlError"] or "无法确认日期输入控件")
                raise _NotReady("日期输入尚未加载完整日期" if initial["icons"] else "日期控件尚未出现")
            await pause()
        if len(initial["icons"]) != 1:
            raise DatePickerStructureError("可见日历入口不唯一")
        if initial["controlError"] or not initial["control"]:
            raise DatePickerStructureError(initial["controlError"] or "无法确认日期输入控件")
        control_path = initial["control"]["path"]
        anchor = _full_date(initial["control"]["value"])
        if anchor is None:
            raise DatePickerStructureError("日期输入未显示可核验的完整日期")
        if anchor == target:
            return
        if anchor.year != target.year:
            raise DatePickerStructureError("日期控件年份与目标不同，当前不支持跨年选择")
        before_paths = {scope["path"] for scope in initial["calendars"]}
        await click(initial["icons"][0])

        async def current_calendar():
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
            while True:
                state = await read()
                scopes = state["calendars"]
                newly_visible = [scope for scope in scopes if scope["path"] not in before_paths]
                candidates = newly_visible if newly_visible else scopes
                if len(candidates) == 1:
                    return candidates[0]
                if asyncio.get_running_loop().time() >= deadline:
                    if len(candidates) > 1:
                        raise DatePickerStructureError("日历展开后存在多个可见日期面板")
                    raise _NotReady("日历日期网格尚未出现")
                await pause()

        async def calendar_month(anchored):
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
            while True:
                scope = await current_calendar()
                try:
                    year, month, anchored = _year_month(scope, anchor, anchored)
                    return scope, year, month, anchored
                except DatePickerStructureError:
                    # A month header and its grid can be rendered separately.
                    if asyncio.get_running_loop().time() >= deadline:
                        raise
                    await pause()

        async def choose_and_verify(scope, index):
            chosen = scope["cells"][index]
            if chosen["disabled"]:
                raise _NotReady("目标日期处于禁用状态")
            locator = page.locator("xpath=" + chosen["path"])
            if not await locator.is_enabled():
                raise _NotReady("目标日期处于禁用状态")
            await click(chosen["path"])
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
            while True:
                state = await read()
                control = state["control"]
                if control and control["path"] != control_path:
                    raise DatePickerStructureError("选择日期后日期输入控件身份发生变化")
                if control and _full_date(control["value"]) == target:
                    logging.info("[日期] 已核对表单日期：%s", target.isoformat())
                    return
                if asyncio.get_running_loop().time() >= deadline:
                    raise _NotReady("点击日期后表单未显示目标完整日期，未继续选房")
                await pause()

        # A complete date identifies the day even when the month heading uses
        # unfamiliar wording. Require one calendar, one exact cell and the same
        # form receipt; month parsing is needed only for navigation/fallback.
        scope = await current_calendar()
        exact = _full_date_index(scope["cells"], target)
        if exact is not None:
            if not scope["cells"][exact]["disabled"]:
                await choose_and_verify(scope, exact)
                return
            # The adjacent month's grey placeholder can be disabled even when
            # that date is available after navigating to its own month.
            try:
                shown_year, shown_month, _ = _year_month(scope, anchor, False)
            except DatePickerStructureError:
                raise _NotReady("目标日期处于禁用状态")
            if (shown_year, shown_month) == (target.year, target.month):
                raise _NotReady("目标日期处于禁用状态")

        scope, year, month, anchored = await calendar_month(False)
        if year != target.year:
            raise DatePickerStructureError("日历显示年份与目标年份不一致")
        changed_month = month != target.month
        initial_grid = [(cell["day"], cell["dates"]) for cell in scope["cells"]]
        if changed_month:
            names = [_MONTHS[target.month - 1], f"{target.month}月", f"{target.month} 月"]
            changed = False
            for _ in range(max(1, retries)):
                scope = await current_calendar()
                triggers = [item for item in scope["triggers"] if _month(item["text"]) is not None]
                if len(triggers) != 1:
                    raise DatePickerStructureError("当前日历的月份下拉控件不唯一")
                old_options = set(await page.evaluate(_OPTIONS, names))
                await click(triggers[0]["path"])
                deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
                while True:
                    check_stop()
                    options = [path for path in await page.evaluate(_OPTIONS, names)
                               if path not in old_options]
                    if len(options) > 1:
                        raise DatePickerStructureError("可见目标月份选项不唯一")
                    if options:
                        await click(options[0])
                        changed = True
                        break
                    if asyncio.get_running_loop().time() >= deadline:
                        break
                    await pause()
                if changed:
                    break
            if not changed:
                raise _NotReady("目标月份选项尚未出现")
            deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
            while True:
                scope, year, month, anchored = await calendar_month(anchored)
                if (year, month) == (target.year, target.month):
                    break
                if asyncio.get_running_loop().time() >= deadline:
                    raise _NotReady("选择月份后日历尚未切换到目标年月")
                await pause()

        # Re-read after the month transition. No old element handles survive it.
        grid_deadline = asyncio.get_running_loop().time() + timeout_ms / 1000
        while True:
            scope, year, month, anchored = await calendar_month(anchored)
            if (year, month) != (target.year, target.month):
                raise DatePickerStructureError("点击日期前日历年月发生变化")
            if (changed_month
                    and [(cell["day"], cell["dates"]) for cell in scope["cells"]] == initial_grid):
                # The header can update before the old cells are replaced. A
                # disabled neighboring-month placeholder may already carry the
                # full target date, so it must not bypass this redraw wait.
                if asyncio.get_running_loop().time() >= grid_deadline:
                    raise _NotReady("月份标题已切换，但日期网格尚未更新")
                await pause()
                continue
            try:
                index = _day_index(scope["cells"], year, month, target)
                break
            except DatePickerStructureError:
                if asyncio.get_running_loop().time() >= grid_deadline:
                    raise
                await pause()
        await choose_and_verify(scope, index)
    except (DatePickerStructureError, _NotReady, PlaywrightError) as exc:
        diagnostic = dict(snapshot or {})
        diagnostic["calendars"] = last_calendars
        _diagnostic(diagnostic, target, diagnostic_dir, exc)
        if isinstance(exc, PlaywrightError):
            raise RuntimeError("日期控件操作未完成：" + str(exc).splitlines()[0]) from exc
        raise
