"""PanelWindow：半透明圆角面板窗口（v1.5：毛玻璃 + 缩放缓入 + 关闭兜底）。

## 背景绘制（硬约束）

- 背景一律在 ``paintEvent`` 里画，**严禁** ``setWindowOpacity`` /
  ``QGraphicsOpacityEffect``（那会把子控件一起淡掉，opacity=0 时整块面板消失）。
  淡入用的是 painter 级 ``setOpacity``，只影响背景层，不影响子控件。
- ``material == "blur"``（v1.5 默认）：截屏 → 裁剪面板区域 → 降采样 1/8 →
  平滑放大回来（经典降采样模糊，视觉等效高斯，实测与高斯无差别但成本 < 10ms），
  再叠加 ``cat.bg_color`` + ``cat.opacity`` 的 tint，最后加 1px 顶部高光 + 内侧
  暗边做玻璃厚度感。
- ``material == "solid"``：v1.4 行为（纯 ``cat.bg_color`` 填充）。
- ``cat.opacity == 0`` 时**连模糊底图一起不画**：保持"只剩内容"的既有语义
  （模糊底属于背景，不是内容）。

## 关闭路径（四条，互不排斥）

| 原因 | 触发 | 实现 |
|---|---|---|
| ``deactivate`` | 点其他应用/桌面/任务栏（Qt 激活事件） | ``event()`` + ``changeEvent()`` → 120ms 复核；``GRACE_MS`` 内的失活**作废** |
| ``click-outside`` | 面板外左键按下（**不依赖 Qt 激活事件**） | ``OutsideClickWatcher`` 40ms 轮询 |
| ``focus-lost`` | 曾经是前台，之后持续 ≥180ms 不是 | 同上 |
| ``esc`` | 键盘 Esc | ``keyPressEvent`` |

为什么必须有 ``click-outside``：面板由 SAO Utils 唤起后台进程，Windows 前台锁定
常让 ``SetForegroundWindow`` 失败 → 面板从未成为前台 → 点桌面**没有**任何激活
事件 → 前两条路径全线失效（实测 100% 复现，见 ``tests/probe_close_bug.py``）。
"""

from __future__ import annotations

import logging
import time

from PySide6.QtCore import (
    QEasingCurve,
    QEvent,
    QPoint,
    QPointF,
    QPropertyAnimation,
    QRect,
    QRectF,
    QSize,
    Qt,
    QTimer,
    QVariantAnimation,
    Signal,
)
from PySide6.QtGui import (
    QColor,
    QFontMetrics,
    QGuiApplication,
    QImage,
    QPainter,
    QPainterPath,
    QPen,
    QPixmap,
)
from PySide6.QtWidgets import (
    QHBoxLayout,
    QLabel,
    QPushButton,
    QVBoxLayout,
    QWidget,
)

from app.config import MATERIAL_BLUR, CategoryConfig
from app.panel_model import PanelModel
from app.win32 import input_watch
from app.windows.panel_grid import PanelGrid
from app.windows.panel_watch import GRACE_MS, OutsideClickWatcher, SystemInputSource

logger = logging.getLogger(__name__)

MIN_W = 240
MIN_H = 160
EDGE = 6          # 边缘拉伸带宽度（px）
HEADER_H = 36     # 标题拖动区高度
CLOSE_DELAY_MS = 120

# -- v1.5 外观动画 ---------------------------------------------------------
SCALE_START = 0.94        # 出现时从 94% 缩放到 100%
FADE_IN_MS = 140          # 出现动画时长（OutCubic）
FADE_OUT_MS = 80          # 关闭动画时长（可选）
BLUR_DOWNSCALE = 8        # 模糊降采样倍数（1/8 → 平滑放大）
RECAPTURE_DEBOUNCE_MS = 150   # 几何变化后重新截屏的去抖

REASON_DEACTIVATE = "deactivate"
REASON_ESC = "esc"
REASON_PROGRAMMATIC = "programmatic"


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


def scaled_rect(rect: QRect, origin: QPointF, factor: float) -> QRect:
    """以 ``origin`` 为不动点把矩形缩放 ``factor`` 倍（纯函数，便于单测）。

    Args:
        rect: 目标矩形（动画终点）。
        origin: 不动点（全局坐标）。锚点模式下取"锚点被 clamp 进矩形后的点"，
            于是面板看起来是**从图标方向展开**的，而不是从屏幕中心。
        factor: 缩放系数（``SCALE_START`` = 0.94）。

    Returns:
        缩放后的矩形（宽高至少 1px）。
    """
    w = max(1, int(round(rect.width() * factor)))
    h = max(1, int(round(rect.height() * factor)))
    left = origin.x() + (rect.left() - origin.x()) * factor
    top = origin.y() + (rect.top() - origin.y()) * factor
    return QRect(int(round(left)), int(round(top)), w, h)


def image_looks_blank(img: QImage) -> bool:
    """截图是否"全黑/全透明"（远程桌面、UAC 保护、锁屏时常见）。

    9 点采样即可：只要有一个采样点有可见亮度就认为有效。这样既避开"整屏
    黑屏被当成有效底图"（面板变成一块黑玻璃），也不会因为某个点恰好是
    纯黑而误判。
    """
    if img is None or img.isNull():
        return True
    w, h = img.width(), img.height()
    if w <= 0 or h <= 0:
        return True
    for i in range(9):
        x = min(w - 1, max(0, int(w * ((i % 3) + 0.5) / 3.0)))
        y = min(h - 1, max(0, int(h * ((i // 3) + 0.5) / 3.0)))
        color = QColor.fromRgba(int(img.pixel(x, y)))
        if color.alpha() > 0 and (color.red() + color.green() + color.blue()) > 12:
            return False
    return True


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
        self.close_reason = ""              # 关闭原因（deactivate/click-outside/…）
        self.setMouseTracking(True)  # 无按键时也要收到 move 以更新光标
        self.setAttribute(Qt.WidgetAttribute.WA_TranslucentBackground)
        # 注意：面板不加 WA_ShowWithoutActivating / NOACTIVATE —— 它必须能
        # 接收焦点才能实现 Esc 关闭与失焦关闭。

        # -- 背景（毛玻璃底图 / 淡入系数） ---------------------------------
        self._backdrop: QPixmap | None = None
        self._backdrop_dirty: bool = True
        self._fade: float = 1.0             # 背景 alpha 系数 0..1（禁用窗口透明度）
        self._fade_pending: bool = False    # prepare_show 之后、animate_show 之前
        self._closing: bool = False
        # show() 的单调时钟戳（秒）。用于 deactivate 路径的开场宽限：
        # 0.0 表示"从未 show 过"，此时宽限判定必然早已过期，不会误推迟关闭。
        self._shown_at: float = 0.0
        self._last_blur_ms: float = 0.0
        self._final_geom = QRect()
        self._anim_start_geom = QRect()

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

        # -- 关闭兜底轮询（不依赖 Qt 激活事件） ---------------------------
        self._watch = OutsideClickWatcher(
            self._panel_rect,
            source=SystemInputSource(lambda: int(self.winId())),
            parent=self,
        )
        self._watch.triggered.connect(self.close_with_reason)

        # -- 动画 ---------------------------------------------------------
        self._geom_anim = QPropertyAnimation(self, b"geometry", self)
        self._geom_anim.setDuration(FADE_IN_MS)
        self._geom_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim = QVariantAnimation(self)
        self._fade_anim.setDuration(FADE_IN_MS)
        self._fade_anim.setEasingCurve(QEasingCurve.Type.OutCubic)
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.valueChanged.connect(self._on_fade_value)
        self._fade_anim.finished.connect(self._on_fade_finished)
        self._fade_out_anim = QVariantAnimation(self)
        self._fade_out_anim.setDuration(FADE_OUT_MS)
        self._fade_out_anim.setEasingCurve(QEasingCurve.Type.InCubic)
        self._fade_out_anim.valueChanged.connect(self._on_fade_value)
        self._fade_out_anim.finished.connect(self._on_fade_out_finished)

        self._recapture_timer = QTimer(self)
        self._recapture_timer.setSingleShot(True)
        self._recapture_timer.setInterval(RECAPTURE_DEBOUNCE_MS)
        self._recapture_timer.timeout.connect(self._on_recapture_timeout)

    # -- 文字颜色随背景明暗自动切换 -------------------------------------------

    def _apply_text_colors(self) -> None:
        color = QColor(self.cat.bg_color)
        # ITU-R BT.601 亮度（0–255），< 128 判定为深色底
        lum = 0.299 * color.red() + 0.587 * color.green() + 0.114 * color.blue()
        is_dark_bg = lum < 128
        # 方向：深色底配浅字，浅色底配深字（先保对比度，再谈好看）
        fg = "#F2F4F8" if is_dark_bg else "#1F2430"
        # hover：深底 → 纯白（更亮），浅底 → 纯黑（更深）
        hover_fg = "#FFFFFF" if is_dark_bg else "#000000"
        self._title.setStyleSheet(f"font-weight: 600; color: {fg}; background: transparent;")
        self._folder_btn.setStyleSheet(
            "QPushButton { background: rgba(127,127,127,60); color: %s; "
            "border: none; border-radius: 5px; padding: 2px 12px; } "
            "QPushButton:hover { background: rgba(127,127,127,100); }" % fg
        )
        self.grid.set_hover_colors(fg, hover_fg)

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

    # -- 出现动画：94% → 100% + 背景 alpha 0 → 目标 -------------------------

    def prepare_show(self, anchor_rect: QRect | None = None) -> None:
        """``show()`` **之前**调用：截模糊底图 + 把几何缩到 94%（动画起点）。

        必须早于 ``show()`` 截屏，否则会把面板自己截进底图。

        Args:
            anchor_rect: 面板锚点（1×1 矩形）。用于决定缩放不动点：
                矩形外的锚点会被 clamp 到矩形边上，于是面板"从图标方向展开"。
        """
        self._final_geom = QRect(self.geometry())
        self._backdrop_dirty = False
        self._backdrop = self._capture_backdrop()
        origin = self._scale_origin(anchor_rect)
        self._anim_start_geom = scaled_rect(self._final_geom, origin, SCALE_START)
        self._fade = 0.0
        self._fade_pending = True
        self.setGeometry(self._anim_start_geom)
        self.update()

    def animate_show(self) -> None:
        """``show()`` **之后**调用：几何 94%→100%，背景 alpha 0→目标（140ms）。

        两个动画都是"属性动画"，与窗口透明度无关（硬约束）。
        """
        if not self._fade_pending:
            # 没走 prepare_show（例如单测直接 show）→ 直接把背景画满
            self._fade = 1.0
            self.update()
            return
        self._fade_pending = False
        self._geom_anim.stop()
        self._geom_anim.setStartValue(QRect(self._anim_start_geom))
        self._geom_anim.setEndValue(QRect(self._final_geom))
        self._geom_anim.start()
        self._fade_out_anim.stop()
        self._fade_anim.stop()
        self._fade_anim.setStartValue(0.0)
        self._fade_anim.setEndValue(1.0)
        self._fade_anim.start()

    def _scale_origin(self, anchor_rect: QRect | None) -> QPointF:
        """缩放不动点：居中模式取面板中心，锚点模式取"锚点被 clamp 进面板"的点。"""
        geo = self._final_geom if self._final_geom.isValid() else QRect(self.geometry())
        if anchor_rect is None or self.cat.position_mode == "center":
            return QPointF(geo.center())
        ax = min(max(anchor_rect.center().x(), geo.left()), geo.right())
        ay = min(max(anchor_rect.center().y(), geo.top()), geo.bottom())
        return QPointF(ax, ay)

    def _on_fade_value(self, value) -> None:
        try:
            self._fade = max(0.0, min(1.0, float(value)))
        except (TypeError, ValueError):
            self._fade = 1.0
        self.update()

    def _on_fade_finished(self) -> None:
        self._fade = 1.0
        self.update()

    def _on_fade_out_finished(self) -> None:
        self._fade = 0.0
        self.update()
        self.close()

    @property
    def fade(self) -> float:
        """背景 alpha 系数（0..1，单测/探针断言用）。"""
        return self._fade

    @property
    def blur_cost_ms(self) -> float:
        """最近一次模糊底图的生成耗时（毫秒，验收证据用）。"""
        return self._last_blur_ms

    @property
    def has_blur(self) -> bool:
        """当前是否持有一张有效的模糊底图。"""
        return self._backdrop is not None and not self._backdrop.isNull()

    @property
    def backdrop_size(self) -> tuple[float, float]:
        """模糊底图的**逻辑**尺寸（无底图时返回 ``(0.0, 0.0)``，探针断言用）。"""
        if self._backdrop is None or self._backdrop.isNull():
            return 0.0, 0.0
        dpr = float(self._backdrop.devicePixelRatio()) or 1.0
        return self._backdrop.width() / dpr, self._backdrop.height() / dpr

    # -- 模糊底图 ------------------------------------------------------------

    def _use_blur(self) -> bool:
        """是否该用模糊底图：材质为 blur **且** 背景不是全透明。

        ``opacity == 0`` 的语义是"只剩内容"，此时连背景一起不画（v1.4 既有
        需求，模糊底属于背景而不是内容）。
        """
        return self.cat.material == MATERIAL_BLUR and int(self.cat.opacity) > 0

    def refresh_backdrop(self) -> None:
        """重新截屏生成模糊底图（失败/全黑/尺寸不符 → 回退纯色，不抛异常）。

        幂等：材质不是 blur、或 ``opacity == 0`` 时清空底图（回到纯色路径）。
        """
        self._backdrop_dirty = False
        if not self._use_blur():
            self._backdrop = None
            return
        start = time.perf_counter()
        try:
            backdrop = self._capture_blurred()
        except Exception as exc:  # noqa: BLE001 - 取不到底图绝不能影响面板可用性
            logger.warning("模糊底图生成失败，回退纯色绘制: %s", exc)
            backdrop = None
        self._backdrop = backdrop
        self._last_blur_ms = (time.perf_counter() - start) * 1000.0
        logger.info("面板模糊底图: %s（%.2f ms）",
                    "成功" if backdrop is not None else "失败→纯色回退",
                    self._last_blur_ms)

    def _capture_backdrop(self) -> QPixmap | None:
        """``prepare_show`` 用：生成底图并返回（实现复用 :meth:`refresh_backdrop`）。"""
        self.refresh_backdrop()
        return self._backdrop

    def _capture_blurred(self) -> QPixmap | None:
        """截屏 → 裁剪面板区域 → 降采样 1/8 → 平滑放大（等效高斯模糊）。

        DPI：``grabWindow`` 返回的 pixmap 带 ``devicePixelRatio``，其像素是**物理
        像素**；``frameGeometry()`` 是逻辑坐标。裁剪矩形因此必须乘 dpr，最后再
        把 dpr 设回结果 pixmap，绘制时就能按逻辑矩形 1:1 铺满。
        """
        geo = self.frameGeometry()
        if geo.width() < 8 or geo.height() < 8:
            return None
        screen = QGuiApplication.screenAt(geo.center()) or QGuiApplication.primaryScreen()
        if screen is None:
            return None
        # 只抓面板所在的那一小块，不要抓整屏：整屏 1920x1080 实测 25–29ms，
        # 只抓面板区域 6–8ms（900x700 面板也就 10ms 出头）。
        #
        # ⚠️ grabWindow(0, x, y, w, h) 的 x/y/w/h 是**逻辑坐标**：Qt 内部会乘
        # devicePixelRatio 再抓物理像素，并把 dpr 设到返回的 pixmap 上
        # （实测：请求 575x412 得到 719x515 的 pixmap，dpr=1.25）。
        # 这里若再自己乘 dpr，就会抓错区域（偏移 25%）—— 踩过一次。
        dpr = float(screen.devicePixelRatio()) or 1.0
        sgeo = screen.geometry()
        grab_x = int(round(geo.x() - sgeo.x()))
        grab_y = int(round(geo.y() - sgeo.y()))
        grab_w = int(round(geo.width()))
        grab_h = int(round(geo.height()))
        if grab_w < 8 or grab_h < 8:
            return None
        shot = screen.grabWindow(0, grab_x, grab_y, grab_w, grab_h)
        if shot is None or shot.isNull():
            logger.info("截屏失败（grabWindow 返回空），回退纯色")
            return None
        crop = shot.toImage()
        if crop.isNull():
            logger.info("截屏转 QImage 失败，回退纯色")
            return None
        # 尺寸不符（多屏负坐标 / 屏幕边缘越界）时按实际拿到的区域继续，
        # 只要还能模糊就没有问题；实在太小就回退。
        if crop.width() < 8 or crop.height() < 8:
            logger.info("截屏裁剪区域无效（%sx%s），回退纯色", crop.width(), crop.height())
            return None
        if image_looks_blank(crop):
            logger.info("截屏内容全黑/全透明，回退纯色")
            return None
        small = crop.scaled(
            max(1, crop.width() // BLUR_DOWNSCALE),
            max(1, crop.height() // BLUR_DOWNSCALE),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        blurred = small.scaled(
            crop.width(), crop.height(),
            Qt.AspectRatioMode.IgnoreAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        pixmap = QPixmap.fromImage(blurred)
        if pixmap.isNull():
            return None
        pixmap.setDevicePixelRatio(dpr)
        logical_w = pixmap.width() / dpr
        logical_h = pixmap.height() / dpr
        if abs(logical_w - geo.width()) > 2 or abs(logical_h - geo.height()) > 2:
            # 尺寸不符（多屏负坐标 / 屏幕边缘越界）→ 底图会按 rect 拉伸，
            # 视觉上仍是模糊背景，但记一条便于定位的日志。
            logger.info("底图逻辑尺寸 %.0fx%.0f 与面板 %dx%d 不符（按拉伸绘制）",
                        logical_w, logical_h, geo.width(), geo.height())
        return pixmap

    # -- 绘制：圆角背景（模糊底 + tint + 玻璃描边） ---------------------------

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._fade <= 0.001:
            return      # 完全透明：不画背景（子控件照常由 Qt 自己绘制）
        p = QPainter(self)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        p.setOpacity(self._fade)   # painter 级透明度：只影响背景层，不影响子控件
        r = float(self.cat.radius)
        rect = QRectF(self.rect()).adjusted(0.5, 0.5, -0.5, -0.5)
        path = QPainterPath()
        path.addRoundedRect(rect, r, r)

        has_backdrop = self.has_blur
        if has_backdrop:
            p.save()
            p.setClipPath(path)
            p.drawPixmap(self.rect(), self._backdrop)
            p.restore()

        bg = QColor(self.cat.bg_color)
        bg.setAlpha(round(self.cat.opacity / 100 * 255))
        p.fillPath(path, bg)

        if has_backdrop:
            # 玻璃厚度：外圈 1px 高光 + 顶部更亮的一条 + 内侧 1px 暗边
            p.setBrush(Qt.BrushStyle.NoBrush)
            p.setPen(QPen(QColor(255, 255, 255, 70), 1.0))
            p.drawPath(path)
            p.save()
            p.setClipPath(path)
            p.setPen(QPen(QColor(255, 255, 255, 120), 1.0))
            p.drawLine(QPointF(rect.left() + r, 1.0), QPointF(rect.right() - r, 1.0))
            p.restore()
            inner = QPainterPath()
            inner.addRoundedRect(rect.adjusted(1, 1, -1, -1), max(0.0, r - 1), max(0.0, r - 1))
            p.setPen(QPen(QColor(0, 0, 0, 45), 1.0))
            p.drawPath(inner)

    # -- 关闭（含原因） ------------------------------------------------------

    def close_with_reason(self, reason: str) -> None:
        """带原因关闭：淡出 80ms 后真正 ``close()``（已关闭则幂等返回）。

        原因会写进 ``closeEvent`` 的日志，便于区分"点外面关的"和"Esc 关的"。
        """
        if self._closing:
            return
        if not self.close_reason:
            self.close_reason = str(reason or "")
        if not self.isVisible():
            self.close()
            return
        self._closing = True
        self._watch.stop()
        self._fade_anim.stop()
        if self._fade > 0.05:
            self._fade_out_anim.stop()
            self._fade_out_anim.setStartValue(float(self._fade))
            self._fade_out_anim.setEndValue(0.0)
            self._fade_out_anim.start()
        else:
            self.close()

    def _panel_rect(self) -> QRect:
        """面板的全局矩形（逻辑坐标）——外部点击判定的唯一依据。"""
        return QRect(self.frameGeometry())

    # -- 失焦关闭（既有路径，行为不变） ---------------------------------------

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
        """复核"是否真的失活了"，确认后关闭面板。

        红线 1（开场宽限）在**激活事件**这一半同样成立：``show()`` 之后
        ``GRACE_MS`` 内收到的失活一律**作废**——不关闭，也不排到宽限之后再判。

        为什么必须有这一半：用户连点 SAO 图标唤起时，第二次点击往往落在**图标**
        上——图标在面板之外。那一下会让面板瞬间失活，而 v1.5 之前 ``event()``
        这条路径没有宽限，面板会"一闪而过"（实测 71ms 失活 → 190ms 被关）。
        这是 P0-1 的镜像：关不掉修好了，不能留下"打不开"。

        为什么是"作废"而不是"推迟到宽限结束再判"（**实测教训**）：推迟等于把
        **同一个事件**挪到更晚的时刻重新判一次，而那一刻的 ``activeWindow()``
        早就不是面板了（激活丢失本来就发生在宽限内），于是必然判死——推迟不但
        没解决问题，反而把原本无害的早期复核变成了有害的晚期复核。
        实测评测：改成推迟后 QA 的 B1/B3/B4/B5/B6 一起红（面板开出来 0.8s 后
        已经被自己关掉）。作废才是对的：这一次失活属于"打开它的那个手势"，
        不是"用户走开了"。用户真的走开时，宽限之后还会有失活/外点，那才该关。

        时间基准 ``_shown_at`` 由 ``showEvent`` 写入；120ms 的 ``CLOSE_DELAY_MS``
        语义不变（宽限之外的复核与以前完全一致）。
        """
        from PySide6.QtWidgets import QApplication

        if not self.isVisible():
            return
        elapsed_ms = (time.monotonic() - self._shown_at) * 1000.0
        if elapsed_ms < GRACE_MS:
            logger.debug("开场宽限内的失活（%.0fms < %sms）：作废，不关闭面板",
                         elapsed_ms, GRACE_MS)
            return
        if QApplication.activeWindow() is not self:
            self.close_with_reason(REASON_DEACTIVATE)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        # Esc 关闭面板：桌面弹窗没有系统返回键，Esc 是唯一的键盘退出路径
        if event.key() == Qt.Key.Key_Escape:
            self.close_with_reason(REASON_ESC)
            event.accept()
            return
        super().keyPressEvent(event)

    # -- 显示 / 隐藏 / 关闭：动画与轮询定时器的生命周期 -----------------------

    def showEvent(self, event) -> None:  # noqa: N802
        super().showEvent(event)
        self._closing = False
        # 开场宽限的起点：deactivate 与轮询共用同一个 GRACE_MS 口径
        self._shown_at = time.monotonic()
        self._recapture_timer.stop()
        if not self._fade_pending:
            self._fade = 1.0
        # 可选兜底：尽力真正抢到前台（抢到了激活事件自然正常；抢不到靠轮询）
        try:
            input_watch.force_foreground(int(self.winId()))
        except Exception as exc:  # noqa: BLE001 - 抢前台失败绝不能影响显示
            logger.debug("force_foreground 异常: %s", exc)
        # 兜底轮询：从此刻开始计时 300ms 宽限期
        self._watch.start()

    def hideEvent(self, event) -> None:  # noqa: N802
        self._watch.stop()          # 定时器绝不能泄漏
        self._recapture_timer.stop()
        super().hideEvent(event)

    def moveEvent(self, event) -> None:  # noqa: N802
        super().moveEvent(event)
        self._on_geometry_changed()

    def resizeEvent(self, event) -> None:  # noqa: N802
        super().resizeEvent(event)
        self._on_geometry_changed()

    def _on_geometry_changed(self) -> None:
        """几何变化 → 底图失效。可见期间不重截（会截到面板自己）。"""
        self._backdrop_dirty = True
        if self.isVisible():
            return
        self._recapture_timer.start()

    def _on_recapture_timeout(self) -> None:
        if self.isVisible():
            return          # 已经可见了：不重截，避免把面板自己截进底图
        if self._backdrop_dirty:
            self.refresh_backdrop()

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
        self._watch.stop()               # 定时器绝不能泄漏
        self._recapture_timer.stop()
        for anim in (self._geom_anim, self._fade_anim, self._fade_out_anim):
            anim.stop()
        reason = self.close_reason or REASON_PROGRAMMATIC
        logger.info("面板已关闭（原因=%s）: %s", reason, self.cat.id)
        self.closed.emit(self.cat.id)
        super().closeEvent(event)

    # -- 尺寸约束辅助 ---------------------------------------------------------

    def sizeHint(self) -> QSize:  # noqa: N802
        return QSize(max(MIN_W, int(self.cat.panel_w)), max(MIN_H, int(self.cat.panel_h)))
