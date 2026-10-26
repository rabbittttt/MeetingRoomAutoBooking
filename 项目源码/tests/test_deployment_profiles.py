# -*- coding: utf-8 -*-
"""部署配置的离线验证；不访问网页，不改写正式配置。"""
import ast
from copy import deepcopy
from pathlib import Path
import re
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import deployment_profiles as profiles


class DeploymentProfileTests(unittest.TestCase):
    def test_f5_preserves_confirmed_roster_and_release_rules(self):
        profile = profiles.get_profile("F5")
        self.assertEqual(profile["city"], "深圳(Shenzhen)")
        self.assertEqual(profile["zone"], "F")
        self.assertEqual(profile["release_open_at"], "08:30:00")
        self.assertEqual(profile["expected_counts"], {"F5": 3, "F5-1A": 13})
        self.assertEqual(profile["room_catalog"]["F5"], {
            "label": "科研楼F5", "advance_days": 7,
            "rooms": {"7F-A05R": 1, "7F-A28R": 2, "7F-B02R": 3}})
        second = profile["room_catalog"]["F5-1A"]
        self.assertEqual(second["label"], "科研楼F5-1A外来接待区")
        self.assertEqual(second["advance_days"], 3)
        self.assertEqual(list(second["rooms"].items()), [
            ("1F-A10R", 1), ("1F-A11R", 2), ("1F-A12R", 3), ("1F-A13R", 4),
            ("1F-A14R", 5), ("1F-A15R", 6), ("1F-A16R", 7), ("1F-A17R", 8),
            ("1F-A18R", 9), ("1F-A20R", 10), ("1F-A24R", 11), ("1F-A25R", 12),
            ("1F-A26R", 13)])

    def test_h7_preserves_user_supplied_fifteen_rooms_in_page_order(self):
        profile = profiles.get_profile("H7")
        self.assertEqual(profile["city"], "东莞(DongGuan)")
        self.assertEqual(profile["zone"], "溪流背坡村(XLBPC)")
        self.assertEqual(profile["release_open_at"], "08:45:00")
        self.assertEqual(profile["expected_counts"], {"H7": 15})
        area = profile["room_catalog"]["H7"]
        self.assertEqual(area["label"], "H7")
        self.assertEqual(area["advance_days"], 7)
        self.assertEqual(list(area["rooms"].items()), [
            ("2-A12R", 1), ("2-A13R", 2), ("2-A14R", 3), ("2-A15R", 4),
            ("2-B12R", 5), ("2-B13R", 6), ("2-B14R", 7), ("3-A12R", 8),
            ("3-A14R", 9), ("3-A15R", 10), ("3-B13R", 11), ("3-B14R", 12),
            ("4-A11R", 13), ("4-A12R", 14), ("4-A14R", 15)])

    def test_f5_full_names_reject_same_short_names_in_other_buildings(self):
        profile = profiles.get_profile("F5")
        pattern = profile["full_name_pattern"]
        # Both real F5 areas use F5 in the full name, including the 1F rooms.
        for area, config in profile["room_catalog"].items():
            for short in config["rooms"]:
                with self.subTest(area=area, short=short):
                    confirmed = "CN-ShenZhen-F5-" + short
                    self.assertIsNotNone(re.fullmatch(pattern, confirmed, re.IGNORECASE))
                    self.assertIsNotNone(re.fullmatch(
                        pattern, confirmed.replace("-", "_").lower(), re.IGNORECASE))
                    for building in ("F6", "F50"):
                        other = f"CN-ShenZhen-{building}-{short}"
                        # DOM extraction uses search; it must not admit the other building.
                        self.assertIsNone(re.search(pattern, other, re.IGNORECASE))

    def test_profile_copy_cannot_change_original_or_later_lookup(self):
        before = deepcopy(profiles.PROFILES)
        first = profiles.get_profile("H7")
        first["room_catalog"]["H7"]["rooms"].clear()
        first["expected_counts"]["H7"] = 0
        first["city"] = "修改后的测试城市"
        self.assertEqual(profiles.PROFILES, before)
        self.assertEqual(profiles.get_profile("H7"), before["H7"])
        self.assertEqual(profiles.get_profile("F5"), before["F5"])

    def test_b2a_matches_only_four_requested_rooms_and_confirmed_release_rules(self):
        profile = profiles.get_profile("B2a")
        self.assertEqual(profile["city"], "深圳(Shenzhen)")
        self.assertEqual(profile["zone"], "B")
        self.assertEqual(profile["release_open_at"], "08:30:00")
        self.assertEqual(profile["expected_counts"], {"B2a": 4})
        self.assertEqual(profile["room_catalog"], {"B2a": {
            "label": "华电B2", "advance_days": 7,
            "rooms": {"2-A48R": 1, "2-A50R": 2, "2-B35R": 3, "2-B41R": 4}}})
        for short in profile["room_catalog"]["B2a"]["rooms"]:
            full = "CN-HQ-B2a-" + short
            self.assertIsNotNone(re.search(profile["full_name_pattern"], full))
            self.assertEqual(re.search(profile["short_name_pattern"], full).group(1), short)
        for other in ("CN-HQ-B2a-2-A48R2", "CN-HQ-B2a-3-A48R",
                      "CN-HQ-B2b-2-A48R", "CN-HQ-B2a-2-A49R",
                      "other-CN-HQ-B2a-2-A48R"):
            with self.subTest(other=other):
                self.assertIsNone(re.search(profile["full_name_pattern"], other))

    def test_selected_profile_is_independent_of_incomplete_other_profile(self):
        for selected, other in (("F5", "H7"), ("H7", "F5")):
            with self.subTest(selected=selected):
                broken = deepcopy(profiles.PROFILES[other])
                next(iter(broken["room_catalog"].values()))["rooms"].clear()
                with patch.dict(profiles.PROFILES, {other: broken}):
                    self.assertEqual(profiles.get_profile(selected)["id"], selected)
                    with self.assertRaisesRegex(ValueError, "完整.*会议室"):
                        profiles.get_profile(other)

    def test_h7_missing_any_room_rejects_build_and_render(self):
        for keep_empty in (True, False):
            with self.subTest(empty=keep_empty):
                broken = profiles.get_profile("H7")
                rooms = broken["room_catalog"]["H7"]["rooms"]
                rooms.clear() if keep_empty else rooms.pop("3-B14R")
                with patch.dict(profiles.PROFILES, {"H7": broken}):
                    with self.assertRaisesRegex(ValueError, "完整的15间"):
                        profiles.get_profile("H7")
                    self.assertEqual(profiles.get_profile_label("H7"), "H7")
                with self.assertRaisesRegex(ValueError, "完整的15间"):
                    profiles.render_profile(broken)

    def test_render_contains_only_selected_profile_literal_and_round_trips_utf8(self):
        for profile_id in ("F5", "H7", "B2a"):
            with self.subTest(profile_id=profile_id):
                profile = profiles.get_profile(profile_id)
                rendered = profiles.render_profile(profile)
                self.assertEqual(rendered.encode("utf-8").decode("utf-8"), rendered)
                tree = ast.parse(rendered)
                self.assertEqual(len(tree.body), 1)
                assignment = tree.body[0]
                self.assertIsInstance(assignment, ast.Assign)
                self.assertEqual([item.id for item in assignment.targets], ["PROFILE"])
                self.assertEqual(ast.literal_eval(assignment.value), profile)
                compile(rendered, "选定部署配置.py", "exec")
                if profile_id == "H7":
                    self.assertNotIn("科研楼F5", rendered)
                    self.assertNotIn("ShenZhen", rendered)
                else:
                    self.assertNotIn("溪村", rendered)
                    self.assertNotIn("DongGuan", rendered)

    def test_real_full_names_extract_profile_short_names(self):
        examples = {
            "F5": ["CN-ShenZhen-F5-7F-A05R", "CN_ShenZhen_F5_7F_A05R"],
            "H7": ["溪村-H7-2-A12R", "溪村_H7_4_A14R"],
        }
        for profile_id, names in examples.items():
            profile = profiles.get_profile(profile_id)
            for full in names:
                with self.subTest(profile_id=profile_id, full=full):
                    self.assertIsNotNone(re.fullmatch(profile["full_name_pattern"], full))
                    match = re.search(profile["short_name_pattern"], full)
                    self.assertIsNotNone(match)
                    short = match.group(1).replace("_", "-")
                    self.assertTrue(any(short in area["rooms"]
                                        for area in profile["room_catalog"].values()))

    def test_full_name_rules_do_not_accept_other_site(self):
        self.assertIsNone(re.fullmatch(profiles.get_profile("F5")["full_name_pattern"], "溪村-H7-2-A12R"))
        self.assertIsNone(re.fullmatch(profiles.get_profile("H7")["full_name_pattern"], "CN-ShenZhen-F5-7F-A05R"))

    def test_short_name_format_accepts_optional_floor_f_without_changing_official_roster(self):
        profile = profiles.get_profile("F5")
        rooms = profile["room_catalog"]["F5"]["rooms"]
        rooms["7-A05R"] = rooms.pop("7F-A05R")
        profiles.validate_profile(profile)
        self.assertIn("7F-A05R", profiles.PROFILES["F5"]["room_catalog"]["F5"]["rooms"])

    def test_bad_short_names_are_rejected(self):
        for name in ("-2-A12R", "溪村-H7-2-A12R", "2-A12", "2-a12R", "2-A12R ", "2-A12R\n"):
            with self.subTest(name=name):
                profile = profiles.get_profile("H7")
                rooms = profile["room_catalog"]["H7"]["rooms"]
                rooms[name] = rooms.pop("2-A12R")
                with self.assertRaises(ValueError):
                    profiles.validate_profile(profile)

    def test_duplicate_noninteger_and_noncontinuous_rows_are_rejected(self):
        for row in (2, 0, 16, True, "1", 1.0, None):
            with self.subTest(row=row):
                profile = profiles.get_profile("H7")
                profile["room_catalog"]["H7"]["rooms"]["2-A12R"] = row
                with self.assertRaisesRegex(ValueError, "行号"):
                    profiles.validate_profile(profile)

    def test_counts_must_match_catalog_and_be_positive_integers(self):
        for counts in ({}, {"H7": 14}, {"H7": True}, {"H7": 0}, {"H7": 51},
                       {"H7": "15"}, {"H7": 15, "F5": 3}):
            with self.subTest(counts=counts):
                profile = profiles.get_profile("H7")
                profile["expected_counts"] = counts
                with self.assertRaises(ValueError):
                    profiles.validate_profile(profile)

    def test_empty_city_zone_and_labels_are_rejected(self):
        for field in ("city", "zone", "label"):
            for value in (None, "", " ", "有空格 "):
                with self.subTest(field=field, value=value):
                    profile = profiles.get_profile("H7")
                    profile[field] = value
                    with self.assertRaises(ValueError):
                        profiles.validate_profile(profile)
        profile = profiles.get_profile("H7")
        profile["room_catalog"]["H7"]["label"] = ""
        with self.assertRaisesRegex(ValueError, "网页区域名称"):
            profiles.validate_profile(profile)

    def test_invalid_opening_clock_is_rejected(self):
        for opening in ("8:45:00", "08:45", "24:00:00", "08:60:00", "08:45:60",
                        "08:45:00+08:00", None, "08:45:00 "):
            with self.subTest(opening=opening):
                profile = profiles.get_profile("H7")
                profile["release_open_at"] = opening
                with self.assertRaisesRegex(ValueError, "放号时间"):
                    profiles.validate_profile(profile)

    def test_invalid_advance_days_are_rejected(self):
        for days in (-1, 366, True, "7", 7.0, None):
            with self.subTest(days=days):
                profile = profiles.get_profile("H7")
                profile["room_catalog"]["H7"]["advance_days"] = days
                with self.assertRaisesRegex(ValueError, "提前天数"):
                    profiles.validate_profile(profile)

    def test_profile_identifiers_cannot_contain_paths(self):
        for profile_id in ("../H7", "H7/other", "H7\\other", "D:H7", " H7", "", None, "H7\n"):
            with self.subTest(profile_id=profile_id):
                with self.assertRaisesRegex(ValueError, "不能包含路径"):
                    profiles.get_profile(profile_id)
                profile = profiles.get_profile("H7")
                profile["id"] = profile_id
                with self.assertRaises(ValueError):
                    profiles.render_profile(profile)

    def test_area_identifiers_cannot_contain_paths(self):
        profile = profiles.get_profile("H7")
        profile["room_catalog"]["../H7"] = profile["room_catalog"].pop("H7")
        profile["expected_counts"] = {"../H7": 15}
        with self.assertRaisesRegex(ValueError, "不能包含路径"):
            profiles.validate_profile(profile)

    def test_unknown_profile_and_inconsistent_internal_id_are_rejected(self):
        with self.assertRaisesRegex(ValueError, "未知部署配置"):
            profiles.get_profile("Unknown")
        broken = profiles.get_profile("H7")
        broken["id"] = "Other"
        with patch.dict(profiles.PROFILES, {"H7": broken}):
            with self.assertRaisesRegex(ValueError, "内部编号"):
                profiles.get_profile("H7")

    def test_invalid_regex_and_capture_shape_are_rejected(self):
        for field, pattern in (("full_name_pattern", "["), ("short_name_pattern", "["),
                               ("short_name_pattern", r"\d+-[A-Z]\d+R"),
                               ("short_name_pattern", r"(\d+)-([A-Z]\d+R)")):
            with self.subTest(field=field, pattern=pattern):
                profile = profiles.get_profile("H7")
                profile[field] = pattern
                with self.assertRaises(ValueError):
                    profiles.validate_profile(profile)

    def test_schema_rejects_missing_fields_and_embedded_unselected_profiles(self):
        profile = profiles.get_profile("H7")
        profile.pop("city")
        with self.assertRaisesRegex(ValueError, "缺少字段"):
            profiles.render_profile(profile)
        profile = profiles.get_profile("H7")
        profile["all_profiles"] = deepcopy(profiles.PROFILES)
        with self.assertRaisesRegex(ValueError, "未支持的字段"):
            profiles.render_profile(profile)


if __name__ == "__main__":
    unittest.main()
