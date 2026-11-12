"""OrderStore（隐藏的 .order.json）单元测试。

覆盖：读取/写入 roundtrip、文件缺失、JSON 损坏容错（且不删文件）、
结构非法、非字符串项过滤、stale 保留顺序、大小写不敏感、隐藏属性切换。
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.order_store import ORDER_FILE_NAME, OrderStore  # noqa: E402
from app.win32 import window_ops  # noqa: E402
from tests.qt_support import (  # noqa: E402
    FILE_ATTRIBUTE_HIDDEN,
    FILE_ATTRIBUTE_NORMAL,
    clear_hidden,
    get_attrs,
    is_hidden,
    make_hidden,
)


class _TempDirCase(unittest.TestCase):
    """每个用例一个独立临时目录，结束必清理。"""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_order_")
        self.dir = Path(self._tmp.name)
        self.store = OrderStore(self.dir)
        self.path = self.dir / ORDER_FILE_NAME

    def tearDown(self) -> None:
        self._tmp.cleanup()

    def _write_raw(self, text: str) -> None:
        clear_hidden(self.path)
        self.path.write_text(text, encoding="utf-8")


class LoadTests(_TempDirCase):
    def test_missing_file_returns_empty(self) -> None:
        self.assertEqual(self.store.load(), [])

    def test_broken_json_returns_empty_and_keeps_file(self) -> None:
        self._write_raw("{broken json!!")
        before = self.path.read_bytes()
        self.assertEqual(self.store.load(), [])
        self.assertTrue(self.path.exists(), "损坏的 .order.json 不得被删除")
        self.assertEqual(self.path.read_bytes(), before, "加载不得改写用户文件")

    def test_empty_file_returns_empty(self) -> None:
        self._write_raw("")
        self.assertEqual(self.store.load(), [])

    def test_non_dict_json_returns_empty(self) -> None:
        self._write_raw(json.dumps(["a.lnk"]))
        self.assertEqual(self.store.load(), [])

    def test_missing_order_key_returns_empty(self) -> None:
        self._write_raw(json.dumps({"version": 1}))
        self.assertEqual(self.store.load(), [])

    def test_order_not_list_returns_empty(self) -> None:
        self._write_raw(json.dumps({"version": 1, "order": "a.lnk"}))
        self.assertEqual(self.store.load(), [])

    def test_non_string_entries_are_dropped(self) -> None:
        self._write_raw(json.dumps({"version": 1, "order": ["a.lnk", 1, None, {"x": 1}, "b.exe"]}))
        self.assertEqual(self.store.load(), ["a.lnk", "b.exe"])

    def test_version_mismatch_still_parsed(self) -> None:
        """设计要求：version 不匹配也按 order 尽力解析。"""
        self._write_raw(json.dumps({"version": 99, "order": ["a.lnk"]}))
        self.assertEqual(self.store.load(), ["a.lnk"])

    def test_utf8_names_roundtrip(self) -> None:
        self._write_raw(json.dumps({"version": 1, "order": ["微信.lnk", "开发工具"]}, ensure_ascii=False))
        self.assertEqual(self.store.load(), ["微信.lnk", "开发工具"])


class SaveTests(_TempDirCase):
    def test_save_roundtrip_and_schema(self) -> None:
        self.store.save(["b.lnk", "a.lnk"], [])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["version"], 1)
        self.assertEqual(data["order"], ["b.lnk", "a.lnk"])
        datetime.fromisoformat(data["updated"])  # 必须能被解析（ISO 8601）
        self.assertEqual(self.store.load(), ["b.lnk", "a.lnk"])

    def test_no_tmp_file_left(self) -> None:
        self.store.save(["a.lnk"], [])
        leftovers = [p.name for p in self.dir.iterdir() if ".tmp." in p.name]
        self.assertEqual(leftovers, [], "原子写不得留下临时文件")

    def test_stale_entries_kept_in_order(self) -> None:
        self.store.save(["a.lnk"], ["b.lnk", "a.lnk", "ghost.lnk"])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["order"], ["a.lnk", "b.lnk", "ghost.lnk"])

    def test_stale_dedup_is_case_insensitive(self) -> None:
        self.store.save(["A.LNK"], ["a.lnk", "ghost.lnk"])
        data = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(data["order"], ["A.LNK", "ghost.lnk"])

    def test_repeated_save_does_not_accumulate_stale(self) -> None:
        old = ["a.lnk", "ghost.lnk"]
        self.store.save(["a.lnk"], old)
        first = json.loads(self.path.read_text(encoding="utf-8"))["order"]
        self.store.save(["a.lnk"], first)
        second = json.loads(self.path.read_text(encoding="utf-8"))["order"]
        self.assertEqual(first, second)

    def test_save_over_broken_file_replaces_it(self) -> None:
        self._write_raw("{broken")
        self.store.save(["a.lnk"], [])
        self.assertEqual(self.store.load(), ["a.lnk"])

    def test_save_over_hidden_file_succeeds(self) -> None:
        """Windows 拒绝覆盖隐藏文件：save 内部必须先清属性。"""
        self.store.save(["a.lnk"], [])
        self.assertTrue(is_hidden(self.path))
        self.store.save(["b.lnk"], ["a.lnk"])
        self.assertEqual(self.store.load(), ["b.lnk", "a.lnk"])


class HiddenAttributeTests(_TempDirCase):
    def test_saved_file_is_hidden(self) -> None:
        self.store.save(["a.lnk"], [])
        self.assertTrue(is_hidden(self.path))
        self.assertTrue(get_attrs(self.path) & FILE_ATTRIBUTE_HIDDEN)

    def test_clear_then_set_cycle(self) -> None:
        self.store.save(["a.lnk"], [])
        window_ops.clear_file_hidden(self.path)
        self.assertFalse(is_hidden(self.path))
        self.assertTrue(get_attrs(self.path) & FILE_ATTRIBUTE_NORMAL)
        window_ops.set_file_hidden(self.path)
        self.assertTrue(is_hidden(self.path))

    def test_load_does_not_change_attributes(self) -> None:
        self.store.save(["a.lnk"], [])
        self.store.load()
        self.assertTrue(is_hidden(self.path))


if __name__ == "__main__":
    unittest.main(verbosity=2)
