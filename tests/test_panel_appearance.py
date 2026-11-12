"""v1.5 外观单测：毛玻璃底图 / 缩放缓入 / 悬停高亮 / 材质配置。

全部在 offscreen 平台下跑（``tests.qt_support.qt_app()``），不需要真实桌面：

| 覆盖点 | 断言方式 |
|---|---|
| 缩放不动点 | :func:`scaled_rect` 纯函数数学断言（含锚点在矩形外时的 clamp） |
| 截屏失败回退 | offscreen 下 ``grabWindow`` 取不到 → ``has_blur`` False，绘图层不抛异常 |
| 全黑截图判定 | :func:`image_looks_blank` 对纯黑/彩色/空图分别断言 |
| 淡入 | ``prepare_show`` 后 ``fade == 0``；``animate_show`` 后 140ms 内到 1.0 |
| 几何动画 | 起点 = 目标矩形的 94%，终点 == 目标矩形 |
| 悬停 | 进度 0→1 缓动；``hover_icon_scale`` 1.0 → 1.08；渲染像素对比确有变化 |
| 材质配置 | 缺字段默认 blur、非法值回落 blur、roundtrip、sanitize |
| opacity=0 | 连模糊底图一起不画（保持"只剩内容"的既有语义） |
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QPointF, QRect  # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap  # noqa: E402

from app.config import (  # noqa: E402
    MATERIAL_BLUR,
    MATERIAL_SOLID,
    CategoryConfig,
    normalize_material,
)
from app.folder_model import Entry  # noqa: E402
from app.panel_model import PanelModel  # noqa: E402
from app.windows.panel_grid import HOVER_SCALE  # noqa: E402
from app.windows.panel_window import (  # noqa: E402
    FADE_IN_MS,
    SCALE_START,
    PanelWindow,
    image_looks_blank,
    scaled_rect,
)
from tests.qt_support import qt_app  # noqa: E402


def pump(seconds: float) -> None:
    """跑事件循环若干秒（动画/定时器需要真实时间推进）。"""
    app = qt_app()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


class StubIcons:
    """最小 IconCache 替身：给任意路径返回一张纯色 pixmap。"""

    def pixmap(self, path, px: int) -> QPixmap:
        pm = QPixmap(int(px), int(px))
        pm.fill(QColor("#4C8DFF"))
        return pm

    def default_icon(self, px: int) -> QPixmap:
        return self.pixmap("", px)

    def invalidate_dir(self, directory) -> None:   # noqa: ANN001 - 接口对齐
        return None


def make_cat(**kwargs) -> CategoryConfig:
    base = dict(id="cat_test", dir="测试", label="测试", panel_w=320, panel_h=240)
    base.update(kwargs)
    return CategoryConfig(**base)


def make_entries(n: int = 3) -> list[Entry]:
    return [
        Entry(name=f"item{i}.lnk", path=Path(rf"C:\nonexistent\item{i}.lnk"),
              kind="link", norm=f"item{i}.lnk")
        for i in range(n)
    ]


class ScaledRectTests(unittest.TestCase):
    """缩放不动点：锚点侧展开，而不是从屏幕中心展开。"""

    def test_scale_around_center(self) -> None:
        rect = QRect(100, 100, 200, 100)
        out = scaled_rect(rect, QPointF(200, 150), SCALE_START)
        self.assertEqual(out.size().width(), round(200 * SCALE_START))
        self.assertEqual(out.size().height(), round(100 * SCALE_START))
        self.assertEqual(out.center(), rect.center())     # 以中心为基准 → 中心不动

    def test_scale_around_corner(self) -> None:
        """锚点在左上角 → 左上角不动，面板向右下展开。"""
        rect = QRect(100, 100, 200, 100)
        out = scaled_rect(rect, QPointF(100, 100), SCALE_START)
        self.assertEqual((out.left(), out.top()), (100, 100))
        self.assertLess(out.width(), rect.width())

    def test_anchor_outside_rect_clamps_to_edge(self) -> None:
        """图标在面板左侧 → 不动点被 clamp 到左边中点，面板向右展开。"""
        cat = make_cat(position_mode="anchor")
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.setGeometry(QRect(500, 300, 300, 200))
            pw._final_geom = QRect(pw.geometry())      # noqa: SLF001 - 直接构造前置状态
            origin = pw._scale_origin(QRect(400, 400, 1, 1))   # noqa: SLF001
            self.assertEqual(origin.x(), 500)          # clamp 到左边界
            self.assertEqual(origin.y(), 400)          # 锚点在矩形内的高度
            start = scaled_rect(pw._final_geom, origin, SCALE_START)  # noqa: SLF001
            self.assertEqual(start.left(), 500)
            self.assertGreater(start.right(), 500)     # 向右变宽（而不是向左）
        finally:
            pw.close()
            pw.deleteLater()


class BlankImageTests(unittest.TestCase):
    def test_black_image_is_blank(self) -> None:
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0))
        self.assertTrue(image_looks_blank(img))

    def test_transparent_image_is_blank(self) -> None:
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 0))
        self.assertTrue(image_looks_blank(img))

    def test_colored_image_is_not_blank(self) -> None:
        img = QImage(64, 64, QImage.Format.Format_ARGB32)
        img.fill(QColor("#3A6EA5"))
        self.assertFalse(image_looks_blank(img))

    def test_null_image_is_blank(self) -> None:
        self.assertTrue(image_looks_blank(QImage()))
        self.assertTrue(image_looks_blank(None))


class MaterialConfigTests(unittest.TestCase):
    """材质字段：向后兼容（v1.4 配置没有该字段 → 默认毛玻璃）。"""

    def test_default_is_blur(self) -> None:
        cat = CategoryConfig.from_dict({"id": "cat_a", "dir": "a"})
        self.assertEqual(cat.material, MATERIAL_BLUR)

    def test_solid_roundtrip(self) -> None:
        cat = CategoryConfig.from_dict({"id": "cat_a", "dir": "a", "material": "solid"})
        self.assertEqual(cat.material, MATERIAL_SOLID)
        self.assertEqual(CategoryConfig.from_dict(cat.to_dict()).material, MATERIAL_SOLID)

    def test_invalid_falls_back_to_blur(self) -> None:
        for bad in ("", None, "acrylic", 123, "  "):
            self.assertEqual(normalize_material(bad), MATERIAL_BLUR)
        self.assertEqual(normalize_material("SOLID"), MATERIAL_SOLID)   # 大小写不敏感

    def test_sanitize_clamps_material(self) -> None:
        cat = make_cat(material="weird")
        cat.sanitize()
        self.assertEqual(cat.material, MATERIAL_BLUR)


class BlurBackdropTests(unittest.TestCase):
    @staticmethod
    def _patch_grab(return_value):
        """把 QScreen.grabWindow 换成受控替身（回退路径必须与环境无关）。"""
        from unittest.mock import patch

        return patch.object(
            type(qt_app().primaryScreen()), "grabWindow", return_value=return_value
        )

    def test_capture_failure_falls_back_without_raising(self) -> None:
        """grabWindow 返回空 pixmap → 必须静默回退纯色，且不抛异常。"""
        cat = make_cat(material=MATERIAL_BLUR, opacity=88)
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.setGeometry(QRect(100, 100, 320, 240))
            with self._patch_grab(QPixmap()):
                pw.prepare_show(QRect(80, 120, 1, 1))
            self.assertFalse(pw.has_blur)        # 截屏失败 → 回退
            self.assertGreaterEqual(pw.blur_cost_ms, 0.0)
            pw.show()
            pump(0.05)
            pw.repaint()                         # 纯色回退路径不炸
        finally:
            pw.close()
            pw.deleteLater()

    def test_all_black_capture_falls_back(self) -> None:
        """整块截图全黑（锁屏 / 远程桌面常见）→ 也必须回退，不能变黑玻璃。"""
        black = QPixmap(320, 240)
        black.fill(QColor(0, 0, 0))
        cat = make_cat(material=MATERIAL_BLUR, opacity=88)
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.setGeometry(QRect(100, 100, 320, 240))
            with self._patch_grab(black):
                pw.prepare_show(QRect(80, 120, 1, 1))
            self.assertFalse(pw.has_blur)
        finally:
            pw.close()
            pw.deleteLater()

    def test_valid_capture_is_used(self) -> None:
        """正常截到内容 → 使用底图（毛玻璃路径），且不抛异常。"""
        content = QPixmap(320, 240)
        content.fill(QColor("#3A6EA5"))
        cat = make_cat(material=MATERIAL_BLUR, opacity=88)
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.setGeometry(QRect(100, 100, 320, 240))
            with self._patch_grab(content):
                pw.prepare_show(QRect(80, 120, 1, 1))
            self.assertTrue(pw.has_blur)
            pw.show()
            pump(0.05)
            pw.repaint()                         # 毛玻璃绘制路径不炸
        finally:
            pw.close()
            pw.deleteLater()

    def test_solid_material_never_captures(self) -> None:
        qt_app()
        cat = make_cat(material=MATERIAL_SOLID)
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.prepare_show(QRect(100, 100, 1, 1))
            self.assertFalse(pw.has_blur)
            self.assertEqual(pw.blur_cost_ms, 0.0)   # 压根没尝试截屏
        finally:
            pw.close()
            pw.deleteLater()

    def test_opacity_zero_disables_blur(self) -> None:
        """opacity=0 的语义是"只剩内容"：模糊底也属于背景，必须一起不画。"""
        qt_app()
        pw = PanelWindow(make_cat(material=MATERIAL_BLUR, opacity=0), 48, StubIcons())
        try:
            self.assertFalse(pw._use_blur())      # noqa: SLF001
        finally:
            pw.close()
            pw.deleteLater()

    def test_opacity_zero_paints_nothing(self) -> None:
        """背景全透明时 paintEvent 直接返回：面板像素保持全透明（子控件照画）。"""
        qt_app()
        pw = PanelWindow(make_cat(material=MATERIAL_SOLID, opacity=0), 48, StubIcons())
        try:
            pw.setGeometry(QRect(10, 10, 120, 90))
            pw.show()
            pump(0.05)
            img = pw.grab().toImage()
            # 取一个远离子控件的角落：alpha 必须是 0（没有背景填充）
            self.assertEqual(QColor.fromRgba(img.pixel(img.width() - 2, 2)).alpha(), 0)
        finally:
            pw.close()
            pw.deleteLater()


class FadeAnimationTests(unittest.TestCase):
    def test_prepare_show_sets_zero_fade_and_scaled_geometry(self) -> None:
        qt_app()
        cat = make_cat(position_mode="center", material=MATERIAL_SOLID)
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.setGeometry(QRect(200, 200, 300, 200))
            target = QRect(pw.geometry())
            pw.prepare_show(QRect(target.center().x(), target.center().y(), 1, 1))
            self.assertEqual(pw.fade, 0.0)
            self.assertEqual(pw.width(), round(target.width() * SCALE_START))
            self.assertEqual(pw.width(), pw._anim_start_geom.width())   # noqa: SLF001
            self.assertEqual(pw._final_geom, target)                   # noqa: SLF001
        finally:
            pw.close()
            pw.deleteLater()

    def test_animate_show_reaches_full_fade_and_final_geometry(self) -> None:
        qt_app()
        cat = make_cat(position_mode="center", material=MATERIAL_SOLID)
        pw = PanelWindow(cat, 48, StubIcons())
        try:
            pw.setGeometry(QRect(200, 200, 300, 200))
            target = QRect(pw.geometry())
            pw.prepare_show(QRect(target.center().x(), target.center().y(), 1, 1))
            pw.show()
            pw.animate_show()
            pump(FADE_IN_MS / 1000.0 + 0.25)
            self.assertAlmostEqual(pw.fade, 1.0, places=3)
            self.assertEqual(pw.geometry(), target)
        finally:
            pw.close()
            pw.deleteLater()

    def test_show_without_prepare_paints_immediately(self) -> None:
        """没走 prepare_show（例如单测/探针直接 show）时不能"看不见面板"。"""
        qt_app()
        pw = PanelWindow(make_cat(material=MATERIAL_SOLID), 48, StubIcons())
        try:
            pw.show()
            pump(0.05)
            self.assertEqual(pw.fade, 1.0)
        finally:
            pw.close()
            pw.deleteLater()


class HoverTests(unittest.TestCase):
    def _make_panel(self) -> PanelWindow:
        qt_app()
        pw = PanelWindow(make_cat(bg_color="#F5F6F8"), 48, StubIcons())
        model = PanelModel()
        model.build(make_entries(3), StubIcons(), 48)
        pw.set_model(model)
        pw.setGeometry(QRect(50, 50, 320, 240))
        pw.show()
        pump(0.1)
        return pw

    def test_icon_scale_curve(self) -> None:
        pw = self._make_panel()
        try:
            delegate = pw.grid.delegate
            self.assertAlmostEqual(delegate.hover_icon_scale(0.0), 1.0)
            self.assertAlmostEqual(delegate.hover_icon_scale(1.0), HOVER_SCALE)
            self.assertAlmostEqual(delegate.hover_icon_scale(0.5), (1.0 + HOVER_SCALE) / 2)
            self.assertAlmostEqual(HOVER_SCALE, 1.08)
        finally:
            pw.close()
            pw.deleteLater()

    def test_hover_progress_eases_to_one(self) -> None:
        pw = self._make_panel()
        try:
            grid = pw.grid
            self.assertEqual(grid.hover_progress(0), 0.0)
            grid.set_hover_row(0)
            self.assertTrue(grid._hover_timer.isActive())     # noqa: SLF001
            pump(0.05)
            mid = grid.hover_progress(0)
            self.assertTrue(0.0 < mid < 1.0, f"中点应在缓动过程中，实测 {mid}")
            pump(0.3)
            self.assertEqual(grid.hover_progress(0), 1.0)
            self.assertFalse(grid._hover_timer.isActive())    # 到位后停表，不空转
        finally:
            pw.close()
            pw.deleteLater()

    def test_hover_leaves_and_resets(self) -> None:
        pw = self._make_panel()
        try:
            grid = pw.grid
            grid.set_hover_row(1, immediate=True)
            self.assertEqual(grid.hover_progress(1), 1.0)
            grid.set_hover_row(-1, immediate=True)
            self.assertEqual(grid.hover_progress(1), 0.0)
        finally:
            pw.close()
            pw.deleteLater()

    def test_hover_text_color_follows_background(self) -> None:
        """浅底 → 悬停加深（#000000）；深底 → #FFFFFF。"""
        qt_app()
        light = PanelWindow(make_cat(bg_color="#F5F6F8"), 48, StubIcons())
        dark = PanelWindow(make_cat(id="cat_dark", bg_color="#1F1F23"), 48, StubIcons())
        try:
            self.assertEqual(light.grid.hover_text_color.name(), "#000000")
            self.assertEqual(dark.grid.hover_text_color.name(), "#ffffff")
        finally:
            for w in (light, dark):
                w.close()
                w.deleteLater()

    def test_hover_changes_rendered_pixels(self) -> None:
        """渲染级证据：悬停行的像素相对常态确有变化（图标放大 + 文字变色）。"""
        pw = self._make_panel()
        try:
            grid = pw.grid
            rect = grid.visualRect(grid.model().index(0, 0))
            self.assertTrue(rect.isValid() and rect.width() > 10)

            grid.set_hover_row(-1, immediate=True)
            pump(0.05)
            normal = grid.viewport().grab(rect).toImage()

            grid.set_hover_row(0, immediate=True)
            pump(0.05)
            hovered = grid.viewport().grab(rect).toImage()

            diff = 0
            for y in range(normal.height()):
                for x in range(normal.width()):
                    if normal.pixel(x, y) != hovered.pixel(x, y):
                        diff += 1
            self.assertGreater(diff, 50, f"悬停后变动的像素数过少: {diff}")
        finally:
            pw.close()
            pw.deleteLater()

    def test_hover_does_not_break_edit_mode(self) -> None:
        """悬停与抖动共存：编辑态里抖动优先，且不改变顺序/选中逻辑。"""
        pw = self._make_panel()
        try:
            grid = pw.grid
            grid.set_hover_row(0, immediate=True)
            grid.enter_edit_mode(select_row=0)
            self.assertTrue(grid.edit_mode)
            self.assertTrue(grid._jiggle_timer.isActive())    # noqa: SLF001
            self.assertEqual(len(grid.selectionModel().selectedIndexes()), 1)
            grid.viewport().repaint()                          # 抖动 + 悬停同时绘制不炸
            grid.exit_edit_mode()
            self.assertFalse(grid.edit_mode)
        finally:
            pw.close()
            pw.deleteLater()


if __name__ == "__main__":
    unittest.main()
