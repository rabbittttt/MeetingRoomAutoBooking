"""管理员名单、旧配置迁移及运行参数校验；不访问公司网络或创建窗口。"""
import ast
import copy
import importlib.util
import json
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch

import booking_core as core
import 会议室自动抢订_GUI as gui


class RoomCatalogTests(unittest.TestCase):
    def setUp(self):
        self.area = next(iter(core.AREA_ROOM_ROWS))
        self.name = next(iter(core.AREA_ROOM_ROWS[self.area]))

    def test_default_selection_is_independent_of_catalog_and_other_callers(self):
        first = core.default_room_selection()
        first[self.area].clear()
        self.assertTrue(core.default_room_selection()[self.area])
        self.assertIn(self.name, core.AREA_ROOM_ROWS[self.area])

    def test_selected_room_uses_compiled_row(self):
        selected = {self.area: [self.name]}
        self.assertEqual(core.resolve_rooms(selected), [
            core.RoomSpec(self.area, self.name, core.AREA_ROOM_ROWS[self.area][self.name])])

    def test_external_selection_cannot_add_room_area_or_raw_row(self):
        for selected in ({self.area: ["99F-UNLISTED"]}, {"UNLISTED": [self.name]},
                         {self.area: [1]}, {self.area: [True]},
                         {self.area: [{"name": self.name, "row": 50}]},
                         {self.area: self.name}, {self.area: [self.name, self.name]},
                         [], {}):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                core.resolve_rooms(selected)

    def test_f5_1a_legacy_integer_bypass_is_rejected(self):
        if "F5-1A" not in core.AREA_ROOM_ROWS:
            self.skipTest("当前管理员名单没有历史 F5-1A 区域")
        with self.assertRaises(ValueError):
            core.resolve_rooms({"F5-1A": [50]})

    def test_apply_rejects_tampering_before_changing_active_selection(self):
        original_selection, original_rooms = core.ROOM_SELECTION, core.ROOMS
        for selected in ({self.area: [50]}, {self.area: ["UNLISTED"]},
                         {"UNLISTED": []}, {self.area: self.name}):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                core.apply_gui_settings({"rooms": selected})
            self.assertIs(core.ROOM_SELECTION, original_selection)
            self.assertIs(core.ROOMS, original_rooms)

    def test_admin_duplicate_rows_and_invalid_rows_fail_early(self):
        for rows in ({"8F-A01R": 1, "8F-A02R": 1}, {"8F-A01R": True},
                     {"8F-A01R": 0}, {"8F-A01R": 51}, {" 8F-A01R": 1}, {}):
            catalog = {"F9": {"label": "科研楼F9", "advance_days": 2, "rooms": rows}}
            with self.subTest(rows=rows), self.assertRaises(ValueError):
                core.validate_room_catalog(catalog)

    def test_admin_invalid_opening_rules_and_empty_labels_fail_early(self):
        for label, days in (("", 2), ("科研楼F9", -1), ("科研楼F9", True)):
            catalog = {"F9": {"label": label, "advance_days": days, "rooms": {"8F-A01R": 1}}}
            with self.subTest(label=label, days=days), self.assertRaises(ValueError):
                core.validate_room_catalog(catalog)

    def test_only_changing_source_catalog_drives_gui_defaults_and_preview(self):
        # 只替换管理员配置节点，保留其他源代码，模拟管理员换区域和楼层后重新打包。
        catalog = {"F9": {"label": "科研楼F9", "advance_days": 2,
                           "rooms": {"8F-A01R": 1, "8F-A02R": 2}}}
        source = Path(core.__file__).read_text(encoding="utf-8")
        tree = ast.parse(source)
        changed = 0
        for node in tree.body:
            if isinstance(node, ast.Assign) and any(
                    isinstance(target, ast.Name) and target.id == "ROOM_CATALOG"
                    for target in node.targets):
                node.value = ast.parse(repr(catalog), mode="eval").body
                changed += 1
        self.assertEqual(changed, 1)
        module = types.ModuleType("_catalog_fixture_core")
        module.__file__ = core.__file__
        with patch.dict(sys.modules, {module.__name__: module}):
            exec(compile(ast.fix_missing_locations(tree), core.__file__, "exec"), module.__dict__)
        self.assertEqual(module.AREA_ROOM_ROWS, {"F9": catalog["F9"]["rooms"]})
        self.assertEqual(module.AREA_EXPECTED_SHORT_NAMES, {"F9": ("8F-A01R", "8F-A02R")})
        self.assertEqual(module.AREA_ADVANCE_DAYS, {"F9": 2})
        self.assertEqual(module.AREA_LABELS, {"F9": "科研楼F9"})
        spec = importlib.util.spec_from_file_location("_catalog_fixture_gui", gui.__file__)
        fixture_gui = importlib.util.module_from_spec(spec)
        with patch.dict(sys.modules, {"booking_core": module}):
            spec.loader.exec_module(fixture_gui)
        self.assertEqual(fixture_gui.AREAS, ("F9",))
        self.assertEqual(fixture_gui.DEFAULT_CONFIG["rooms"], {"F9": ["8F-A01R", "8F-A02R"]})
        self.assertEqual(fixture_gui.release_rule_summary(), "F9提前2天")
        config = copy.deepcopy(fixture_gui.DEFAULT_CONFIG)
        config["rooms"] = {"F9": ["8F-A02R"]}
        config["release_targets"] = [dict(date="2030-01-05", start="10:00", end="12:00", areas=["F9"])]
        _, _, batches = fixture_gui.compute_release_preview(
            config, now=module.at_cn(module.date(2030, 1, 1), "08:00:00"))
        self.assertEqual(len(batches), 1)
        batch, status = batches[0]
        self.assertEqual(batch.release_day.isoformat(), "2030-01-03")
        self.assertEqual(status, "待开放")
        self.assertEqual([(room.area, room.name, room.row) for _, room in batch.tasks],
                         [("F9", "8F-A02R", 2)])


class RoomSelectionMigrationTests(unittest.TestCase):
    def test_existing_selection_keeps_only_current_catalog_names(self):
        rows = {"F9": {"8F-A01R": 1, "8F-A02R": 2, "8F-A03R": 3},
                "F10": {"2F-B01R": 1}}
        config = {"rooms": {"F9": ["7F-OLD", "8F-A02R", "8F-A02R"],
                            "REMOVED": ["1F-OLD"]}}
        with patch.object(core, "AREA_ROOM_ROWS", rows):
            self.assertEqual(gui.configured_room_selection(config),
                             {"F9": ["8F-A02R"], "F10": []})

    def test_absent_selection_defaults_to_all_current_rooms(self):
        self.assertEqual(gui.configured_room_selection({}), core.default_room_selection())

    def test_explicit_empty_selection_stays_empty(self):
        self.assertEqual(gui.configured_room_selection({"rooms": {}}),
                         {area: [] for area in core.AREA_ROOM_ROWS})

    def test_malformed_saved_selection_does_not_select_arbitrary_rows(self):
        area = next(iter(core.AREA_ROOM_ROWS))
        for selected in (None, [], {area: [1]}, {area: "room"}):
            with self.subTest(selected=selected), self.assertRaises(ValueError):
                gui.configured_room_selection({"rooms": selected})

    def test_load_config_migrates_without_changing_other_preferences(self):
        area = next(iter(core.AREA_ROOM_ROWS))
        name = next(iter(core.AREA_ROOM_ROWS[area]))
        saved = {"rooms": {area: [name, "REMOVED"]}, "release_open_at": "09:40:00", "dry_run": False}
        with tempfile.TemporaryDirectory(prefix="booking_catalog_") as directory:
            config_file = Path(directory) / "config.json"
            config_file.write_text(json.dumps(saved), encoding="utf-8")
            with patch.object(gui, "CONFIG_FILE", config_file):
                got = gui.load_config()
        self.assertEqual(got["rooms"][area], [name])
        self.assertEqual(sum(map(len, got["rooms"].values())), 1)
        self.assertEqual(got["release_open_at"], "09:40:00")
        self.assertFalse(got["dry_run"])
        self.assertEqual(saved["rooms"][area], [name, "REMOVED"])

    def test_config_without_rooms_uses_current_catalog_not_cached_defaults(self):
        with tempfile.TemporaryDirectory(prefix="booking_catalog_") as directory:
            config_file = Path(directory) / "config.json"
            config_file.write_text('{"dry_run":true}', encoding="utf-8")
            with patch.object(gui, "CONFIG_FILE", config_file), \
                 patch.object(core, "AREA_ROOM_ROWS", {"F9": {"8F-A01R": 1}}):
                self.assertEqual(gui.load_config()["rooms"], {"F9": ["8F-A01R"]})


if __name__ == "__main__":
    unittest.main()
