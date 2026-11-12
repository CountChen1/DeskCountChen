"""拖拽重排语义单元测试（纯逻辑，不需要 Qt / 真实桌面）。

v1.3 密集插入语义（用户最终决策：完全对齐手机桌面文件夹，空位体系废除）：
1. 向前拖到占用图标 = 插入让位，原位置由后面图标**自动补齐**（不再留空位）；
2. 向后拖同理，网格永远紧凑；
3. 拖到网格外 = 追加末尾（含不连续多选）；
4. 批量（含不连续多选）搬移落点直觉正确 —— 这是 v1.1 off-by-one 的教训点。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.slot_ops import apply_move, densify  # noqa: E402


def slots(*values) -> list[str | None]:
    """用紧凑写法构造序列（空字符串 = 空位 None，仅用于兼容旧入参）。"""
    return [None if v == "" else v for v in values]


class DensifyTests(unittest.TestCase):
    def test_drops_none_and_empty(self) -> None:
        self.assertEqual(densify(slots("", "A", None, "B", "")), ["A", "B"])

    def test_keeps_all_names_in_order(self) -> None:
        self.assertEqual(densify(["b.lnk", "a.lnk"]), ["b.lnk", "a.lnk"])

    def test_empty_input(self) -> None:
        self.assertEqual(densify([]), [])
        self.assertEqual(densify([None, None]), [])


class ForwardDragTests(unittest.TestCase):
    """向前拖（落点行号 < 被拖行号）。"""

    def test_forward_insert_fills_source_slot(self) -> None:
        """①向前拖到占用图标：被拖条目落到松手位置，原位置由后方补齐。"""
        result = apply_move(slots("A", "B", "C", "D"), [2], 0)
        self.assertEqual(result, ["C", "A", "B", "D"])
        self.assertEqual(result[0], "C", "被拖条目必须落到松手的位置")
        self.assertEqual(result[2], "B", "B 前移一格补齐 C 的原位置（密集语义）")
        self.assertEqual(result[3], "D", "D 不动")
        self.assertNotIn(None, result, "不允许出现空位")

    def test_forward_drag_to_tail_neighbour(self) -> None:
        result = apply_move(slots("A", "B", "C"), [2], 1)
        self.assertEqual(result, ["A", "C", "B"])

    def test_forward_drag_keeps_others_relative_order(self) -> None:
        result = apply_move(slots("A", "B", "C", "D", "E"), [4], 1)
        self.assertEqual(result, ["A", "E", "B", "C", "D"])


class BackwardDragTests(unittest.TestCase):
    """向后拖（落点行号 > 被拖行号）。"""

    def test_backward_insert_fills_source_slot(self) -> None:
        """②向后拖：A 插到 C 的位置，B 前移补位，网格紧凑。"""
        result = apply_move(slots("A", "B", "C", "D"), [0], 2)
        self.assertEqual(result, ["B", "A", "C", "D"])
        self.assertEqual(result[0], "B", "B 前移一格补齐（手机桌面语义）")
        self.assertEqual(result[1], "A", "A 落到 C 原来的格子")

    def test_backward_drag_to_last_slot(self) -> None:
        """落到最后一格 = 插到该图标之前（不是"追加到末尾"）。"""
        result = apply_move(slots("A", "B", "C"), [0], 2)
        self.assertEqual(result, ["B", "A", "C"])

    def test_drop_outside_appends_to_end(self) -> None:
        result = apply_move(slots("A", "B", "C"), [0], 99)
        self.assertEqual(result, ["B", "C", "A"])

    def test_drop_outside_when_already_last_is_noop(self) -> None:
        self.assertEqual(apply_move(slots("A", "B", "C"), [2], -1),
                         ["A", "B", "C"])


class DenseInvariantTests(unittest.TestCase):
    """③密集不变式：数量守恒、无重复、输出无空位。"""

    def test_count_conserved_and_no_duplicates(self) -> None:
        original = ["A", "B", "C", "D", "E"]
        for src in range(5):
            for target in (-1, 0, 1, 2, 3, 4, 99):
                result = apply_move(original, [src], target)
                self.assertEqual(sorted(result), sorted(original),
                                 f"src={src} target={target}: 条目数量/身份不守恒")
                self.assertEqual(len(result), len(set(result)),
                                 f"src={src} target={target}: 出现重复")

    def test_output_is_always_dense(self) -> None:
        for original in (slots("A", "", "B"), slots("", "A", "", "B", ""),
                         slots("A", "B", "C")):
            for target in (-1, 0, 1, 2, 99):
                result = apply_move(list(original), [i for i, s in enumerate(original) if s], target)
                self.assertNotIn(None, result, f"target={target}: 输出含空位")

    def test_repeated_drag_converges(self) -> None:
        """同一落点的拖拽重复执行必须收敛（不能每次都继续变化）。"""
        original = ["A", "B", "C"]
        once = apply_move(original, [0], 2)     # A 拖到 C 上 → 插到 C 之前
        self.assertEqual(once, ["B", "A", "C"])
        twice = apply_move(once, [1], 2)        # A 已紧邻 C，再拖到 C 上 → 恒等
        self.assertEqual(twice, once, "同一落点反复拖拽必须收敛")
        thrice = apply_move(twice, [1], 2)
        self.assertEqual(thrice, twice)

    def test_legacy_none_input_is_densified(self) -> None:
        """兼容旧模型行：含 None 的入参先去空位，行号按密集空间换算。"""
        result = apply_move(slots("A", "", "B"), [2], 0)
        self.assertEqual(result, ["B", "A"])

    def test_legacy_row_pointing_at_hole_appends(self) -> None:
        """落点指向空位行（旧数据）= 视为网格外 → 追加末尾。"""
        result = apply_move(slots("A", "", "B"), [2], 1)
        self.assertEqual(result, ["A", "B"])


class BatchMoveTests(unittest.TestCase):
    """④批量前移 / 后移落点正确。"""

    def test_batch_forward(self) -> None:
        result = apply_move(slots("A", "B", "C", "D", "E"), [3, 4], 0)
        self.assertEqual(result, ["D", "E", "A", "B", "C"])

    def test_batch_backward_target_is_where_pointer_released(self) -> None:
        """向后批量搬移：块插到落点图标（D）之前，不出现行号偏移。"""
        result = apply_move(slots("A", "B", "C", "D", "E"), [0, 1], 3)
        self.assertEqual(result, ["C", "A", "B", "D", "E"],
                         "A/B 必须紧贴在 D 之前（这就是 v1.1 的 off-by-one 缺陷点）")

    def test_batch_keeps_relative_order(self) -> None:
        result = apply_move(slots("A", "B", "C", "D"), [2, 3], 0)
        self.assertEqual(result, ["C", "D", "A", "B"])

    def test_batch_onto_own_row_is_noop(self) -> None:
        self.assertEqual(apply_move(slots("A", "B", "C", "D"), [1, 2], 2),
                         ["A", "B", "C", "D"])

    def test_single_onto_itself_is_noop(self) -> None:
        self.assertEqual(apply_move(slots("A", "B", "C"), [1], 1),
                         ["A", "B", "C"])

    def test_out_of_range_rows_are_ignored(self) -> None:
        self.assertEqual(apply_move(slots("A", "B"), [5, -3], 0), ["A", "B"])


class NonContiguousMoveTests(unittest.TestCase):
    """非连续多选的前移 / 后移 / 追加。"""

    def test_non_contiguous_forward_to_occupied(self) -> None:
        """[1,3] 拖到 0：整块 [B,D] 插到 A 之前。"""
        result = apply_move(slots("A", "B", "C", "D", "E"), [1, 3], 0)
        self.assertEqual(result, ["B", "D", "A", "C", "E"])

    def test_non_contiguous_backward_to_occupied(self) -> None:
        """[0,2] 拖到 4：整块 [A,C] 插到 E 之前，块外 B、D 依次前移补位。"""
        result = apply_move(slots("A", "B", "C", "D", "E"), [0, 2], 4)
        self.assertEqual(result, ["B", "D", "A", "C", "E"])

    def test_non_contiguous_onto_own_row_is_noop(self) -> None:
        """落点属于被拖行 → 恒等（哪怕选择不连续）。"""
        self.assertEqual(apply_move(slots("A", "B", "C", "D", "E"), [0, 2], 2),
                         ["A", "B", "C", "D", "E"])


class AppendOutOfGridTests(unittest.TestCase):
    """拖到网格外 = 追加到末尾（密集语义下不再有"锚点"概念）。"""

    def test_non_contiguous_append(self) -> None:
        result = apply_move(slots("A", "B", "C"), [0, 2], -1)
        self.assertEqual(result, ["B", "A", "C"])

    def test_non_contiguous_selection_before_tail_icon_moves(self) -> None:
        """['A','B','C','D'] 选 [1,2]：块 [B,C] 追加到末尾。"""
        result = apply_move(slots("A", "B", "C", "D"), [1, 2], -1)
        self.assertEqual(result, ["A", "D", "B", "C"])

    def test_contiguous_append_still_works(self) -> None:
        result = apply_move(slots("A", "B", "C", "D", "E"), [0, 1], -1)
        self.assertEqual(result, ["C", "D", "E", "A", "B"])

    def test_contiguous_append_single_item(self) -> None:
        self.assertEqual(apply_move(slots("A", "B", "C"), [1], -1),
                         ["A", "C", "B"])

    def test_append_when_block_already_at_tail_is_noop(self) -> None:
        for original in (["A", "B", "C"], ["A", "B"]):
            picked = [len(original) - 1]
            self.assertEqual(apply_move(original, picked, -1), original,
                             str(original))


if __name__ == "__main__":
    unittest.main(verbosity=2)
