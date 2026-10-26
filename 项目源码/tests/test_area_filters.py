"""本地 Chrome 模拟弹窗；不连接 WeLink、不提交预约。"""
import os
import unittest
from unittest.mock import AsyncMock, patch
from playwright.async_api import async_playwright
import booking_core as core

STYLE = """<style>
.ev_Dialog_Panel {position:relative; width:600px; height:400px;}
.ev_select_div {position:absolute; left:30px; top:20px; width:240px; height:30px;}
.ev_select_div .ev_select_div {left:0; top:0;}
.ev_input {width:240px; height:30px; background:white;}
.ev_select_drop_icon {position:absolute; right:0; top:0; width:24px; height:30px; background:#ddd;}
[role=option] {background:white; height:25px;}
</style>"""

def control(label=None, *, cls='', style='', nested=False, field=True):
    label = label or core.AREA_LABELS['F5-1A']
    body = (f'<div class="ev_input">{label}<span class="ev_select_drop_icon" '
            'onclick="window.clicked=(window.clicked||0)+1"></span></div>')
    if not field:
        body = body.replace('class="ev_input"', 'class="plain"')
    if nested:
        body = f'<div class="ev_select_div inner-state">{body}</div>'
    return f'<div class="ev_select_div {cls}" style="{style}">{body}</div>'

class AreaFilterBrowserTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.pw = await async_playwright().start()
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get('BOOKING_TEST_BROWSER', 'chrome'), headless=True)
        self.page = await self.browser.new_page()
        await self.page.route('**/*', lambda route: route.abort())

    async def asyncTearDown(self):
        await self.browser.close()
        await self.pw.stop()

    async def load(self, content):
        await self.page.set_content(STYLE + content)

    async def test_nested_containers_sharing_one_arrow(self):
        await self.load('<div class="ev_Dialog_Panel">' + control(nested=True) + '</div>')
        entry = await core.find_building_dropdown(self.page, 'F5-1A')
        self.assertIsNotNone(entry)
        await entry.click(timeout=500)
        self.assertEqual(await self.page.evaluate('window.clicked'), 1)

    async def test_overlapping_copies_choose_pointer_target(self):
        await self.load('<div class="ev_Dialog_Panel">' + control(cls='old-state') +
                        control(cls='new-state', style='z-index:2') + '</div>')
        entry = await core.find_building_dropdown(self.page, 'F5-1A')
        self.assertIsNotNone(entry)
        await entry.click(timeout=500)
        self.assertEqual(await self.page.evaluate('window.clicked'), 1)

    async def test_distinct_positions_remain_ambiguous(self):
        await self.load('<div class="ev_Dialog_Panel">' + control() +
                        control(style='top:65px') + '</div>')
        self.assertIsNone(await core.find_building_dropdown(self.page, 'F5-1A'))

    async def test_identical_markup_is_not_node_identity(self):
        await self.load('<div class="ev_Dialog_Panel">' + control(field=False) +
                        control(field=False, style='top:65px') + '</div>')
        self.assertIsNone(await core.find_building_dropdown(self.page))

    async def test_multiple_visible_dialogs_remain_ambiguous(self):
        await self.load('<div class="ev_Dialog_Panel">' + control() + '</div>' +
                        '<div class="ev_Dialog_Panel">' + control() + '</div>')
        self.assertIsNone(await core.find_building_dropdown(self.page))

    async def test_recorded_id_outside_active_dialog_ignored(self):
        await self.load('<div id="ev_slt_10015_ev_se">' + control() + '</div>' +
                        '<div class="ev_Dialog_Panel" style="top:80px">' +
                        control(nested=True) + '</div>')
        entry = await core.find_building_dropdown(self.page, 'F5-1A')
        self.assertIsNotNone(entry)
        self.assertTrue(await entry.evaluate("e => !!e.closest('.ev_Dialog_Panel')"))

    async def test_different_values_are_not_collapsed(self):
        await self.load('<div class="ev_Dialog_Panel">' + control() +
                        control(core.AREA_LABELS['F5'], style='z-index:2') + '</div>')
        self.assertIsNone(await core.find_building_dropdown(self.page))

    async def test_hidden_option_copy_does_not_block_visible_option(self):
        await self.load('<button id="entry">楼栋</button>'
                        '<div role="option" style="display:none" aria-label="选项1, 科研楼F5"></div>'
                        '<div role="option" aria-label="选项1, 科研楼F5" '
                        'onclick="window.chosen=true">科研楼F5</div>')
        with patch.object(core, 'FILTER_OPTION_TIMEOUT_MS', 100), \
             patch.object(core, 'save_area_filter_diagnostic', new=AsyncMock()):
            await core.select_building(self.page, 'F5', self.page.locator('#entry'))
        self.assertTrue(await self.page.evaluate('window.chosen'))


    async def test_switch_both_areas_and_validate_all_16_room_identities(self):
        import json
        names = {area: ['CN-ShenZhen-F5-' + short for short in expected]
                 for area, expected in core.AREA_EXPECTED_SHORT_NAMES.items()}
        for area, initial in [('F5', 'F5-1A'), ('F5-1A', 'F5')]:
            with self.subTest(area=area):
                await self.load(
                    '<button aria-label="图标 添加会议室" onclick="document.querySelector(\'.ev_Dialog_Panel\').style.display=\'block\'">添加</button>'
                    '<div class="ev_Dialog_Panel" style="display:none">'
                    + control(core.AREA_LABELS[initial], nested=True) +
                    '<div id="options" style="display:none;position:absolute;top:55px">'
                    '<div role="option" aria-label="选项1, 科研楼F5" data-area="F5">科研楼F5</div>'
                    '<div role="option" aria-label="选项2, 科研楼F5-1A外来接待区" data-area="F5-1A">科研楼F5-1A外来接待区</div></div>'
                    '<button id="query" style="position:absolute;top:85px">查询</button>'
                    '<div class="ev_Dialog_PanelContent" style="position:absolute;top:120px"></div></div>')
                await self.page.evaluate("""({names, initial}) => {
                    window.selectedArea=initial;
                    window.renderRooms=area => {
                        document.querySelector('.ev_Dialog_PanelContent').innerHTML=
                            names[area].map(n=>'<div>'+n+'</div>').join('');
                    };
                    window.renderRooms(initial);
                    document.querySelector('.ev_select_drop_icon').onclick=()=>{
                        document.getElementById('options').style.display='block';
                    };
                    document.querySelectorAll('[role=option]').forEach(e=>e.onclick=()=>{
                        window.selectedArea=e.dataset.area;
                        document.getElementById('options').style.display='none';
                    });
                    document.getElementById('query').onclick=()=>{
                        setTimeout(()=>window.renderRooms(window.selectedArea), 40);
                    };
                }""", {'names': names, 'initial': initial})
                with patch.object(core, 'AREA_RESULT_WAIT_SECONDS', 0.6):
                    self.assertEqual(await core.select_area(self.page, area), 'explicit')
                observed = await core.read_visible_room_names(self.page)
                self.assertTrue(core.rooms_match_area(observed, area))
                for room in core.ROOMS:
                    if room.area == area:
                        await core.verify_room_identity(self.page, room)
        # 即使下拉点击完成，查询仍返回旧区域也必须被拒绝。
        await self.page.evaluate("document.getElementById('query').onclick=()=>{}")
        with patch.object(core, 'AREA_RESULT_WAIT_SECONDS', 0.12):
            with self.assertRaises(core.SelectorConfigurationError):
                await core.query_area_rooms(self.page, 'F5')


    async def test_text_fallback_cannot_reach_outside_dialog(self):
        await self.load('<div class="ev_Dialog_Panel"><span>科研楼F5</span></div>'
                        '<span id="outside" class="ev_select_drop_icon"></span>')
        self.assertIsNone(await core.find_building_dropdown(self.page))

    async def test_text_fallback_collects_different_labels_before_resolving(self):
        await self.load('<div class="ev_Dialog_Panel">' +
                        control(core.AREA_LABELS['F5'], field=False) +
                        control(core.AREA_LABELS['F5-1A'], field=False, style='top:65px') + '</div>')
        self.assertIsNone(await core.find_building_dropdown(self.page))

    async def test_inserted_city_control_cannot_retarget_selected_building(self):
        await self.load('<div class="ev_Dialog_Panel">' + control() + '</div>')
        entry = await core.find_building_dropdown(self.page, 'F5-1A')
        self.assertIsNotNone(entry)
        await self.page.evaluate("""() => {
            document.querySelector('.ev_Dialog_Panel').insertAdjacentHTML('afterbegin',
              '<div class="ev_select_div" style="top:65px"><div class="ev_input">深圳' +
              '<span class="ev_select_drop_icon" onclick="window.cityClicked=true"></span></div></div>');
        }""")
        try:
            await entry.click(timeout=500)
            self.assertEqual(await self.page.evaluate('window.clicked'), 1)
            self.assertFalse(await self.page.evaluate('!!window.cityClicked'))
        finally:
            if hasattr(entry, 'dispose'):
                await entry.dispose()

if __name__ == '__main__':
    unittest.main()

