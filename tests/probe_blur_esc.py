"""面板关闭路径探针（不是 unittest，防被 discover 收走）——第二轮回归重点。

覆盖团队要求的两组用例：
A. 失焦关闭"三情形"：点其他应用窗口 / 点桌面（Progman）/ 点任务栏（Shell_TrayWnd）；
   每种情形都先确认"面板真的失去了激活状态"，否则记"无法验证"而不是误判 FAIL。
B. Esc 关闭面板：面板打开时按 Esc → 面板关闭；
   编辑态下按 Esc → 只退出编辑态、面板不关（与设计/README 一致）。

现场保护（2026-11-12 事故后重做）：``app.paths`` 整个命名空间在 main() 里第一步
就被重定向到临时沙箱（``tests/sandbox.py``），探针往分类目录里放的 ``zz_qa_item.lnk``、
配置回写、图标落点全部在沙箱内；真实 ``%USERPROFILE%\\DeskCountChen`` 只读一次取 sha256
指纹，结束时断言零改动。真实桌面同样不被写入（落点指向临时目录）。
**不再使用"备份 + 无条件还原"**：finally 不保证执行。

用法：.venv\\Scripts\\python.exe tests\\probe_blur_esc.py
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import tempfile
import time
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QEvent, QPointF, Qt  # noqa: E402
from PySide6.QtGui import QKeyEvent, QMouseEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前完成重定向
from app.controller import AppController  # noqa: E402

PROBE_CAT_DIR = "QA探针 分类"

RESULTS: list[tuple[str, bool, str]] = []
user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND


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


def send_escape(app, panel) -> None:
    """把 Esc 发给真正持有焦点的控件（若焦点在面板子树内），否则发给面板本身。"""
    target = QApplication.focusWidget()
    inside = False
    w = target
    while w is not None:
        if w is panel:
            inside = True
            break
        w = w.parentWidget()
    widget = target if (inside and target is not None) else panel
    ev = QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)
    QApplication.sendEvent(widget, ev)
    info("Esc 事件发送对象", f"{type(widget).__name__}（panel 子树内={inside}）")


def run_case(app, controller, cat_id: str, label: str, trigger, expect_close: bool = True) -> None:
    """打开面板 → 触发失焦/按键 → 断言面板是否关闭。"""
    controller.open_panel(cat_id)
    pump(app, 0.8)
    pw = controller.panel
    if pw is None:
        check(f"{label}: 面板已打开", False, "open_panel 未产生面板")
        return
    was_active = pw.isActiveWindow()
    info(f"{label} 前置", f"panel.isActiveWindow={was_active}")

    trigger()
    pump(app, 0.8)

    still_open = controller.panel is not None
    # 面板关闭后会被 deleteLater 销毁，此时访问 C++ 对象会抛 RuntimeError；
    # "对象已销毁"本身就是"面板已关闭"的证据。
    try:
        visible = pw.isVisible()
        deactivated = not pw.isActiveWindow()
    except RuntimeError:
        visible = False
        deactivated = True
    info(f"{label} 结果", f"still_open={still_open} visible={visible} "
                          f"deactivated={deactivated} activeWindow={QApplication.activeWindow()}")

    if expect_close:
        if not deactivated:
            check(f"{label}: 面板失焦后自动关闭", False,
                  "本环境下未能真正让面板失激活，结果不可判定（需人工）")
        else:
            check(f"{label}: 面板失焦后自动关闭", (not still_open) and (not visible),
                  f"still_open={still_open} visible={visible}")
    else:
        check(f"{label}: 面板保持打开", still_open and visible,
              f"still_open={still_open} visible={visible}")

    if controller.panel is not None:
        controller.close_panel()
        pump(app, 0.4)


def _run() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(4)
    controller = AppController(app)
    # v1.2：桌面图标是真实 .lnk，探针绝不能往用户桌面写文件 → 全部指向临时目录
    # v1.4：默认落点已改为私有 .launchers；探针验的是桌面链路，显式切回 desktop
    controller.shortcuts.mode = "desktop"
    probe_desktop = Path(tempfile.mkdtemp(prefix="dbx_probe_desktop_"))
    controller.shortcuts.desktop_dir = probe_desktop
    controller.shortcuts.state_path = probe_desktop / "lnk_state.json"
    controller.shortcuts.ico_dir = probe_desktop / "ico"
    controller.start()
    pump(app, 1.2)

    cats = controller.cfg.categories
    if not cats:
        print("没有分类，无法验证")
        return 1
    cat_id = cats[0].id

    # ---------- B. Esc 关闭面板（先验，避免被失焦逻辑干扰） ----------
    print("\n--- B. Esc 关闭面板 ---")

    def noop() -> None:
        send_escape(app, controller.panel)

    run_case(app, controller, cat_id, "Esc（非编辑态）", noop, expect_close=True)

    # 编辑态 Esc：只退出编辑态，面板不关（用真实"按下 400ms"路径触发）
    case_dir = cats[0].dir_path(paths.ROOT)
    probe_item = case_dir / "zz_qa_item.lnk"
    probe_item.write_bytes(b"")
    try:
        controller.open_panel(cat_id)
        pump(app, 0.8)
        pw = controller.panel
        rows = pw.grid.panel_model.model.rowCount() if pw is not None else 0
        check("面板里有条目可用于编辑态验证", rows >= 1, f"rowCount={rows}")
        if pw is not None and rows >= 1:
            grid = pw.grid
            rect = grid.visualRect(grid.model().index(0, 0))
            local = (rect.center().x(), rect.center().y())
            glob = (pw.x() + local[0], pw.y() + local[1])
            press = QMouseEvent(QEvent.Type.MouseButtonPress, QPointF(*local), QPointF(*glob),
                                Qt.MouseButton.LeftButton, Qt.MouseButton.LeftButton,
                                Qt.KeyboardModifier.NoModifier)
            QApplication.sendEvent(grid.viewport(), press)
            info("已按下条目，等待 400ms 长按定时器", "…")
            pump(app, 0.7)   # 让 400ms 定时器真的到期
            info("编辑态", f"edit_mode={grid.edit_mode} 抖动定时器={grid._jiggle_timer.isActive()} "
                          f"选中数={len(grid.selectionModel().selectedIndexes())}")
            check("长按 400ms 进入编辑态（无异常）", grid.edit_mode)
            check("长按进入编辑态：抖动定时器已启动", grid._jiggle_timer.isActive())
            check("长按进入编辑态：条目被选中",
                  len(grid.selectionModel().selectedIndexes()) >= 1,
                  str(len(grid.selectionModel().selectedIndexes())))
            QApplication.sendEvent(grid.viewport(),
                                   QMouseEvent(QEvent.Type.MouseButtonRelease, QPointF(*local),
                                               QPointF(*glob), Qt.MouseButton.LeftButton,
                                               Qt.MouseButton.NoButton,
                                               Qt.KeyboardModifier.NoModifier))
            pump(app, 0.3)
            check("长按松手不会启动程序（编辑态吞掉点击）", controller.panel is not None)
            send_escape(app, pw)
            pump(app, 0.5)
            check("编辑态按 Esc：退出编辑态", not grid.edit_mode, str(grid.edit_mode))
            check("编辑态按 Esc：面板不关闭", controller.panel is not None)
            controller.close_panel()
            pump(app, 0.4)
    finally:
        probe_item.unlink(missing_ok=True)

    # ---------- A. 失焦关闭三情形 ----------
    print("\n--- A1. 点其他应用窗口 ---")
    other_holder: list[QWidget] = []

    def click_other_app() -> None:
        other = QWidget()
        other.setWindowTitle("QA 另一个窗口")
        other.setGeometry(400, 300, 160, 120)
        other.show()
        other.raise_()
        other.activateWindow()
        other_holder.append(other)

    run_case(app, controller, cat_id, "A1 点其他应用", click_other_app)
    for w in other_holder:
        w.close()
        w.deleteLater()
    pump(app, 0.3)

    print("\n--- A2. 点桌面（Progman） ---")
    # v1.2 起 window_ops 不再封装 Progman（悬浮窗体系已移除），探针直接查
    progman = int(user32.FindWindowW("Progman", None) or 0)
    info("Progman hwnd", hex(progman))
    run_case(app, controller, cat_id, "A2 点桌面",
             lambda: (user32.SetForegroundWindow(progman), pump(app, 0.3)))

    print("\n--- A3. 点任务栏（Shell_TrayWnd） ---")
    tray = int(user32.FindWindowW("Shell_TrayWnd", None) or 0)
    info("Shell_TrayWnd hwnd", hex(tray))
    run_case(app, controller, cat_id, "A3 点任务栏",
             lambda: (user32.SetForegroundWindow(tray), pump(app, 0.3)))

    controller.shutdown()
    pump(app, 0.3)
    app.quit()
    shutil.rmtree(probe_desktop, ignore_errors=True)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("关闭路径探针全部通过")
    return 0


def _seed_sandbox_config() -> Path:
    """在沙箱里造一个分类（配置 + 目录 + 一个条目），探针才有东西可点。

    这样探针不必依赖用户真实配置：真实数据根一个字节都不碰。
    """
    sandbox_root = sandbox.sandbox_paths(prefix="dbx_probe_blur_esc_")
    paths.ensure_base_dirs()
    cat_dir = paths.DATA_ROOT / PROBE_CAT_DIR
    cat_dir.mkdir(parents=True, exist_ok=True)
    (cat_dir / "probe_item.lnk").write_bytes(b"")
    config = {
        "version": 1,
        "app": {},
        "categories": [
            {
                "id": "cat_probe_a",
                "dir": PROBE_CAT_DIR,
                "label": "QA探针",
                "panel_w": 360,
                "panel_h": 260,
            }
        ],
    }
    # 这里的 CONFIG_PATH 已经指向沙箱（上一行 sandbox_paths 已重定向），不是用户配置
    paths.CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"沙箱数据根：{paths.DATA_ROOT}（真实目录不再被写入）")
    return sandbox_root


def main() -> int:
    """探针入口：真实配置只读取指纹 → 沙箱 → 跑用例 → 断言零改动。"""
    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")

    sandbox_root = _seed_sandbox_config()
    code = 1
    try:
        code = _run()
    finally:
        shutil.rmtree(sandbox_root, ignore_errors=True)
        ok, detail = sandbox.guard_real_untouched(real)
        check("真实用户配置零改动（sha256 一致）", ok, detail)
        if not ok:
            code = 1          # 护栏失败一律判失败，不能被用例的 0 覆盖
    return code


if __name__ == "__main__":
    sys.exit(main())
