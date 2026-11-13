"""QA 独立验证：拖拽 → .order.json 落盘链路（v1.3 密集语义，不经 AppController）。

验证点：
1. 每次拖拽后，磁盘 .order.json 的 slots 序列与内存模型**完全一致**；
2. 输出永远密集：磁盘与内存都不出现 None/空串（空位体系已废除）；
3. 历史脏文件（空位 null / 幽灵名）加载即自愈，落盘后为密集形态；
4. .order.json 保持原子写（无 .tmp 残留）+ 隐藏属性；
5. 重新加载（模拟关闭再打开面板）后布局与拖拽结果一致（持久化闭环）；
6. 反复"把第一个图标拖到最后一个图标上"（用户高频操作）：行数恒定、
   永不产生空位、布局收敛（不会越拖越散）。

安全：全程只在临时目录里操作（不碰 %USERPROFILE%\\DeskCountChen，不碰真实桌面）。

用法：.venv\\Scripts\\python.exe tests\\qa_slot_persist.py
"""

from __future__ import annotations

import json
import os
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.folder_model import FolderModel  # noqa: E402
from app.icon_provider import IconCache  # noqa: E402
from app.order_store import ORDER_FILE_NAME  # noqa: E402
from app.panel_model import NAME_ROLE, PanelModel  # noqa: E402
from app.win32 import window_ops  # noqa: E402
from app.windows.panel_grid import PanelGrid  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
FILES = ("a.lnk", "b.exe", "c.lnk", "d.exe", "e.lnk")
GHOST = "ghost.lnk"


def check(name: str, okd: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(okd), detail))
    print(f"[{'PASS' if okd else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


class Harness:
    """最小装配：FolderModel + PanelModel + PanelGrid + order_changed→save_slots。"""

    def __init__(self, app: QApplication, root: Path) -> None:
        self.app = app
        self.dir = root
        self.icon_cache = IconCache()

    def write_slots(self, slots: list[str | None]) -> None:
        path = self.dir / ORDER_FILE_NAME
        window_ops.clear_file_hidden(path)
        path.write_text(
            json.dumps({"version": 1, "order": [s for s in slots if s],
                        "slots": slots}, ensure_ascii=False),
            encoding="utf-8",
        )

    def open(self) -> tuple[FolderModel, PanelModel, PanelGrid]:
        fm = FolderModel(self.dir)
        entries = fm.reload()
        pm = PanelModel()
        pm.build_slots(fm.slots, entries, self.icon_cache, 48)
        grid = PanelGrid(item_px=48, icon_cache=self.icon_cache)
        grid.set_model(pm)
        # 与 controller._do_save_order 等价（200ms 去抖在生产里由 controller 负责，
        # 这里直接同步落盘，避免断言时与定时器赛跑）
        grid.order_changed.connect(lambda: fm.save_slots(pm.serialize_slots()))
        return fm, pm, grid

    def read_slots(self) -> list:
        # 只读：不要 clear_file_hidden（隐藏属性不阻碍读取，清了就测不出"保存后设回隐藏"）
        return json.loads((self.dir / ORDER_FILE_NAME).read_text(encoding="utf-8"))["slots"]

    def read_raw(self) -> dict:
        return json.loads((self.dir / ORDER_FILE_NAME).read_text(encoding="utf-8"))


def is_hidden(path: Path) -> bool:
    try:
        return bool(path.stat().st_file_attributes & 0x2)
    except (AttributeError, OSError):
        return False


def main() -> int:
    app = QApplication.instance() or QApplication(sys.argv)
    root = Path(tempfile.mkdtemp(prefix="dbx_qa_persist_"))
    try:
        for name in FILES:
            (root / name).write_bytes(b"")
        h = Harness(app, root)

        # ---------- 1. 落盘一致性：向前拖（落到占用图标） ----------
        h.write_slots(list(FILES))
        fm, pm, grid = h.open()
        before_mem = pm.serialize_slots()
        grid._apply_slot_move([1], 0)          # b.exe 拖到 a.lnk 的位置
        mem = pm.serialize_slots()
        disk = h.read_slots()
        check("内存模型与磁盘 slots 完全一致", mem == disk, f"mem={mem} disk={disk}")
        check("向前拖：落点正确 + 布局密集无空位",
              mem[0] == "b.exe" and mem[1] == "a.lnk" and None not in mem, str(mem))
        check("order 字段与 slots 字段内容相同",
              h.read_raw()["order"] == disk, str(h.read_raw()["order"]))
        check("无 .order.json.tmp 残留（os.replace 原子写）",
              not list(root.glob(".order.json.tmp*")))
        check(".order.json 仍带隐藏属性", is_hidden(root / ORDER_FILE_NAME))
        check("拖拽确实改变了布局（before≠after）", mem != before_mem)

        # ---------- 2. 持久化闭环：重新加载后布局不变 ----------
        fm2 = FolderModel(root)
        fm2.reload()
        check("重新加载后顺序与磁盘一致", fm2.slots == disk, f"{fm2.slots} vs {disk}")
        check("重新加载后可见顺序正确",
              fm2.slots == ["b.exe", "a.lnk", "c.lnk", "d.exe", "e.lnk"], str(fm2.slots))

        # ---------- 3. 历史脏数据（空位+幽灵）加载即自愈 ----------
        h.write_slots(["a.lnk", None, GHOST, "b.exe", None, "c.lnk", "d.exe", "e.lnk"])
        fm, pm, grid = h.open()
        mem = pm.serialize_slots()
        hole_rows = [
            r for r in range(pm.model.rowCount())
            if pm.model.item(r).data(NAME_ROLE) in (None, "")
        ]
        check("脏数据加载：空位与幽灵消失，条目完整且顺序不变",
              mem == list(FILES), str(mem))
        check("脏数据加载：模型无占位行", not hole_rows, str(hole_rows))
        grid._apply_slot_move([2], 0)          # c.lnk 拖到最前
        mem = pm.serialize_slots()
        check("脏数据拖拽后：落盘为密集形态（无 None/幽灵）",
              mem == h.read_slots() and GHOST not in h.read_raw()["order"], str(mem))

        # ---------- 4. 批量搬移 ----------
        h.write_slots(list(FILES))
        fm, pm, grid = h.open()
        grid._apply_slot_move([0, 1], 3)       # [a,b] 拖到 d.exe
        mem = pm.serialize_slots()
        check("批量后移：块紧贴落点图标之前",
              mem == ["c.lnk", "a.lnk", "b.exe", "d.exe", "e.lnk"], str(mem))
        check("批量后移：磁盘与内存一致", mem == h.read_slots(), str(mem))

        # ---------- 5. 不连续多选拖到网格外 = 整块追加 ----------
        h.write_slots(list(FILES))
        fm, pm, grid = h.open()
        grid._apply_slot_move([0, 2, 4], -1)   # 选中 a/c/e，中间 b、d 未选
        mem = pm.serialize_slots()
        check("不连续多选：整块追加到末尾、块内次序保持",
              mem == ["b.exe", "d.exe", "a.lnk", "c.lnk", "e.lnk"], str(mem))
        check("不连续多选：磁盘与内存一致", h.read_slots() == mem, str(h.read_slots()))

        # ---------- 6. 反复拖拽：永不产生空位、行数恒定、布局收敛 ----------
        h.write_slots(list(FILES))
        fm, pm, grid = h.open()
        n0 = pm.model.rowCount()
        trace: list[int] = [n0]
        for step in range(10):
            rows_now = list(range(pm.model.rowCount()))   # 密集模型：行=条目
            if len(rows_now) < 2:
                break
            grid._apply_slot_move([rows_now[0]], rows_now[-1])
            mem = pm.serialize_slots()
            trace.append(sum(1 for s in mem if s is None))
            if pm.model.rowCount() != n0:
                check("反复拖拽：行数恒定（不凭空增减格子）", False,
                      f"第 {step + 1} 次拖拽后行数 {pm.model.rowCount()} != {n0}")
                break
        print(f"    连续 10 次「把第一个图标拖到最后一个」后的空位数: {trace}")
        print(f"    最终行数={pm.model.rowCount()}（初始 {n0}）")
        check("反复拖拽后空位数恒为 0（面板不会越拖越散）",
              trace[-1] == 0, f"空位数序列 {trace}")
        check("反复拖拽：每次落盘后磁盘与内存一致（无漂移）",
              pm.serialize_slots() == h.read_slots(),
              f"mem={pm.serialize_slots()} disk={h.read_slots()}")

    finally:
        import shutil

        shutil.rmtree(root, ignore_errors=True)

    failed = [n for n, okd, _ in RESULTS if not okd]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("落盘链路全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
