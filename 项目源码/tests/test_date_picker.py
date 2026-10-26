"""Date selection receipts using local calendar fixtures; network is blocked."""
from datetime import date
import json
import os
from pathlib import Path
import tempfile
import unittest

from playwright.async_api import async_playwright

from date_picker import (DatePickerStructureError, _DIAGNOSTIC_TIMES,
                         _day_index, select_booking_date)


# 用户于2026-10-20提供的两行实际日期格。外围表单/日历标题仍由本地夹具提供；
# 只保留这两行，确保测试依赖完整data-value，而不能借助完整月序列兜底。
COMPANY_CALENDAR_ROWS = """
<tr>
<td data-value="2026/09/27" data-year="2026" data-month="9" data-date="27" data-raw="Sun Sep 27 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_prev ev_calendar_disabled">27</td>
<td data-value="2026/09/28" data-year="2026" data-month="9" data-date="28" data-raw="Mon Sep 28 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_prev ev_calendar_disabled">28</td>
<td data-value="2026/09/29" data-year="2026" data-month="9" data-date="29" data-raw="Tue Sep 29 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_prev ev_calendar_disabled">29</td>
<td data-value="2026/09/30" data-year="2026" data-month="9" data-date="30" data-raw="Wed Sep 30 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_prev ev_calendar_disabled">30</td>
<td data-value="2026/10/01" data-year="2026" data-month="10" data-date="1" data-raw="Thu Oct 01 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current ev_calendar_disabled">1</td>
<td data-value="2026/10/02" data-year="2026" data-month="10" data-date="2" data-raw="Fri Oct 02 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current ev_calendar_disabled">2</td>
<td data-value="2026/10/03" data-year="2026" data-month="10" data-date="3" data-raw="Sat Oct 03 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current ev_calendar_disabled">3</td>
</tr><tr>
<td data-value="2026/10/25" data-year="2026" data-month="10" data-date="25" data-raw="Sun Oct 25 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">25</td>
<td data-value="2026/10/26" data-year="2026" data-month="10" data-date="26" data-raw="Mon Oct 26 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">26</td>
<td data-value="2026/10/27" data-year="2026" data-month="10" data-date="27" data-raw="Tue Oct 27 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">27</td>
<td data-value="2026/10/28" data-year="2026" data-month="10" data-date="28" data-raw="Wed Oct 28 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">28</td>
<td data-value="2026/10/29" data-year="2026" data-month="10" data-date="29" data-raw="Thu Oct 29 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">29</td>
<td data-value="2026/10/30" data-year="2026" data-month="10" data-date="30" data-raw="Fri Oct 30 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">30</td>
<td data-value="2026/10/31" data-year="2026" data-month="10" data-date="31" data-raw="Sat Oct 31 2026 00:00:00 GMT+0800 (中国标准时间)" tabindex="-1" class="ev_calendar_current">31</td>
</tr>
"""


class DateGridEvidenceTests(unittest.TestCase):
    def test_duplicate_27_resolves_to_full_current_month(self):
        days = list(range(27, 31)) + list(range(1, 32)) + list(range(1, 8))
        cells = [{"day": day} for day in days]
        self.assertEqual(_day_index(cells, 2026, 10, date(2026, 10, 27)), 30)

    def test_two_complete_months_are_rejected(self):
        with self.assertRaises(DatePickerStructureError):
            _day_index([{"day": d} for d in list(range(1, 32)) * 2],
                       2026, 10, date(2026, 10, 27))

    def test_broken_neighbor_sequence_is_rejected(self):
        with self.assertRaises(DatePickerStructureError):
            _day_index([{"day": d} for d in [28, 30] + list(range(1, 32))],
                       2026, 10, date(2026, 10, 27))
        cells = [{"day": day, "x": (index % 10) * 32, "y": (index // 10) * 24,
                  "h": 24} for index, day in enumerate(list(range(27, 31))
                                                      + list(range(1, 32))
                                                      + list(range(1, 8)))]
        with self.assertRaisesRegex(DatePickerStructureError, "七列"):
            _day_index(cells, 2026, 10, date(2026, 10, 27))


def fixture(*, initial="2026-10-20", month=10, year=2026, year_label=True,
            hidden_copy=False, duplicate=False, attributes=False, disabled=False,
            wrong_receipt=False, portal=True, ambiguous_input=False, redraw_delay=0,
            title_first=False):
    config = json.dumps(dict(month=month, year=year, yearLabel=year_label,
                             hiddenCopy=hidden_copy, duplicate=duplicate,
                             attributes=attributes, disabled=disabled,
                             wrongReceipt=wrong_receipt, portal=portal,
                             redrawDelay=redraw_delay, titleFirst=title_first))
    return r"""<!doctype html><meta charset="utf-8"><style>
      td {width:32px;height:24px;text-align:center} .ev_calendar{display:inline-block}
      button,li {min-width:30px;min-height:22px} .menu{background:white}
      .ev_calendar_trigger{display:inline-block;min-width:45px}
    </style><input id="topic" value="PRIVATE MEETING TITLE">
    <div id="field"><input id="date" value='""" + initial + r"""' readonly>
    <button class="ev_datepicker_icon_calendar" onclick="openCalendar()">日历</button>""" + (
        '<input value="2026-10-21">' if ambiguous_input else "") + r"""</div>
    <div id="portal"></div><script>
      var cfg=""" + config + r"""; window.clicks=[]; window.opened=0; window.redraws=0;
      var months=['一月','二月','三月','四月','五月','六月','七月','八月','九月','十月','十一月','十二月'];
      function iso(y,m,d){return `${y}-${String(m).padStart(2,'0')}-${String(d).padStart(2,'0')}`;}
      function draw(id='calendar',hidden=false) {
        const old=document.getElementById(id); if(old)old.remove();
        const root=document.createElement('div');root.id=id;root.className='ev_calendar';
        if(hidden)root.style.display='none';
        root.innerHTML=(cfg.yearLabel?`<span class="ev_calendar_trigger">${cfg.year}年</span>`:'')+
          `<button class="ev_calendar_trigger" onclick="showMonths(this)">${months[cfg.month-1]}</button>`;
        const table=document.createElement('table'); const body=document.createElement('tbody');table.append(body);
        const first=new Date(cfg.year,cfg.month-1,1); const offset=first.getDay();
        const cursor=new Date(cfg.year,cfg.month-1,1-offset);
        for(let row=0;row<6;row++){const tr=document.createElement('tr');body.append(tr);
          for(let col=0;col<7;col++){const td=document.createElement('td'), y=cursor.getFullYear(),
            m=cursor.getMonth()+1,d=cursor.getDate();td.textContent=d;
            const full=iso(y,m,d);if(cfg.attributes)td.setAttribute('data-date',full);
            if(cfg.disabled&&m===cfg.month&&d===27)td.setAttribute('aria-disabled','true');
            td.onclick=()=>{window.clicks.push(full);
              if(!cfg.wrongReceipt)document.getElementById('date').value=full;
              root.remove();};
            tr.append(td);cursor.setDate(cursor.getDate()+1);
          }}
        root.append(table);document.getElementById('portal').append(root);
      }
      function openCalendar(){window.opened++;draw();
        if(cfg.hiddenCopy)draw('hidden-calendar',true);if(cfg.duplicate)draw('other-calendar');}
      function showMonths(trigger){const menu=document.createElement('ul');menu.className='menu';
        months.forEach((name,i)=>{const li=document.createElement('li');li.textContent=name;
          li.onclick=()=>{cfg.month=i+1;menu.remove();window.redraws++;
            if(cfg.redrawDelay){
              if(cfg.titleFirst)document.querySelector('#calendar button.ev_calendar_trigger').textContent=months[i];
              else document.getElementById('calendar').remove();
              setTimeout(()=>draw(),cfg.redrawDelay);}
            else draw();};menu.append(li);});
        (cfg.portal?document.body:trigger.parentElement).append(menu);}
    </script>"""


class DatePickerBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        _DIAGNOSTIC_TIMES.clear()
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.page = await self.browser.new_page()
        await self.page.route("**/*", lambda route: route.abort())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def select(self, target, **options):
        await self.page.set_content(fixture(**options))
        await select_booking_date(self.page, target, timeout_ms=500, retries=1)
        self.assertEqual(await self.page.locator("#date").input_value(), target.isoformat())
        self.assertEqual(await self.page.evaluate("window.clicks"), [target.isoformat()])

    async def test_duplicate_27_and_hidden_copy(self):
        await self.select(date(2026, 10, 27), hidden_copy=True)

    async def test_month_start_and_end(self):
        for target in (date(2026, 10, 1), date(2026, 10, 31)):
            with self.subTest(target=target):
                await self.select(target)

    async def test_month_switch_redraw_with_portal_options(self):
        for portal in (True, False):
            with self.subTest(portal=portal):
                await self.select(date(2026, 11, 27), portal=portal, redraw_delay=100)
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)
        await self.select(date(2026, 12, 27), redraw_delay=150, title_first=True)
        self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_only_month_label_uses_owned_input_year(self):
        await self.page.set_content(fixture(year_label=False))
        await self.page.evaluate("""() => {
            const button=document.createElement('button');button.textContent='十一月';
            button.onclick=()=>window.backgroundClicked=true;document.body.prepend(button);
        }""")
        await select_booking_date(self.page, date(2026, 11, 27), timeout_ms=500)
        self.assertEqual(await self.page.locator("#date").input_value(), "2026-11-27")
        self.assertFalse(await self.page.evaluate("window.backgroundClicked===true"))

    async def test_full_date_attribute_precedes_repeated_day_label(self):
        await self.select(date(2026, 10, 27), attributes=True)

    async def company_calendar(self, *, disabled_target=False, month_control="normal",
                               duplicate_target=False, conflict_target=False,
                               missing_target_date=False, wrong_receipt=False):
        await self.page.set_content(fixture(initial="2026/10/20"))
        await self.page.evaluate("""({rows, disabledTarget, monthControl, duplicateTarget,
                                     conflictTarget, missingTargetDate, wrongReceipt}) => {
            const originalDraw=window.draw;
            window.draw=()=> {
                originalDraw();
                const root=document.getElementById('calendar');
                root.querySelector('tbody').innerHTML=rows;
                if(monthControl==='unknown')
                    root.querySelector('button.ev_calendar_trigger').textContent='十月 ▼';
                else if(monthControl==='empty') {
                    for(const trigger of root.querySelectorAll('.ev_calendar_trigger'))trigger.remove();
                    const trigger=document.createElement('span');
                    trigger.className='ev_calendar_trigger';
                    trigger.style.cssText='display:inline-block;width:16px;height:16px';
                    root.prepend(trigger);
                }
                else if(monthControl==='absent')
                    for(const trigger of root.querySelectorAll('.ev_calendar_trigger'))trigger.remove();
                const target=root.querySelector('td[data-value="2026/10/27"]');
                if(duplicateTarget)target.parentElement.append(target.cloneNode(true));
                if(conflictTarget)target.setAttribute('title','2026/10/28');
                if(missingTargetDate)target.removeAttribute('data-value');
                for(const cell of root.querySelectorAll('td')) {
                    if(disabledTarget&&cell.dataset.value==='2026/10/27')
                        cell.classList.add('ev_calendar_disabled');
                    // 不在夹具事件中拦截禁用格，确保是程序自身识别禁用状态。
                    cell.onclick=()=> {
                        window.clicks.push(cell.dataset.value);
                        if(!wrongReceipt)document.getElementById('date').value=cell.dataset.value;
                        root.remove();
                    };
                }
            };
        }""", {"rows": COMPANY_CALENDAR_ROWS, "disabledTarget": disabled_target,
               "monthControl": month_control, "duplicateTarget": duplicate_target,
               "conflictTarget": conflict_target, "missingTargetDate": missing_target_date,
               "wrongReceipt": wrong_receipt})

    async def test_company_data_value_selects_october_27_not_disabled_september_27(self):
        await self.company_calendar()
        await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=500)
        self.assertEqual(await self.page.evaluate("window.clicks"), ["2026/10/27"])
        self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/27")

    async def test_company_disabled_class_is_respected_without_aria_disabled(self):
        await self.company_calendar(disabled_target=True)
        with self.assertRaisesRegex(RuntimeError, "禁用") as caught:
            await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=500)
        self.assertNotIsInstance(caught.exception, DatePickerStructureError)
        self.assertEqual(await self.page.evaluate("window.clicks"), [])
        self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/20")

    async def test_company_full_date_does_not_require_a_recognizable_month_control(self):
        for month_control in ("unknown", "empty", "absent"):
            with self.subTest(month_control=month_control):
                await self.company_calendar(month_control=month_control)
                await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=400)
                self.assertEqual(await self.page.evaluate("window.clicks"), ["2026/10/27"])
                self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/27")

    async def test_company_full_date_without_month_control_still_respects_disabled(self):
        for month_control in ("unknown", "empty", "absent"):
            with self.subTest(month_control=month_control):
                await self.company_calendar(month_control=month_control, disabled_target=True)
                with self.assertRaisesRegex(RuntimeError, "禁用") as caught:
                    await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=300)
                self.assertNotIsInstance(caught.exception, DatePickerStructureError)
                self.assertEqual(await self.page.evaluate("window.clicks"), [])
                self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/20")

    async def test_company_ambiguous_full_dates_are_rejected_before_clicking(self):
        for options in ({"duplicate_target": True}, {"conflict_target": True}):
            with self.subTest(options=options):
                await self.company_calendar(month_control="absent", **options)
                with self.assertRaises(DatePickerStructureError):
                    await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=300)
                self.assertEqual(await self.page.evaluate("window.clicks"), [])
                self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/20")

    async def test_company_missing_full_date_with_unknown_month_cannot_be_guessed(self):
        await self.company_calendar(month_control="unknown", missing_target_date=True)
        with self.assertRaises(DatePickerStructureError):
            await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=300)
        self.assertEqual(await self.page.evaluate("window.clicks"), [])
        self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/20")

    async def test_company_full_date_without_month_control_requires_correct_receipt(self):
        await self.company_calendar(month_control="absent", wrong_receipt=True)
        with self.assertRaisesRegex(RuntimeError, "表单未显示") as caught:
            await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=300)
        self.assertNotIsInstance(caught.exception, DatePickerStructureError)
        self.assertEqual(await self.page.evaluate("window.clicks"), ["2026/10/27"])
        self.assertEqual(await self.page.locator("#date").input_value(), "2026/10/20")

    async def company_month_calendar(self, *, redraw_delay=120):
        """Use the supplied clickable header and year/month menu structure."""
        await self.page.set_content(fixture(initial="2026/10/20", attributes=True))
        await self.page.evaluate("""(delay) => {
            window.headerClicks=0;window.monthClicks=[];window.backgroundMonths=[];
            for(const name of ['十一月','十二月']) {
                const unrelated=document.createElement('button');
                unrelated.textContent=name;
                unrelated.onclick=()=>window.backgroundMonths.push(name);
                document.body.prepend(unrelated);
            }
            const originalDraw=window.draw;
            window.draw=()=> {
                originalDraw();
                const root=document.getElementById('calendar');
                for(const trigger of root.querySelectorAll('.ev_calendar_trigger'))trigger.remove();
                const dropdown=document.createElement('div');
                dropdown.className='ev_calendar_dropdown';
                dropdown.innerHTML=`
                  <div class="ev_calendar_dropdown_header" title="选择年月 (Ctrl+ 下键)"
                       data-event-id="handleToggle" tabindex="0">
                    <span class="">${cfg.year} ${months[cfg.month-1]}</span>
                    <span class="ev_calendar_trigger" style="display:inline-block;width:16px;min-width:16px;height:16px"></span>
                  </div>
                  <div class="ev_calendar_dropdown_content" style="display:none">
                    <ul class="ev_calendar_list_y"><li>2026</li><li>2027</li></ul>
                    <ul class="ev_calendar_list_m"></ul>
                  </div>`;
                root.prepend(dropdown);
                const header=dropdown.querySelector('.ev_calendar_dropdown_header');
                const content=dropdown.querySelector('.ev_calendar_dropdown_content');
                header.onclick=()=>{window.headerClicks++;content.style.display='block';};
                for(const [index,name] of months.entries()) {
                    const option=document.createElement('li');
                    option.textContent=name;
                    if(index<9) {
                        option.className='ev_calendar_disabled';
                        option.setAttribute('data-disabled','true');
                    } else if(index===cfg.month-1)option.className='ev_calendar_selected';
                    option.onclick=()=> {
                        window.monthClicks.push(name);
                        if(option.dataset.disabled==='true')return;
                        cfg.month=index+1;content.style.display='none';window.redraws++;
                        header.querySelector('span').textContent=`${cfg.year} ${name}`;
                        // The real header changes before the old date grid is replaced.
                        setTimeout(()=>window.draw(),delay);
                    };
                    content.querySelector('.ev_calendar_list_m').append(option);
                }
                for(const cell of root.querySelectorAll('td')) {
                    const full=cell.getAttribute('data-date');
                    const [year,month,day]=full.split('-');
                    cell.dataset.value=full.replaceAll('-','/');
                    cell.dataset.year=year;cell.dataset.month=String(Number(month));
                    cell.dataset.date=String(Number(day));
                    const cellMonth=Number(year)*12+Number(month), shownMonth=cfg.year*12+cfg.month;
                    cell.className=cellMonth===shownMonth ? 'ev_calendar_current'
                        : (cellMonth<shownMonth ? 'ev_calendar_prev' : 'ev_calendar_next')+' ev_calendar_disabled';
                    cell.onclick=()=> {
                        window.clicks.push(cell.dataset.value);
                        document.getElementById('date').value=cell.dataset.value;
                        root.remove();
                    };
                }
            };
        }""", redraw_delay)

    async def test_company_header_switches_month_without_clicking_background_names(self):
        for month, label in ((11, "十一月"), (12, "十二月")):
            with self.subTest(month=month):
                await self.company_month_calendar()
                await select_booking_date(self.page, date(2026, month, 27),
                                          timeout_ms=700, retries=1)
                expected = f"2026/{month:02d}/27"
                self.assertEqual(await self.page.locator("#date").input_value(), expected)
                self.assertEqual(await self.page.evaluate("window.clicks"), [expected])
                self.assertEqual(await self.page.evaluate("window.headerClicks"), 1)
                self.assertEqual(await self.page.evaluate("window.monthClicks"), [label])
                self.assertEqual(await self.page.evaluate("window.backgroundMonths"), [])
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_company_disabled_neighbor_date_navigates_to_its_enabled_month(self):
        await self.company_month_calendar()
        await select_booking_date(self.page, date(2026, 11, 1), timeout_ms=700, retries=1)
        self.assertEqual(await self.page.locator("#date").input_value(), "2026/11/01")
        self.assertEqual(await self.page.evaluate("window.clicks"), ["2026/11/01"])
        self.assertEqual(await self.page.evaluate("window.headerClicks"), 1)
        self.assertEqual(await self.page.evaluate("window.monthClicks"), ["十一月"])
        self.assertEqual(await self.page.evaluate("window.backgroundMonths"), [])
        self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_leap_february(self):
        await self.select(date(2028, 2, 29), initial="2028-02-10", year=2028, month=2)

    async def test_two_visible_calendars_rejected_without_clicking_day(self):
        await self.page.set_content(fixture(duplicate=True))
        with self.assertRaisesRegex(DatePickerStructureError, "多个可见"):
            await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=1000)
        self.assertEqual(await self.page.evaluate("window.clicks"), [])

    async def test_disabled_date_is_retryable(self):
        await self.page.set_content(fixture(disabled=True))
        with self.assertRaisesRegex(RuntimeError, "禁用") as caught:
            await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=300)
        self.assertNotIsInstance(caught.exception, DatePickerStructureError)
        self.assertEqual(await self.page.evaluate("window.clicks"), [])

    async def test_wrong_receipt_cannot_succeed_and_diagnostic_is_local(self):
        with tempfile.TemporaryDirectory(prefix="booking_date_") as directory:
            for _ in range(2):
                await self.page.set_content(fixture(wrong_receipt=True))
                with self.assertRaisesRegex(RuntimeError, "表单未显示") as caught:
                    await select_booking_date(self.page, date(2026, 10, 27),
                                              timeout_ms=300, diagnostic_dir=directory)
                self.assertNotIsInstance(caught.exception, DatePickerStructureError)
            files = list(Path(directory).glob("*.json"))
            self.assertEqual(len(files), 1)
            self.assertNotIn("PRIVATE MEETING TITLE", files[0].read_text(encoding="utf-8"))
            self.assertEqual(len(json.loads(files[0].read_text(encoding="utf-8"))["calendars"][0]["cells"]), 42)

    async def test_already_correct_does_not_open_calendar(self):
        for initial in ("2026-10-27", ""):
            with self.subTest(initial=initial):
                await self.page.set_content(fixture(initial=initial))
                if not initial:
                    await self.page.evaluate("setTimeout(()=>document.getElementById('date').value='2026-10-27',100)")
                await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=500)
                self.assertEqual(await self.page.evaluate("window.opened"), 0)

    async def test_ambiguous_owned_input_is_rejected(self):
        await self.page.set_content(fixture(ambiguous_input=True))
        with self.assertRaisesRegex(DatePickerStructureError, "多个完整日期"):
            await select_booking_date(self.page, date(2026, 10, 27), timeout_ms=300)

    async def test_stop_exception_propagates_without_click_or_diagnostic(self):
        class StopRequested(RuntimeError):
            pass

        def stop():
            raise StopRequested("stop")

        await self.page.set_content(fixture())
        with self.assertRaises(StopRequested):
            await select_booking_date(self.page, date(2026, 10, 27), stop_check=stop)
        self.assertEqual(await self.page.evaluate("window.opened"), 0)


if __name__ == "__main__":
    unittest.main()
