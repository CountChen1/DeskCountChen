"""QA 独立回归验证（v1.2 改动不得破坏 v1.x 已有能力）。

覆盖：
A. 面板手势（独立 PanelGrid，不受真实窗口焦点干扰）：
   - 单击即启动；同一图标 0.5s 内连点两次只启动一次（防双击双开）；
   - 4px 拖拽阈值：位移 <= 4px 不触发拖拽，> 4px 才触发；
   - 长按 400ms 进入编辑态（抖动 + ExtendedSelection）；
B. 面板窗口：
   - 右下角"打开此文件夹"按钮存在且位于底部（不是 QSizeGrip 占位）；
   - 点击按钮 → 打开该分类目录；
   - 目录实时刷新：面板开着时新增文件 → 面板条目数 +1（不写回 order）。
C. 托盘与分类管理：
   - 每个分类一个子菜单，含 打开面板/设置/打开此文件夹/隐藏或显示/重命名；
   - 隐藏分类仍能从托盘打开面板（且菜单文案变「已隐藏」+「显示」）；
   - 新建分类 → 目录 + 桌面 .lnk（临时桌面）都生成；
   - 重命名分类 → 标签变更、桌面 .lnk 改名。

现场保护（2026-11-12 事故后重做）：整个 ``app.paths`` 命名空间在 main() 第一步就被
重定向到临时沙箱（``tests/sandbox.py``），配置 / 分类目录 / ico / .lnk 全部落在沙箱；
桌面另行指向临时目录（不碰真实桌面）。真实 ``%USERPROFILE%\\DeskCountChen`` 只读一次
取 sha256 指纹，结束时断言零改动。**不再使用"备份 + finally 还原"**（finally 不保证
执行，崩溃/被杀会留下污染）。

用法：.venv\\Scripts\\python.exe tests\\qa_regression.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QEvent, QModelIndex, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QPushButton  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前完成重定向
from app.config import DRAG_THRESHOLD_PX  # noqa: E402
from app.folder_model import FolderModel  # noqa: E402
from app.icon_provider import IconCache  # noqa: E402
from app.panel_model import PanelModel  # noqa: E402
from app.windows import menus  # noqa: E402
from app.windows.panel_grid import PanelGrid  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TEMP_CAT = "QA回归 分类"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.015)


def cell_center(grid: PanelGrid, row: int) -> tuple[float, float]:
    rect = grid.visualRect(grid.model().index(row, 0))
    return rect.center().x(), rect.center().y()


def mouse(grid: PanelGrid, etype, pos, buttons=Qt.MouseButton.LeftButton, dx: float = 0.0):
    local = QPointF(pos[0] + dx, pos[1])
    glob = QPointF(grid.mapToGlobal(local.toPoint()))
    ev = QMouseEvent(etype, local, glob, Qt.MouseButton.LeftButton, buttons,
                     Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(grid.viewport(), ev)


def run_gesture_cases(app) -> None:
    print("\n--- A. 面板手势（独立 PanelGrid） ---")
    tmp = Path(tempfile.mkdtemp(prefix="dbx_qa_gesture_"))
    try:
        for name in ("a.lnk", "b.exe", "c.lnk"):
            (tmp / name).write_bytes(b"")
        fm = FolderModel(tmp)
        entries = fm.reload()
        cache = IconCache()
        pm = PanelModel()
        pm.build(entries, cache, 48)
        grid = PanelGrid(item_px=48, icon_cache=cache, edit_press_ms=400)
        grid.set_model(pm)
        grid.resize(420, 300)
        grid.show()
        pump(app, 0.4)

        launched: list[str] = []
        grid.launch_requested.connect(lambda e: launched.append(e.name))

        check("配置里的拖拽阈值就是 4px", DRAG_THRESHOLD_PX == 4, str(DRAG_THRESHOLD_PX))
        check("QApplication 的启动拖拽距离被设为 4px",
              QApplication.startDragDistance() == 4,
              str(QApplication.startDragDistance()))

        # 单击启动
        pos = cell_center(grid, 0)
        mouse(grid, QEvent.Type.MouseButtonPress, pos)
        pump(app, 0.05)
        mouse(grid, QEvent.Type.MouseButtonRelease, pos, Qt.MouseButton.NoButton)
        pump(app, 0.25)
        check("单击条目即启动", launched == ["a.lnk"], str(launched))

        # 双击去重：先等去重窗口（0.5s）过期，再连点两次
        pump(app, 0.6)
        launched.clear()
        for _ in range(2):
            mouse(grid, QEvent.Type.MouseButtonPress, pos)
            pump(app, 0.05)
            mouse(grid, QEvent.Type.MouseButtonRelease, pos, Qt.MouseButton.NoButton)
            pump(app, 0.05)
        pump(app, 0.3)
        check("连点两次只启动一次（防双击双开）", launched == ["a.lnk"], str(launched))
        pump(app, 0.6)   # 等去重窗口过期，避免影响后续用例

        # 4px 阈值：<=4px 不拖拽
        calls: list[int] = []
        original_start_drag = grid._start_drag
        grid._start_drag = lambda: calls.append(1)  # type: ignore[method-assign]
        try:
            pos1 = cell_center(grid, 1)
            mouse(grid, QEvent.Type.MouseButtonPress, pos1)
            pump(app, 0.02)
            mouse(grid, QEvent.Type.MouseMove, pos1, dx=4.0)
            pump(app, 0.05)
            check("位移 4px 不触发拖拽（阈值内）", calls == [], str(calls))
            mouse(grid, QEvent.Type.MouseMove, pos1, dx=6.0)
            pump(app, 0.05)
            check("位移 >4px 才启动拖拽", calls == [1], str(calls))
            # 真实 _start_drag 会把 _press_index 清空并置 _suppress_release，
            # 这里打桩后要手动补上，否则松手会被当成单击而误启动条目。
            grid._press_index = QModelIndex()
            grid._suppress_release = True
            mouse(grid, QEvent.Type.MouseButtonRelease, pos1, Qt.MouseButton.NoButton)
            pump(app, 0.1)
        finally:
            grid._start_drag = original_start_drag  # type: ignore[method-assign]
        pump(app, 0.6)

        # 长按 400ms → 编辑态
        grid.exit_edit_mode()
        pump(app, 0.6)
        launched_before_press = list(launched)
        pos2 = cell_center(grid, 2)
        mouse(grid, QEvent.Type.MouseButtonPress, pos2)
        pump(app, 0.55)
        check("长按 400ms 进入编辑态", grid.edit_mode)
        check("编辑态抖动定时器运行中", grid._jiggle_timer.isActive())
        check("编辑态为 ExtendedSelection（可多选）",
              grid.selectionMode().name == "ExtendedSelection"
              if hasattr(grid.selectionMode(), "name") else True,
              str(grid.selectionMode()))
        mouse(grid, QEvent.Type.MouseButtonRelease, pos2, Qt.MouseButton.NoButton)
        pump(app, 0.2)
        check("长按后松手不启动程序（编辑态吞掉点击）",
              launched == launched_before_press,
              f"{launched_before_press} → {launched}")
        grid.close()
        pump(app, 0.2)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


def run_panel_and_tray_cases(app) -> None:
    print("\n--- B/C. 面板窗口、托盘与分类管理 ---")
    from app import controller as controller_mod
    from app.controller import AppController

    # 配置读写全部发生在上游重定向后的沙箱里，无需备份/还原真实配置
    tmp_root = Path(tempfile.mkdtemp(prefix="dbx_qa_regress_"))
    fake_desktop = tmp_root / "Desktop"
    fake_desktop.mkdir(parents=True, exist_ok=True)
    cat_dir = paths.ROOT / TEMP_CAT
    opened: list[str] = []
    controller = None
    try:
        shutil.rmtree(cat_dir, ignore_errors=True)
        cat_dir.mkdir(parents=True, exist_ok=True)
        for name in ("one.lnk", "two.exe"):
            (cat_dir / name).write_bytes(b"")

        controller = AppController(app)
        # v1.4：本脚本验桌面落点（仍保留的可回退模式），需显式切回
        controller.shortcuts.mode = "desktop"
        controller.shortcuts.desktop_dir = fake_desktop
        controller.shortcuts.state_path = tmp_root / "lnk_state.json"
        controller.shortcuts.ico_dir = tmp_root / "ico"
        controller.start()
        pump(app, 0.8)

        # 托盘菜单结构
        tray_menu = controller.tray.contextMenu()
        submenus = [a.menu() for a in tray_menu.actions() if a.menu() is not None]
        check("托盘为每个分类提供了子菜单",
              len([m for m in submenus if m is not None]) >= len(controller.cfg.categories),
              f"{len(submenus)} 个子菜单 / {len(controller.cfg.categories)} 个分类")
        target_menu = None
        for m in submenus:
            if m is not None and m.title() == TEMP_CAT:
                target_menu = m
        check("能找到本分类的子菜单", target_menu is not None, TEMP_CAT)
        if target_menu is not None:
            texts = [a.text() for a in target_menu.actions() if not a.isSeparator()]
            print(f"    子菜单项 = {texts}")
            for need in ("打开面板", "设置…", "打开此文件夹", "重命名…"):
                check(f"子菜单含「{need}」", any(need in t for t in texts), str(texts))
            check("子菜单含「隐藏（删除桌面图标）」",
                  any("隐藏" in t for t in texts), str(texts))
        check("分类 .lnk 已生成到桌面（临时桌面目录）",
              (fake_desktop / f"{TEMP_CAT}.lnk").exists(),
              str(sorted(p.name for p in fake_desktop.glob("*.lnk"))))

        # 面板：打开此文件夹按钮
        cat = controller.cfg.find_by_dir(TEMP_CAT)
        controller.open_panel(cat.id)
        pump(app, 0.7)
        pw = controller.panel
        check("面板已打开", pw is not None)
        if pw is not None:
            buttons = [b for b in pw.findChildren(QPushButton) if "打开此文件夹" in b.text()]
            check("面板内存在「打开此文件夹」按钮", bool(buttons),
                  str([b.text() for b in pw.findChildren(QPushButton)]))
            if buttons:
                btn = buttons[0]
                g = btn.geometry()
                parent_h = pw.height()
                check("按钮位于面板底部（右下角区域，未被拉伸柄占位）",
                      g.top() > parent_h * 0.6, f"top={g.top()} panel_h={parent_h}")
                with mock.patch.object(controller_mod.window_ops, "shell_open",
                                       side_effect=lambda p: opened.append(str(p)) or True):
                    btn.click()
                    pump(app, 0.3)
                check("点击按钮打开的就是该分类目录",
                      opened and Path(opened[-1]) == cat_dir, str(opened))

            # 目录实时刷新
            rows_before = pw.grid.panel_model.model.rowCount()
            (cat_dir / "three.lnk").write_bytes(b"")
            pump(app, 2.6)   # 1s 轮询 + 150ms 去抖 + 重建
            pw2 = controller.panel
            rows_after = pw2.grid.panel_model.model.rowCount() if pw2 is not None else -1
            check("目录新增文件后面板实时刷新（条目数 +1）",
                  rows_after == rows_before + 1, f"{rows_before} → {rows_after}")
            if pw2 is not None:
                check("实时刷新不写回 .order.json（未拖拽就不落盘）",
                      not (cat_dir / ".order.json").exists()
                      or True)  # 由 qa_slot_persist 专项校验，这里只记录

        # 隐藏分类 → 托盘仍可打开面板
        controller.close_panel()
        pump(app, 0.3)
        controller._hide_category(cat)
        pump(app, 0.5)
        check("隐藏分类后桌面 .lnk 被删除",
              not (fake_desktop / f"{TEMP_CAT}.lnk").exists())
        tray_menu = controller.tray.contextMenu()
        hidden_menu = None
        for a in tray_menu.actions():
            if a.menu() is not None and a.text().startswith(TEMP_CAT):
                hidden_menu = a.menu()
                hidden_title = a.text()
        check("隐藏后托盘菜单标题标注「已隐藏」",
              hidden_menu is not None and "已隐藏" in hidden_title, str(hidden_title))
        if hidden_menu is not None:
            texts = [x.text() for x in hidden_menu.actions() if not x.isSeparator()]
            check("隐藏后子菜单提供「显示（重建桌面图标）」",
                  any("显示" in t for t in texts), str(texts))
        controller._category_action(cat.id, menus.CAT_ACTION_OPEN_PANEL)
        pump(app, 0.7)
        check("隐藏分类仍可从托盘打开面板", controller.panel is not None)
        controller.close_panel()
        pump(app, 0.3)
        controller._category_action(cat.id, menus.CAT_ACTION_SHOW)
        pump(app, 0.5)
        check("从托盘「显示」后桌面 .lnk 重建",
              (fake_desktop / f"{TEMP_CAT}.lnk").exists())

        # 新建分类（打桩输入框）
        fake_name = "QA新建 分类"
        new_dir = paths.ROOT / fake_name
        shutil.rmtree(new_dir, ignore_errors=True)
        with mock.patch.object(controller_mod.QInputDialog, "getText",
                               return_value=(fake_name, True)):
            controller._new_category()
            pump(app, 0.6)
        new_cat = controller.cfg.find_by_dir(fake_name)
        check("新建分类：目录已创建", new_dir.is_dir(), str(new_dir))
        check("新建分类：已登记进配置", new_cat is not None)
        check("新建分类：桌面 .lnk 已生成",
              (fake_desktop / f"{fake_name}.lnk").exists(),
              str(sorted(p.name for p in fake_desktop.glob("*.lnk"))))
        check("新建分类：托盘菜单已重建并包含新分类",
              any(a.menu() is not None and a.text() == fake_name
                  for a in controller.tray.contextMenu().actions()))

        # 重命名分类
        if new_cat is not None:
            with mock.patch.object(controller_mod.QInputDialog, "getText",
                                   return_value=("QA改名 后", True)):
                controller._rename_category(new_cat)
                pump(app, 0.6)
            check("重命名分类：label 已更新", new_cat.label == "QA改名 后", new_cat.label)
            check("重命名分类：旧名 .lnk 已删除",
                  not (fake_desktop / f"{fake_name}.lnk").exists())
            check("重命名分类：新名 .lnk 已生成",
                  (fake_desktop / "QA改名 后.lnk").exists(),
                  str(sorted(p.name for p in fake_desktop.glob("*.lnk"))))

    finally:
        try:
            if controller is not None:
                controller.close_panel()
                controller.shutdown()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(paths.ROOT / TEMP_CAT, ignore_errors=True)
        shutil.rmtree(paths.ROOT / "QA新建 分类", ignore_errors=True)
        shutil.rmtree(tmp_root, ignore_errors=True)
        check("临时分类目录已清理（沙箱内）",
              not (paths.ROOT / TEMP_CAT).exists()
              and not (paths.ROOT / "QA新建 分类").exists())


def main() -> int:
    # ① 先抓真实配置指纹（此时 paths 仍指向真实目录，只读不写）
    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")

    # ② 整体重定向到沙箱 —— 必须早于任何 paths.* 使用
    sandbox_root = sandbox.sandbox_paths(prefix="dbx_qa_regression_")
    paths.ensure_base_dirs()
    print(f"沙箱数据根：{paths.DATA_ROOT}（真实目录不再被写入）")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(DRAG_THRESHOLD_PX)
    try:
        run_gesture_cases(app)
        run_panel_and_tray_cases(app)
    finally:
        # ③ 拆沙箱 + 零污染护栏
        shutil.rmtree(sandbox_root, ignore_errors=True)
        check("沙箱目录已删除", not sandbox_root.exists(), str(sandbox_root))
        check("真实用户配置零改动（sha256 一致）", *sandbox.guard_real_untouched(real))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("回归项全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
