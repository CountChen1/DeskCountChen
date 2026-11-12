"""【P0-1】面板关不掉 —— 缺陷复现探针（不是 unittest，防被 discover 收走）。

## 复现的用户现象

> 「呼出面板后再点击桌面关不掉，必须要点击一次面板内部再点击桌面才会关掉；
>   呼出面板 10 秒内鼠标没有任何动作的话，点击桌面 100% 关不掉。」

## 根因假设（本探针负责取证，而不是"看起来像"）

面板由 SAO Utils 的 HUD 图标唤起 ``pythonw`` 后台进程。Windows 有前台锁定
（``SPI_GETFOREGROUNDLOCKTIMEOUT``），后台进程的 ``SetForegroundWindow`` 常被拒绝
→ 面板 **从未真正成为前台窗口** → 点桌面不会产生 ``WindowDeactivate`` /
``ActivationChange(False)`` → ``_check_deactivate`` 永不被调用 → 面板永不关闭。
用户点一次面板内部时，真实鼠标点击让 Windows 真正激活它，此后 Deactivate 才正常。

## 本探针如何**确定性地**制造这个前置条件

真实环境里"能否抢到前台"取决于当前前台进程是谁，不能靠运气。因此探针做
**故障注入**（fault injection），等价于"Windows 拒绝了 SetForegroundWindow"：

1. 面板 ``show()`` 时附加 ``WA_ShowWithoutActivating``（Qt 层不激活）；
2. 面板 ``activateWindow()`` 换成"把前台让给 Progman"（系统层不激活）。

注入后取证 ``GetForegroundWindow() != 面板 hwnd``，即进入"未激活态"。
这是用户机器上的真实状态，只是这里由探针**确定地**建立，而不是等它偶然发生。

## 用例

| 用例 | 前置 | 动作 | 期望 |
|---|---|---|---|
| A | 面板未激活（故障注入） | 点击面板**外**（屏幕空白处 / 桌面） | 面板关闭（v1.4 实测：**不关** → 缺陷） |
| B | 面板未激活（故障注入） | 点击面板**内**（标题区，不启动条目） | 面板**保持打开**（不得误关） |
| C | 面板已激活（正常 show） | 点击面板外 | 面板关闭（既有 deactivate 路径回归） |

点击注入用 ``SetCursorPos`` + ``mouse_event``（真实左键按下/抬起）——只有这样
``GetAsyncKeyState(VK_LBUTTON)`` 与系统激活行为才真实。注入前记录鼠标原位置，
结束后还原，不改变用户桌面图标状态（单击不启动程序）。

现场保护：``app.paths`` 在 main() 第一步整体重定向到临时沙箱（``tests/sandbox.py``），
真实 ``%USERPROFILE%\\DesktopBox`` 只读一次取 sha256 指纹，结束时断言零改动。

用法：``.venv\\Scripts\\python.exe tests\\probe_close_bug.py``
"""

from __future__ import annotations

import ctypes
import json
import os
import shutil
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
# 必须清掉：真实窗口探针不能在 offscreen 平台下跑（否则 winId/前台全部失真）
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前完成重定向
from app.controller import AppController  # noqa: E402
from app.windows.panel_window import PanelWindow  # noqa: E402

try:                                             # pragma: no cover - 基线兼容
    from app.win32 import input_watch            # v1.5 才有（前台兜底）
except ImportError:                              # v1.4 基线分支
    input_watch = None

PROBE_CAT_DIR = "QA关闭探针"

RESULTS: list[tuple[str, bool, str]] = []

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.GetForegroundWindow.argtypes = []
user32.GetForegroundWindow.restype = wintypes.HWND
user32.FindWindowW.argtypes = [wintypes.LPCWSTR, wintypes.LPCWSTR]
user32.FindWindowW.restype = wintypes.HWND
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [
    wintypes.DWORD, wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
    ctypes.c_size_t,
]
user32.mouse_event.restype = None

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


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


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def panel_hwnd(pw) -> int:
    try:
        return int(pw.winId())
    except RuntimeError:      # 对象已销毁
        return 0


def progman_hwnd() -> int:
    return int(user32.FindWindowW("Progman", None) or 0)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def click_at(x: int, y: int, hold_s: float = 0.08) -> None:
    """真实左键单击（物理像素坐标）：先移动，按下，保持，抬起。

    **必须在后台线程里按住/抬起**：兜底轮询是 Qt 定时器，只有主线程回到事件
    循环才会触发。如果在主线程 ``time.sleep(hold_s)``，定时器整段时间都不会
    跑，注入的点击 100% 漏检 —— 那是探针自己制造的假失败，不是产品缺陷。
    """
    def _worker() -> None:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.06)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(hold_s)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    worker = threading.Thread(target=_worker, daemon=True)
    worker.start()
    return worker


def logical_to_physical(x: float, y: float) -> tuple[int, int]:
    """Qt 逻辑坐标 → 物理像素（DPI 120/125% 时必须换算，否则点击落错位置）。"""
    screen = QGuiApplication.primaryScreen()
    dpr = float(screen.devicePixelRatio()) or 1.0
    geo = screen.geometry()
    return (
        int(round(geo.x() * dpr + x * dpr)),
        int(round(geo.y() * dpr + y * dpr)),
    )


def pick_outside_point(panel_rect) -> tuple[int, int]:
    """挑一个在面板之外、且在屏幕可用区之内的逻辑坐标（尽量远离面板）。"""
    screen = QGuiApplication.primaryScreen()
    avail = screen.availableGeometry()
    candidates = [
        (avail.right() - 90, avail.top() + 90),     # 右上（桌面图标通常从左上排列）
        (avail.left() + 90, avail.bottom() - 90),
        (avail.right() - 90, avail.bottom() - 90),
        (avail.left() + 90, avail.top() + 90),
    ]
    for cx, cy in candidates:
        if not panel_rect.contains(cx, cy):
            return cx, cy
    return avail.left() + 40, avail.top() + 40


def install_foreground_denial() -> tuple:
    """故障注入：等价于"Windows 全程拒绝把前台交给本进程"。

    三处都注入，缺一不可（只注入一处的话 v1.5 的 force_foreground 兜底会把
    面板真的激活，注入就失效了 —— 那也就不是用户机器上的真实形态）：
    1. ``show()`` 带 ``WA_ShowWithoutActivating``（Qt 层不激活）；
    2. ``activateWindow()`` 改成把前台让给 Progman（系统层不激活）；
    3. ``input_watch.force_foreground`` 恒返回 False（系统层拒绝抢前台）。
    """
    orig_show = PanelWindow.show
    orig_activate = PanelWindow.activateWindow
    orig_force = input_watch.force_foreground if input_watch is not None else None

    def _quiet_show(self) -> None:
        # Qt 层不激活（= 系统拒绝 SetForegroundWindow 的真实表现之一）
        self.setAttribute(Qt.WidgetAttribute.WA_ShowWithoutActivating, True)
        orig_show(self)
        # 系统层把前台固定给 Progman（桌面），模拟"后台进程抢前台失败"
        user32.SetForegroundWindow(progman_hwnd())

    def _denied_activate(self) -> None:      # noqa: ANN001
        user32.SetForegroundWindow(progman_hwnd())

    PanelWindow.show = _quiet_show            # type: ignore[method-assign]
    PanelWindow.activateWindow = _denied_activate  # type: ignore[method-assign]
    if input_watch is not None:
        input_watch.force_foreground = lambda hwnd: False  # type: ignore[assignment]
    return orig_show, orig_activate, orig_force


def uninstall_foreground_denial(orig: tuple) -> None:
    PanelWindow.show = orig[0]                # type: ignore[method-assign]
    PanelWindow.activateWindow = orig[1]      # type: ignore[method-assign]
    if input_watch is not None and orig[2] is not None:
        input_watch.force_foreground = orig[2]  # type: ignore[assignment]


def open_and_verify_inactive(app, controller, cat_id: str) -> object | None:
    """故障注入下打开面板，并取证"面板确实处于未激活态"。"""
    controller.open_panel(cat_id)
    pump(app, 0.6)
    pw = controller.panel
    if pw is None:
        check("故障注入下 open_panel 产出面板", False, "controller.panel is None")
        return None
    hwnd = panel_hwnd(pw)
    fg = foreground_hwnd()
    info("面板 hwnd", hex(hwnd))
    info("GetForegroundWindow", hex(fg))
    info("isActiveWindow / QApplication.activeWindow",
         f"{pw.isActiveWindow()} / {QApplication.activeWindow()}")
    check("前置条件：GetForegroundWindow() != 面板 hwnd（面板未成为前台窗口）",
          bool(hwnd) and fg != hwnd, f"fg={hex(fg)} panel={hex(hwnd)}")
    watch = getattr(pw, "_watch", None)
    info("兜底轮询", f"存在={watch is not None} 运行中="
                     f"{watch.is_running() if watch is not None else 'N/A'} "
                     f"间隔={watch.interval_ms if watch is not None else '-'}ms "
                     f"宽限={watch.grace_ms if watch is not None else '-'}ms")
    return pw


def case_outside_click_inactive(app, controller, cat_id: str) -> None:
    """用例 A：未激活态 + 点击面板外 → 应关闭（v1.4 这里是红的）。"""
    print("\n--- A. 未激活态：点击面板外 → 期望关闭 ---")
    pw = open_and_verify_inactive(app, controller, cat_id)
    if pw is None:
        return
    geo = pw.frameGeometry()
    info("面板几何(逻辑)", f"{geo.x()},{geo.y()} {geo.width()}x{geo.height()}")
    pump(app, 0.5)     # 静置 0.5s：模拟"面板放开后鼠标没有动作"
    still_open = controller.panel is not None
    check("A 前置：静置 0.5s 后面板仍打开", still_open)
    if not still_open:
        return

    lx, ly = pick_outside_point(geo)
    px, py = logical_to_physical(lx, ly)
    info("点击面板外", f"逻辑=({lx},{ly}) 物理=({px},{py})")
    saved = cursor_pos()
    try:
        worker = click_at(px, py)
        # 光标必须留在落点上直到判定结束：兜底轮询靠"按键 + 落点"两路证据，
        # 立刻把鼠标移回去会让证据自相矛盾（真实用户点击后鼠标也是留在那里的）。
        pump(app, 0.9)
        worker.join(1.0)
    finally:
        user32.SetCursorPos(saved[0], saved[1])

    closed = controller.panel is None
    check("A 未激活态点面板外：面板关闭（不依赖 Qt 激活事件）", closed,
          f"controller.panel={'None(已关)' if closed else '仍非 None'}")
    if not closed:
        controller.close_panel()
        pump(app, 0.4)


def case_inside_click_inactive(app, controller, cat_id: str) -> None:
    """用例 B：未激活态 + 点击面板内（标题区）→ 不得误关。"""
    print("\n--- B. 未激活态：点击面板内（标题区）→ 期望保持打开 ---")
    pw = open_and_verify_inactive(app, controller, cat_id)
    if pw is None:
        return
    geo = pw.frameGeometry()
    # 标题区左边（HEADER_H=36 内）不会有条目、不会有按钮 → 点击无副作用
    lx, ly = geo.left() + 12, geo.top() + 8
    px, py = logical_to_physical(lx, ly)
    info("点击面板内", f"逻辑=({lx},{ly}) 物理=({px},{py})")
    saved = cursor_pos()
    try:
        worker = click_at(px, py)
        pump(app, 0.9)
        worker.join(1.0)
    finally:
        user32.SetCursorPos(saved[0], saved[1])

    kept = controller.panel is not None
    check("B 未激活态点面板内：面板保持打开（不得误关）", kept,
          f"controller.panel={'仍打开' if kept else 'None(被误关)'}")
    controller.close_panel()
    pump(app, 0.4)


def case_outside_click_active(app, controller, cat_id: str) -> None:
    """用例 C：激活态 + 点击面板外 → 关闭（既有 deactivate 路径回归）。"""
    print("\n--- C. 激活态（正常 show）：点击面板外 → 期望关闭 ---")
    controller.open_panel(cat_id)
    pump(app, 0.7)
    pw = controller.panel
    if pw is None:
        check("C open_panel 产出面板", False, "controller.panel is None")
        return
    hwnd = panel_hwnd(pw)
    fg = foreground_hwnd()
    info("C 前置", f"fg={hex(fg)} panel={hex(hwnd)} isActive={pw.isActiveWindow()}")
    if fg != hwnd:
        info("C 无法验证", "本环境下面板未能成为前台窗口，跳过（用例 A 已覆盖该形态）")
    else:
        geo = pw.frameGeometry()
        lx, ly = pick_outside_point(geo)
        px, py = logical_to_physical(lx, ly)
        saved = cursor_pos()
        try:
            worker = click_at(px, py)
            pump(app, 0.9)
            worker.join(1.0)
        finally:
            user32.SetCursorPos(saved[0], saved[1])
        closed = controller.panel is None
        check("C 激活态点面板外：面板关闭（deactivate 路径未回退）", closed,
              f"controller.panel={'None(已关)' if closed else '仍非 None'}")
    if controller.panel is not None:
        controller.close_panel()
        pump(app, 0.4)


def _run() -> int:
    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(4)
    controller = AppController(app)
    controller.start()
    pump(app, 1.0)

    cats = controller.cfg.categories
    if not cats:
        print("没有分类，无法验证")
        return 1
    cat_id = cats[0].id
    info("分类", f"{cats[0].label} ({cat_id}) dir={cats[0].dir}")

    orig = install_foreground_denial()
    try:
        case_outside_click_inactive(app, controller, cat_id)
        case_inside_click_inactive(app, controller, cat_id)
    finally:
        uninstall_foreground_denial(orig)

    case_outside_click_active(app, controller, cat_id)

    controller.shutdown()
    pump(app, 0.3)
    app.quit()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("面板关闭探针全部通过")
    return 0


def _seed_sandbox_config() -> Path:
    """在沙箱里造一个分类（配置 + 目录 + 条目），探针才有东西可点。"""
    root = sandbox.sandbox_paths(prefix="dbx_probe_close_")
    paths.ensure_base_dirs()
    cat_dir = paths.DATA_ROOT / PROBE_CAT_DIR
    cat_dir.mkdir(parents=True, exist_ok=True)
    for name in ("probe_a.lnk", "probe_b.txt"):
        (cat_dir / name).write_bytes(b"")
    config = {
        "version": 1,
        "app": {},
        "categories": [
            {
                "id": "cat_probe_close",
                "dir": PROBE_CAT_DIR,
                "label": "QA关闭探针",
                "panel_w": 420,
                "panel_h": 300,
                "position_mode": "center",
            }
        ],
    }
    paths.CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    print(f"沙箱数据根：{paths.DATA_ROOT}（真实目录不再被写入）")
    return root


def main() -> int:
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
            code = 1
    return code


if __name__ == "__main__":
    sys.exit(main())
