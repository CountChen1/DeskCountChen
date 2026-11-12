"""FolderModel 的扫描过滤 + _merge 合并排序单元测试（纯逻辑，无需 GUI）。

验收判据（红线 5）：
- 记录序优先、未记录的新条目按名称（StrCmpLogicalW）排末尾；
- 记录里已不存在的 ghost 不显示，但记录保留（save 侧）；
- 大小写不敏感；
- .order.json / desktop.ini / thumbs.db / 隐藏文件被过滤；
- 文件缺失或 JSON 损坏 → 按名排序，不崩溃不删文件。
"""

from __future__ import annotations

import json
import sys
import tempfile
import types
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.folder_model import Entry, FolderModel  # noqa: E402
from app.order_store import OrderStore  # noqa: E402
from tests.qt_support import clear_hidden, make_hidden  # noqa: E402


class _TempDirCase(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_merge_")
        self.dir = Path(self._tmp.name)
        self.fm = FolderModel(self.dir)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 造数据（不依赖 QObject 状态，直接调 _scan_entries） -------------------
    def touch_file(self, name: str) -> Path:
        p = self.dir / name
        p.write_bytes(b"")
        return p

    def touch_dir(self, name: str) -> Path:
        p = self.dir / name
        p.mkdir()
        return p

    def scan(self) -> list[Entry]:
        return FolderModel._scan_entries(types.SimpleNamespace(dir=self.dir))

    def names(self, order_names=()) -> list[str]:
        return [e.name for e in FolderModel._merge(self.scan(), list(order_names))]


class ScanFilterTests(_TempDirCase):
    def test_allowed_kinds(self) -> None:
        self.touch_file("a.lnk")
        self.touch_file("b.exe")
        self.touch_dir("工具")
        self.assertEqual(sorted(self.names()), sorted(["a.lnk", "b.exe", "工具"]))

    def test_all_file_types_included(self) -> None:
        """v1.4.1 语义变更：不再按扩展名白名单过滤，任意文件都进面板。

        背景（用户明确要求"全部显示"）：白名单 {.lnk, .exe} 会把
        ``帝国时代笔记.txt`` / ``Age of Empires II Definitive Edition.url``
        整类丢掉，导致"文件夹里 27 项、面板只有 25 项"。
        """
        for name in ("readme.txt", "data.xlsx", "note.md", "x.bat", "site.url", "pic.png"):
            self.touch_file(name)
        self.assertEqual(
            sorted(self.names()),
            sorted(["readme.txt", "data.xlsx", "note.md", "x.bat", "site.url", "pic.png"]),
        )

    def test_non_special_suffix_kind_is_file(self) -> None:
        """除 .lnk/.exe/目录以外的条目，kind 必须是 "file"（供去扩展名逻辑判断）。"""
        self.touch_file("帝国时代笔记.txt")
        self.touch_file("Age of Empires II Definitive Edition.url")
        self.touch_file("manual.pdf")
        by_name = {e.name: e for e in self.scan()}
        for name in ("帝国时代笔记.txt", "Age of Empires II Definitive Edition.url",
                     "manual.pdf"):
            self.assertEqual(by_name[name].kind, "file", name)
        self.assertFalse(by_name["manual.pdf"].is_dir())

    def test_order_json_excluded(self) -> None:
        self.touch_file(".order.json")
        self.touch_file(".order.json.tmp.1234")
        self.assertEqual(self.names(), [])

    def test_system_files_excluded(self) -> None:
        """desktop.ini / thumbs.db 不应出现在面板里。"""
        self.touch_file("desktop.ini")
        self.touch_file("Thumbs.db")
        self.touch_file("thumbs.db")
        self.assertEqual(self.names(), [])

    def test_kind_and_norm_fields(self) -> None:
        self.touch_file("A.LNK")
        self.touch_dir("Tool")
        by_name = {e.name: e for e in self.scan()}
        self.assertEqual(by_name["A.LNK"].kind, "link")
        self.assertEqual(by_name["Tool"].kind, "dir")
        self.assertEqual(by_name["A.LNK"].norm, "a.lnk")
        self.assertTrue(by_name["Tool"].is_dir())

    def test_hidden_file_with_allowed_suffix_excluded(self) -> None:
        """红线 5：带隐藏属性的文件也必须过滤（当前实现可能未覆盖）。"""
        hidden_link = self.touch_file("hidden.lnk")
        make_hidden(hidden_link)
        try:
            self.assertNotIn("hidden.lnk", self.names(), "隐藏属性的 .lnk 不应显示")
        finally:
            clear_hidden(hidden_link)

    def test_hidden_directory_excluded(self) -> None:
        """红线 5：带隐藏属性的目录也必须过滤。"""
        hidden_dir = self.touch_dir("hidden_dir")
        make_hidden(hidden_dir)
        try:
            self.assertNotIn("hidden_dir", self.names(), "隐藏属性的目录不应显示")
        finally:
            clear_hidden(hidden_dir)


class MergeOrderTests(_TempDirCase):
    def test_record_order_first_then_new_by_name(self) -> None:
        for name in ("a.lnk", "b.lnk", "c.lnk"):
            self.touch_file(name)
        self.assertEqual(self.names(["b.lnk", "a.lnk"]), ["b.lnk", "a.lnk", "c.lnk"])

    def test_new_entries_natural_sort(self) -> None:
        """StrCmpLogicalW 语义：9 在 10 之前，alpha 在 Zeta 之前。"""
        for name in ("10.lnk", "9.lnk", "alpha.lnk", "Zeta.exe"):
            self.touch_file(name)
        self.assertEqual(self.names(), ["9.lnk", "10.lnk", "alpha.lnk", "Zeta.exe"])

    def test_ghost_recorded_name_not_displayed(self) -> None:
        self.touch_file("a.lnk")
        self.touch_file("b.lnk")
        self.assertEqual(self.names(["b.lnk", "ghost.lnk", "a.lnk"]), ["b.lnk", "a.lnk"])

    def test_case_insensitive_match(self) -> None:
        self.touch_file("aaa.lnk")
        self.assertEqual(self.names(["AAA.LNK"]), ["aaa.lnk"])

    def test_duplicate_record_names_yield_single_entry(self) -> None:
        self.touch_file("a.lnk")
        self.assertEqual(self.names(["a.lnk", "A.LNK", "a.LNK"]), ["a.lnk"])

    def test_empty_dir_and_empty_record(self) -> None:
        self.assertEqual(self.names(), [])

    def test_record_of_only_ghosts(self) -> None:
        self.touch_file("a.lnk")
        self.assertEqual(self.names(["ghost1.lnk", "ghost2.exe"]), ["a.lnk"])


class GhostSurvivesRepeatedSaveTests(_TempDirCase):
    """回归用例 A（架构师复核提出）：ghost 记录必须扛住"连续两次拖拽写回"。"""

    def write_order_file(self, names: list[str]) -> None:
        """手写 .order.json（隐藏属性会让 open('w') 被拒，先清属性）。"""
        path = self.dir / ".order.json"
        if path.exists():
            clear_hidden(path)
        path.write_text(json.dumps({"version": 1, "order": names}, ensure_ascii=False),
                        encoding="utf-8")

    def read_order_file(self) -> list[str]:
        path = self.dir / ".order.json"
        clear_hidden(path)
        return json.loads(path.read_text(encoding="utf-8"))["order"]

    def test_ghost_survives_two_drag_cycles(self) -> None:
        for name in ("a.lnk", "b.exe"):
            self.touch_file(name)
        self.write_order_file(["a.lnk", "b.exe", "ghost.lnk"])

        # 打开面板：ghost 不显示
        self.fm.reload()
        self.assertEqual([e.name for e in self.fm.entries], ["a.lnk", "b.exe"])

        # 第一次拖拽：a 拖到 b 后面
        self.fm.save_order(["b.exe", "a.lnk"])
        first = self.read_order_file()
        self.assertEqual(first, ["b.exe", "a.lnk", "ghost.lnk"], "第一次拖拽后 ghost 应在尾部")

        # 第二次拖拽：再调一次顺序（此时内存记录必须已含 ghost，否则会被抹掉）
        self.fm.save_order(["a.lnk", "b.exe"])
        second = self.read_order_file()
        self.assertIn("ghost.lnk", second, "第二次拖拽后 ghost 记录被抹掉了（缺陷未修好）")
        self.assertEqual(second, ["a.lnk", "b.exe", "ghost.lnk"])
        self.assertEqual(second.count("ghost.lnk"), 1, "ghost 不应重复累积")

    def test_ghost_survives_three_cycles_with_panel_reopen(self) -> None:
        for name in ("a.lnk", "b.exe", "c.exe"):
            self.touch_file(name)
        self.write_order_file(["a.lnk", "ghost.lnk"])

        cycles = [["b.exe", "a.lnk", "c.exe"], ["c.exe", "b.exe", "a.lnk"], ["a.lnk", "c.exe", "b.exe"]]
        for idx, names in enumerate(cycles, start=1):
            # 每轮都重新打开面板（reload 会重新读文件）——最接近真实"拖拽-关面板-再打开-再拖"
            self.fm.reload()
            self.fm.save_order(names)
            current = self.read_order_file()
            self.assertEqual(current[:len(names)], names, f"第 {idx} 轮可见顺序写回失败")
            self.assertIn("ghost.lnk", current, f"第 {idx} 轮后 ghost 记录丢失")
            self.assertEqual(current.count("ghost.lnk"), 1)

    def test_ghost_survives_when_no_visible_entries(self) -> None:
        """极端情形：面板清空后保存，记录本身也不能被清掉。"""
        self.write_order_file(["ghost.lnk", "ghost2.exe"])
        self.fm.reload()
        self.assertEqual(self.fm.entries, [])
        self.fm.save_order([])
        self.assertEqual(self.read_order_file(), ["ghost.lnk", "ghost2.exe"])

    def test_saved_order_file_is_hidden_and_rejects_plain_open(self) -> None:
        """隐藏属性正面验证：带 H 属性时 Python 的 open('w') 会被 Windows 拒绝。"""
        self.touch_file("a.lnk")
        self.fm.reload()
        self.fm.save_order(["a.lnk"])
        path = self.dir / ".order.json"
        from tests.qt_support import is_hidden

        self.assertTrue(is_hidden(path), "保存后 .order.json 必须带隐藏属性")
        with self.assertRaises(PermissionError):
            with open(path, "w", encoding="utf-8") as fh:
                fh.write("should not be allowed")
        # 文件内容未被破坏
        self.assertEqual(self.fm.reload()[0].name, "a.lnk")


class ReloadTests(_TempDirCase):
    def test_reload_reads_order_file(self) -> None:
        for name in ("a.lnk", "b.lnk", "c.lnk"):
            self.touch_file(name)
        OrderStore(self.dir).save(["c.lnk", "b.lnk"], [])
        entries = self.fm.reload()
        self.assertEqual([e.name for e in entries], ["c.lnk", "b.lnk", "a.lnk"])
        self.assertEqual(self.fm.order_names, ["c.lnk", "b.lnk"])

    def test_reload_with_broken_order_file(self) -> None:
        self.touch_file("a.lnk")
        self.touch_file("b.lnk")
        clear_hidden(self.dir / ".order.json")
        (self.dir / ".order.json").write_text("{broken", encoding="utf-8")
        entries = self.fm.reload()
        self.assertEqual([e.name for e in entries], ["a.lnk", "b.lnk"])
        self.assertTrue((self.dir / ".order.json").exists(), "损坏文件不得被删除")

    def test_save_order_keeps_ghost(self) -> None:
        for name in ("a.lnk", "b.lnk"):
            self.touch_file(name)
        OrderStore(self.dir).save(["b.lnk", "a.lnk", "ghost.lnk"], [])
        self.fm.reload()
        self.fm.save_order(["a.lnk", "b.lnk"])
        data = json.loads((self.dir / ".order.json").read_text(encoding="utf-8"))
        self.assertEqual(data["order"], ["a.lnk", "b.lnk", "ghost.lnk"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
