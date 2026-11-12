"""PanelGrid(QListView) + JiggleDelegate：拖拽重排、长按编辑态、单击启动。

v1.3 排序语义 = **密集插入**（完全对齐手机桌面文件夹，空位体系已废除，
详见 app/slot_ops.py）：
- 拖 A 落到图标 B 上：A 插到 B 的位置，B 及其后整体后移一格，
  A 原位置由后面图标自动补齐——网格永远紧凑，不产生任何空位；
- 拖到面板空白处 / 网格外：追加到末尾；
- 落在自己身上：顺序不变；
- 多选批量搬移（含不连续多选）：整块插入到落点之前，块内相对次序保持。

历史（勿回退）：v1.1~v1.2 曾实现"空位保留"的槽位制（HOLE_ROLE 占位行、
"绝不向前补位"），实测空位随拖拽无界累积、面板越拖越散，用户最终决策
改为手机式密集补齐；历史 .order.json 的空位记录在加载边界 densify 消失。

实现要点：
- 原生 QListView InternalMove 已弃用（其行为不可控），拖拽改自研：
  >4px 位移启动 QDrag，drop 时按密集插入语义重排（app/slot_ops.apply_move）；
- 编辑态抖动 + 多选批量移动（整块搬移，相对次序保持）。

实测修正（历史沉淀，勿回退）：
- QStandardItemModel.moveRows() 恒返回 False；
- dropMimeData(MoveAction) 是复制不是移动；
- QAbstractItemView.SelectionFlag 在 PySide6 6.12 不存在，
  必须用 QItemSelectionModel.SelectionFlag。
"""

from __future__ import annotations

import json
import logging
import time

from PySide6.QtCore import (
    QItemSelectionModel,
    QModelIndex,
    QMimeData,
    QPoint,
    QSize,
    Qt,
    QTimer,
    Signal,
)
from PySide6.QtCore import QEvent, QRect
from PySide6.QtGui import QColor, QDrag, QIcon, QMouseEvent
from PySide6.QtGui import QPalette
from PySide6.QtWidgets import (
    QAbstractItemView,
    QApplication,
    QFrame,
    QListView,
    QStyle,
    QStyledItemDelegate,
    QStyleOptionViewItem,
)

from app.config import DRAG_THRESHOLD_PX
from app.folder_model import Entry
from app.icon_provider import IconCache
from app.panel_model import HOLE_ROLE, NAME_ROLE, PanelModel
from app.slot_ops import apply_move

logger = logging.getLogger(__name__)

JIGGLE_INTERVAL_MS = 50          # 20fps 相位推进
LAUNCH_DEDUP_SECONDS = 0.5       # 防双击触发两次启动
MIME_TYPE = "application/x-desktopbox-slots"   # 自研槽位拖拽的 MIME

# v1.5 悬停效果：图标放大到 1.08（以单元格图标中心为基准），名字颜色变亮，
# 150ms 内缓动（逐帧推进，不是瞬间跳变，避免闪烁/残影）。
HOVER_SCALE = 1.08
HOVER_ANIM_MS = 150.0
HOVER_FRAME_MS = 16

# 空位占位行的"不可交互"标记由 HOLE_ROLE 承载（见 app/panel_model.py）


class JiggleDelegate(QStyledItemDelegate):
    """编辑态抖动 + 悬停高亮：都只影响绘制，不动数据、不重建模型。

    绘制优先级（**顺序敏感**）：
    1. ``HOLE_ROLE`` 空位行 → 什么都不画（保留网格空格）；
    2. 编辑态 → 抖动（±1.2px 平移 + ±2° 旋转）。抖动是"正在重排"的强反馈，
       此时不叠加悬停缩放，避免两个变换互相干扰；
    3. 悬停进度 > 0 → 自绘图标（放大到 1.08，以图标中心为基准）+ 文字变亮；
    4. 其余 → 走 Qt 原生绘制（行为与 v1.4 完全一致）。
    """

    def __init__(self, parent=None) -> None:
        super().__init__(parent)
        self.phase: float = 0.0
        self.edit_mode: bool = False
        # 悬停状态由 PanelGrid 持有（进度缓动、颜色都跟着面板背景走）
        self.grid = None
        self.hover_color: QColor = QColor("#FFFFFF")
        self.normal_color: QColor = QColor("#1F2430")

    def paint(self, painter, option, index: QModelIndex) -> None:
        if index.data(HOLE_ROLE):
            return  # 空位占位行：完全不绘制（留出网格空格）
        if self.edit_mode and option.rect.isValid():
            row = index.row()
            rot = 2.0 * (1.0 if (int(self.phase / 3.0) + row) % 2 else -1.0)
            dx = 1.2 * (1.0 if (int(self.phase / 2.5) + row) % 2 else -1.0)
            center = option.rect.center()
            painter.save()
            painter.translate(center.x() + dx, center.y())
            painter.rotate(rot)
            painter.translate(-center.x(), -center.y())
            super().paint(painter, option, index)
            painter.restore()
            return
        progress = self.hover_progress(option, index)
        if progress > 0.01 and option.rect.isValid():
            self._paint_hovered(painter, option, index, progress)
            return
        if option.rect.isValid():
            # 条目文字颜色跟随面板背景明暗（深底浅字 / 浅底深字）。
            # v1.4 走样式默认的 palette 黑字，深色面板上几乎看不见；
            # 悬停色必须与常态色不同，"名字变亮"这个效果才成立。
            opt = QStyleOptionViewItem(option)
            opt.palette.setColor(QPalette.ColorRole.Text, self.normal_color)
            opt.palette.setColor(QPalette.ColorRole.HighlightedText, self.normal_color)
            super().paint(painter, opt, index)
            return
        super().paint(painter, option, index)

    # -- 悬停 ---------------------------------------------------------------

    def hover_progress(self, option, index: QModelIndex) -> float:
        """该行的悬停进度 0..1。

        主来源是 :meth:`PanelGrid.hover_progress`（150ms 缓动值）；同时兼容
        样式自带的 ``State_MouseOver``（某些平台/Qt 版本会置位）——若样式认为
        悬停而缓动还没起步，直接按 1.0 处理，保证"有 hover 就一定看得出"。
        """
        progress = 0.0
        if self.grid is not None:
            try:
                progress = float(self.grid.hover_progress(index.row()))
            except Exception:  # noqa: BLE001 - 绘制期绝不能被异常打断
                progress = 0.0
        if (option.state & QStyle.StateFlag.State_MouseOver) and progress <= 0.0:
            progress = 1.0
        return max(0.0, min(1.0, progress))

    def hover_icon_scale(self, progress: float) -> float:
        """悬停时图标的缩放系数（纯函数，便于单测断言 1.0 → 1.08）。"""
        return 1.0 + (HOVER_SCALE - 1.0) * max(0.0, min(1.0, float(progress)))

    def _paint_hovered(self, painter, option, index: QModelIndex, progress: float) -> None:
        """自绘悬停态：图标按 1.08 缩放、文字改用高对比颜色。"""
        opt = QStyleOptionViewItem(option)
        self.initStyleOption(opt, index)
        widget = opt.widget
        style = widget.style() if widget is not None else QApplication.style()

        # 1) 背景层（选中底色 / 焦点框）交给样式，图标与文字由我们接管
        bg_opt = QStyleOptionViewItem(opt)
        bg_opt.text = ""
        bg_opt.icon = QIcon()
        style.drawControl(QStyle.ControlElement.CE_ItemViewItem, bg_opt, painter, widget)

        # 2) 图标：以单元格内图标中心为基准放大
        icon = index.data(Qt.ItemDataRole.DecorationRole)
        if isinstance(icon, QIcon) and not icon.isNull():
            icon_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemDecoration, opt, widget)
            if not icon_rect.isValid() or icon_rect.isEmpty():
                side = opt.decorationSize.width() if opt.decorationSize.isValid() else 48
                icon_rect = QRect(0, 0, max(16, int(side)), max(16, int(side)))
                icon_rect.moveCenter(QRect(opt.rect).center())
            factor = self.hover_icon_scale(progress)
            side = max(1, int(round(min(icon_rect.width(), icon_rect.height()) * factor)))
            scaled = QRect(0, 0, side, side)
            scaled.moveCenter(icon_rect.center())
            icon.paint(painter, scaled, Qt.AlignmentFlag.AlignCenter)

        # 3) 文字：悬停色（深底→白 / 浅底→黑），同样居中省略
        text = opt.text
        if text:
            text_rect = style.subElementRect(
                QStyle.SubElement.SE_ItemViewItemText, opt, widget)
            if text_rect.isValid() and not text_rect.isEmpty():
                painter.save()
                painter.setPen(self.hover_color)
                painter.setFont(opt.font)
                elided = opt.fontMetrics.elidedText(
                    text, Qt.TextElideMode.ElideRight, text_rect.width())
                painter.drawText(text_rect, int(opt.displayAlignment), elided)
                painter.restore()


class PanelGrid(QListView):
    """面板内条目网格（槽位制）。"""

    order_changed = Signal()
    launch_requested = Signal(object)        # Entry
    edit_mode_changed = Signal(bool)

    def __init__(self, item_px: int = 48, icon_cache: IconCache | None = None,
                 edit_press_ms: int = 400, parent=None) -> None:
        super().__init__(parent)
        self.item_px = int(item_px)
        self.icon_cache = icon_cache
        self.panel_model: PanelModel | None = None
        self.edit_mode = False
        self.phase: float = 0.0

        # 关闭原生拖拽（InternalMove 会补位），改用自研槽位拖拽
        self.setViewMode(QListView.ViewMode.IconMode)
        self.setMovement(QListView.Movement.Static)
        self.setDragDropMode(QAbstractItemView.DragDropMode.NoDragDrop)
        self.setDragEnabled(False)
        self.setAcceptDrops(True)
        self.viewport().setAcceptDrops(True)
        self.setResizeMode(QListView.ResizeMode.Adjust)
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.setEditTriggers(QAbstractItemView.EditTrigger.NoEditTriggers)
        self.setFrameShape(QFrame.Shape.NoFrame)
        self.setWrapping(True)
        self.setSpacing(2)
        self._apply_item_px()
        self.setStyleSheet("QListView { background: transparent; border: none; }")
        self.viewport().setAutoFillBackground(False)

        self.delegate = JiggleDelegate(self)
        self.delegate.grid = self
        self.setItemDelegate(self.delegate)

        # -- v1.5 悬停状态（图标 1.08 缩放 + 名字变亮，150ms 缓动） ----------
        # setMouseTracking：无按键时也要收到 MouseMove，否则悬停无法跟随
        self.setMouseTracking(True)
        self.viewport().setMouseTracking(True)
        self.viewport().setAttribute(Qt.WidgetAttribute.WA_Hover, True)
        self._hover_row = -1
        self._hover_progress: dict[int, float] = {}
        self._hover_last = time.monotonic()
        self._hover_timer = QTimer(self)
        self._hover_timer.setInterval(HOVER_FRAME_MS)
        self._hover_timer.timeout.connect(self._hover_tick)
        self.normal_text_color = QColor("#1F2430")
        self.hover_text_color = QColor("#FFFFFF")

        # 手势状态
        self._press_index = QModelIndex()
        self._press_pos = QPoint()          # 全局坐标
        self._suppress_release = False      # QDrag.exec 吞掉释放事件后放行下一次
        self._edit_timer = QTimer(self)
        self._edit_timer.setSingleShot(True)
        self._edit_timer.setInterval(max(200, int(edit_press_ms)))
        self._edit_timer.timeout.connect(self._on_edit_timeout)
        self._jiggle_timer = QTimer(self)
        self._jiggle_timer.setInterval(JIGGLE_INTERVAL_MS)
        self._jiggle_timer.timeout.connect(self._jiggle_tick)
        self._last_launch = ("", 0.0)
        self._suppress_change = False
        self._structure_timer = QTimer(self)
        self._structure_timer.setSingleShot(True)
        self._structure_timer.setInterval(0)
        self._structure_timer.timeout.connect(self._emit_order)
        self._last_emitted_order: list[str | None] = []

        self.viewport().installEventFilter(self)

    # -- 模型 ---------------------------------------------------------------

    def set_model(self, panel_model: PanelModel) -> None:
        """绑定 PanelModel 并连接结构变化信号（兜底顺序检测）。"""
        self.panel_model = panel_model
        self.setModel(panel_model.model)
        self._last_emitted_order = panel_model.serialize_slots()
        panel_model.model.rowsInserted.connect(self._on_rows_changed)
        panel_model.model.rowsRemoved.connect(self._on_rows_changed)

    # -- 悬停效果（v1.5） ----------------------------------------------------

    def set_hover_colors(self, normal_color, hover_color) -> None:
        """设置条目文字的正常/悬停颜色（面板按背景明暗算好传进来）。"""
        self.normal_text_color = QColor(normal_color)
        self.hover_text_color = QColor(hover_color)
        self.delegate.normal_color = self.normal_text_color
        self.delegate.hover_color = self.hover_text_color
        self.viewport().update()

    def set_hover_row(self, row: int, immediate: bool = False) -> None:
        """设置当前悬停行（``-1`` = 没有任何条目悬停）。

        Args:
            row: 行号（越界/负数视为"无悬停"）。
            immediate: True = 立即到位（不缓动），单测/截图对比用。
        """
        row = int(row)
        if row < 0 or (self.panel_model is not None
                       and row >= self.panel_model.model.rowCount()):
            row = -1
        if row == self._hover_row:
            return
        old = self._hover_row
        self._hover_row = row
        if immediate:
            if old >= 0:
                self._hover_progress.pop(old, None)
            if row >= 0:
                self._hover_progress[row] = 1.0
            self.viewport().update()
            return
        if row >= 0:
            self._hover_progress.setdefault(row, 0.0)
        self._hover_last = time.monotonic()
        if not self._hover_timer.isActive():
            self._hover_timer.start()

    def hover_row(self) -> int:
        """当前悬停行（-1 = 无）。"""
        return self._hover_row

    def hover_progress(self, row: int) -> float:
        """该行的悬停进度 0..1（0=常态，1=完全悬停）。"""
        return float(self._hover_progress.get(int(row), 0.0))

    def _hover_tick(self) -> None:
        """逐帧推进悬停进度（150ms 内到位），没有变化就停表。"""
        now = time.monotonic()
        dt = max(0.0, min(0.1, now - self._hover_last))
        self._hover_last = now
        step = (dt * 1000.0) / HOVER_ANIM_MS
        moving = False
        for row in list(self._hover_progress):
            target = 1.0 if row == self._hover_row else 0.0
            current = self._hover_progress[row]
            nxt = (min(target, current + step) if current < target
                   else max(target, current - step))
            if nxt != current:
                moving = True
                self._hover_progress[row] = nxt
            if nxt <= 0.0:
                self._hover_progress.pop(row, None)
        if moving:
            self.viewport().update()
        else:
            self._hover_timer.stop()

    def set_item_px(self, px: int) -> None:
        self.item_px = int(px)
        self._apply_item_px()

    def _apply_item_px(self) -> None:
        self.setIconSize(QSize(self.item_px, self.item_px))
        self.setGridSize(QSize(self.item_px + 42, self.item_px + 34))

    def reload_entries(self, entries: list[Entry], selected_names=(),
                       slots: list[str | None] | None = None) -> None:
        """外部内容刷新重建模型：不触发 order_changed（文件系统增删不写回）。

        slots 给定时按该顺序重建（v1.3 密集语义），否则按条目自然顺序。
        同时把选中状态同步到视图级 selectionModel（PanelModel.rebuild
        只恢复 PanelItem 的模型级标记）。
        """
        if self.panel_model is None or self.icon_cache is None:
            return
        self._suppress_change = True
        try:
            if slots is None:
                self.panel_model.rebuild(entries, self.icon_cache, self.item_px, selected_names)
            else:
                self.panel_model.build_slots(slots, entries, self.icon_cache, self.item_px)
            sm = self.selectionModel()
            if sm is not None:
                sm.clearSelection()
                model = self.panel_model.model
                for row in range(model.rowCount()):
                    item = model.item(row)
                    if item is not None and item.isSelected():
                        idx = model.index(row, 0)
                        if idx.isValid():
                            sm.select(idx, QItemSelectionModel.SelectionFlag.Select)
        finally:
            self._suppress_change = False

    def selected_names(self) -> list[str]:
        """当前选中条目的名字。"""
        sm = self.selectionModel()
        if sm is None:
            return []
        names = []
        for idx in sm.selectedIndexes():
            item = self.panel_model.model.item(idx.row()) if self.panel_model else None
            if item is not None:
                name = item.data(NAME_ROLE) or item.text()
                names.append(str(name))
        return names

    # -- 手势状态机（面板条目） ----------------------------------------------

    def eventFilter(self, obj, ev) -> bool:  # noqa: N802
        if obj is self.viewport() and isinstance(ev, QMouseEvent):
            t = ev.type()
            if t == QMouseEvent.Type.MouseButtonPress and ev.button() == Qt.MouseButton.LeftButton:
                idx = self.indexAt(ev.position().toPoint())
                self._press_index = QModelIndex(idx)
                self._suppress_release = False
                if idx.isValid():
                    self._press_pos = ev.globalPosition().toPoint()
                    if not self.edit_mode:
                        self._edit_timer.start()
                else:
                    self._press_index = QModelIndex()
                    if self.edit_mode:
                        self.exit_edit_mode()  # 点击空白退出编辑态
            elif t == QMouseEvent.Type.MouseMove:
                if not self._press_index.isValid():
                    # 无按键移动 → 更新悬停行（悬停跟随鼠标）
                    idx_hover = self.indexAt(ev.position().toPoint())
                    self.set_hover_row(idx_hover.row() if idx_hover.isValid() else -1)
                if self._press_index.isValid():
                    delta = ev.globalPosition().toPoint() - self._press_pos
                    if abs(delta.x()) > DRAG_THRESHOLD_PX or abs(delta.y()) > DRAG_THRESHOLD_PX:
                        self._edit_timer.stop()
                        self._start_drag()
                        return True  # 本次 move 已被拖拽流程消费
            elif t == QMouseEvent.Type.MouseButtonRelease and ev.button() == Qt.MouseButton.LeftButton:
                if self._suppress_release:
                    # QDrag.exec 后的残留释放事件：吞掉，避免误触发单击启动
                    self._suppress_release = False
                    self._press_index = QModelIndex()
                    return True
                if self._edit_timer.isActive():
                    self._edit_timer.stop()
                if not self.edit_mode and self._press_index.isValid() and self.panel_model:
                    entry = self.panel_model.entry_at(self._press_index.row())
                    if entry is not None:
                        self._maybe_launch(entry)
                self._press_index = QModelIndex()
            elif t == QEvent.Type.Leave:
                self.set_hover_row(-1)
        return super().eventFilter(obj, ev)

    def keyPressEvent(self, event) -> None:  # noqa: N802
        if self.edit_mode and event.key() == Qt.Key.Key_Escape:
            self.exit_edit_mode()
            event.accept()
            return
        super().keyPressEvent(event)

    def _maybe_launch(self, entry: Entry) -> None:
        """单击启动（防双击连发两次）。"""
        now = time.monotonic()
        if entry.name == self._last_launch[0] and (now - self._last_launch[1]) < LAUNCH_DEDUP_SECONDS:
            return
        self._last_launch = (entry.name, now)
        self.launch_requested.emit(entry)

    def _on_edit_timeout(self) -> None:
        """静止按住满阈值 → 进入编辑态（抖动 + 多选）。"""
        if self._press_index.isValid():
            self.enter_edit_mode(select_row=self._press_index.row())

    def enter_edit_mode(self, select_row: int = -1) -> None:
        if self.edit_mode:
            return
        # 先置状态并启动抖动：pythonw 会吞掉槽内异常，若把可能出错的
        # 选中逻辑放在前面，一旦抛异常就会停在"edit_mode=True 但不抖"的
        # 半初始化状态且完全无感。核心反馈（抖动）不被选中逻辑连累。
        self.edit_mode = True
        self.setSelectionMode(QAbstractItemView.SelectionMode.ExtendedSelection)
        self.delegate.edit_mode = True
        self._jiggle_timer.start()
        try:
            if select_row >= 0 and self.panel_model:
                idx = self.panel_model.model.index(select_row, 0)
                if idx.isValid() and self.selectionModel() is not None:
                    # 注意：QAbstractItemView.SelectionFlag 在 PySide6 6.12 不存在，
                    # 选中标志必须用 QItemSelectionModel.SelectionFlag
                    self.selectionModel().select(
                        idx, QItemSelectionModel.SelectionFlag.Select
                    )
        except Exception as exc:  # noqa: BLE001 - 选中失败只记日志，不影响编辑态
            logger.warning("编辑态预选中失败（不影响抖动）: %s", exc)
        finally:
            # 信号放在 finally：语义是"编辑态已完整就绪"（选中已尽力完成），
            # 订阅者不会读到半成品状态
            self.edit_mode_changed.emit(True)

    def exit_edit_mode(self) -> None:
        if not self.edit_mode:
            return
        self.edit_mode = False
        self.setSelectionMode(QAbstractItemView.SelectionMode.SingleSelection)
        self.clearSelection()
        self.delegate.edit_mode = False
        self._jiggle_timer.stop()
        self.viewport().update()
        self.edit_mode_changed.emit(False)

    def _jiggle_tick(self) -> None:
        self.phase += 1.0
        self.delegate.phase = self.phase
        self.viewport().update()

    # -- 自研槽位拖拽 ---------------------------------------------------------

    def _drag_rows(self) -> list[int]:
        """本次拖拽要搬移的行：编辑态且按住选中集合成员 → 整组，否则单行。"""
        if self.panel_model is None:
            return []
        row = self._press_index.row()
        item = self.panel_model.model.item(row)
        if item is None or item.data(HOLE_ROLE):
            return []  # 空位不可拖
        if self.edit_mode:
            sm = self.selectionModel()
            if sm is not None:
                sel = sorted({i.row() for i in sm.selectedIndexes()})
                if row in sel and len(sel) > 1:
                    return sel
        return [row]

    def _start_drag(self) -> None:
        """>4px 位移：启动槽位拖拽（QDrag 模态循环）。"""
        if self.panel_model is None:
            return
        rows = self._drag_rows()
        if not rows:
            return
        first = self.panel_model.model.index(rows[0], 0)
        rect = self.visualRect(first)
        if not rect.isValid():
            return
        pm = self.viewport().grab(rect)
        drag = QDrag(self)
        mime = QMimeData()
        mime.setData(MIME_TYPE, json.dumps(rows).encode("ascii"))
        drag.setMimeData(mime)
        drag.setPixmap(pm)
        local = self.viewport().mapFromGlobal(self._press_pos)
        hot = QPoint(
            max(0, min(rect.width() - 1, local.x() - rect.left())),
            max(0, min(rect.height() - 1, local.y() - rect.top())),
        )
        drag.setHotSpot(hot)
        self._suppress_release = True   # 拖拽结束后的释放事件不再当作单击
        self._press_index = QModelIndex()
        drag.exec(Qt.DropAction.MoveAction)

    def dragEnterEvent(self, event) -> None:  # noqa: N802
        if event.source() is self and event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event) -> None:  # noqa: N802
        if event.source() is self and event.mimeData().hasFormat(MIME_TYPE):
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragLeaveEvent(self, event) -> None:  # noqa: N802
        event.accept()

    def dropEvent(self, event) -> None:  # noqa: N802
        if event.source() is not self or not event.mimeData().hasFormat(MIME_TYPE):
            event.ignore()
            return
        try:
            rows = [int(r) for r in json.loads(
                bytes(event.mimeData().data(MIME_TYPE)).decode("ascii"))]
        except (ValueError, UnicodeDecodeError, TypeError):
            event.ignore()
            return
        idx = self.indexAt(event.position().toPoint())
        target = idx.row() if idx.isValid() else -1   # -1=落在网格外 → 追加
        event.acceptProposedAction()
        self._apply_slot_move(rows, target)

    def _apply_slot_move(self, rows: list[int], target: int) -> None:
        """把拖拽落地成顺序变化（v1.3 密集插入，纯逻辑在 app/slot_ops.apply_move）。

        Args:
            rows: 被搬移的行号（单条=1 个；批量=多个，相对次序保持）。
            target: 落点行号（-1 或网格外 = 追加到末尾）。
        """
        if self.panel_model is None or not rows:
            return
        slots = self.panel_model.serialize_slots()
        new_slots = apply_move(slots, rows, target)
        if new_slots == slots:
            return
        self._suppress_change = True
        try:
            self.panel_model.build_slots(
                new_slots, self.panel_model.entries, self.icon_cache, self.item_px
            )
        finally:
            self._suppress_change = False
        self._emit_order()

    # -- 顺序回写 -------------------------------------------------------------

    def _on_rows_changed(self, *args) -> None:
        """rowsInserted/rowsRemoved 兜底（非拖拽路径的结构变化）。"""
        if self._suppress_change:
            return
        self._structure_timer.start()

    def _emit_order(self) -> None:
        """读取当前槽位序列，与上次不同则发 order_changed（由 controller 落盘）。"""
        if self.panel_model is None:
            return
        slots = self.panel_model.serialize_slots()
        if slots != self._last_emitted_order:
            self._last_emitted_order = list(slots)
            self.order_changed.emit()

    def mousePressEvent(self, event) -> None:  # noqa: N802
        # 保持 QListView 原生行为（选择/框选），拖拽在手势状态机与 dropEvent 里
        super().mousePressEvent(event)
