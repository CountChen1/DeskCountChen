"""设置对话框：单个分类的皮肤设置（改动只影响该分类）。

可调项（均为分类独立外观）：
显示文字 / 图标尺寸 48–128 / 图标图片 / 面板宽高 / 圆角 0–24 /
背景色 / 透明度 0–100 / 弹出位置模式（贴合鼠标落点 / 屏幕居中）。

v1.2 说明：桌面分类图标已改为真实的 Windows 快捷方式（.lnk），它在桌面上
显示多大由 Windows（视图尺寸 + DPI）决定。"图标尺寸"只决定本分类 ICO 源图的
清晰度档位，不影响桌面图标的实际像素大小。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import Qt, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QColorDialog,
    QComboBox,
    QDialog,
    QDialogButtonBox,
    QFileDialog,
    QFormLayout,
    QHBoxLayout,
    QLineEdit,
    QPushButton,
    QSlider,
    QSpinBox,
    QVBoxLayout,
)

from app.config import (
    CategoryConfig,
    ICON_PX_MAX,
    ICON_PX_MIN,
    LABEL_MAX_CHARS,
    MIN_PANEL_H,
    MIN_PANEL_W,
    OPACITY_MAX,
    OPACITY_MIN,
    RADIUS_MAX,
    RADIUS_MIN,
)

# 注意：这里**不要**写 `from app.paths import ICONS_DIR`。那是导入期的值拷贝，
# 一旦 paths.set_data_root() / 测试沙箱重定向，模块里留着的仍是旧路径。
# 需要路径时统一 `from app import paths` 再 `paths.ICONS_DIR`（动态读取）。

logger = logging.getLogger(__name__)

IMAGE_FILTER = "图片 (*.png *.ico *.jpg *.jpeg *.bmp *.gif)"
INVALID_FILENAME_CHARS = '\\/:*?"<>|'


class SettingsDialog(QDialog):
    """单分类皮肤设置。OK 后直接写回 cat（sanitize 保证约束）。"""

    settings_applied = Signal()            # 任意设置已写回 cat
    category_renamed = Signal(str)         # cat_id（label 有变化时）
    open_folder_requested = Signal(str)    # cat_id（"打开此分类文件夹"按钮）

    def __init__(self, cat: CategoryConfig, parent=None) -> None:
        super().__init__(parent)
        self.cat = cat
        self._image_source = ""      # 用户新选的原图路径；""=不变
        self._image_cleared = False  # 用户清空图片=恢复默认
        self._bg_color = cat.bg_color
        self._old_label = cat.label
        self.setWindowTitle(f"分类设置 - {cat.label}")
        self.setMinimumWidth(360)
        self._build_ui()

    # -- UI ----------------------------------------------------------------

    def _build_ui(self) -> None:
        self.ed_label = QLineEdit(self.cat.label)
        self.ed_label.setMaxLength(LABEL_MAX_CHARS)

        self.sp_icon_px = QSpinBox()
        self.sp_icon_px.setRange(ICON_PX_MIN, ICON_PX_MAX)
        self.sp_icon_px.setValue(self.cat.icon_px)
        self.sp_icon_px.setSuffix(" px")
        self.sp_icon_px.setToolTip(
            "桌面图标的实际大小由 Windows 决定（视图尺寸 + DPI）；\n"
            "此值只决定本分类 ICO 源图的清晰度档位。"
        )

        self.ed_image = QLineEdit(self.cat.image)
        self.ed_image.setPlaceholderText("（空 = 使用内置默认图标）")
        btn_browse = QPushButton("浏览…")
        btn_browse.clicked.connect(self._on_browse_image)
        btn_clear = QPushButton("恢复默认")
        btn_clear.clicked.connect(self.ed_image.clear)
        image_row = QHBoxLayout()
        image_row.addWidget(self.ed_image, 1)
        image_row.addWidget(btn_browse)
        image_row.addWidget(btn_clear)

        self.sp_panel_w = QSpinBox()
        self.sp_panel_w.setRange(MIN_PANEL_W, 4000)
        self.sp_panel_w.setSingleStep(10)
        self.sp_panel_w.setValue(self.cat.panel_w)

        self.sp_panel_h = QSpinBox()
        self.sp_panel_h.setRange(MIN_PANEL_H, 3000)
        self.sp_panel_h.setSingleStep(10)
        self.sp_panel_h.setValue(self.cat.panel_h)

        self.sp_radius = QSpinBox()
        self.sp_radius.setRange(RADIUS_MIN, RADIUS_MAX)
        self.sp_radius.setValue(self.cat.radius)
        self.sp_radius.setSuffix(" px")

        self.btn_color = QPushButton(self._bg_color)
        self._update_color_button()
        self.btn_color.clicked.connect(self._on_pick_color)

        self.sl_opacity = QSlider()
        self.sl_opacity.setOrientation(Qt.Orientation.Horizontal)
        self.sl_opacity.setRange(OPACITY_MIN, OPACITY_MAX)
        self.sl_opacity.setValue(self.cat.opacity)
        self.lb_opacity = QLineEdit(str(self.cat.opacity))
        self.lb_opacity.setFixedWidth(48)
        self.sl_opacity.valueChanged.connect(lambda v: self.lb_opacity.setText(str(v)))
        opacity_row = QHBoxLayout()
        opacity_row.addWidget(self.sl_opacity, 1)
        opacity_row.addWidget(self.lb_opacity)

        self.cmb_position = QComboBox()
        self.cmb_position.addItem("贴合鼠标落点", "anchor")
        self.cmb_position.addItem("屏幕居中", "center")
        idx = 0 if self.cat.position_mode == "anchor" else 1
        self.cmb_position.setCurrentIndex(idx)

        form = QFormLayout()
        form.addRow("显示文字：", self.ed_label)
        form.addRow("图标尺寸：", self.sp_icon_px)
        form.addRow("图标图片：", image_row)
        form.addRow("面板宽度：", self.sp_panel_w)
        form.addRow("面板高度：", self.sp_panel_h)
        form.addRow("面板圆角：", self.sp_radius)
        form.addRow("背景色：", self.btn_color)
        form.addRow("透明度：", opacity_row)
        form.addRow("弹出位置：", self.cmb_position)

        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
        )
        buttons.accepted.connect(self._on_accept)
        buttons.rejected.connect(self.reject)

        btn_open_folder = QPushButton("打开此分类文件夹")
        btn_open_folder.setCursor(Qt.CursorShape.PointingHandCursor)
        btn_open_folder.clicked.connect(
            lambda: self.open_folder_requested.emit(self.cat.id)
        )

        layout = QVBoxLayout(self)
        layout.addLayout(form)
        layout.addWidget(btn_open_folder)
        layout.addWidget(buttons)

    def _update_color_button(self) -> None:
        self.btn_color.setStyleSheet(
            f"QPushButton {{ background: {self._bg_color}; color: #222; "
            f"border: 1px solid #888; border-radius: 4px; padding: 3px 16px; }}"
        )
        self.btn_color.setText(self._bg_color)

    # -- 交互 ----------------------------------------------------------------

    def _on_browse_image(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "选择分类图标图片", "", IMAGE_FILTER)
        if path:
            self.ed_image.setText(path)

    def _on_pick_color(self) -> None:
        color = QColorDialog.getColor(QColor(self._bg_color), self, "选择面板背景色")
        if color.isValid():
            self._bg_color = color.name().upper()
            self._update_color_button()

    # -- 收集 ----------------------------------------------------------------

    def _on_accept(self) -> None:
        cat = self.cat
        label = self.ed_label.text().strip()
        cat.label = (label or cat.label)[:LABEL_MAX_CHARS]
        cat.icon_px = self.sp_icon_px.value()
        cat.panel_w = self.sp_panel_w.value()
        cat.panel_h = self.sp_panel_h.value()
        cat.radius = self.sp_radius.value()
        cat.bg_color = self._bg_color
        cat.opacity = self.sl_opacity.value()
        cat.position_mode = self.cmb_position.currentData() or "anchor"

        image = self.ed_image.text().strip()
        if image:
            self._image_source = image
            self._image_cleared = False
        else:
            self._image_source = ""
            self._image_cleared = True
        cat.sanitize()
        if self._old_label != cat.label:
            self.category_renamed.emit(cat.id)
        self.settings_applied.emit()
        self.accept()

    # -- 对外属性 -------------------------------------------------------------

    @property
    def image_source(self) -> str:
        """用户新选的原图路径（""=不变）；controller 负责复制到 .icons。"""
        return self._image_source

    @property
    def image_cleared(self) -> bool:
        """用户是否选择恢复默认图标。"""
        return self._image_cleared

    def exec_(self) -> int:
        """兼容命名的 exec 别名。"""
        return self.exec()
