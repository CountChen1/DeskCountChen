"""把 Entry 列表构建成 QStandardItemModel（图标/文字/自定义 Role 携带真实信息）。

v1.4 新增（面板内名称去扩展名）：
- 显示文字 = 文件名去扩展名（``a.lnk`` → ``a``、``QQ.exe`` → ``QQ``），
  文件夹保持原样；**只影响 ``item.text()``**；
- v1.4.1 起对**所有非目录条目**生效（``帝国时代笔记.txt`` → ``帝国时代笔记``、
  ``xxx.url`` → ``xxx``），因为面板已显示全部文件类型；
- ``NAME_ROLE`` 仍存真实文件名：排序、``.order.json`` 落盘、启动定位全部走
  NAME_ROLE，所以"显示名"与"真实名"不一致不会影响任何持久化/启动逻辑；
- 显示名与真实名不同时才设 tooltip（悬停可看完整文件名）；
- ``strip_extensions=False`` 时完全保持 v1.3 行为（显示真实文件名）。
"""

from __future__ import annotations

import logging
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon, QStandardItem, QStandardItemModel

from app.folder_model import Entry
from app.icon_provider import IconCache
from app.slot_ops import densify

logger = logging.getLogger(__name__)

NAME_ROLE = int(Qt.ItemDataRole.UserRole) + 0        # 真实文件名（排序回写用）
PATH_ROLE = int(Qt.ItemDataRole.UserRole) + 1        # 绝对路径字符串
KIND_ROLE = int(Qt.ItemDataRole.UserRole) + 2        # "dir"/"link"/"exe"/"file"
HOLE_ROLE = int(Qt.ItemDataRole.UserRole) + 3        # True=空位占位行（不绘制、不可选）
SELECTED_ROLE = int(Qt.ItemDataRole.UserRole) + 9    # 无视图时的选中标记

# v1.4.1：所有**非目录**条目都去扩展名（.lnk/.exe 之外还有 .txt/.url/.pdf…）。
# 文件夹保留原名。
# 已知取舍：文件名本身带点但没有扩展名的（如 ``v1.4``）会被截成 ``v1``——
# tooltip 与 NAME_ROLE 仍是真实名，可接受；要改的话必须同时改落盘/启动口径。
_STRIP_KINDS = ("link", "exe", "file")


def display_name(name: str, kind: str, strip_extensions: bool = True) -> str:
    """条目在面板里显示的文字（纯函数，便于单测）。

    Args:
        name: 真实文件名（含扩展名）。
        kind: ``"dir" | "link" | "exe" | "file"``。
        strip_extensions: 是否去扩展名。

    Returns:
        显示文字。去扩展名后为空（如 ``".lnk"`` 这种怪名）时回退原名。
    """
    text = str(name or "")
    if not strip_extensions or kind not in _STRIP_KINDS:
        return text
    stem = Path(text).stem
    return stem or text


def _make_hole(ghost_name: str = "") -> PanelItem:
    """空位占位行：只保留 ItemIsEnabled（可作拖放目标，不可选中/不可启动）。

    ghost_name 非空时携带幽灵记录名（磁盘上已不存在的槽位记录），
    序列化时随槽位一起写回，保证"拖几次都不丢记录"。
    """
    item = PanelItem("")
    if ghost_name:
        item.setData(ghost_name, NAME_ROLE)
    item.setData(True, HOLE_ROLE)
    item.setFlags(Qt.ItemFlag.ItemIsEnabled)
    return item


class PanelItem(QStandardItem):
    """带选中标记的条目。

    QStandardItem 没有 setSelected/isSelected（Qt C++ 亦无）——选中状态
    本属于视图的 QItemSelectionModel。面板刷新重建模型时视图可能尚未
    重新绑定，需要"模型级"的选中标记；这里用自定义 Role 承载并补齐
    同名方法。真实视图选中仍由 PanelGrid 的 selectionModel 管理。
    """

    def setSelected(self, value: bool) -> None:
        self.setData(bool(value), SELECTED_ROLE)

    def isSelected(self) -> bool:
        return bool(self.data(SELECTED_ROLE))


class PanelModel:
    """面板网格的数据模型包装（非 QObject，生命周期随 PanelGrid）。"""

    def __init__(self, strip_extensions: bool = True) -> None:
        """
        Args:
            strip_extensions: True = 条目显示名去掉扩展名（.lnk/.exe/任意文件）
                （NAME_ROLE 仍是真实文件名，不影响排序与启动）。
        """
        self.model = QStandardItemModel()
        self.entries: list[Entry] = []
        self.strip_extensions = bool(strip_extensions)

    def build(self, entries: list[Entry], icon_cache, item_px: int) -> None:
        """重建模型内容（密集顺序，无空位）；图标取图失败时逐条兜底。"""
        self._fill([e.name for e in entries], entries, icon_cache, item_px)

    def build_slots(self, slots: list[str | None], entries: list[Entry],
                    icon_cache, item_px: int) -> None:
        """按顺序重建模型（v1.3 密集语义）。

        入参先 densify（去 None/空串）：空位体系已废除，任何边界进来的
        历史空位记录都在这里被挡住，保证模型里永远没有占位行——面板
        不会再出现"越拖越散"的大片留白。幽灵名同样被丢弃。
        """
        self._fill(densify(list(slots)), entries, icon_cache, item_px)

    def _fill(self, slots: list[str | None], entries: list[Entry],
              icon_cache, item_px: int) -> None:
        """按槽位序列逐行填充模型（slots 与 entries 是 build/build_slots 的公共实现）。"""
        self.entries = list(entries)
        if self.model.rowCount():
            self.model.removeRows(0, self.model.rowCount())
        by_norm: dict[str, Entry] = {}
        for e in entries:
            by_norm.setdefault(e.norm, e)
        for s in slots:
            e = by_norm.get(str(s).casefold()) if s else None
            if e is None:
                # 空位 / 幽灵记录（磁盘上已不存在的名字）
                self.model.appendRow(_make_hole(str(s) if s else ""))
                continue
            try:
                pm = icon_cache.pixmap(e.path, item_px)
            except Exception as exc:  # noqa: BLE001
                logger.warning("条目取图失败 %s: %s", e.path, exc)
                pm = icon_cache.default_icon(item_px)
            # 显示文字与真实名分离：NAME_ROLE/PATH_ROLE/KIND_ROLE 才是权威数据
            shown = display_name(e.name, e.kind, self.strip_extensions)
            item = PanelItem(shown)
            item.setEditable(False)
            item.setIcon(QIcon(pm))
            item.setData(e.name, NAME_ROLE)
            item.setData(str(e.path), PATH_ROLE)
            item.setData(e.kind, KIND_ROLE)
            if shown != e.name:
                item.setToolTip(e.name)
            self.model.appendRow(item)

    def rebuild(self, entries: list[Entry], icon_cache, item_px: int,
                selected_names: list[str] | tuple = ()) -> None:
        """重建并按名字恢复选中标记（内容实时刷新时避免闪烁）。

        注意：这里恢复的是 PanelItem 的模型级标记；视图级选中由
        PanelGrid.reload_entries 通过 selectionModel 同步。
        """
        keep = {n.casefold() for n in selected_names}
        self.build(entries, icon_cache, item_px)
        if not keep:
            return
        for row in range(self.model.rowCount()):
            name = self.model.item(row).data(NAME_ROLE)
            if name and name.casefold() in keep:
                self.model.item(row).setSelected(True)  # PanelItem

    def serialize(self) -> list[str]:
        """输出当前可视顺序（NAME_ROLE 序列，不含空位）。"""
        return [n for n in self.serialize_slots() if n]

    def serialize_slots(self) -> list[str | None]:
        """输出当前槽位序列（None=空位；幽灵名随占位行一起输出）。"""
        out: list[str | None] = []
        for row in range(self.model.rowCount()):
            name = self.model.item(row).data(NAME_ROLE)
            out.append(str(name) if name else None)
        return out

    def entry_at(self, row: int) -> Entry | None:
        """按模型行号取 Entry。

        关键：InternalMove 拖拽只重排模型行、不动 self.entries，
        所以必须以模型行的 NAME_ROLE 反查 Entry，保证"行号 → 条目"
        映射始终与用户看到的顺序一致（否则单击会启动错误的程序）。
        """
        if 0 <= row < self.model.rowCount():
            name = self.model.item(row).data(NAME_ROLE)
            if name:
                return self.entry_by_name(str(name))
        return None

    def entry_by_name(self, name: str) -> Entry | None:
        """按 casefold 名字取 Entry。"""
        key = name.casefold()
        for e in self.entries:
            if e.norm == key:
                return e
        return None
