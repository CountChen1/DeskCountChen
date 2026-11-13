"""【P2-5】全局设置：把单个分类的皮肤应用到全部分类。

覆盖两条红线：
1. **每个分类的独立设置必须完整保留** —— 只复制 :data:`SKIN_FIELDS`
   （面板宽高 / 圆角 / 背景色 / 透明度 / 弹出位置 / 材质），
   ``label`` / ``image`` / ``icon_px`` / ``visible`` / ``dir`` 一律不动；
2. **立即落盘** —— 点按钮后配置文件里就应该是新值（不等 400ms 去抖）。

现场保护：``app.paths`` 整体重定向到临时沙箱（``tests/sandbox.py``），
真实 ``%USERPROFILE%\\DeskCountChen`` 只读一次取 sha256 指纹，结束时断言零改动。
"""

from __future__ import annotations

import json
import shutil
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前完成重定向

from app import paths  # noqa: E402
from app.config import AppConfig, CategoryConfig  # noqa: E402
from app.controller import SKIN_FIELDS, AppController  # noqa: E402
from app.windows.settings_dialog import APPLY_ALL_FEEDBACK_MS, SettingsDialog  # noqa: E402
from tests.qt_support import qt_app  # noqa: E402

CATS = (
    # id,  label, 半径, 背景色,     透明度, 位置,      材质,  图标尺寸
    ("cat_a", "工作", 12, "#F5F6F8", 88, "anchor", "blur", 64),
    ("cat_b", "娱乐", 4, "#1F1F23", 40, "center", "solid", 96),
    ("cat_c", "游戏", 20, "#223344", 100, "anchor", "solid", 48),
)


def _real_state():
    return sandbox.snapshot_real()


def _seed_config(root: Path) -> None:
    """在沙箱里造 3 个分类（皮肤各不相同，用于验证"各自独立"）。"""
    categories = []
    for cat_id, label, radius, bg, opacity, mode, material, icon_px in CATS:
        (root / "data" / label).mkdir(parents=True, exist_ok=True)
        categories.append({
            "id": cat_id,
            "dir": label,
            "label": label,
            "image": f"C:/icons/{cat_id}.png",
            "icon_px": icon_px,
            "visible": cat_id != "cat_c",
            "panel_w": 300 + icon_px,
            "panel_h": 200 + icon_px,
            "radius": radius,
            "bg_color": bg,
            "opacity": opacity,
            "position_mode": mode,
            "material": material,
        })
    paths.CONFIG_PATH.write_text(
        json.dumps({"version": 1, "app": {}, "categories": categories},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )


class ApplyAllCase(unittest.TestCase):
    """公共装置：沙箱 + 一个加载到沙箱配置的控制器。"""

    @classmethod
    def setUpClass(cls) -> None:
        cls.real = _real_state()
        cls.root = sandbox.sandbox_paths(prefix="dbx_apply_all_")
        paths.ensure_base_dirs()
        _seed_config(cls.root)

    @classmethod
    def tearDownClass(cls) -> None:
        shutil.rmtree(cls.root, ignore_errors=True)
        ok, detail = sandbox.guard_real_untouched(cls.real)
        assert ok, f"测试污染了真实用户配置: {detail}"

    def setUp(self) -> None:
        qt_app()
        _seed_config(self.root)          # 每个用例都从同一份夹具出发
        self.controller = AppController(qt_app())
        self.cfg: AppConfig = self.controller.cfg
        self.assertEqual(len(self.cfg.categories), 3)

    def tearDown(self) -> None:
        self.controller.close_panel()
        self.controller._save_timer.stop()      # noqa: SLF001 - 别把定时器带出用例
        self.controller.deleteLater()

    def cat(self, cat_id: str) -> CategoryConfig:
        found = self.cfg.find(cat_id)
        assert found is not None
        return found


class ApplyToAllCategoriesTests(ApplyAllCase):
    def test_skin_fields_are_copied_to_every_category(self) -> None:
        source = self.cat("cat_a")
        count = self.controller.apply_to_all_categories("cat_a")
        self.assertEqual(count, 3)            # 3 个分类（含源分类本身）
        for other in self.cfg.categories:
            for name in SKIN_FIELDS:
                self.assertEqual(
                    getattr(other, name), getattr(source, name),
                    f"{other.id}.{name} 未同步",
                )

    def test_identity_fields_are_untouched(self) -> None:
        """label / image / icon_px / visible / dir 属于身份与数据域，绝不能复制。"""
        before = {
            c.id: (c.label, c.image, c.icon_px, c.visible, c.dir)
            for c in self.cfg.categories
        }
        self.controller.apply_to_all_categories("cat_b")
        after = {
            c.id: (c.label, c.image, c.icon_px, c.visible, c.dir)
            for c in self.cfg.categories
        }
        self.assertEqual(before, after)

    def test_unknown_source_changes_nothing(self) -> None:
        before = [c.to_dict() for c in self.cfg.categories]
        self.assertEqual(self.controller.apply_to_all_categories("cat_missing"), 0)
        self.assertEqual(before, [c.to_dict() for c in self.cfg.categories])

    def test_values_are_clamped_per_category(self) -> None:
        """源分类的非法值经 sanitize 后不会把别的分类写成越界值。"""
        source = self.cat("cat_a")
        source.radius = 999
        source.opacity = -5
        self.controller.apply_to_all_categories("cat_a")
        for cat in self.cfg.categories:
            self.assertLessEqual(cat.radius, 24)
            self.assertGreaterEqual(cat.opacity, 0)

    def test_apply_from_dialog_persists_immediately(self) -> None:
        """「应用到全部分类」必须立即落盘（不是等 400ms 去抖）。"""
        source = self.cat("cat_a")
        source.radius = 18
        source.material = "solid"
        self.controller._apply_all_from_dialog("cat_a")     # noqa: SLF001

        raw = json.loads(paths.CONFIG_PATH.read_text(encoding="utf-8"))
        by_id = {c["id"]: c for c in raw["categories"]}
        for cat_id in ("cat_b", "cat_c"):
            self.assertEqual(by_id[cat_id]["radius"], 18)
            self.assertEqual(by_id[cat_id]["material"], "solid")
        # 身份字段仍各自独立
        self.assertEqual(by_id["cat_b"]["label"], "娱乐")
        self.assertEqual(by_id["cat_b"]["icon_px"], 96)

    def test_apply_from_dialog_closes_open_panel(self) -> None:
        """面板外观在构造期定型，无法热更新 → 应用后关掉已打开的面板。"""
        self.controller.open_panel("cat_a")
        self.assertIsNotNone(self.controller.panel)
        self.controller._apply_all_from_dialog("cat_a")     # noqa: SLF001
        self.assertIsNone(self.controller.panel)


class SettingsDialogApplyAllTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()
        self.cat = CategoryConfig(
            id="cat_dlg", dir="对话框", label="对话框", radius=8,
            bg_color="#101820", opacity=70, position_mode="anchor", material="blur",
        )

    def test_button_disabled_without_handler(self) -> None:
        dlg = SettingsDialog(self.cat)
        try:
            dlg.show()
            qt_app().processEvents()
            self.assertFalse(dlg.btn_apply_all.isEnabled())
            self.assertEqual(dlg.btn_apply_all.text(), "应用到全部分类")
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_button_reports_count_and_restores_text(self) -> None:
        dlg = SettingsDialog(self.cat)
        calls: list[str] = []

        def handler(cat_id: str) -> int:
            calls.append(cat_id)
            return 6

        dlg.apply_all_handler = handler
        try:
            dlg.show()
            qt_app().processEvents()
            self.assertTrue(dlg.btn_apply_all.isEnabled())
            dlg.btn_apply_all.click()
            self.assertEqual(calls, ["cat_dlg"])
            self.assertEqual(dlg.btn_apply_all.text(), "已应用到 6 个分类")
            self.assertEqual(dlg._apply_all_timer.interval(), APPLY_ALL_FEEDBACK_MS)  # noqa: SLF001
            self.assertTrue(dlg._apply_all_timer.isActive())                          # noqa: SLF001
            dlg._reset_apply_all_text()      # noqa: SLF001 - 不等 2 秒
            self.assertEqual(dlg.btn_apply_all.text(), "应用到全部分类")
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_apply_all_uses_current_form_values(self) -> None:
        """点「应用到全部分类」前必须先把界面值写回源分类（否则复制的是旧值）。"""
        dlg = SettingsDialog(self.cat)
        seen: list[tuple[int, str]] = []

        def handler(cat_id: str) -> int:
            seen.append((self.cat.radius, self.cat.material))
            return 1

        dlg.apply_all_handler = handler
        try:
            dlg.show()                               # showEvent 才会同步按钮可用性
            qt_app().processEvents()
            dlg.sp_radius.setValue(21)
            dlg.cmb_material.setCurrentIndex(1)      # 纯色
            dlg.sl_opacity.setValue(10)
            dlg.btn_apply_all.click()
            self.assertEqual(seen, [(21, "solid")])
            self.assertEqual(self.cat.opacity, 10)
        finally:
            dlg.close()
            dlg.deleteLater()

    def test_ok_still_writes_single_category(self) -> None:
        """原有"只作用于当前分类"的路径不能改坏。"""
        dlg = SettingsDialog(self.cat)
        other = CategoryConfig(id="cat_other", dir="别的", label="别的", radius=3)
        try:
            dlg.sp_radius.setValue(16)
            dlg._on_accept()                          # noqa: SLF001 - 等价于点 OK
            self.assertEqual(self.cat.radius, 16)
            self.assertEqual(other.radius, 3)          # 未受影响
        finally:
            dlg.deleteLater()

    def test_material_combo_reflects_config(self) -> None:
        solid = CategoryConfig(id="cat_s", dir="s", label="s", material="solid")
        dlg = SettingsDialog(solid)
        try:
            self.assertEqual(dlg.cmb_material.currentData(), "solid")
            dlg.cmb_material.setCurrentIndex(0)
            dlg._collect_into_cat()                    # noqa: SLF001
            self.assertEqual(solid.material, "blur")
        finally:
            dlg.deleteLater()


if __name__ == "__main__":
    unittest.main()
