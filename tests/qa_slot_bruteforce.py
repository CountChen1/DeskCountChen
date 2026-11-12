"""QA 独立穷举验证：app.slot_ops.apply_move 的**密集插入**语义不变式（v1.3）。

与 tests/test_slot_insert.py 的区别（本文件是**独立**验证，不是它的复制）：
- 穷举 n<=5 的**全部**布局（含 None 兼容旧入参）、**全部**行组合与**全部**落点；
- 不变式用"集合/序列代数"形式表述，不依赖任何手写期望值，
  因此不会和实现共享同一个错误假设。

v1.3 不变式（空位体系废除，完全对齐手机桌面文件夹）：
① 输出永远密集（无 None / 空串）；② 占用条目多重集合守恒；③ 被移动块
连续出现且内部相对次序不变；④ 落到占用图标：块插到该图标之前、
该图标紧跟块之后；⑤ 落到网格外/空行：追加到末尾（已在末尾则恒等）；
⑥ 落到自己身上：恒等；⑦ 块外条目相对次序保持（允许前移补位——这正是
v1.3 要的"自动补齐"）；⑧ 长度恒等于占用条目数。

用法（不参与 unittest discover，文件名不是 test_*.py）：
    .venv\\Scripts\\python.exe tests\\qa_slot_bruteforce.py

现场保护：纯函数验证，**只读**——不导入 ``app.paths``、不读写任何真实路径、
不建目录、不碰配置与桌面。
"""

from __future__ import annotations

import itertools
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.slot_ops import apply_move  # noqa: E402

NAMES = ("A", "B", "C", "D", "E")
FAILS: list[str] = []
CHECKS = 0


def fail(msg: str) -> None:
    FAILS.append(msg)
    if len(FAILS) <= 40:
        print(f"[FAIL] {msg}")


def ok(msg: str) -> None:
    global CHECKS
    CHECKS += 1


def layouts(n: int):
    """生成所有"名字互不重复（可含 None 空位，兼容旧入参）"的长度 n 布局。"""
    for k in range(0, min(n, len(NAMES)) + 1):
        for chosen in itertools.combinations(NAMES, k):
            for positions in itertools.combinations(range(n), k):
                slots: list[str | None] = [None] * n
                for pos, name in zip(positions, chosen):
                    slots[pos] = name
                yield slots


def check_case(slots: list[str | None], rows: tuple[int, ...], target: int) -> None:
    before = list(slots)
    n = len(before)
    after = apply_move(before, list(rows), target)
    ctx = f"before={before} rows={list(rows)} target={target} after={after}"

    # --- 基本健全性 ---------------------------------------------------
    if any(s is None or (isinstance(s, str) and not s) for s in after):
        fail(f"输出不密集（含 None/空串）: {ctx}")
        return
    if len(after) != len(set(after)):
        fail(f"出现重复条目: {ctx}")
        return
    if len(after) != sum(1 for s in before if s):
        fail(f"输出长度 != 占用条目数: {ctx}")
        return

    # ② 占用条目多重集合守恒
    if sorted(after) != sorted(s for s in before if s):
        fail(f"占用条目集合不守恒: {ctx}")
        return

    block = [str(before[r]) for r in rows if r < n and before[r] is not None]
    moved = set(block)

    # ⑥ 落到自己身上（落点是占用行且属于被拖行）→ 恒等
    if 0 <= target < n and target in rows:
        if after != [s for s in before if s]:
            fail(f"落到自身所在行却不是恒等变换: {ctx}")
        else:
            ok("identity")
        return

    # 无变化（恒等）的合法性判据（独立推导，不依赖实现内部结构）：
    # 语义 = "把被拖块从密集序列取出、再插回落点修正后的位置"。
    # 只有"块在密集序列里连续、且插回位置恰为块原起点"时，恒等才合法
    # （例如 C 已紧邻 E 时把 C 拖到 E 上：插到 E 之前 = 原地不动）。
    if after == [s for s in before if s]:
        pos_of: dict[int, int] = {}
        for j, i in enumerate(i for i, s in enumerate(before) if s is not None):
            pos_of[i] = j
        P = sorted(pos_of[r] for r in rows if 0 <= r < n and before[r] is not None)
        T = pos_of[target] if (0 <= target < n and before[target] is not None) else len(after)
        insert_at = T - sum(1 for r in P if r < T)
        contiguous = all(P[k + 1] == P[k] + 1 for k in range(len(P) - 1))
        if not (contiguous and insert_at == P[0]):
            fail(f"无变化但语义上应当搬移: {ctx}")
        else:
            ok("identity")
        return

    # ③ 被移动块在结果里必须连续且内部相对次序不变
    block_span = None
    for start in range(len(after) - len(block) + 1):
        if after[start:start + len(block)] == block:
            block_span = start
            break
    if block_span is None:
        fail(f"被移动块没有连续出现或内部次序被打乱: {ctx}")
        return

    # ⑦ 块外条目相对次序保持（允许前移补位，但绝不允许乱序）
    idx_after = {name: i for i, name in enumerate(after)}
    prev = -1
    for name in before:
        if name is None or name in moved:
            continue
        if idx_after[name] <= prev:
            fail(f"块外条目相对次序被打乱（{name}）: {ctx}")
            return
        prev = idx_after[name]

    target_occupied = 0 <= target < n and before[target] is not None
    if target_occupied:
        # ④ 落到占用图标：块插到它之前，它紧跟块之后
        if after[block_span + len(block)] != before[target]:
            fail(f"落点原图标没有紧跟块之后（期望 {before[target]}）: {ctx}")
            return
    else:
        # ⑤ 落到网格外/空行：追加到末尾
        if after[len(after) - len(block):] != block:
            fail(f"落到网格外但没有追加到末尾: {ctx}")
            return
    ok("invariants")


def run_bruteforce() -> None:
    cases = 0
    for n in range(1, 6):
        for slots in layouts(n):
            occupied = [i for i, s in enumerate(slots) if s is not None]
            if not occupied:
                continue
            subsets = []
            for size in range(1, len(occupied) + 1):
                subsets.extend(itertools.combinations(occupied, size))
            for rows in subsets:
                for target in range(-1, n + 2):   # 含越界落点
                    cases += 1
                    check_case(slots, rows, target)
    print(f"穷举用例数 = {cases}，断言通过 = {CHECKS}，失败 = {len(FAILS)}")


# ---------------------------------------------------------------------------
# 指定方向用例：期望值手工推导，与实现无关
# ---------------------------------------------------------------------------

DIRECTION_CASES: list[tuple[str, list, list[int], int, list]] = [
    # 名称, 初始布局, 拖拽行, 落点, 期望结果（密集输出）
    ("最后拖到最前（占用格）", ["A", "B", "C", "D"], [3], 0, ["D", "A", "B", "C"]),
    ("最前拖到最后（占用格）", ["A", "B", "C", "D"], [0], 3, ["B", "C", "A", "D"]),
    ("首图标拖到最后图标上（高频操作）", ["A", "B", "C", "D", "E"], [0], 4,
     ["B", "C", "D", "A", "E"]),
    ("跨空位前移", ["A", None, "B", "C"], [3], 0, ["C", "A", "B"]),
    ("跨空位后移（落点行被 C 占用 → 插到 C 之前）", ["A", "B", None, "C"], [0], 3,
     ["B", "A", "C"]),
    ("落到相邻后一格 = 插到自己当前位置（恒等）", ["A", "B", "C"], [0], 1,
     ["A", "B", "C"]),
    ("落到相邻前一格", ["A", "B", "C"], [2], 1, ["A", "C", "B"]),
    ("多选块前移落点=落到的图标位置", ["A", "B", "C", "D"], [2, 3], 1,
     ["A", "C", "D", "B"]),
    ("多选块后移落点=落到的图标位置", ["A", "B", "C", "D"], [0, 1], 3,
     ["C", "A", "B", "D"]),
    ("多选块跨空位前移", ["A", None, "B", "C"], [2, 3], 0, ["B", "C", "A"]),
    ("拖到网格外（中段）", ["A", "B", "C"], [1], -1, ["A", "C", "B"]),
    ("拖到网格外（已在末尾）", ["A", None, "B"], [2], -1, ["A", "B"]),
    ("落点指向空行=视为网格外追加", ["A", "B", None, "C"], [0], 2, ["B", "C", "A"]),
    ("单元素网格拖到自身", ["A"], [0], 0, ["A"]),
    ("全部占满时后移一格", ["A", "B", "C", "D", "E"], [4], 0, ["E", "A", "B", "C", "D"]),
]


def run_direction_cases() -> None:
    for name, slots, rows, target, expected in DIRECTION_CASES:
        got = apply_move(list(slots), rows, target)
        if got != expected:
            fail(f"{name}: 期望 {expected}，实际 {got}")
        else:
            ok(name)
    print(f"指定方向用例 {len(DIRECTION_CASES)} 项，失败累计 {len(FAILS)}")


def run_idempotence() -> None:
    """同一落点的拖拽重复执行必须收敛（第二个同参数拖拽不再改变布局）。

    只对**密集布局**检查：含 None 的旧布局在第一次拖拽时就被 densify，
    行号含义随之改变（原空行变成占用行），"同参数 → 同结果"的前提不再
    成立——这是密集语义的固有性质，不是缺陷。
    """
    for n in range(1, 6):
        for slots in layouts(n):
            occupied = [i for i, s in enumerate(slots) if s is not None]
            if len(occupied) < 2 or len(occupied) != n:
                continue  # 跳过含空位的旧布局（理由见 docstring）
            src = occupied[-1]
            for target in range(n):
                once = apply_move(list(slots), [src], target)
                if not once:
                    continue
                name = slots[src]
                row = once.index(name)
                twice = apply_move(once, [row], target)
                if row != target and twice != once:
                    fail(f"重复拖拽不收敛: slots={slots} src={src} target={target} "
                         f"once={once} twice={twice}")
                    return
    ok("idempotent")


def main() -> int:
    run_direction_cases()
    run_idempotence()
    run_bruteforce()
    print()
    if FAILS:
        print(f"结论：FAIL（{len(FAILS)} 项不变式被破坏）")
        return 1
    print("结论：PASS —— 全部密集插入语义不变式成立")
    return 0


if __name__ == "__main__":
    sys.exit(main())
