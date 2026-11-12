"""分类目录扫描 + .order.json 合并排序 + 实时刷新（watcher + 轮询）。"""

from __future__ import annotations

import logging
import os
from dataclasses import dataclass
from functools import cmp_to_key
from pathlib import Path

from PySide6.QtCore import QObject, QTimer, QFileSystemWatcher, Signal

from app.order_store import OrderStore
from app.win32 import window_ops

logger = logging.getLogger(__name__)

# v1.4.1：不再按扩展名白名单过滤（原 ALLOWED_SUFFIXES = {".lnk", ".exe"} 已作废）。
# 用户要求"文件夹里有什么就能在面板里点到"——文件夹即数据，所以只排除
# 程序自己的元数据与 Windows 系统文件。隐藏属性的文件/目录已被 _is_hidden 挡掉，
# 这里再显式挡一次，防止用户手工去掉了隐藏属性。
IGNORED_NAMES = {".order.json", "desktop.ini", "thumbs.db", ".ds_store"}
# 程序自己写盘时用的临时文件前缀（原子写残留），必须一并忽略
IGNORED_PREFIXES = (".order.json.tmp",)
REFRESH_DEBOUNCE_MS = 150
FILE_ATTRIBUTE_HIDDEN = 0x2  # 与 Win32 FILE_ATTRIBUTE_HIDDEN 一致


def _is_ignored(name: str) -> bool:
    """是否属于"永不进面板"的名字（大小写不敏感）。

    ``_scan_entries`` 与 ``_scan_signature`` **必须**用同一判据，否则会出现
    "签名认为变了、扫描结果没变"（或反之）的无效刷新 —— 两个方法的口径
    要同步维护（改动其中一个时请看另一个的 docstring）。
    """
    folded = str(name).casefold()
    return folded in IGNORED_NAMES or folded.startswith(IGNORED_PREFIXES)


def _is_hidden(st: os.stat_result) -> bool:
    """Windows 隐藏属性判定（非 Windows 平台恒 False）。

    隐藏文件/目录不进入面板（与资源管理器默认显示规则一致）。
    """
    return bool(getattr(st, "st_file_attributes", 0) & FILE_ATTRIBUTE_HIDDEN)


@dataclass
class Entry:
    """面板内一个条目（真实文件系统对象）。"""

    name: str       # 真实文件名（os.startfile 用）
    path: Path      # 绝对路径
    kind: str       # "dir" | "link" | "exe" | "file"
    norm: str       # casefold 名，用于去重匹配

    def is_dir(self) -> bool:
        return self.kind == "dir"


class FolderModel(QObject):
    """一个分类的数据目录模型。

    每次刷新：scandir 过滤（收全部条目，仅排除本程序元数据与系统文件）
    → 与 .order.json 合并排序。
    实时刷新 = QFileSystemWatcher + 1s 轮询签名比对，150ms 去抖。
    """

    changed = Signal()  # 目录内容真正发生变化（去抖后）

    def __init__(self, dir_path: Path, poll_ms: int = 1000, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.dir = Path(dir_path)
        self.entries: list[Entry] = []
        self.order_names: list[str] = []
        self.slots: list[str] = []   # 密集顺序（v1.3，无空位）；权威排序
        self._watching = False
        self._signature: tuple = ()
        self._scanning = False

        self._watcher = QFileSystemWatcher(self)
        self._watcher.directoryChanged.connect(self._on_dir_changed)

        self._poll = QTimer(self)
        self._poll.setInterval(max(500, int(poll_ms)))
        self._poll.timeout.connect(self._poll_tick)

        self._debounce = QTimer(self)
        self._debounce.setSingleShot(True)
        self._debounce.setInterval(REFRESH_DEBOUNCE_MS)
        self._debounce.timeout.connect(self._refresh_now)

    # -- 扫描与合并 --------------------------------------------------------

    def _scan_entries(self) -> list[Entry]:
        """扫描目录，收全部条目（目录/快捷方式/程序/任意文件）；单条失败不拖垮整体。

        v1.4.1 起**不再按扩展名过滤**：.txt/.url/.pdf/.docx 等一律显示，
        只有 ``IGNORED_NAMES``（程序自身元数据 + Windows 系统文件）不进来。
        """
        try:
            names = os.listdir(self.dir)
        except OSError as exc:
            logger.warning("扫描目录失败 %s: %s", self.dir, exc)
            return []
        entries: list[Entry] = []
        for name in names:
            if _is_ignored(name):
                continue
            path = self.dir / name
            try:
                st = path.stat()
                is_dir = path.is_dir()
            except OSError:
                continue
            if _is_hidden(st):
                continue
            suffix = Path(name).suffix.lower()
            if is_dir:
                kind = "dir"
            elif suffix == ".lnk":
                kind = "link"
            elif suffix == ".exe":
                kind = "exe"
            else:
                # 任意文件（.txt/.url/.pdf/…）：原先这里 continue，导致被整类丢弃
                kind = "file"
            entries.append(Entry(name=name, path=path, kind=kind, norm=name.casefold()))
        return entries

    def _scan_signature(self) -> tuple:
        """轻量目录签名（不读内容），用于轮询比对。

        与 :meth:`_scan_entries` 的口径关系（**改任何一边都要同步看另一边**）：

        - **忽略项判据必须完全一致**（同一个 ``_is_ignored``）：签名比扫描宽会
          出现"文件变了但不刷新"（极难查），比扫描窄会出现"刷新了但条目没变"
          （多一次无害重建）。这一条是硬约束。
        - **隐藏属性只在 entries 侧判定**：签名是**超集探测器**，隐藏文件变化
          也会让签名变 → 多一次无害刷新（``reload()`` 结果不变）。刻意不做隐藏
          判定，因为 ``stat`` 已经拿到了，省这一次几乎没有收益，反而容易让两边
          判据漂移。
        """
        try:
            names = os.listdir(self.dir)
        except OSError:
            return ()
        sig = []
        for name in names:
            if _is_ignored(name):
                continue
            try:
                st = (self.dir / name).stat()
                sig.append((name, (self.dir / name).is_dir(), st.st_size, st.st_mtime_ns))
            except OSError:
                sig.append((name, None, 0, 0))
        return tuple(sorted(sig))

    @staticmethod
    def _merge(entries: list[Entry], order_names: list[str]) -> list[Entry]:
        """纯函数合并：记录序优先，未记录的新条目按自然排序排末尾。

        - 匹配大小写不敏感（casefold）；
        - 记录里有、磁盘上没有 → 静默跳过（记录保留在文件里）；
        - 同名冲突（不可能真发生）先出现者胜。
        """
        by_norm: dict[str, Entry] = {}
        for e in entries:  # 先出现者胜
            by_norm.setdefault(e.norm, e)

        result: list[Entry] = []
        used: set[str] = set()
        for n in order_names:
            if not isinstance(n, str):
                continue
            k = n.casefold()
            e = by_norm.get(k)
            if e is not None and k not in used:
                result.append(e)
                used.add(k)
            # 记录里有、磁盘上没有 → 跳过（不显示）
        rest = [e for e in entries if e.norm not in used]
        rest.sort(key=cmp_to_key(lambda a, b: window_ops.logical_name_cmp(a.name, b.name)))
        return result + rest

    @staticmethod
    def _merge_slots(entries: list[Entry], raw_slots: list[str | None]) -> list[str]:
        """密集顺序合并（v1.3，手机桌面语义：无空位、无幽灵占格）。

        - 记录名匹配磁盘条目 → 用磁盘规范名落位（大小写不敏感）；
        - 记录名磁盘上没有（幽灵）/ 空位（None）→ 一律丢弃，不再占用格子
          （v1.2 的空位体系整体废除；历史脏数据加载即自愈，无需迁移脚本）；
        - 重复名 → 只保留第一个（防御）；
        - 未记录的新条目按自然排序排在末尾。
        """
        by_norm: dict[str, Entry] = {}
        for e in entries:  # 先出现者胜
            by_norm.setdefault(e.norm, e)
        slots: list[str] = []
        used: set[str] = set()
        for s in raw_slots:
            if not s:
                continue
            e = by_norm.get(str(s).casefold())
            if e is not None and e.norm not in used:
                slots.append(e.name)
                used.add(e.norm)
            # 空位 / 幽灵 / 重复 → 丢弃（密集语义）
        rest = [e for e in entries if e.norm not in used]
        rest.sort(key=cmp_to_key(lambda a, b: window_ops.logical_name_cmp(a.name, b.name)))
        slots.extend(e.name for e in rest)
        return slots

    def reload(self) -> list[Entry]:
        """重新扫描并合并；返回合并后的条目列表（self.slots 为密集顺序）。"""
        self._signature = self._scan_signature()
        entries = self._scan_entries()
        raw_slots = OrderStore(self.dir).load_slots()
        self.order_names = list(raw_slots)
        self.slots = self._merge_slots(entries, raw_slots)
        by_norm: dict[str, Entry] = {}
        for e in entries:
            by_norm.setdefault(e.norm, e)
        self.entries = [
            by_norm[s.casefold()] for s in self.slots
            if s.casefold() in by_norm
        ]
        return self.entries

    def save_slots(self, slots: list[str | None]) -> None:
        """把当前顺序写回 .order.json（v1.3 密集语义，OrderStore 负责归一化）。

        **内存顺序以落盘结果为准**（OrderStore.save_slots 会去空位、去重，
        并把真正写进文件的名单返回）——否则内存与磁盘漂移，下一次拖拽
        又把旧状态写回去。
        """
        normalized = OrderStore(self.dir).save_slots(list(slots))
        self.slots = list(normalized)
        self.order_names = list(normalized)

    def save_order(self, visible_names: list[str]) -> None:
        """把当前可视顺序写回 .order.json（stale 保留逻辑在 OrderStore）。

        关键：写完后内存记录必须与文件内容一致（visible + stale），
        否则连续第二次拖拽时 old_order 缺 stale，ghost 记录会被抹掉。
        """
        old = list(self.order_names)
        OrderStore(self.dir).save(list(visible_names), old)
        visible_keys = {v.casefold() for v in visible_names}
        stale = [n for n in old if isinstance(n, str) and n.casefold() not in visible_keys]
        self.order_names = list(visible_names) + stale

    # -- 实时刷新 ----------------------------------------------------------

    def start_watch(self) -> None:
        """开始监听目录变化（幂等）。"""
        if self._watching:
            return
        self._signature = self._scan_signature()
        try:
            if self.dir.exists():
                self._watcher.addPath(str(self.dir))
        except OSError as exc:
            logger.warning("QFileSystemWatcher addPath 失败 %s: %s", self.dir, exc)
        self._poll.start()
        self._watching = True

    def stop_watch(self) -> None:
        """停止监听（幂等）。"""
        if not self._watching:
            return
        if str(self.dir) in self._watcher.directories():
            try:
                self._watcher.removePath(str(self.dir))
            except (OSError, RuntimeError) as exc:
                logger.warning("QFileSystemWatcher removePath 失败 %s: %s", self.dir, exc)
        self._poll.stop()
        self._debounce.stop()
        self._watching = False

    def _on_dir_changed(self, _path: str) -> None:
        self._debounce.start()

    def _poll_tick(self) -> None:
        sig = self._scan_signature()
        if sig != self._signature:
            self._signature = sig
            self._debounce.start()

    def _refresh_now(self) -> None:
        """去抖到期：重新加载并发 changed。

        走到这里的触发源（watcher 回调 / 轮询签名比对）本身就代表发生过
        变化；即使罕见竞态下签名相同，重复通知也无害（controller 侧重建
        面板代价很低），因此总是通知。
        """
        self.reload()
        self.changed.emit()
