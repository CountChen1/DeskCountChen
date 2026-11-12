"""PanelWindow：半透明圆角面板窗口。

- 背景 = paintEvent 中 QPainterPath.addRoundedRect + fillPath(带 alpha 的
  QColor)；严禁 setWindowOpacity / QGraphicsOpacityEffect（会连子控件一起淡掉）；
  opacity=0 时只剩内容；
- 边缘 6px 拉伸（不用 QSizeGrip，右下角留给"打开此文件夹"）；
- 失焦（WindowDeactivate）延迟 120ms 后若仍非激活则 close()；
- 标题区可拖动窗口。
"""

from __future__ import annotations

import logging

from PySide6.QtCore import QEvent, QPoint, QRect, QRectF, QSize, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QFontMetrics, QGuiApplication, QPainter, QPainterPath
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import CategoryConfig
from app.panel_model import PanelModel
from app.windows.panel_grid import PanelGrid

logger = logging.getLogger(__name__)

MIN_W = 240
MIN_H = 160
EDGE = 6          # 边缘拉伸带宽度（px）
HEADER_H = 36     # 标题拖动区高度
CLOSE_DELAY_MS = 120


def _edge_cursor(edge: str) -> Qt.CursorShape:
    if edge in ("N", "S"):
        return Qt.CursorShape.SizeVerCursor
    if edge in ("E", "W"):
        return Qt.CursorShape.SizeHorCursor
    if edge in ("NE", "SW"):
        return Qt.CursorShape.SizeBDiagCursor
    if edge in ("NW", "SE"):
        return Qt.CursorShape.SizeFDiagCursor
    return Qt.CursorShape.ArrowCursor


class PanelWindow(QWidget):
    """一个分类的面板（同时只存在一个，由 AppController 管理）。"""

    closed = Signal(str)                    # cat_id
    open_folder_requested = Signal(str)     # cat_id
    resized = Signal(str, int, int)         # cat_id, w, h

    def __init__(self, cat: CategoryConfig, item_px: int, icon_cache,
                 edit_press_ms: int = 400, parent: QWidget | None = None) -> None:
        super().__init__(
            parent,
            Qt.WindowType.FramelessWindowHint
            | Qt.WindowType.Tool
            | Qt.WindowType.WindowStaysOnTopHint,
        )
        self.cat = cat
        self.setMouseTracking(True)  # 无按键时也要收到 move 以更新光标
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 注意：面板不加 WA_ShowWithoutActivating / NOACTIVATE —— 它必须能
        # 接收焦点才能实现 Esc 关闭与失焦关闭。

        # 网格
        self.grid = PanelGrid(item_px=item_px, icon_cache=icon_cache,
                              edit_press_ms=edit_press_ms)

        # 标题区
        self._title = QLabel(cat.label)
        self._title.setStyleSheet("font-weight: 600;")
        self._title.setFixedHeight(HEADER_H - 8)

        # 底部入口：打开此文件夹
        self._folder_btn = QPushButton("打开此文件夹")
        self._folder_btn.setCursor(Qt.CursorShape.PointingHandCursor)
        self._folder_btn.setFixedHeight(26)

        btn_row = QHBoxLayout()
        btn_row.setContentsMargins(0, 4, 0, 0)
        btn_row.addStretch(1)
        btn_row.addWidget(self._folder_btn)

        m = 6 if cat.radius > 0 else 2
        layout = QVBoxLayout(self)
        layout.setContentsMargins(m + 6, m + 4, m + 6, m + 4)
        layout.setSpacing(4)
        layout.addWidget(self._title)
        layout.addWidget(self.grid, 1)
        layout.addLayout(btn_row)

        self._folder_btn.clicked.connect(self._on_open_folder)
        # 编辑态标题提示：抖动之外唯一的文字反馈，同时让"Esc 退出"可被发现
        self.grid.edit_mode_changed.connect(self._on_grid_edit_mode_changed)

        # 拉伸/移动状态
        self._resize_edge = ""
        self._moving = False
        self._press_global = QPoint()
        self._start_geom = QRect()
        self._start_pos = QPoint()

        w = max(MIN_W, int(cat.panel_w))
        h = max(MIN_H, int(cat.panel_h))
        self.resize(w, h)
        self._apply_text_colors()

    # -- 文字颜色随背景明暗自动切换 -------------------------------------------

    def _apply_text_colors(self) -> None:
        color = QColor(self.cat.bg_color)
        lum = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        fg = "#1F2430" if lum >= 128 else "#F2F4F8"
        self._title.setStyleSheet(f"font-weight: 600; color: {fg}; background: transparent;")
        self._folder_btn.setStyleSheet(
            "QPushButton { background: rgba(127,127,127,60); color: %s; "
            "border: none; border-radius: 5px; padding: 2px 12px; } "
            "QPushButton:hover { background: rgba(127,127,127,100); }" % fg
        )

    # -- 模型与定位 ----------------------------------------------------------

    def set_model(self, panel_model: PanelModel) -> None:
        """绑定面板数据模型。"""
        self.grid.set_model(panel_model)

    def place_near(self, icon_rect: QRect) -> None:
        """贴合图标弹出：右侧优先，放不下翻左侧，clamp 到该屏可用区。"""
        center = icon_rect.center()
        screen = QGuiApplication.screenAt(center) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        w, h = self.width(), self.height()
        x = icon_rect.right() + 8
        if x + w > avail.right():
            x = icon_rect.left() - 8 - w
        x = max(avail.left(), min(x, avail.right() - w))
        y = max(avail.top(), min(icon_rect.top(), avail.bottom() - h))
        self.move(x, y)

    def place_center(self, icon_rect: QRect) -> None:
        """在图标所在屏的可用区居中弹出。"""
        screen = QGuiApplication.screenAt(icon_rect.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return
        avail = screen.availableGeometry()
        x = avail.left() + (avail.width() - self.width()) // 2
        y = avail.top() + (avail.height() - self.height()) // 2
        self.move(max(avail.left(), x), max(avail.top(), y))

    # -- 绘制：圆角半透明背景（填充 alpha，绝不用窗口透明度） -------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        r = float(self.cat.radius)
        path = QPainterPath()
        path.addRoundedRect(QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5), r, r)
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        bg = QColor(self.cat.bg_color)
        bg.setAlpha(round(self.cat.opacity / 100 * 255))
        p.fillPath(path, bg)

    # -- 失焦关闭 ------------------------------------------------------------

    def event(self, event) -> bool:  # noqa: N802
        # PySide6 6.12 实测：WindowDeactivate 事件只派发到 QWidget.event()，
        # changeEvent 只能收到 ActivationChange（点任务栏时不发 ActivationChange
        # 的场景也要覆盖，因此两处都拦）。
        if event.type() == QEvent.Type.WindowDeactivate:
            QTimer.singleShot(CLOSE_DELAY_MS, self._check_deactivate)
        return super().event(event)

    def changeEvent(self, event) -> None:  # noqa: N802
        super().changeEvent(event)
        if (event.type() == QEvent.Type.ActivationChange
                and not self.isActiveWindow()):
            QTimer.singleShot(CLOSE_DELAY_MS, self._check_deactivate)

    def _check_deactivate(self) -> None:
        from PySide6.QtWidgets import QApplication

        if self.isVisible() and QApplication.activeWindow() is not self:
            self.close()

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Esc 关闭面板：桌面弹窗没有系统返回键，Esc 是唯一的键盘退出路径
        if event.key() == Qt.Key.Key_Escape:
            self.close()
            event.accept()
            return
        super().keyPressEvent(event)

    # -- 边缘拉伸 + 标题拖动 ---------------------------------------------------

    def _edge_at(self, pos: QPoint) -> str:
        r = self.rect()
        left = pos.x() <= EDGE
        right = pos.x() >= r.width() - EDGE
        top = pos.y() <= EDGE
        bottom = pos.y() >= r.height() - EDGE
        if top and left:
            return "NW"
        if top and right:
            return "NE"
        if bottom and left:
            return "SW"
        if bottom and right:
            return "SE"
        if left:
            return "W"
        if right:
            return "E"
        if top:
            return "N"
        if bottom:
            return "S"
        return ""

    def mousePressEvent(self, event) -> None:  # noqa: N802
        if event.button() != Qt.MouseButton.LeftButton:
            super().mousePressEvent(event)
            return
        pos = event.position().toPoint()
        edge = self._edge_at(pos)
        if edge:
            self._resize_edge = edge
            self._press_global = event.globalPosition().toPoint()
            self._start_geom = QRect(self.geometry())
            event.accept()
            return
        if pos.y() <= HEADER_H:
            self._moving = True
            self._press_global = event.globalPosition().toPoint()
            self._start_pos = self.pos()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event) -> None:  # noqa: N802
        g = event.globalPosition().toPoint()
        if self._resize_edge:
            dx = g.x() - self._press_global.x()
            dy = g.y() - self._press_global.y()
            geo = QRect(self._start_geom)
            if "W" in self._resize_edge:
                new_w = max(MIN_W, geo.width() - dx)
                geo.setLeft(geo.right() - new_w + 1)
            if "E" in self._resize_edge:
                geo.setWidth(max(MIN_W, geo.width() + dx))
            if "N" in self._resize_edge:
                new_h = max(MIN_H, geo.height() - dy)
                geo.setTop(geo.bottom() - new_h + 1)
            if "S" in self._resize_edge:
                geo.setHeight(max(MIN_H, geo.height() + dy))
            self.setGeometry(geo)
            event.accept()
            return
        if self._moving:
            self.move(self._start_pos + (g - self._press_global))
            event.accept()
            return
        self.setCursor(_edge_cursor(self._edge_at(event.position().toPoint())))
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event) -> None:  # noqa: N802
        if event.button() == Qt.MouseButton.LeftButton and self._resize_edge:
            self._resize_edge = ""
            self.cat.panel_w, self.cat.panel_h = self.width(), self.height()
            self.resized.emit(self.cat.id, self.width(), self.height())
            event.accept()
            return
        if event.button() == Qt.MouseButton.LeftButton and self._moving:
            self._moving = False
            event.accept()
            return
        super().mouseReleaseEvent(event)

    # -- 关闭与底部入口 -------------------------------------------------------

    def _on_open_folder(self) -> None:
        # 先发信号再关窗：controller 在直接连接的槽里同步完成 shell_open
        self.open_folder_requested.emit(self.cat.id)

    def _on_grid_edit_mode_changed(self, active: bool) -> None:
        """编辑态切换时更新标题：进入提示操作方式，退出恢复分类名。

        文字超出标题宽度时省略号截断（面板可被拉伸得很窄）。
        """
        text = "编辑模式：拖动重排，Esc 退出" if active else self.cat.label
        fm = QFontMetrics(self._title.font())
        self._title.setText(
            fm.elidedText(text, Qt.TextElideMode.ElideRight, self._title.width())
        )

    def closeEvent(self, event) -> None:  # noqa: N802
        self.closed.emit(self.cat.id)
        super().closeEvent(event)

    # -- 尺寸约束辅助 ---------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(max(MIN_W, int(self.cat.panel_w)), max(MIN_H, int(self.cat.panel_h)))
