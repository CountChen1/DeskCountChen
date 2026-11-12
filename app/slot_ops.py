"""面板顺序重排的纯逻辑（不依赖 Qt，可无头单测）。

v1.3（当前语义）——完全对齐手机桌面文件夹：**密集顺序，无空位**。

用户最终决策：v1.1~v1.2 的"空位"方案在实测中反复引发"越拖越散"（插入
留空位，每拖一次净增一格；收编前导也挡不住内部空位累积），故整体放弃
空位体系：

    拖 A 落到图标 B 上 → A 插到 B 的位置，B 及其后的图标整体后移一格，
    A 原来的位置由后面的图标**自动补齐**（网格永远紧凑）；
    拖到面板空白处 / 网格外 → 追加到末尾；
    落在自己身上 → 顺序不变；
    多选（含不连续多选）→ 整块搬移，块内相对次序保持。

因此 v1.1~v1.2 的槽位/空位/幽灵占格体系（HOLE_ROLE、collapse_leading_holes、
"绝不向前补位"不变式）全部废除。历史 ``.order.json`` 里的空位与幽灵名在
加载边界（``OrderStore.load_slots`` / ``FolderModel._merge_slots`` /
``PanelModel.build_slots``）统一 densify 后自然消失，无需迁移脚本。

行号换算（v1.1 off-by-one 的教训集中在此）：
    入参 ``slots`` 允许含 None（兼容旧模型行）；先 densify 并建立
    "原始行号 → 密集行号"映射；被搬行删除后，落点按"其前方被删行数"
    左移修正。所有换算只存在于本函数内，由穷举测试逐例校验
    （``tests/qa_slot_bruteforce.py``）。
"""

from __future__ import annotations

from typing import Iterable

__all__ = ["densify", "apply_move"]


def densify(slots: Iterable[str | None]) -> list[str]:
    """去空位：丢弃 None 与空串，只留真实记录名（v1.3 密集语义）。"""
    return [str(s) for s in slots if s]


def apply_move(slots: list[str | None], rows: list[int],
               target: int) -> list[str]:
    """密集插入语义重排，返回新的顺序列表（不改动入参）。

    Args:
        slots: 当前顺序（v1.3 恒为密集名单；仍兼容含 None 的旧序列）。
        rows: 被搬移的行号（原始行号空间；多条时块内相对次序保持）。
        target: 落点行号（原始行号空间）；<0 / 越界 / 指向空行 =
            落在网格外 → 追加到末尾。

    Returns:
        新的密集顺序列表（不含 None）。
    """
    dense: list[str] = []
    dense_index: dict[int, int] = {}   # 原始行号 → 密集行号
    for i, s in enumerate(slots):
        if s:
            dense_index[i] = len(dense)
            dense.append(str(s))

    picked = sorted({dense_index[int(r)] for r in rows if int(r) in dense_index})
    if not picked:
        return dense

    n = len(dense)
    if int(target) < 0 or int(target) >= len(slots):
        dense_target = n          # 网格外 → 追加末尾
    else:
        dense_target = dense_index.get(int(target), n)

    if dense_target in picked:
        return dense              # 落在自己身上：顺序不变

    names = [dense[r] for r in picked]
    picked_set = set(picked)
    remaining = [s for i, s in enumerate(dense) if i not in picked_set]
    # 落点修正：落点之前每删一行，落点在剩余列表里就左移一格
    shift = sum(1 for r in picked if r < dense_target)
    insert_at = max(0, min(dense_target - shift, len(remaining)))
    return remaining[:insert_at] + names + remaining[insert_at:]
