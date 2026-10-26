# -*- coding: utf-8 -*-
"""区域构建与网页房名回归；暂存写入临时目录，浏览器仅加载本地夹具。"""
import asyncio
import ast
from contextlib import ExitStack
from datetime import date
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import build_exe
from deployment_profiles import get_profile, render_profile


STAGE_PROBE = r'''
import copy
import importlib
import json
from pathlib import Path
import sys
import booking_core as core
import booking_profile
gui = importlib.import_module(sys.argv[1])
snapshot = {
    "deployment_id": core.DEPLOYMENT_ID,
    "catalog": core.ROOM_CATALOG,
    "rooms": gui.DEFAULT_CONFIG["rooms"],
    "areas": list(gui.AREAS),
    "core_open_at": core.RELEASE_OPEN_AT,
    "gui_open_at": gui.DEFAULT_CONFIG["release_open_at"],
    "gui_deployment_id": gui.DEFAULT_CONFIG["deployment_id"],
    "core_tasks": [core.RELEASE_TARGETS, core.WEEKLY_RELEASE_RULES, core.WATCH_TARGETS],
    "gui_tasks": [gui.DEFAULT_CONFIG[k] for k in
                  ("release_targets", "weekly_release_rules", "watch_targets")],
    "app_data": str(core.APP_DATA_DIR),
    "chrome_data": str(core.PROFILE_DIR),
    "logs": str(core.LOG_DIR),
    "config": str(gui.CONFIG_FILE),
    "core_path": str(Path(core.__file__).resolve()),
    "profile_path": str(Path(booking_profile.__file__).resolve()),
    "gui_path": str(Path(gui.__file__).resolve()),
    "core_hash": gui.CORE_SHA256,
}
wrong = copy.deepcopy(gui.DEFAULT_CONFIG)
wrong["deployment_id"] = "H7" if core.DEPLOYMENT_ID == "F5" else "F5"
wrong["release_open_at"] = "23:59:59"
first_area = next(iter(wrong["rooms"]))
wrong["rooms"] = {first_area: wrong["rooms"][first_area][:1]}
before_rooms = copy.deepcopy(core.ROOM_SELECTION)
before_clock = core.RELEASE_OPEN_AT
try:
    core.apply_gui_settings(wrong)
except ValueError as exc:
    snapshot["core_mismatch_error"] = str(exc)
else:
    snapshot["core_mismatch_error"] = ""
snapshot["core_mismatch_unchanged"] = (
    core.ROOM_SELECTION == before_rooms and core.RELEASE_OPEN_AT == before_clock)
gui.CONFIG_FILE.parent.mkdir(parents=True, exist_ok=True)
gui.CONFIG_FILE.write_text(json.dumps(wrong, ensure_ascii=False), encoding="utf-8")
try:
    gui.load_config()
except ValueError as exc:
    snapshot["gui_mismatch_error"] = str(exc)
else:
    snapshot["gui_mismatch_error"] = ""
print(json.dumps(snapshot, ensure_ascii=False))
'''


class RegionBuildTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="booking_region_tests_")
        self.addCleanup(self.temp.cleanup)
        self.build_root = Path(self.temp.name) / "project"
        self.build_root.mkdir()
        # 只读取正式运行模块；管理员模块使用自造标记，绝不读取管理员私钥。
        for filename in build_exe.RUNTIME_MODULES:
            (self.build_root / filename).write_bytes((ROOT / filename).read_bytes())
        (self.build_root / "deployment_profiles.py").write_text(
            "ALL_DEPLOYMENTS_MUST_NOT_SHIP = True\n", encoding="utf-8")
        (self.build_root / "license_admin.py").write_text(
            "ADMIN_TEST_MARKER_MUST_NOT_SHIP = True\n", encoding="utf-8")
        (self.build_root / "booking_profile.py").write_text(
            render_profile(get_profile("F5")), encoding="utf-8")
        self.source = ROOT / "会议室预定.py"
        self.gui = ROOT / "会议室自动抢订_GUI.py"
        self.patches = ExitStack()
        self.addCleanup(self.patches.close)
        self.patches.enter_context(patch.object(build_exe, "ROOT", self.build_root))
        self.patches.enter_context(patch.object(build_exe, "out"))

    def settings(self, profile_id):
        values = dict(build_exe.DEFAULTS)
        values.update(profile_id=profile_id)
        return values

    def stage(self, profile_id):
        return build_exe.prepare_build_sources(
            self.settings(profile_id), self.source, self.gui, get_profile(profile_id))

    def inspect_stage(self, source, gui):
        env = os.environ.copy()
        env["PYTHONPATH"] = str(source.parent)
        env["LOCALAPPDATA"] = str(Path(self.temp.name) / "user_data")
        env["PYTHONIOENCODING"] = "utf-8"
        result = subprocess.run(
            [sys.executable, "-B", "-c", STAGE_PROBE, gui.stem],
            cwd=source.parent, env=env, encoding="utf-8", errors="replace",
            stdout=subprocess.PIPE, stderr=subprocess.PIPE, timeout=30)
        self.assertEqual(result.returncode, 0, result.stdout + "\n" + result.stderr)
        return json.loads(result.stdout.strip())

    def test_regions_import_correct_runtime_defaults_and_use_isolated_data(self):
        snapshots = {}
        for profile_id in ("F5", "H7", "B2a"):
            with self.subTest(profile_id=profile_id):
                source, gui = self.stage(profile_id)
                actual = self.inspect_stage(source, gui)
                snapshots[profile_id] = actual
                expected = get_profile(profile_id)
                self.assertEqual(actual["deployment_id"], profile_id)
                self.assertEqual(actual["gui_deployment_id"], profile_id)
                self.assertEqual(actual["catalog"], expected["room_catalog"])
                self.assertEqual(actual["areas"], list(expected["room_catalog"]))
                self.assertEqual(actual["rooms"], {
                    area: list(values["rooms"]) for area, values in expected["room_catalog"].items()})
                self.assertEqual(actual["core_open_at"], expected["release_open_at"])
                self.assertEqual(actual["gui_open_at"], expected["release_open_at"])
                self.assertEqual(actual["core_tasks"], [[], [], []])
                self.assertEqual(actual["gui_tasks"], [[], [], []])
                self.assertEqual(actual["core_hash"], build_exe.sha256(self.source))
                self.assertEqual(Path(actual["core_path"]), source.resolve())
                self.assertEqual(Path(actual["gui_path"]), gui.resolve())
                self.assertEqual(Path(actual["profile_path"]), (source.parent / "booking_profile.py").resolve())
                expected_data = Path(self.temp.name) / "user_data" / "MeetingRoomAutoBooking"
                if profile_id != "F5":
                    expected_data /= profile_id
                self.assertEqual(Path(actual["app_data"]), expected_data)
                self.assertEqual(Path(actual["chrome_data"]), expected_data / "chrome_profile")
                self.assertEqual(Path(actual["logs"]), expected_data / "logs")
                self.assertEqual(Path(actual["config"]), expected_data / "config.json")
        for field in ("app_data", "chrome_data", "logs", "config"):
            self.assertEqual(len({values[field] for values in snapshots.values()}), 3)

    def test_stage_contains_only_selected_profile_and_runtime_modules(self):
        for profile_id in ("F5", "H7", "B2a"):
            with self.subTest(profile_id=profile_id):
                source, gui = self.stage(profile_id)
                expected_files = set(build_exe.RUNTIME_MODULES) | {
                    "booking_core.py", "booking_profile.py", "build_info.py", gui.name}
                self.assertEqual({p.name for p in source.parent.iterdir()}, expected_files)
                tree = ast.parse((source.parent / "booking_profile.py").read_text(encoding="utf-8"))
                self.assertEqual(len(tree.body), 1)
                self.assertEqual(ast.literal_eval(tree.body[0].value), get_profile(profile_id))
                for filename in ("deployment_profiles.py", "license_admin.py"):
                    self.assertFalse((source.parent / filename).exists())

    def test_repeated_f5_h7_f5_staging_preserves_sources_and_default_profile(self):
        watched = [self.source, self.gui, ROOT / "booking_profile.py",
                   self.build_root / "booking_profile.py"]
        before = {path: path.read_bytes() for path in watched}
        first, _ = self.stage("F5")
        first_profile = (first.parent / "booking_profile.py").read_bytes()
        second, _ = self.stage("H7")
        second_profile = (second.parent / "booking_profile.py").read_bytes()
        third, _ = self.stage("F5")
        self.assertEqual(first, third)
        self.assertNotEqual(first.parent, second.parent)
        self.assertEqual((third.parent / "booking_profile.py").read_bytes(), first_profile)
        self.assertEqual((second.parent / "booking_profile.py").read_bytes(), second_profile)
        for path, original in before.items():
            self.assertEqual(path.read_bytes(), original, str(path))
        default_tree = ast.parse((ROOT / "booking_profile.py").read_text(encoding="utf-8-sig"))
        self.assertEqual(ast.literal_eval(default_tree.body[0].value)["id"], "F5")

    def test_build_command_uses_selected_stage_and_separate_output_work_spec_paths(self):
        commands, outputs = {}, {}
        for profile_id in ("F5", "H7", "B2a"):
            source, gui = self.stage(profile_id)
            command, output = build_exe.build_command(
                Path(sys.executable), self.settings(profile_id), gui, source)
            commands[profile_id], outputs[profile_id] = command, output
            self.assertEqual(command[-1], str(gui))
            self.assertEqual(command[command.index("--paths") + 1], str(source.parent))
            self.assertEqual(command[command.index("--distpath") + 1], str(self.build_root / "dist" / profile_id))
            self.assertEqual(command[command.index("--workpath") + 1], str(self.build_root / "build" / "pyinstaller" / profile_id))
            self.assertEqual(command[command.index("--specpath") + 1], str(self.build_root / "build" / "specs" / profile_id))
            self.assertEqual(output, self.build_root / "dist" / profile_id / f"会议室自动抢订_{profile_id}.exe")
            imports = [command[index + 1] for index, value in enumerate(command) if value == "--hidden-import"]
            self.assertIn("booking_profile", imports)
            self.assertNotIn("deployment_profiles", imports)
            self.assertNotIn("license_admin", imports)
        self.assertEqual(len(set(outputs.values())), 3)

    def test_explicit_other_deployment_config_is_rejected_before_runtime_mutation(self):
        for profile_id in ("F5", "H7", "B2a"):
            with self.subTest(profile_id=profile_id):
                source, gui = self.stage(profile_id)
                actual = self.inspect_stage(source, gui)
                self.assertTrue(actual["core_mismatch_error"], "核心入口必须拒绝其他区域的配置")
                self.assertTrue(actual["gui_mismatch_error"], "界面入口必须拒绝其他区域的配置")
                self.assertTrue(actual["core_mismatch_unchanged"], "拒绝错配前不得改动正在使用的房间或时间")


class RegionBrowserTests(unittest.IsolatedAsyncioTestCase):
    """本地Chrome验证生产的房名读取、房间核验和JavaScript格子定位。"""
    async def asyncSetUp(self):
        from playwright.async_api import async_playwright
        import booking_core
        sys.path.insert(0, str(ROOT / "tests"))
        import test_grid_regressions
        self.core, self.fixture = booking_core, test_grid_regressions
        self.stack = ExitStack()
        self.addCleanup(self.stack.close)
        self.pw = await async_playwright().start()
        self.addAsyncCleanup(self.pw.stop)
        self.browser = await self.pw.chromium.launch(
            channel=os.environ.get("BOOKING_TEST_BROWSER", "chrome"), headless=True)
        self.addAsyncCleanup(self.browser.close)
        self.context = await self.browser.new_context(viewport={"width": 2400, "height": 900})
        await self.context.route("**/*", lambda route: route.abort())
        self.page = await self.context.new_page()
        self.stack.enter_context(patch.object(self.core, "save_grid_diagnostic", AsyncMock()))
        self.stack.enter_context(patch.object(self.core, "ACTION_TIMEOUT_MS", 2500))
        self.stack.enter_context(patch.object(self.core, "DRY_RUN", False))
        for name in ("STOP_REQUESTED", "DRIVER_LOST", "AUTH_BLOCKED", "CONFIG_BLOCKED"):
            self.stack.enter_context(patch.object(self.core, name, asyncio.Event()))

    async def load_region(self, profile_id, area, **fixture_options):
        profile = get_profile(profile_id)
        self.stack.enter_context(patch.object(self.core, "ROOM_FULL_NAME_PATTERN", profile["full_name_pattern"]))
        self.stack.enter_context(patch.object(self.core, "ROOM_SHORT_NAME_PATTERN", profile["short_name_pattern"]))
        expected = {key: tuple(value["rooms"]) for key, value in profile["room_catalog"].items()}
        self.stack.enter_context(patch.object(self.core, "AREA_EXPECTED_SHORT_NAMES", expected))
        rooms = profile["room_catalog"][area]["rooms"]
        with patch.object(self.fixture, "ROOMS", tuple(rooms)):
            html = self.fixture.fixture_html(**fixture_options)
        if profile_id == "H7":
            html = html.replace("CN-ShenZhen-F5-", "溪村-H7-")
        await self.page.set_content(html)
        return rooms

    async def test_h7_full_names_are_read_and_first_last_rows_select_correct_grid(self):
        for name in ("2-A12R", "4-A14R"):
            with self.subTest(name=name):
                rooms = await self.load_region("H7", "H7", background=True, header=True, redraw=True)
                visible = await self.core.read_visible_room_names(self.page)
                self.assertEqual([self.core.room_short_name(item["name"]) for item in visible], list(rooms))
                self.assertTrue(self.core.rooms_match_area(visible, "H7"))
                row = rooms[name]
                await self.core.verify_room_identity(self.page, self.core.RoomSpec("H7", name, row))
                target = self.core.Target(date(2030, 1, 3), "10:00", "12:00", ("H7",))
                await self.core.choose_room_cells(self.page, name, row, target)
                self.assertEqual(await self.page.evaluate("window.clicked"), [f"room{row}-21", f"room{row}-24"])
                self.assertTrue(await self.page.locator("#confirm").is_enabled())
                self.assertEqual(await self.page.evaluate("window.redraws"), 1)

    async def test_f5_names_and_grid_continue_to_work(self):
        rooms = await self.load_region("F5", "F5", background=True, header=True)
        visible = await self.core.read_visible_room_names(self.page)
        self.assertEqual([self.core.room_short_name(item["name"]) for item in visible], list(rooms))
        self.assertTrue(self.core.rooms_match_area(visible, "F5"))
        name, row = "7F-B02R", rooms["7F-B02R"]
        await self.core.verify_room_identity(self.page, self.core.RoomSpec("F5", name, row))
        target = self.core.Target(date(2030, 1, 3), "10:00", "12:00", ("F5",))
        await self.core.choose_room_cells(self.page, name, row, target)
        self.assertEqual(await self.page.evaluate("window.clicked"), ["room3-21", "room3-24"])
        self.assertTrue(await self.page.locator("#confirm").is_enabled())

    async def test_h7_wrong_row_is_rejected_without_clicking_neighbor(self):
        await self.load_region("H7", "H7")
        with self.assertRaises(self.core.SelectorConfigurationError):
            await self.core.verify_room_identity(self.page, self.core.RoomSpec("H7", "2-A12R", 2))
        target = self.core.Target(date(2030, 1, 3), "10:00", "12:00", ("H7",))
        with self.assertRaises(self.core.SelectorConfigurationError):
            await self.core.choose_room_cells(self.page, "2-A12R", 2, target)
        self.assertEqual(await self.page.evaluate("window.clicked"), [])


if __name__ == "__main__":
    unittest.main()
