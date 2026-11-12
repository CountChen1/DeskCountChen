"""PanelModel 单元测试（offscreen Qt，无需真实桌面）。

重点验证：模型行号与 Entry 列表的对应关系。
面板内拖拽重排（InternalMove）只改 QStandardItemModel 的行顺序，
不改 PanelModel.entries —— 两者一旦错位，单击条目会启动错误的程序。

v1.4 追加：显示文字去扩展名，但 NAME_ROLE 必须仍是真实文件名
（排序、.order.json 落盘、启动全部依赖 NAME_ROLE）。
"""

from __future__ import annotations

import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize, Qt  # noqa: E402
from PySide6.QtGui import QPixmap  # noqa: E402

from app.folder_model import Entry  # noqa: E402
from app.panel_model import NAME_ROLE, PanelModel, display_name  # noqa: E402
from tests.qt_support import qt_app  # noqa: E402


class _StubIcons:
    """替身图标缓存：不读真实文件，避免平台相关的取图差异。"""

    def pixmap(self, path, px):  # noqa: D401
        return QPixmap(QSize(int(px), int(px)))

    def default_icon(self, px):
        return QPixmap(QSize(int(px), int(px)))


def make_entry(name: str, kind: str = "link") -> Entry:
    return Entry(name=name, path=Path("C:/fake") / name, kind=kind, norm=name.casefold())


class PanelModelTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        qt_app()

    def setUp(self) -> None:
        self.model = PanelModel()
        self.entries = [make_entry("a.lnk"), make_entry("b.lnk"), make_entry("工具", "dir")]
        self.model.build(self.entries, _StubIcons(), 48)

    def test_build_row_count_and_roles(self) -> None:
        self.assertEqual(self.model.model.rowCount(), 3)
        for row, entry in enumerate(self.entries):
            item = self.model.model.item(row)
            self.assertEqual(item.data(NAME_ROLE), entry.name, "NAME_ROLE 必须是真实文件名")
            # v1.4：显示文字去掉 .lnk/.exe 扩展名（文件夹保持原名）
            self.assertEqual(item.text(), display_name(entry.name, entry.kind))
            self.assertFalse(item.isEditable(), "条目不允许就地编辑")

    def test_serialize_matches_order(self) -> None:
        self.assertEqual(self.model.serialize(), ["a.lnk", "b.lnk", "工具"])

    def test_entry_at_bounds(self) -> None:
        self.assertEqual(self.model.entry_at(0).name, "a.lnk")
        self.assertIsNone(self.model.entry_at(-1))
        self.assertIsNone(self.model.entry_at(99))

    def test_entry_by_name_is_case_insensitive(self) -> None:
        self.assertIsNotNone(self.model.entry_by_name("A.LNK"))
        self.assertIsNone(self.model.entry_by_name("missing.lnk"))

    def test_rebuild_keeps_selection_by_name(self) -> None:
        self.model.rebuild(self.entries, _StubIcons(), 48, selected_names=["b.lnk"])
        selected = [
            self.model.model.item(r).data(NAME_ROLE)
            for r in range(self.model.model.rowCount())
            if self.model.model.item(r).isSelected()
        ]
        self.assertEqual(selected, ["b.lnk"])

    def test_entry_at_follows_model_after_reorder(self) -> None:
        """拖拽重排后，行号 → Entry 的映射必须仍然正确。

        模拟 QListView InternalMove 的效果：把第 0 行搬到末尾。
        """
        taken = self.model.model.takeRow(0)
        self.model.model.appendRow(taken)

        row0_name = self.model.model.item(0).data(NAME_ROLE)
        entry = self.model.entry_at(0)
        self.assertIsNotNone(entry)
        self.assertEqual(
            entry.name,
            row0_name,
            "重排后 entry_at(row) 与模型行不一致：单击条目会启动错误的程序",
        )


class DisplayNameTests(unittest.TestCase):
    """v1.4 名称显示规则（纯函数）。"""

    def test_stem_for_link_and_exe(self) -> None:
        self.assertEqual(display_name("微信.lnk", "link"), "微信")
        self.assertEqual(display_name("QQ.exe", "exe"), "QQ")
        self.assertEqual(display_name("a.b.exe", "exe"), "a.b", "只去掉最后一段扩展名")

    def test_folder_keeps_name(self) -> None:
        self.assertEqual(display_name("工具", "dir"), "工具")
        self.assertEqual(display_name("工作.v2", "dir"), "工作.v2", "文件夹名里的点不是扩展名")

    def test_switch_off_keeps_full_name(self) -> None:
        self.assertEqual(display_name("微信.lnk", "link", strip_extensions=False), "微信.lnk")

    def test_weird_name_falls_back(self) -> None:
        self.assertEqual(display_name(".lnk", "link"), ".lnk", "去扩展名后为空要回退原名")

    def test_stem_for_generic_files(self) -> None:
        """v1.4.1：任意文件同样去扩展名（面板已显示全部文件类型）。"""
        self.assertEqual(display_name("帝国时代笔记.txt", "file"), "帝国时代笔记")
        self.assertEqual(
            display_name("Age of Empires II Definitive Edition.url", "file"),
            "Age of Empires II Definitive Edition",
        )
        self.assertEqual(display_name("manual.pdf", "file"), "manual")
        self.assertEqual(display_name("a.tar.gz", "file"), "a.tar", "只去掉最后一段扩展名")

    def test_file_without_extension_keeps_name(self) -> None:
        self.assertEqual(display_name("LICENSE", "file"), "LICENSE")
        self.assertEqual(display_name(".txt", "file"), ".txt", "去扩展名后为空要回退原名")

    def test_file_kind_respects_switch(self) -> None:
        self.assertEqual(display_name("笔记.txt", "file", strip_extensions=False), "笔记.txt")


class ExtensionStripInModelTests(unittest.TestCase):
    """模型层的显示文字 / tooltip / NAME_ROLE 三者关系。"""

    @classmethod
    def setUpClass(cls) -> None:
        qt_app()

    def _item(self, name: str, kind: str, strip: bool):
        """造一个条目；**model 必须保活**（否则 PanelItem 的 C++ 对象会被回收）。"""
        model = PanelModel(strip_extensions=strip)
        model.build([make_entry(name, kind)], _StubIcons(), 48)
        self._models = getattr(self, "_models", [])
        self._models.append(model)
        return model.model.item(0)

    def test_text_is_stem_but_name_role_is_real(self) -> None:
        item = self._item("微信.lnk", "link", True)
        self.assertEqual(item.text(), "微信")
        self.assertEqual(item.data(NAME_ROLE), "微信.lnk")

    def test_tooltip_shows_real_name_when_different(self) -> None:
        item = self._item("微信.lnk", "link", True)
        self.assertEqual(item.toolTip(), "微信.lnk")

    def test_no_tooltip_when_text_equals_name(self) -> None:
        item = self._item("工具", "dir", True)
        self.assertEqual(item.text(), "工具")
        self.assertEqual(item.toolTip(), "", "显示名与真实名相同时不必重复提示")

    def test_switch_off_keeps_extension_in_text(self) -> None:
        item = self._item("微信.lnk", "link", False)
        self.assertEqual(item.text(), "微信.lnk")
        self.assertEqual(item.data(NAME_ROLE), "微信.lnk")

    def test_serialize_still_uses_real_names(self) -> None:
        """去扩展名只影响显示：落盘/排序用的名字必须还是真实文件名。"""
        model = PanelModel()
        entries = [make_entry("a.lnk"), make_entry("b.exe")]
        model.build(entries, _StubIcons(), 48)
        self.assertEqual(model.serialize(), ["a.lnk", "b.exe"])
        self.assertEqual([model.model.item(r).text() for r in range(2)], ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
