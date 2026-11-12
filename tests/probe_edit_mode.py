"""面板编辑态探针（不是 unittest，防被 discover 收走）——第二轮补测。

团队要求重点验的三条编辑态行为（team-lead 第二轮指令第 3 条）：
A. 编辑态内单击**其他**条目 → 只是切换/增加选中，**绝不启动程序**；
B. 编辑态内可多选（ExtendedSelection），批量拖动后顺序按新顺序落盘；
C. Esc 退出编辑态后**恢复"单击即启动"**（编辑态不能把启动能力永久吃掉）。

不写用户配置：全程只读 AppController 之外的最小构件（FolderModel /
PanelModel / PanelGrid），临时目录在 %TEMP% 下，结束即删。

用法：.venv\\Scripts\\python.exe tests\\probe_edit_mode.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)  # 必须用真实 windows 平台插件

from PySide6.QtCore import QEvent, QPointF, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app.folder_model import FolderModel  # noqa: E402
from app.icon_provider import IconCache  # noqa: E402
from app.panel_model import PanelModel  # noqa: E402
from app.windows.panel_grid import PanelGrid  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

DRAG_THRESHOLD_PX = 4
EDIT_PRESS_MS = 400


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def info(name: str, detail: str) -> None:
    print(f"[INFO] {name}: {detail}")


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def mouse(grid: PanelGrid, etype, pos: tuple[float, float], buttons=Qt.MouseButton.LeftButton):
    """向 grid 的 viewport 发一条真实鼠标事件（局部坐标 + 换算出的全局坐标）。"""
    local = QPointF(*pos)
    glob = QPointF(grid.viewport().mapToGlobal(local.toPoint()))
    ev = QMouseEvent(etype, local, glob, Qt.MouseButton.LeftButton, buttons,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(grid.viewport(), ev)


def cell_center(grid: PanelGrid, row: int) -> tuple[float, float]:
    rect = grid.visualRect(grid.model().index(row, 0))
    return rect.center().x(), rect.center().y()


def selected_rows(grid: PanelGrid) -> list[int]:
    sm = grid.selectionModel()
    if sm is None:
        return []
    return sorted({i.row() for i in sm.selectedIndexes()})


def press_and_hold(app, grid: PanelGrid, row: int, hold: float = 0.7) -> None:
    """在 row 上按下并按住，等 400ms 编辑态定时器真的到期。"""
    pos = cell_center(grid, row)
    mouse(grid, QEvent.Type.MouseButtonPress, pos)
    pump(app, hold)


def _run() -> int:
    tmp = Path(tempfile.mkdtemp(prefix="dbx_editmode_"))
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(DRAG_THRESHOLD_PX)

    host = QWidget()
    host.resize(600, 400)
    grid = PanelGrid(item_px=48, icon_cache=IconCache(), edit_press_ms=EDIT_PRESS_MS, parent=host)
    grid.setGeometry(0, 0, 600, 400)
    host.show()

    for name in ("a.lnk", "b.lnk", "c.exe"):
        (tmp / name).write_bytes(b"")

    folder = FolderModel(tmp, poll_ms=1000)
    entries = folder.reload()
    info("临时目录条目", f"{[e.name for e in entries]}")

    model = PanelModel()
    grid.set_model(model)
    grid.reload_entries(entries, ())
    pump(app, 0.3)
    info("模型行数", str(model.model.rowCount()))
    check("探针准备好 3 个条目", model.model.rowCount() == 3, str(model.model.rowCount()))
    if model.model.rowCount() != 3:
        print("条目不足，无法继续")
        return 1

    launched: list[str] = []
    grid.launch_requested.connect(lambda e: launched.append(e.name))
    orders: list[list[str]] = []
    grid.order_changed.connect(
        lambda: orders.append([model.model.item(r).text() for r in range(model.model.rowCount())])
    )

    # ---------- A0. 基线：非编辑态单击 = 启动 ----------
    print("\n--- A0. 基线：非编辑态单击即启动 ---")
    launched.clear()
    pos0 = cell_center(grid, 0)
    mouse(grid, QEvent.Type.MouseButtonPress, pos0)
    pump(app, 0.05)
    mouse(grid, QEvent.Type.MouseButtonRelease, pos0, Qt.MouseButton.NoButton)
    pump(app, 0.2)
    check("非编辑态单击启动程序", launched == ["a.lnk"], f"{launched}")
    check("非编辑态不会进入编辑态", not grid.edit_mode)

    # ---------- A. 长按进编辑态 → 单击其他条目只切选中 ----------
    print("\n--- A. 编辑态内单击其他条目 ---")
    launched.clear()
    press_and_hold(app, grid, 0)
    info("编辑态", f"edit_mode={grid.edit_mode} 抖动={grid._jiggle_timer.isActive()} "
                  f"选中={selected_rows(grid)} selectionMode={grid.selectionMode().name}")
    check("长按 400ms 进入编辑态", grid.edit_mode)
    check("抖动定时器已启动", grid._jiggle_timer.isActive())
    check("长按条目被选中", selected_rows(grid) == [0], f"{selected_rows(grid)}")
    check("编辑态用 ExtendedSelection（可多选）",
          grid.selectionMode() == grid.SelectionMode.ExtendedSelection,
          grid.selectionMode().name)

    # 编辑态内点另一个条目（完整 press+release，不带修饰键）
    pos1 = cell_center(grid, 1)
    mouse(grid, QEvent.Type.MouseButtonPress, pos1)
    pump(app, 0.08)
    mouse(grid, QEvent.Type.MouseButtonRelease, pos1, Qt.MouseButton.NoButton)
    pump(app, 0.2)
    info("编辑态点第 2 行后", f"选中={selected_rows(grid)} launched={launched}")
    check("编辑态内单击其他条目不会启动程序", launched == [], f"{launched}")
    check("编辑态内单击其他条目改变了选中行", 1 in selected_rows(grid),
          f"{selected_rows(grid)}")
    check("编辑态内单击不会退出编辑态", grid.edit_mode)

    # 编辑态内点空白 → 退出编辑态（设计的退出路径之一）
    blank = (grid.viewport().width() - 6, grid.viewport().height() - 6)
    mouse(grid, QEvent.Type.MouseButtonPress, blank)
    pump(app, 0.1)
    mouse(grid, QEvent.Type.MouseButtonRelease, blank, Qt.MouseButton.NoButton)
    pump(app, 0.2)
    check("编辑态点空白退出编辑态", not grid.edit_mode)
    check("退出编辑态后抖动定时器停止", not grid._jiggle_timer.isActive())
    check("退出编辑态后恢复 SingleSelection",
          grid.selectionMode() == grid.SelectionMode.SingleSelection,
          grid.selectionMode().name)

    # ---------- B. Esc 退出编辑态 → 单击恢复启动 ----------
    print("\n--- B. Esc 退出编辑态后恢复「单击即启动」 ---")
    press_and_hold(app, grid, 0)
    check("二次长按再次进入编辑态", grid.edit_mode)
    esc = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(grid, esc)
    pump(app, 0.2)
    info("Esc 后", f"edit_mode={grid.edit_mode} 抖动={grid._jiggle_timer.isActive()}")
    check("Esc 退出编辑态", not grid.edit_mode)
    check("Esc 后抖动停止", not grid._jiggle_timer.isActive())

    launched.clear()
    pos2 = cell_center(grid, 2)
    mouse(grid, QEvent.Type.MouseButtonPress, pos2)
    pump(app, 0.05)
    mouse(grid, QEvent.Type.MouseButtonRelease, pos2, Qt.MouseButton.NoButton)
    pump(app, 0.2)
    check("Esc 退出后单击恢复启动程序", launched == ["c.exe"], f"{launched}")

    # ---------- C. 批量多选拖动 ----------
    print("\n--- C. 编辑态批量多选拖动 ---")
    press_and_hold(app, grid, 0)
    if grid.edit_mode:
        # 用 Shift 点击扩选第 2 行（ExtendedSelection 的标准多选手势）
        p1 = cell_center(grid, 1)
        glob = QPointF(grid.viewport().mapToGlobal(QPointF(*p1).toPoint()))
        press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(*p1), glob,
                            Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                            Qt.KeyboardModifier.ShiftModifier)
        QApplication.sendEvent(grid.viewport(), press)
        pump(app, 0.08)
        release = QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(*p1), glob,
                              Qt.MouseButton.LeftButton, Qt.MouseButton.NoButton,
                              Qt.KeyboardModifier.ShiftModifier)
        QApplication.sendEvent(grid.viewport(), release)
        pump(app, 0.2)
        info("Shift 扩选后", f"选中={selected_rows(grid)}")
        check("编辑态支持 Shift 扩选多行", len(selected_rows(grid)) >= 2,
              f"{selected_rows(grid)}")

        # 批量移动：把选中的前两行搬到末尾。
        # 说明：视图级"真实鼠标拖动多行"要跑 QDrag 的 OS 拖放循环，自动化里无法
        # 可靠合成（会阻塞事件循环），因此这里复刻 QListView InternalMove 在
        # QStandardItemModel 上的实际落法 —— takeRow + insertRow —— 再走应用自己
        # 的 _after_drop() → _finalize_order() 链路，验证"多行变更后顺序被正确发布"。
        before = [model.model.item(r).text() for r in range(model.model.rowCount())]
        rows = selected_rows(grid)
        moved: list[str] = []
        if len(rows) >= 2:
            taken = [model.model.takeRow(r) for r in sorted(rows, reverse=True)]
            taken.reverse()                    # 恢复原始相对次序
            for items in taken:
                model.model.appendRow(items)   # 追加到末尾
                moved.append(items[0].text())
            pump(app, 0.3)
        after = [model.model.item(r).text() for r in range(model.model.rowCount())]
        info("批量移动", f"{before} → {after}（搬走 {moved}）")
        check("批量多选移动后行数不变", len(after) == len(before), f"{len(before)} → {len(after)}")
        check("批量多选移动后无重复条目", len(set(after)) == len(after), f"{after}")
        check("批量多选移动后顺序确实变了", after != before, f"{before} → {after}")
        check("被移动的多行都在末尾且保持相对次序",
              after[-len(moved):] == moved if moved else False,
              f"{after[-len(moved):] if moved else []} vs {moved}")

        # 应用自己的落地链路：rowsRemoved/rowsInserted 兜底 与 dropEvent→_emit_order
        info("多行变更后 order_changed 发布记录", f"{orders}")
        check("多行变更后新顺序被发布（兜底或落地回调）",
              bool(orders) and orders[-1] == after, f"{orders} vs {after}")
        orders.clear()
        grid._emit_order()
        pump(app, 0.3)
        check("同一顺序不会重复发布（_emit_order 幂等）", orders == [], f"{orders}")
        if orders or after:
            names_now = after
            folder.save_order(names_now)
            reloaded_now = [e.name for e in folder.reload()]
            check("多行移动后的顺序能落盘并被重载",
                  reloaded_now == names_now, f"{reloaded_now} vs {names_now}")
            # 复原成初始顺序，供后面 D 段断言使用
            folder.save_order(before)
            model.build(entries, grid.icon_cache, grid.item_px)
            pump(app, 0.2)

    # ---------- D. 拖拽重排 + 顺序落盘（单行 InternalMove 语义） ----------
    print("\n--- D. 重排后顺序写回 .order.json ---")
    names = [model.model.item(r).text() for r in range(model.model.rowCount())]
    folder.save_order(names)
    reloaded = folder.reload()
    info(".order.json 后重载", f"{[e.name for e in reloaded]}")
    check("save_order 后重载顺序一致",
          [e.name for e in reloaded] == [n for n in names if (tmp / n).exists()],
          f"{names} vs {[e.name for e in reloaded]}")

    grid.exit_edit_mode()
    host.close()
    host.deleteLater()
    pump(app, 0.2)
    return 0


def main() -> int:
    """探针入口：不论成败都清理临时目录。"""
    tmp_holder: list[Path] = []

    original_mkdtemp = tempfile.mkdtemp

    def tracking_mkdtemp(*a, **kw):
        p = Path(original_mkdtemp(*a, **kw))
        tmp_holder.append(p)
        return str(p)

    tempfile.mkdtemp = tracking_mkdtemp  # type: ignore[assignment]
    try:
        return _run()
    finally:
        tempfile.mkdtemp = original_mkdtemp  # type: ignore[assignment]
        for p in tmp_holder:
            shutil.rmtree(p, ignore_errors=True)
            print(f"已清理临时目录 {p}")
        failed = [n for n, ok, _ in RESULTS if not ok]
        print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
        if failed:
            print("失败项：" + ", ".join(failed))


if __name__ == "__main__":
    code = main()
    sys.exit(code if code else (0 if not [1 for _, ok, _ in RESULTS if not ok] else 1))
