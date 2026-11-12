"""密集语义归一化（v1.3）单元测试。

背景（用户最终决策）：v1.1~v1.2 的"空位"方案在实测中反复引发"越拖越散"，
v1.3 整体废除空位体系，完全对齐手机桌面文件夹：
    - 落盘（OrderStore.save_slots）只写密集名单（去 None/空串/重复）；
    - 加载（OrderStore.load_slots / FolderModel._merge_slots /
      PanelModel.build_slots）对历史脏文件（空位 null、幽灵名）即读即归一，
      无需迁移脚本；
    - order 与 slots 字段内容相同（旧版工具兼容）。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402

from app.folder_model import Entry, FolderModel  # noqa: E402
from app.order_store import ORDER_FILE_NAME, OrderStore  # noqa: E402
from app.panel_model import PanelModel  # noqa: E402
from app.slot_ops import densify  # noqa: E402
from tests.qt_support import qt_app  # noqa: E402


def slots(*values) -> list:
    """紧凑写法：空字符串 = 空位 None（模拟历史脏数据用）。"""
    return [None if v == "" else v for v in values]


def make_entry(name: str, kind: str = "link") -> Entry:
    return Entry(name=name, path=Path("C:/fake") / name, kind=kind, norm=name.casefold())


class _StubIcons:
    """替身图标缓存：不读真实文件，平台无关。"""

    def pixmap(self, path, px):  # noqa: D401
        return QPixmap(QSize(int(px), int(px)))

    def default_icon(self, px):
        return QPixmap(QSize(int(px), int(px)))


class DensifyTests(unittest.TestCase):
    def test_pure_function_basic(self) -> None:
        self.assertEqual(densify(slots("", "", "A", "B")), ["A", "B"])

    def test_internal_and_leading_all_removed(self) -> None:
        """v1.3 不再区分前导/内部：所有空位一律去掉。"""
        self.assertEqual(densify(slots("", "A", "", "", "B")), ["A", "B"])

    def test_ghost_is_just_a_name(self) -> None:
        """densify 只认"非空"：幽灵名是否丢弃由合并层按磁盘条目决定。"""
        self.assertEqual(densify(slots("ghost.lnk", "", "A")), ["ghost.lnk", "A"])

    def test_input_not_mutated(self) -> None:
        original = slots("", "A", "B")
        densify(original)
        self.assertEqual(original, slots("", "A", "B"))


class SaveSlotsTests(unittest.TestCase):
    """落盘只写密集名单；重复名去重；order 与 slots 内容相同。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_dense_")
        self.dir = Path(self._tmp.name)
        self.store = OrderStore(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def on_disk(self) -> list:
        data = json.loads((self.dir / ORDER_FILE_NAME).read_text(encoding="utf-8"))
        return data["slots"]

    def test_holes_dropped_on_save(self) -> None:
        written = self.store.save_slots(slots("", "", "A", "", "B", ""))
        self.assertEqual(written, ["A", "B"])
        self.assertEqual(self.on_disk(), ["A", "B"])

    def test_duplicates_dropped(self) -> None:
        written = self.store.save_slots(["A", "a", "B"])
        self.assertEqual(written, ["A", "B"], "重复名（大小写不敏感）只保留第一个")

    def test_relative_order_kept_on_save(self) -> None:
        names = ["a.lnk", "b.exe", "c.lnk", "d.exe"]
        self.store.save_slots([None, None] + names)
        self.assertEqual(self.on_disk(), names)

    def test_order_field_mirrors_slots(self) -> None:
        self.store.save_slots(["A", "B"])
        data = json.loads((self.dir / ORDER_FILE_NAME).read_text(encoding="utf-8"))
        self.assertEqual(data["order"], ["A", "B"])
        self.assertEqual(data["slots"], ["A", "B"])

    def test_return_value_matches_disk(self) -> None:
        written = self.store.save_slots(slots("", "A", "A", "", "B", ""))
        self.assertEqual(written, self.on_disk())
        self.assertEqual(written, ["A", "B"])

    def test_roundtrip_is_stable(self) -> None:
        """写 → 读 → 再写 幂等。"""
        self.store.save_slots(slots("", "A", "", "B"))
        first = self.store.load_slots()
        self.store.save_slots(first)
        self.assertEqual(self.store.load_slots(), first)


class LoadPathTests(unittest.TestCase):
    """加载归一化：历史脏数据（空位/幽灵）下次启动自愈（无迁移脚本）。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_dense_load_")
        self.dir = Path(self._tmp.name)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def write_raw(self, slots_value: list) -> None:
        (self.dir / ORDER_FILE_NAME).write_text(
            json.dumps({"version": 1,
                        "order": [s for s in slots_value if s],
                        "slots": slots_value}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_load_slots_drops_holes(self) -> None:
        self.write_raw(slots("", "A", "", "B"))
        self.assertEqual(OrderStore(self.dir).load_slots(), ["A", "B"])

    def test_merge_slots_drops_holes_and_ghosts(self) -> None:
        entries = [make_entry("A"), make_entry("B")]
        merged = FolderModel._merge_slots(entries, slots("", "A", "ghost.lnk", "", "B"))
        self.assertEqual(merged, ["A", "B"], "空位与幽灵名不再占格")

    def test_merge_slots_appends_new_entries_sorted(self) -> None:
        entries = [make_entry("A"), make_entry("B"), make_entry("C")]
        merged = FolderModel._merge_slots(entries, ["B"])
        self.assertEqual(merged, ["B", "A", "C"])

    def test_reload_heals_dirty_file(self) -> None:
        """真实脏数据形态：25 槽、17 个前导空位、只剩 4 个图标 → 加载即密集。"""
        names = ["a.lnk", "b.exe", "c.lnk", "d.exe"]
        for name in names:
            (self.dir / name).write_bytes(b"")
        dirty = [None] * 17 + names + [None] * 4
        self.write_raw(dirty)

        fm = FolderModel(self.dir)
        fm.reload()
        self.assertEqual(fm.slots, names, "空位全部消失、顺序不变")
        self.assertEqual([e.name for e in fm.entries], names)
        # 脏文件本身不改（加载只影响内存）；下一次保存才写回干净布局
        self.assertEqual(json.loads((self.dir / ORDER_FILE_NAME)
                                    .read_text(encoding="utf-8"))["slots"], dirty)

    def test_reload_then_save_writes_dense_layout(self) -> None:
        names = ["a.lnk", "b.exe", "c.lnk"]
        for name in names:
            (self.dir / name).write_bytes(b"")
        self.write_raw([None] * 5 + names + ["ghost.lnk"])

        fm = FolderModel(self.dir)
        fm.reload()
        fm.save_slots(fm.slots)
        disk = json.loads((self.dir / ORDER_FILE_NAME).read_text(encoding="utf-8"))["slots"]
        self.assertEqual(disk, names, "落盘后文件为密集形态，幽灵名不再保留")
        self.assertEqual(fm.slots, disk, "内存与磁盘必须一致")


class PanelBuildTests(unittest.TestCase):
    """视图侧同样归一：任何边界进来的空位记录都被挡住，模型无占位行。"""

    @classmethod
    def setUpClass(cls) -> None:
        qt_app()

    def test_build_slots_drops_holes(self) -> None:
        entries = [make_entry("A"), make_entry("B")]
        model = PanelModel()
        model.build_slots(slots("", "", "A", "", "B"), entries, _StubIcons(), 48)
        self.assertEqual(model.serialize_slots(), ["A", "B"])
        self.assertEqual(model.model.rowCount(), 2, "不允许出现占位行")

    def test_build_slots_all_holes_yields_empty_model(self) -> None:
        model = PanelModel()
        model.build_slots(slots("", "", ""), [], _StubIcons(), 48)
        self.assertEqual(model.model.rowCount(), 0)

    def test_build_slots_dense_is_identity(self) -> None:
        entries = [make_entry("A"), make_entry("B")]
        model = PanelModel()
        model.build_slots(["A", "B"], entries, _StubIcons(), 48)
        self.assertEqual(model.serialize_slots(), ["A", "B"])
        self.assertEqual(model.model.rowCount(), 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
