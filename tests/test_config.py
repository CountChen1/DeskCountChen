"""配置文件数据层单元测试。

验收判据（红线 4）：
- 全局 Settings 只允许行为参数，不得出现任何外观字段；
- 外观字段（icon_px/image/label/panel_w/panel_h/radius/bg_color/opacity/
  position_mode）必须全部按分类独立，且缺一不可；
- 旧配置迁移：app 段里的旧外观字段下发到各分类后从 app 段丢弃；
- 默认值与越界 clamp 正确；配置损坏 → 默认配置且不覆盖用户文件。

v1.2 追加：桌面图标改为 Windows 真实快捷方式后，``cat.x/cat.y`` 与
``app.watchdog_ms`` 已移除，旧配置里出现这两个字段必须被**静默忽略**。
"""

from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import (  # noqa: E402
    AppConfig,
    CategoryConfig,
    Settings,
    atomic_write_json,
    iso_utc_now,
)

SKIN_FIELDS = (
    "label",
    "image",
    "icon_px",
    "panel_w",
    "panel_h",
    "radius",
    "bg_color",
    "opacity",
    "position_mode",
)
APPEARANCE_ONLY_FIELDS = ("radius", "bg_color", "opacity", "panel_w", "panel_h", "position_mode")


class SettingsTests(unittest.TestCase):
    def test_defaults(self) -> None:
        s = Settings()
        self.assertEqual(s.drag_threshold_px, 4)
        self.assertEqual(s.edit_press_ms, 400)
        self.assertEqual(s.watch_poll_ms, 1000)
        self.assertEqual(s.icon_px_default, 64)
        self.assertEqual(s.panel_item_px, 48)

    def test_no_appearance_fields_on_settings(self) -> None:
        s = Settings()
        for name in APPEARANCE_ONLY_FIELDS:
            self.assertFalse(hasattr(s, name), f"全局 Settings 不允许出现外观字段 {name}")

    def test_watchdog_field_is_gone(self) -> None:
        """看门狗随悬浮窗体系一起移除：配置里不该再有 watchdog_ms。"""
        self.assertFalse(hasattr(Settings(), "watchdog_ms"))
        self.assertNotIn("watchdog_ms", Settings().to_dict())

    def test_to_dict_has_no_appearance_keys(self) -> None:
        d = Settings().to_dict()
        self.assertEqual(
            set(d),
            {
                "icon_px_default",
                "panel_item_px",
                "drag_threshold_px",
                "edit_press_ms",
                "watch_poll_ms",
                # v1.4 新增（行为/路径类，仍然不含任何外观字段）
                "data_root",
                "category_exclude",
                "shortcut_mode",
                "strip_extensions",
                "strip_shortcut_overlay",
            },
        )
        for name in APPEARANCE_ONLY_FIELDS:
            self.assertNotIn(name, d)

    def test_from_dict_drops_unknown_appearance_keys(self) -> None:
        s = Settings.from_dict({"radius": 20, "bg_color": "#000000", "opacity": 50, "drag_threshold_px": 6})
        self.assertEqual(s.drag_threshold_px, 6)
        self.assertFalse(hasattr(s, "radius"))

    def test_clamp_behavior_values(self) -> None:
        s = Settings.from_dict({"drag_threshold_px": 999, "edit_press_ms": 1, "watch_poll_ms": 1})
        self.assertEqual(s.drag_threshold_px, 16)
        self.assertEqual(s.edit_press_ms, 200)
        self.assertEqual(s.watch_poll_ms, 500)


class SettingsV14Tests(unittest.TestCase):
    """v1.4 新增字段：数据根 / 分类排除名单 / 落点模式 / 两个显示开关。"""

    def test_defaults(self) -> None:
        s = Settings()
        self.assertEqual(s.data_root, "", "空串 = 默认 %USERPROFILE%\\DesktopBox")
        self.assertEqual(s.category_exclude, ["快捷方式", "桌面", "z全部"])
        self.assertEqual(s.shortcut_mode, "launcher")
        self.assertTrue(s.strip_extensions)
        self.assertTrue(s.strip_shortcut_overlay)

    def test_shortcut_mode_invalid_falls_back_to_launcher(self) -> None:
        for bad in ("DESKTOPPP", "desktop2", "", None, 123):
            self.assertEqual(Settings.from_dict({"shortcut_mode": bad}).shortcut_mode,
                             "launcher", f"{bad!r} 应回落 launcher")
        self.assertEqual(Settings.from_dict({"shortcut_mode": "DESKTOP"}).shortcut_mode, "desktop")

    def test_category_exclude_normalized(self) -> None:
        s = Settings.from_dict({"category_exclude": [" 快捷方式 ", "快捷方式", "z全部", "", 5]})
        self.assertEqual(s.category_exclude, ["快捷方式", "z全部"], "应去空白 + 大小写不敏感去重")

    def test_category_exclude_non_list_uses_default(self) -> None:
        self.assertEqual(Settings.from_dict({"category_exclude": "快捷方式"}).category_exclude,
                         ["快捷方式", "桌面", "z全部"])

    def test_category_exclude_explicit_empty_list_is_kept(self) -> None:
        """显式 [] 是用户"什么都要登记"的决定，不能被默认值覆盖。"""
        self.assertEqual(Settings.from_dict({"category_exclude": []}).category_exclude, [])

    def test_bool_strings_are_parsed(self) -> None:
        s = Settings.from_dict({"strip_extensions": "false", "strip_shortcut_overlay": "0"})
        self.assertFalse(s.strip_extensions)
        self.assertFalse(s.strip_shortcut_overlay)
        s2 = Settings.from_dict({"strip_extensions": "true"})
        self.assertTrue(s2.strip_extensions)

    def test_data_root_whitespace_and_types(self) -> None:
        self.assertEqual(Settings.from_dict({"data_root": "  E:\\x  "}).data_root, "E:\\x")
        self.assertEqual(Settings.from_dict({"data_root": 123}).data_root, "")
        self.assertEqual(Settings.from_dict({"data_root": None}).data_root, "")

    def test_is_dir_excluded_case_insensitive(self) -> None:
        s = Settings()
        self.assertTrue(s.is_dir_excluded("快捷方式"))
        self.assertTrue(s.is_dir_excluded("Z全部"), "大小写不敏感（Windows 路径语义）")
        self.assertFalse(s.is_dir_excluded("游戏加速"))

    def test_roundtrip_through_config_file(self) -> None:
        tmp = tempfile.TemporaryDirectory(prefix="dbx_cfg14_")
        self.addCleanup(tmp.cleanup)
        path = Path(tmp.name) / ".config.json"
        cfg = AppConfig(config_path=path)
        cfg.app.data_root = "E:\\快捷方式SAO"
        cfg.app.shortcut_mode = "desktop"
        cfg.app.strip_extensions = False
        cfg.app.strip_shortcut_overlay = False
        cfg.app.category_exclude = ["桌面"]
        cfg.save()
        again = AppConfig.load(path)
        self.assertEqual(again.app.data_root, "E:\\快捷方式SAO")
        self.assertEqual(again.app.shortcut_mode, "desktop")
        self.assertFalse(again.app.strip_extensions)
        self.assertFalse(again.app.strip_shortcut_overlay)
        self.assertEqual(again.app.category_exclude, ["桌面"])


class CategoryConfigTests(unittest.TestCase):
    def test_default_skin_fields_complete(self) -> None:
        d = CategoryConfig().to_dict()
        for name in SKIN_FIELDS:
            self.assertIn(name, d, f"每分类外观字段缺失: {name}")
        self.assertEqual(d["radius"], 12)
        self.assertEqual(d["bg_color"], "#F5F6F8")
        self.assertEqual(d["opacity"], 88)
        self.assertEqual(d["position_mode"], "anchor")
        self.assertEqual(d["panel_w"], 420)
        self.assertEqual(d["panel_h"], 300)
        self.assertEqual(d["icon_px"], 64)

    def test_from_dict_clamps(self) -> None:
        cat = CategoryConfig.from_dict(
            {
                "dir": "d",
                "radius": 999,
                "opacity": 250,
                "icon_px": 10,
                "panel_w": 10,
                "panel_h": 10,
                "bg_color": "red",
                "position_mode": "weird",
                "label": "x" * 40,
            }
        )
        self.assertEqual(cat.radius, 24)
        self.assertEqual(cat.opacity, 100, "越界值应被 clamp 到上限")
        self.assertEqual(cat.icon_px, 48)
        self.assertEqual(cat.panel_w, 240)
        self.assertEqual(cat.panel_h, 160)
        self.assertEqual(cat.bg_color, "#F5F6F8")
        self.assertEqual(cat.position_mode, "anchor")
        self.assertEqual(len(cat.label), 20)

    def test_negative_radius_and_opacity(self) -> None:
        cat = CategoryConfig.from_dict({"dir": "d", "radius": -5, "opacity": -5})
        self.assertEqual(cat.radius, 0, "圆角下限 0")
        self.assertEqual(cat.opacity, 0, "透明度下限 0（0=只剩内容）")

    def test_sanitize_fixes_mutated_fields(self) -> None:
        cat = CategoryConfig(dir="d")
        cat.radius = 100
        cat.opacity = -3
        cat.bg_color = "blue"
        cat.position_mode = "diagonal"
        cat.label = ""
        cat.sanitize()
        self.assertEqual(cat.radius, 24)
        self.assertEqual(cat.opacity, 0)
        self.assertEqual(cat.bg_color, "#F5F6F8")
        self.assertEqual(cat.position_mode, "anchor")
        self.assertEqual(cat.label, "分类")

    def test_dir_path(self) -> None:
        cat = CategoryConfig(dir="工作")
        self.assertEqual(cat.dir_path(Path("C:/root")), Path("C:/root/工作"))


class AppConfigTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_cfg_")
        self.dir = Path(self._tmp.name)
        self.path = self.dir / ".config.json"

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def test_load_missing_returns_defaults(self) -> None:
        cfg = AppConfig.load(self.path)
        self.assertEqual(cfg.categories, [])
        self.assertEqual(cfg.app.drag_threshold_px, 4)
        self.assertEqual(cfg.config_path, self.path)

    def test_load_broken_returns_defaults_without_overwrite(self) -> None:
        self.path.write_text("{not json", encoding="utf-8")
        cfg = AppConfig.load(self.path)
        self.assertEqual(cfg.categories, [])
        self.assertEqual(self.path.read_text(encoding="utf-8"), "{not json")

    def test_load_non_dict_json(self) -> None:
        self.path.write_text("[1, 2, 3]", encoding="utf-8")
        cfg = AppConfig.load(self.path)
        self.assertEqual(cfg.categories, [])

    def test_roundtrip_preserves_per_category_skin(self) -> None:
        cat_a = CategoryConfig(
            id="cat_aaaaaaaa", dir="A", label="A", icon_px=96, radius=4,
            bg_color="#112233", opacity=30, panel_w=500, panel_h=400, position_mode="center",
        )
        cat_b = CategoryConfig(
            id="cat_bbbbbbbb", dir="B", label="B", icon_px=48, radius=24,
            bg_color="#FFFFFF", opacity=100, panel_w=240, panel_h=160, position_mode="anchor",
        )
        cfg = AppConfig(app=Settings(), categories=[cat_a, cat_b], config_path=self.path)
        cfg.save()

        loaded = AppConfig.load(self.path)
        self.assertEqual(len(loaded.categories), 2)
        a, b = loaded.categories
        self.assertEqual((a.icon_px, a.radius, a.bg_color, a.opacity, a.panel_w, a.position_mode),
                         (96, 4, "#112233", 30, 500, "center"))
        self.assertEqual((b.icon_px, b.radius, b.bg_color, b.opacity, b.panel_w, b.position_mode),
                         (48, 24, "#FFFFFF", 100, 240, "anchor"))

    def test_editing_one_category_does_not_affect_other(self) -> None:
        cfg = AppConfig(
            categories=[CategoryConfig(id="cat_1", dir="A"), CategoryConfig(id="cat_2", dir="B")],
            config_path=self.path,
        )
        cfg.save()
        loaded = AppConfig.load(self.path)
        loaded.find("cat_1").radius = 0
        loaded.find("cat_1").opacity = 0
        loaded.find("cat_1").bg_color = "#000000"
        loaded.save()
        again = AppConfig.load(self.path)
        cat2 = again.find("cat_2")
        self.assertEqual(cat2.radius, 12)
        self.assertEqual(cat2.opacity, 88)
        self.assertEqual(cat2.bg_color, "#F5F6F8")

    def test_legacy_global_skin_migrates_to_all_categories(self) -> None:
        legacy = {
            "version": 1,
            "app": {
                "radius": 6,
                "bg_color": "#1F1F23",
                "opacity": 55,
                "position_mode": "center",
                "drag_threshold_px": 4,
            },
            "categories": [{"dir": "A", "label": "A"}, {"dir": "B", "label": "B"}],
        }
        self.path.write_text(json.dumps(legacy), encoding="utf-8")
        cfg = AppConfig.load(self.path)
        self.assertEqual(len(cfg.categories), 2)
        for cat in cfg.categories:
            self.assertEqual(cat.radius, 6)
            self.assertEqual(cat.bg_color, "#1F1F23")
            self.assertEqual(cat.opacity, 55)
            self.assertEqual(cat.position_mode, "center")
        # 迁移后 app 段不再保留外观字段
        cfg.save()
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        for name in APPEARANCE_ONLY_FIELDS:
            self.assertNotIn(name, raw["app"], f"迁移后 app 段仍残留外观字段 {name}")

    def test_legacy_skin_does_not_override_category_own_values(self) -> None:
        """回归用例 B（架构师复核提出）：迁移只填充"缺失该字段"的分类。

        分类自己存的 radius=0 / opacity=0 必须赢，不能被旧全局值 24/50 覆盖。
        """
        data = {
            "version": 1,
            "app": {"radius": 24, "opacity": 50, "bg_color": "#123456", "position_mode": "center"},
            "categories": [
                {"dir": "A", "radius": 0, "opacity": 0},   # 自带皮肤值
                {"dir": "B"},                              # 缺字段 → 拿迁移值
            ],
        }
        cfg = AppConfig.from_dict(data)
        self.assertEqual(len(cfg.categories), 2)
        a = cfg.find_by_dir("A")
        b = cfg.find_by_dir("B")
        self.assertEqual((a.radius, a.opacity), (0, 0),
                         "分类自带的 radius/opacity 被旧全局值覆盖了（缺陷未修好）")
        # A 没写的字段仍应被迁移填充
        self.assertEqual((a.bg_color, a.position_mode), ("#123456", "center"))
        # B 全缺 → 全部拿迁移值
        self.assertEqual((b.radius, b.opacity, b.bg_color, b.position_mode),
                         (24, 50, "#123456", "center"))

    def test_legacy_skin_only_fills_missing_fields_per_field(self) -> None:
        """逐字段判定：只写了 radius 的分类，其他字段仍要迁移。"""
        data = {
            "version": 1,
            "app": {"radius": 24, "opacity": 50, "bg_color": "#123456"},
            "categories": [{"dir": "C", "radius": 6}],
        }
        cat = AppConfig.from_dict(data).find_by_dir("C")
        self.assertEqual(cat.radius, 6, "分类自带的 radius 应保留")
        self.assertEqual(cat.opacity, 50, "缺失的 opacity 应被迁移填充")
        self.assertEqual(cat.bg_color, "#123456", "缺失的 bg_color 应被迁移填充")

    def test_duplicate_ids_are_regenerated(self) -> None:
        data = {
            "version": 1,
            "categories": [
                {"id": "cat_dup", "dir": "A"},
                {"id": "cat_dup", "dir": "B"},
            ],
        }
        cfg = AppConfig.from_dict(data)
        self.assertEqual(len({c.id for c in cfg.categories}), 2)

    def test_category_without_dir_is_skipped(self) -> None:
        cfg = AppConfig.from_dict({"categories": [{"id": "cat_x", "dir": ""}, {"dir": "ok"}]})
        self.assertEqual([c.dir for c in cfg.categories], ["ok"])

    def test_add_category_uses_default_icon_px(self) -> None:
        cfg = AppConfig(app=Settings(icon_px_default=80), config_path=self.path)
        c1 = cfg.add_category("A", "A")
        c2 = cfg.add_category("B", "B")
        self.assertEqual(c1.icon_px, 80)
        self.assertEqual(c1.label, "A")
        self.assertNotEqual(c1.id, c2.id)

    def test_desktop_coordinates_are_gone(self) -> None:
        """桌面图标位置由 Windows 管理：配置里不再有 x / y。"""
        cat = CategoryConfig(dir="A", label="A")
        self.assertFalse(hasattr(cat, "x"))
        self.assertFalse(hasattr(cat, "y"))
        self.assertNotIn("x", cat.to_dict())
        self.assertNotIn("y", cat.to_dict())

    def test_legacy_x_y_are_ignored_on_load(self) -> None:
        data = {
            "version": 1,
            "categories": [{"id": "cat_old", "dir": "A", "label": "A", "x": 500, "y": 600,
                            "visible": False}],
        }
        cfg = AppConfig.from_dict(data)
        cat = cfg.find("cat_old")
        self.assertIsNotNone(cat)
        self.assertFalse(cat.visible, "visible 仍要正确读入（现在表示是否保留桌面 .lnk）")
        self.assertNotIn("x", cat.to_dict())

    def test_remove_category_keeps_others(self) -> None:
        cfg = AppConfig(
            categories=[CategoryConfig(id="cat_1", dir="A"), CategoryConfig(id="cat_2", dir="B")]
        )
        cfg.remove_category("cat_1")
        self.assertEqual([c.dir for c in cfg.categories], ["B"])

    def test_save_without_path_is_noop(self) -> None:
        AppConfig().save()  # 不应抛异常

    def test_atomic_write_json_leaves_no_tmp(self) -> None:
        target = self.dir / "x.json"
        atomic_write_json(target, {"a": 1})
        self.assertEqual(json.loads(target.read_text(encoding="utf-8")), {"a": 1})
        self.assertEqual([p.name for p in self.dir.iterdir() if ".tmp." in p.name], [])

    def test_iso_utc_now_is_parseable(self) -> None:
        from datetime import datetime

        text = iso_utc_now()
        parsed = datetime.fromisoformat(text)
        self.assertIsNotNone(parsed.tzinfo)


if __name__ == "__main__":
    unittest.main(verbosity=2)
