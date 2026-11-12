"""聚焦探针：面板「激活事件路径」(deactivate/120ms) 会在哪些时机把面板关掉？

背景：``tests/qa_v15_verify.py`` 的 B2（拖拽排序结束）与 B3（show() 后 80ms 外点）
都观测到 ``close_reason == 'deactivate'``。本探针只做取证，不作判定：

1. 给 ``PanelWindow.event`` / ``changeEvent`` 加日志旁路（记录每个激活相关事件的时间）；
2. 给 ``close_with_reason`` 加**委托式**间谍（记录原因 + 调用栈，行为不变）；
3. 复现三种时序：① 静置→面板内拖拽结束 ② show() 后 80ms 外部单击
   ③ show() 后 80ms 点击本身就在**面板内**（不该失活）。

输出直接回答："deactivate 是被真实 WindowDeactivate/ActivationChange 触发的，
还是被别的路径触发的"，以及"拖拽结束关面板"是否可稳定复现。

用法：``.venv\\Scripts\\python.exe tests\\qa_probe_deactivate.py``
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import threading
import time
import traceback
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QEvent, QPoint  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.windows.panel_window import PanelWindow  # noqa: E402

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

EVENTS: list[tuple[float, str]] = []
CLOSES: list[tuple[float, str, str]] = []
T0 = time.monotonic()


def note(msg: str) -> None:
    EVENTS.append((time.monotonic() - T0, msg))
    print(f"  [{time.monotonic() - T0:7.3f}s] {msg}")


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def to_phys(x: float, y: float) -> tuple[int, int]:
    screen = QGuiApplication.primaryScreen()
    dpr = float(screen.devicePixelRatio()) or 1.0
    geo = screen.geometry()
    return int(round(geo.x() * dpr + x * dpr)), int(round(geo.y() * dpr + y * dpr))


def mouse(steps: list[tuple]) -> threading.Thread:
    def _run() -> None:
        for step in steps:
            k = step[0]
            if k == "move":
                user32.SetCursorPos(int(step[1]), int(step[2]))
            elif k == "down":
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
            elif k == "up":
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
            elif k == "sleep":
                time.sleep(float(step[1]))

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


RELEVANT = {
    QEvent.Type.WindowActivate: "WindowActivate",
    QEvent.Type.WindowDeactivate: "WindowDeactivate",
    QEvent.Type.ActivationChange: "ActivationChange",
    QEvent.Type.FocusIn: "FocusIn",
    QEvent.Type.FocusOut: "FocusOut",
    QEvent.Type.Show: "Show",
    QEvent.Type.Hide: "Hide",
    QEvent.Type.Close: "Close",
    QEvent.Type.WindowStateChange: "WindowStateChange",
}


def install_spies() -> tuple:
    orig_event = PanelWindow.event
    orig_change = PanelWindow.changeEvent
    orig_close = PanelWindow.close_with_reason

    def event(self, ev):                              # noqa: ANN001
        name = RELEVANT.get(ev.type())
        if name and name != "Show":
            try:
                active = QApplication.activeWindow()
            except RuntimeError:
                active = None
            note(f"event({name}) isActive={self.isActiveWindow()} "
                 f"activeWindow={'self' if active is self else active}")
        return orig_event(self, ev)

    def change_event(self, ev):                       # noqa: ANN001
        if ev.type() == QEvent.Type.ActivationChange:
            note(f"changeEvent(ActivationChange) isActive={self.isActiveWindow()}")
        return orig_change(self, ev)

    def close_with_reason(self, reason):              # noqa: ANN001
        stack = "".join(traceback.format_stack(limit=8)[:-1])
        CLOSES.append((time.monotonic() - T0, str(reason), stack))
        note(f"close_with_reason({reason!r}) 调用栈:\n{stack}")
        return orig_close(self, reason)

    PanelWindow.event = event                          # type: ignore[method-assign]
    PanelWindow.changeEvent = change_event             # type: ignore[method-assign]
    PanelWindow.close_with_reason = close_with_reason  # type: ignore[method-assign]
    return orig_event, orig_change, orig_close


def uninstall_spies(orig: tuple) -> None:
    PanelWindow.event = orig[0]                        # type: ignore[method-assign]
    PanelWindow.changeEvent = orig[1]                  # type: ignore[method-assign]
    PanelWindow.close_with_reason = orig[2]            # type: ignore[method-assign]


def seed() -> list[str]:
    root = sandbox.sandbox_paths(prefix="dbx_probe_deact_")
    paths.ensure_base_dirs()
    cats = []
    for i, name in enumerate(["甲", "乙"]):
        d = paths.DATA_ROOT / name
        d.mkdir(parents=True, exist_ok=True)
        for n in range(8):
            (d / f"e{n}.txt").write_text("x")
        cats.append({"id": f"c{i}", "dir": name, "label": name,
                     "panel_w": 420, "panel_h": 300, "position_mode": "anchor",
                     "material": "solid"})
    paths.CONFIG_PATH.write_text(json.dumps(
        {"version": 1, "app": {"data_root": str(paths.DATA_ROOT)}, "categories": cats},
        ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"沙箱：{root}")
    return [c["id"] for c in cats]


def case_drag(app, controller, cat_id: str) -> None:
    print("\n--- ① 静置 → 面板内拖拽排序（终点仍在面板内）→ 松手 ---")
    EVENTS.clear()
    controller.open_panel(cat_id)
    pump(app, 1.0)
    pw = controller.panel
    if pw is None:
        note("面板未打开")
        return
    note(f"面板稳定：geo={pw.frameGeometry().x()},{pw.frameGeometry().y()} "
         f"{pw.frameGeometry().width()}x{pw.frameGeometry().height()}")
    model = pw.grid.model()
    r0 = pw.grid.visualRect(model.index(0, 0))
    r2 = pw.grid.visualRect(model.index(2, 0))
    src = pw.grid.viewport().mapToGlobal(r0.center())
    dst = pw.grid.viewport().mapToGlobal(r2.center())
    sx, sy = to_phys(src.x(), src.y())
    dx, dy = to_phys(dst.x(), dst.y())
    note(f"拖拽 {src.x()},{src.y()} → {dst.x()},{dst.y()}")
    saved = cursor_pos()
    t = mouse([("move", sx, sy), ("sleep", 0.08), ("down",), ("sleep", 0.12),
               ("move", dx, dy), ("sleep", 0.40), ("move", dx, dy + 5), ("sleep", 0.2),
               ("up",)])
    pump(app, 1.0)
    note(f"拖拽循环已返回，controller.panel={'存活' if controller.panel else 'None'}")
    t.join(3.0)
    pump(app, 1.0)
    user32.SetCursorPos(saved[0], saved[1])
    note(f"最终 controller.panel={'存活' if controller.panel else 'None'}")
    print(f"  结论：拖拽期间/之后是否关闭 = {controller.panel is None}"
          f"；关闭原因 = {CLOSES[-1][1] if CLOSES else '无'}")
    if controller.panel:
        controller.close_panel()
    pump(app, 0.4)


def case_click(app, controller, cat_id: str, where: str) -> None:
    print(f"\n--- ② show() 后 80ms 单击（落点=面板{where}）---")
    EVENTS.clear()
    t0 = time.monotonic()
    controller.open_panel(cat_id)
    pw = controller.panel
    if pw is None:
        note("面板未打开")
        return
    geo = pw.frameGeometry()
    if where == "外":
        avail = QGuiApplication.primaryScreen().availableGeometry()
        pt = QPoint(avail.right() - 90, avail.top() + 90)
        if geo.contains(pt):
            pt = QPoint(avail.left() + 90, avail.bottom() - 90)
    else:
        pt = QPoint(geo.left() + 12, geo.top() + 8)     # 标题区，无副作用
    px, py = to_phys(pt.x(), pt.y())
    saved = cursor_pos()
    t = mouse([("move", px, py), ("sleep", 0.04), ("down",), ("sleep", 0.08), ("up",)])
    pump(app, 1.2)
    t.join(2.0)
    user32.SetCursorPos(saved[0], saved[1])
    elapsed = (time.monotonic() - t0) * 1000
    note(f"落点={pt.x()},{pt.y()} 在面板内={geo.contains(pt)}；"
         f"面板{'已关闭' if controller.panel is None else '仍存活'}")
    print(f"  结论：{elapsed:.0f}ms 后 controller.panel = "
          f"{'None(关闭)' if controller.panel is None else '存活'}"
          f"；关闭原因 = {CLOSES[-1][1] if CLOSES else '无'}")
    if controller.panel:
        controller.close_panel()
    pump(app, 0.4)


def main() -> int:
    global T0
    ids = seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(4)
    controller = AppController(app)
    controller.start()
    pump(app, 1.0)
    ids = [c.id for c in controller.cfg.categories]
    orig = install_spies()
    try:
        T0 = time.monotonic()
        case_drag(app, controller, ids[0])
        case_click(app, controller, ids[0], "外")
        case_click(app, controller, ids[0], "内")
    finally:
        uninstall_spies(orig)
        controller.close_panel()
        pump(app, 0.3)
        controller.shutdown()

    print("\n=== 关闭事件汇总 ===")
    for t, reason, _ in CLOSES:
        print(f"  {t:7.3f}s  reason={reason}")
    print("\n=== 激活事件时间线 ===")
    for t, msg in EVENTS:
        first = msg.splitlines()[0]
        print(f"  {t:7.3f}s  {first}")
    root = sandbox.sandbox_root()
    if root:
        shutil.rmtree(root, ignore_errors=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
