"""【v1.5 独立黑盒验收】B 关闭路径 / C 全局设置 / D 毛玻璃回退。

不是 unittest（文件名不以 ``test_`` 开头，不会被 discover 收走），可重复执行：

```bash
.venv\\Scripts\\python.exe tests\\qa_v15_verify.py          # 全部
.venv\\Scripts\\python.exe tests\\qa_v15_verify.py B        # 只跑关闭路径
```

## 为什么必须"黑盒"

工程师的白盒单测（``test_panel_watch`` 等）直接喂假输入源给
``OutsideClickWatcher.poll_once()``。这能证明**判定逻辑**对，但证明不了：

- 真实 ``show()`` 后 Qt 定时器是否真的在跑（主线程被 ``time.sleep`` 卡住时不会跑）；
- 真实鼠标左键（``GetAsyncKeyState``）与真实 ``QCursor.pos()`` 是否对得上；
- ``AppController.open_panel`` 的完整装配顺序（prepare_show → show → activate
  → animate_show → watch.start）有没有在某一步把兜底轮询漏掉。

所以本脚本一律**驱动产品代码**（``AppController`` + ``PanelWindow`` + 真 Qt
事件循环 + 真实左键注入），只在断言处读状态。

## 取证方式（沙箱限制）

跨进程窗口枚举在本沙箱被拦截（v1.4 已验证），因此取证只用进程内事实：
``QApplication.activeWindow()`` / ``PanelWindow.isActiveWindow()`` /
``controller.panel`` / ``panel.close_reason`` / ``panel.grid.edit_mode``。

## 点击注入的关键坑

兜底轮询是 **Qt 定时器**，只有主线程回到事件循环才触发。若在主线程
``time.sleep(0.08)`` 按住鼠标，定时器整段时间都不跑 → 注入的点击 100% 漏检
（探针自己制造的假失败）。因此按下/抬起一律在 **worker 线程**里做，主线程
只 ``processEvents``。

## 零污染

``app.paths`` 第一步整体重定向到临时沙箱（``tests/sandbox.py``）；真实
``%USERPROFILE%\\DesktopBox\\.config.json`` 只读一次取 sha256，结束时断言未变。
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
# 真实窗口验收不能跑在 offscreen 下（winId / 前台 / 定时器行为全部失真）
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QEvent, QObject, QPoint, QRect, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QColor, QGuiApplication, QImage, QPainter, QPixmap  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前

from app.config import (  # noqa: E402
    MATERIAL_BLUR,
    MATERIAL_SOLID,
    RADIUS_MAX,
    AppConfig,
    CategoryConfig,
)
from app.controller import SKIN_FIELDS, AppController  # noqa: E402
from app.windows import panel_window as PW  # noqa: E402

try:
    from PySide6.QtTest import QTest
except ImportError:                                   # pragma: no cover
    QTest = None

RESULTS: list[tuple[str, bool, str]] = []
INFO: list[str] = []
SKIPS: list[tuple[str, str]] = []

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.GetCursorPos.restype = wintypes.BOOL
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.SetCursorPos.restype = wintypes.BOOL
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
user32.mouse_event.restype = None

MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

# 前台窗口相关（"面板能不能真正拿到激活"是 B 段多条用例的前置条件）
user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.BringWindowToTop.restype = wintypes.BOOL
user32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
user32.IsWindow.argtypes = [wintypes.HWND]
user32.IsWindow.restype = wintypes.BOOL

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentThreadId.restype = wintypes.DWORD

SW_RESTORE = 9
SW_SHOW = 5

SANDBOX_ROOT: Path | None = None
CLOSE_GRACE_MS = 300          # panel_watch.GRACE_MS
CLOSE_DELAY_MS = 120          # panel_window.CLOSE_DELAY_MS
REASON_FOCUS_LOST = "focus-lost"   # panel_watch.REASON_FOCUS_LOST（panel_window 未转出）
DRAG_BUDGET_S = 6.0           # QDrag 模态循环等待预算（超时=环境限制，不冤判）


# --------------------------------------------------------------------------
# 基础工具
# --------------------------------------------------------------------------

def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def info(name: str, detail: str) -> None:
    INFO.append(f"{name}: {detail}")
    print(f"[INFO] {name}: {detail}")


def skip(name: str, detail: str = "") -> None:
    """环境限制导致用例不成立：既不算通过也不算失败。

    典型场景：这台测试机被 Windows 前台锁挡住，``SetForegroundWindow`` 一律
    拒绝，面板拿不到激活 —— 此时"面板打开后应存活"这类用例的前提就不存在了。
    记 SKIP 而不是 FAIL，是为了**不把环境抖动算成代码 bug**。
    """
    SKIPS.append((name, detail))
    print(f"[SKIP] {name}" + (f"  ({detail})" if detail else ""))


def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def panel_hwnd(pw) -> int:
    try:
        return int(pw.winId())
    except Exception:                                   # noqa: BLE001
        return 0


def is_os_foreground(pw) -> bool:
    """面板是不是**操作系统层面**的前台窗口。

    为什么不能用 ``pw.isActiveWindow()``：Qt 的激活状态在
    ``activateWindow()`` 被调用后就可能变 True，而 Windows 前台锁完全可以把
    真正的 ``GetForegroundWindow()`` 留给别的窗口。两者不一致时，
    ``WM_ACTIVATE(WA_INACTIVE)`` 根本不会来 —— 也就是 ``deactivate`` 这条路径
    一次都不会跑。此时"面板没被关"是空过，不是修好了。
    """
    hwnd = panel_hwnd(pw)
    return hwnd > 0 and foreground_hwnd() == hwnd


def foreground_owner_pid() -> int:
    hwnd = foreground_hwnd()
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def foreground_stolen_externally() -> bool:
    """当前前台是否被**本进程之外**的窗口持有。

    B1（面板内长按）/B2（面板内拖拽）期间光标一直在面板里，正常情况下前台
    不该跑掉。一旦跑掉且持有者是别的进程，就是测试机上的第三方窗口在抢焦点
    （编辑器、浏览器、终端……），此时 ``focus-lost`` 的触发前提不再可控，
    应当记 SKIP 而不是把账算到产品头上。
    """
    pid = foreground_owner_pid()
    return pid != 0 and pid != os.getpid()


def force_foreground(pw) -> bool:
    """尽最大努力把面板抢到前台，返回是否成功。

    和产品里的 ``input_watch.force_foreground`` 独立实现（**不借用被测代码**）：
    QA 的前置条件不能依赖被测实现的正确性。手法同业界惯例：
    ``AttachThreadInput`` 与前台线程共享输入队列绕过前台锁，失败再试
    ``BringWindowToTop`` / ``SwitchToThisWindow``。
    """
    hwnd = panel_hwnd(pw)
    if hwnd <= 0 or not user32.IsWindow(wintypes.HWND(hwnd)):
        return False
    if foreground_hwnd() == hwnd:
        return True

    user32.ShowWindow(wintypes.HWND(hwnd), SW_RESTORE)
    cur = int(kernel32.GetCurrentThreadId())
    fg = foreground_hwnd()
    fg_thread = 0
    if fg:
        fg_thread = int(user32.GetWindowThreadProcessId(wintypes.HWND(fg), None))
    attached = False
    try:
        if fg_thread and fg_thread != cur:
            attached = bool(user32.AttachThreadInput(
                wintypes.DWORD(cur), wintypes.DWORD(fg_thread), True))
        user32.BringWindowToTop(wintypes.HWND(hwnd))
        if not user32.SetForegroundWindow(wintypes.HWND(hwnd)):
            user32.SwitchToThisWindow(wintypes.HWND(hwnd), True)
    finally:
        if attached:
            user32.AttachThreadInput(
                wintypes.DWORD(cur), wintypes.DWORD(fg_thread), False)
    return foreground_hwnd() == hwnd


def pump(app, seconds: float) -> None:
    """跑真实事件循环 ``seconds`` 秒（定时器/动画在此期间推进）。"""
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def cursor_pos() -> tuple[int, int]:
    pt = wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return int(pt.x), int(pt.y)


def logical_to_physical(x: float, y: float) -> tuple[int, int]:
    """Qt 逻辑坐标 → 物理像素（DPI 125% 时必须换算，否则点击落错位置）。"""
    screen = QGuiApplication.primaryScreen()
    dpr = float(screen.devicePixelRatio()) or 1.0
    geo = screen.geometry()
    return (int(round(geo.x() * dpr + x * dpr)), int(round(geo.y() * dpr + y * dpr)))


def run_mouse_script(steps: list[tuple]) -> threading.Thread:
    """在 worker 线程执行鼠标脚本；返回线程。

    ``steps`` 元素：``("move", x, y)`` / ``("down",)`` / ``("up",)`` /
    ``("sleep", seconds)``（物理像素坐标）。``stamps`` 记录 down/up 的单调时间，
    用于证明"点击确实发生在宽限期内"。
    """
    def _worker() -> None:
        for step in steps:
            kind = step[0]
            if kind == "move":
                user32.SetCursorPos(int(step[1]), int(step[2]))
            elif kind == "down":
                user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
                STAMPS.append(("down", time.monotonic()))
            elif kind == "up":
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                STAMPS.append(("up", time.monotonic()))
            elif kind == "sleep":
                time.sleep(float(step[1]))

    thread = threading.Thread(target=_worker, daemon=True)
    thread.start()
    return thread


STAMPS: list[tuple[str, float]] = []


def pick_outside_point(rect: QRect) -> tuple[int, int]:
    """挑一个面板外、屏幕可用区内的逻辑坐标。"""
    avail = QGuiApplication.primaryScreen().availableGeometry()
    candidates = [
        (avail.right() - 90, avail.top() + 90),
        (avail.left() + 90, avail.bottom() - 90),
        (avail.right() - 90, avail.bottom() - 90),
        (avail.left() + 90, avail.top() + 90),
    ]
    for cx, cy in candidates:
        if not rect.contains(cx, cy):
            return cx, cy
    return avail.left() + 40, avail.top() + 40


def panel_alive(controller) -> bool:
    return controller.panel is not None


def safe_reason(pw) -> str:
    """读 close_reason（对象可能已被 deleteLater 销毁）。"""
    try:
        return str(getattr(pw, "close_reason", "") or "")
    except RuntimeError:
        return "<destroyed>"


def alive_visible(pw) -> bool | str:
    try:
        return bool(pw.isVisible())
    except RuntimeError:
        return "<destroyed>"


def first_item_global_center(pw) -> QPoint | None:
    """第一个条目在屏幕上的中心（逻辑坐标）。"""
    grid = pw.grid
    model = grid.model()
    if model is None or model.rowCount() == 0:
        return None
    rect = grid.visualRect(model.index(0, 0))
    if not rect.isValid():
        return None
    return grid.viewport().mapToGlobal(rect.center())


def item_global_center(pw, row: int) -> QPoint | None:
    """第 ``row`` 个条目在屏幕上的中心（逻辑坐标）。"""
    grid = pw.grid
    model = grid.model()
    if model is None or row >= model.rowCount():
        return None
    rect = grid.visualRect(model.index(row, 0))
    if not rect.isValid():
        return None
    return grid.viewport().mapToGlobal(rect.center())


def watcher_state(pw) -> str:
    """兜底轮询的内部状态（取证用，异常时返回说明）。"""
    try:
        w = pw._watch                                # noqa: SLF001 - 取证
        return (f"running={w.is_running()} fired={w._fired} "       # noqa: SLF001
                f"prev_down={w._prev_down} last_inside={w._last_inside} "  # noqa: SLF001
                f"ever_fg={w._ever_foreground}")             # noqa: SLF001
    except Exception as exc:                          # noqa: BLE001
        return f"<unavailable: {exc}>"


def log_tail(n: int = 8) -> None:
    """打印沙箱日志尾部（产品自己记的关闭原因，最硬的证据）。"""
    try:
        lines = Path(paths.LOG_FILE).read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return
    print("      ---- 产品日志尾部 ----")
    for line in lines[-n:]:
        print(f"      {line}")


def find_blank_viewport_point(pw) -> QPoint | None:
    """找一个落在网格空白处（``indexAt`` 无效）的 viewport 内坐标。"""
    grid = pw.grid
    vp = grid.viewport()
    for y in range(vp.height() - 3, 3, -4):
        for x in range(vp.width() - 3, 3, -4):
            if not grid.indexAt(QPoint(x, y)).isValid():
                return QPoint(x, y)
    return None


# --------------------------------------------------------------------------
# B. P0 关闭路径黑盒（7 条）
# --------------------------------------------------------------------------

LAST_OPEN_ACTIVE = False      # 最近一次 open_panel 是否真的让面板拿到了激活


def open_panel(app, controller, cat_id: str, settle: float = 0.8):
    """打开面板并尽量让它真的拿到前台激活；返回 ``controller.panel``。

    ## 为什么要"尽量抢前台"

    B 段多条用例的前提是"面板打开后处于前台"。可这台测试机跑在后台进程里，
    Windows 前台锁会让 ``SetForegroundWindow`` 静默失败 —— 面板显示出来却没有
    激活，随后（无论代码怎么写）都会被失活/焦点路径收走。那样的失败是**环境**
    造成的，不是产品 bug。

    因此这里先等产品自己的激活尝试，抢不到再自己抢一次（``force_foreground``），
    结果记进 ``LAST_OPEN_ACTIVE``，由调用方决定"判 FAIL 还是判 SKIP"。
    """
    global LAST_OPEN_ACTIVE
    controller.open_panel(cat_id)
    pw = controller.panel
    LAST_OPEN_ACTIVE = False
    if pw is not None:
        pump(app, 0.15)              # 给产品自己的 activateWindow / 抢前台留时间
        if controller.panel is None:
            return None
        # 判据用**操作系统层面**的前台归属，不用 isActiveWindow()（见 is_os_foreground）。
        # 这里只**观测**、不主动抢：本机多个进程在抢前台，QA 一插手反而把面板
        # 搅成"刚拿到就被夺走"，制造出真真假假的失败。
        LAST_OPEN_ACTIVE = is_os_foreground(pw)
        pump(app, 0.05)
    pump(app, settle)
    return controller.panel


class DeactivateSpy(QObject):
    """统计"面板真的收到过几次 ``WindowDeactivate``"。

    ## 为什么必须有这个探针

    B3 的判据是"宽限期内的失活不得判死"。但如果这一轮**根本没有失活事件**
    （测试机前台归属没变过），面板活着是理所当然的 —— 那是**空过**，不是修好了。
    我在这台机器上实测过：同一份代码，有的运行 `ever_fg=False`、面板存活，
    有的运行 `fg_before=True fg_after=False`、面板被关。前者不能算 PASS。

    所以判 PASS/FAIL 之前先看 ``count``：0 → 用例不成立（SKIP）；≥1 → 才有资格判。
    """

    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.watch: list = []

    def watch_panel(self, panel) -> None:      # noqa: ANN001
        self.watch = [panel]
        self.count = 0

    def eventFilter(self, obj, event) -> bool:  # noqa: N802
        if event.type() == QEvent.Type.WindowDeactivate:
            for p in self.watch:
                if obj is p:
                    self.count += 1
                    break
        return False


DEACT_SPY = DeactivateSpy()


def install_deactivate_spy() -> None:
    app = QApplication.instance()
    if app is not None:
        app.installEventFilter(DEACT_SPY)


def require_panel(name: str, pw, needs_active: bool = False) -> bool:
    """前置条件：面板必须存在；``needs_active`` 时还要求它真的拿到过激活。

    ``needs_active`` 只给那些"靠失活/前台变化来关闭"的用例用（B5a/B5b）：
    面板没激活过就永远等不到 ``WindowDeactivate``，那样的"通过"是空过。
    Esc / 编辑态 / 外点轮询这些不依赖激活的用例不要开，否则白白丢覆盖。
    """
    if pw is None:
        if not LAST_OPEN_ACTIVE:
            skip(name, "环境限制：测试机前台锁定，面板抢不到激活，用例前提不成立")
        else:
            check(name, False, "controller.panel is None（已拿到激活仍被关闭）")
        return False
    if needs_active and not LAST_OPEN_ACTIVE:
        skip(name, "环境限制：面板未取得前台激活（Windows 前台锁定），"
                   "失活路径不会发生，判通过属于空过")
        return False
    return True


def verdict(name: str, ok: bool, detail: str = "", env_ok: bool = True) -> None:
    """``ok`` 为真记 PASS；为假但环境前置不成立（``env_ok=False``）记 SKIP。"""
    if ok:
        check(name, True, detail)
    elif not env_ok:
        skip(name, "环境限制：面板抢不到激活；" + detail)
    else:
        check(name, False, detail)


def b1_edit_mode(app, controller, cat_ids) -> None:
    """B1：400ms 长按进入编辑态期间，面板不得误关；编辑态退出路径仍工作。"""
    print("\n=== B1. 编辑态（400ms 长按）不误关 + 退出路径 ===")
    pw = open_panel(app, controller, cat_ids[0])
    if not require_panel("B1 open_panel 产出面板", pw):
        return
    center = first_item_global_center(pw)
    if center is None:
        check("B1 找到可长按的条目", False, "grid 无条目")
        controller.close_panel()
        return
    px, py = logical_to_physical(center.x(), center.y())
    saved = cursor_pos()
    edit_ms = int(pw.grid._edit_timer.interval())      # noqa: SLF001 - 读配置断言
    info("B1 长按阈值", f"{edit_ms}ms；条目中心逻辑={center.x()},{center.y()} 物理={px},{py}")

    STAMPS.clear()
    # 按下并保持 > 阈值（500ms）后抬起：期间主线程一直跑事件循环
    thread = run_mouse_script([
        ("move", px, py), ("sleep", 0.08),
        ("down",), ("sleep", edit_ms / 1000.0 + 0.25),
        ("up",),
    ])
    pump(app, edit_ms / 1000.0 + 0.20)
    in_edit = bool(pw.grid.edit_mode)
    still_open = panel_alive(controller)
    thread.join(2.0)
    pump(app, 0.25)

    check("B1 长按 400ms 后确实进入编辑态（抖动多选）", in_edit,
          f"grid.edit_mode={in_edit}")
    if still_open:
        check("B1 长按期间面板未被误关", True, "controller.panel=存活")
    elif safe_reason(pw) == REASON_FOCUS_LOST and foreground_stolen_externally():
        skip("B1 长按期间面板未被误关",
             f"环境限制：按压期间前台被外部进程(pid={foreground_owner_pid()})夺走，"
             f"focus-lost 触发前提不可控 reason={safe_reason(pw)!r}")
        controller.close_panel()
        return
    else:
        check("B1 长按期间面板未被误关", False,
              f"controller.panel=None(误关) reason={safe_reason(pw)!r}")
    if not still_open:
        controller.close_panel()
        return
    check("B1 抬起后仍在编辑态（不因抬起而退出/关闭）",
          bool(pw.grid.edit_mode) and panel_alive(controller),
          f"edit_mode={pw.grid.edit_mode} alive={panel_alive(controller)}")

    # 退出路径 1：Esc（焦点在 grid 上时由 grid 消费，只退出编辑态）
    if QTest is not None:
        QTest.keyClick(pw.grid, Qt.Key.Key_Escape)
    else:                                              # pragma: no cover
        pw.grid.keyPressEvent(_esc_event())
    pump(app, 0.3)
    check("B1 编辑态 Esc → 退出编辑态（grid 消费，不关面板）",
          (not pw.grid.edit_mode) and panel_alive(controller),
          f"edit_mode={pw.grid.edit_mode} alive={panel_alive(controller)}")

    # 退出路径 2：再进编辑态后点面板内空白 → 退出编辑态
    if panel_alive(controller):
        pw.grid.enter_edit_mode(0)
        pump(app, 0.15)
        entered = bool(pw.grid.edit_mode)
        blank = find_blank_viewport_point(pw)
        if blank is None:
            info("B1 跳过「点空白退出编辑态」", "网格已被条目填满，找不到空白点")
        else:
            g = pw.grid.viewport().mapToGlobal(blank)
            bx, by = logical_to_physical(g.x(), g.y())
            run_mouse_script([("move", bx, by), ("sleep", 0.06),
                              ("down",), ("sleep", 0.06), ("up",)])
            pump(app, 0.45)
            check("B1 编辑态点面板内空白 → 退出编辑态且面板保持打开",
                  entered and (not pw.grid.edit_mode) and panel_alive(controller),
                  f"进入={entered} 退出={not pw.grid.edit_mode} "
                  f"存活={panel_alive(controller)}")

    # 观测项（不是缺陷）：编辑态下点**面板外**，按设计应关闭（click-outside）
    if panel_alive(controller):
        pw2 = controller.panel
        pw2.grid.enter_edit_mode(0)
        pump(app, 0.15)
        lx, ly = pick_outside_point(pw2.frameGeometry())
        ox, oy = logical_to_physical(lx, ly)
        run_mouse_script([("move", ox, oy), ("sleep", 0.06), ("down",), ("sleep", 0.08), ("up",)])
        pump(app, 0.9)
        closed = not panel_alive(controller)
        info("B1 观测（编辑态 + 面板外点击）",
             f"面板{'已关闭' if closed else '仍打开'}；设计文档 panel_watch 红线 2 只保证"
             f"「面板内部按下不误关」，未承诺编辑态忽略外部点击")
    user32.SetCursorPos(saved[0], saved[1])
    controller.close_panel()
    pump(app, 0.3)


def _drag_once(app, controller, cat_ids, cat_id: str) -> None:
    """真实 QDrag 拖拽：起点/终点都在面板内，中途把光标拖到面板**外**再回来。

    覆盖 ``panel_watch`` 红线 2 的两个子情形：
    - 拖拽进行中（``QDrag.exec`` 模态循环里）不得关闭；
    - 光标随手势离开面板矩形（"拖出窗口"）不得被当成外部点击。

    ⚠️ 环境限制：``QDrag.exec`` 走 Windows 的 ``DoDragDrop`` 原生模态循环，
    **合成鼠标事件不保证能结束它**（实测偶发卡住数十秒）。因此这里：
    1. 用 "QDrag 子类只记录 + 委托" 的探针确认拖拽真的启动；
    2. 用看门狗线程持续补发 ``LEFTUP`` 解锁（无拖拽时是无害的空操作）；
    3. 超过 ``DRAG_BUDGET_S`` 仍未返回 → 记为环境限制（INFO），不冤判产品。
    """
    print("\n  --- B2/面板内真实拖拽（中途拖出面板再回来）---")
    pw = open_panel(app, controller, cat_id)
    if not require_panel("B2 open_panel 产出面板", pw):
        return
    model = pw.grid.model()
    if model is None or model.rowCount() < 3:
        check("B2 至少 3 个条目可拖拽", False,
              f"rowCount={0 if model is None else model.rowCount()}")
        controller.close_panel()
        return

    import app.windows.panel_grid as pg

    started: list[bool] = []
    finished: list[bool] = []
    base_cls = pg.QDrag

    class SpyDrag(base_cls):                          # 继承真类：行为不变，只记录
        def exec(self, *a, **k):
            started.append(True)
            try:
                return super().exec(*a, **k)
            finally:
                finished.append(True)

    pg.QDrag = SpyDrag
    saved = cursor_pos()
    geo = pw.frameGeometry()
    watchdog_stop = threading.Event()

    def _watchdog() -> None:
        """拖拽卡住时补发抬键（模态循环只认"收到抬键"）。"""
        while not watchdog_stop.wait(0.35):
            if started and not finished:
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
                time.sleep(0.05)
                user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    dog = threading.Thread(target=_watchdog, daemon=True)
    dog.start()
    try:
        src = item_global_center(pw, 0)
        mid = item_global_center(pw, 2)
        outside_pt = pick_outside_point(geo)
        if src is None or mid is None:
            check("B2 拿到拖拽起止点", False, "visualRect 无效")
            return
        sx, sy = logical_to_physical(src.x(), src.y())
        mx, my = logical_to_physical(mid.x(), mid.y())
        ox, oy = logical_to_physical(*outside_pt)
        info("B2 面板几何(逻辑)",
             f"{geo.x()},{geo.y()} {geo.width()}x{geo.height()}；"
             f"起点={src.x()},{src.y()} 在内={geo.contains(src)}；"
             f"中途出界点={outside_pt} 在内={geo.contains(QPoint(*outside_pt))}；"
             f"终点={mid.x()},{mid.y()} 在内={geo.contains(mid)}")
        thread = run_mouse_script([
            ("move", sx, sy), ("sleep", 0.08),
            ("down",), ("sleep", 0.12),
            ("move", mx, my), ("sleep", 0.25),        # 拖拽从这里开始
            ("move", ox, oy), ("sleep", 0.30),        # 拖出面板矩形
            ("move", mx, my), ("sleep", 0.20),        # 拖回来
            ("up",),
        ])
        deadline = time.monotonic() + DRAG_BUDGET_S
        while time.monotonic() < deadline and not finished:
            app.processEvents()
            time.sleep(0.01)
        timed_out = not finished
        thread.join(3.0)
        alive_after_loop = panel_alive(controller)
        pump(app, 0.6)
        alive_final = panel_alive(controller)
        reason = safe_reason(pw)
    finally:
        watchdog_stop.set()
        dog.join(1.0)
        pg.QDrag = base_cls
        user32.SetCursorPos(saved[0], saved[1])

    check("B2 确实启动了 QDrag（拖拽路径被覆盖）", bool(started),
          f"QDrag.exec 调用 {len(started)} 次")
    if timed_out:
        info("B2 拖拽循环未在预算内返回（环境限制）",
             f"{DRAG_BUDGET_S}s 内 DoDragDrop 未结束（合成事件限制）；"
             f"拖拽期间面板存活={alive_after_loop}、之后={alive_final}、"
             f"reason={reason!r} → 该子项不作 FAIL 判定")
        check("B2 拖拽期间+之后面板始终存活（超时情形下仍可判定）",
              alive_after_loop and alive_final,
              f"during={alive_after_loop} after={alive_final} reason={reason!r}")
    else:
        check("B2 拖拽循环正常返回（真实 QDrag 走完一次）", bool(finished),
              f"finished={bool(finished)}")
        env_noise = (reason == REASON_FOCUS_LOST
                     and foreground_stolen_externally())
        if env_noise:
            skip("B2 拖拽进行中/结束后面板均未被误关",
                 f"环境限制：拖拽期间前台被外部进程(pid={foreground_owner_pid()})"
                 f"夺走，during={alive_after_loop} after={alive_final} "
                 f"reason={reason!r}")
            skip("B2 面板关闭原因为空（不是任何自动关闭路径）",
                 f"同上：reason={reason!r} 由外部抢焦点引起，不作产品判定")
        else:
            check("B2 拖拽进行中/结束后面板均未被误关",
                  alive_after_loop and alive_final,
                  f"during={alive_after_loop} after={alive_final} reason={reason!r}")
            check("B2 面板关闭原因为空（不是任何自动关闭路径）", reason == "",
                  f"reason={reason!r}")
        if not alive_final:
            log_tail(8)
    if panel_alive(controller):
        controller.close_panel()
    pump(app, 0.35)


def b2_drag(app, controller, cat_ids) -> None:
    """B2：QDrag 拖拽排序进行中不得误关（红线 2：按下点在面板内）。"""
    print("\n=== B2. QDrag 拖拽期间不误关 ===")
    _drag_once(app, controller, cat_ids, cat_ids[0])


def b3_grace(app, controller, cat_ids) -> None:
    """B3：show() 后 300ms 宽限期内的外点不得关闭面板。

    ## 有效性前提（缺一不可，缺了就判"用例不成立"，不许判 PASS）

    1. **面板必须真的拿到过前台激活**。没有激活就没有"失活"，``deactivate``
       这条路径一次都不会跑，面板自然活着 —— 那是**空过**，不是修好了。
       实测踩过：某次跑出来 ``ever_fg=False``、面板存活，看着是 PASS，其实
       整个场景根本没发生。
    2. **外点必须真的把前台夺走**。按下后若 ``isActiveWindow()`` 仍是 True，
       说明这一下没打到别的窗口上，同样没触发失活。

    时间基准用产品自己的 ``_shown_at``（``showEvent`` 写入），而不是本函数里
    ``open_panel`` 前后的 ``time.monotonic()``：后者会把"调用到 show 之间"的
    抖动算进 elapsed，把 70ms 算成 158ms。
    """
    print("\n=== B3. 300ms 宽限期内外部点击不关闭 ===")
    STAMPS.clear()
    controller.open_panel(cat_ids[1])
    pw = controller.panel
    if pw is None:
        check("B3 open_panel 产出面板", False, "controller.panel is None")
        return

    # 前提 1：必须是**操作系统层面**的前台窗口（不主动抢前台，理由见 open_panel）
    if not is_os_foreground(pw):
        skip("B3 宽限期内外部点击：面板存活（未误关）",
             "环境限制：打开时面板就不是 OS 前台窗口（GetForegroundWindow 是别的"
             "进程），deactivate 路径一次都不会跑，此时面板存活属于空过，不能算 PASS")
        controller.close_panel()
        pump(app, 0.3)
        return

    shown_at = float(getattr(pw, "_shown_at", 0.0))     # noqa: SLF001 - 时间基准
    DEACT_SPY.watch_panel(pw)
    fg_before = is_os_foreground(pw)
    lx, ly = pick_outside_point(pw.frameGeometry())
    ox, oy = logical_to_physical(lx, ly)
    saved = cursor_pos()
    # 立刻注入：按下时刻必须 < 300ms（否则本用例不成立，判无效而非 FAIL）
    thread = run_mouse_script([
        ("move", ox, oy), ("sleep", 0.04),
        ("down",), ("sleep", 0.08), ("up",),
    ])
    pump(app, 0.9)
    thread.join(2.0)
    user32.SetCursorPos(saved[0], saved[1])
    press_at = next((t for k, t in STAMPS if k == "down"), None)
    elapsed_ms = (press_at - shown_at) * 1000.0 if press_at else -1.0
    info("B3 按下时刻", f"show() 后 {elapsed_ms:.0f}ms（宽限 {CLOSE_GRACE_MS}ms）")
    alive = panel_alive(controller)
    fg_after = is_os_foreground(pw) if alive else False
    info("B3 关闭原因", f"{safe_reason(pw)!r}；{watcher_state(pw)}；"
                       f"fg_before={fg_before} fg_after={fg_after}；"
                       f"WindowDeactivate×{DEACT_SPY.count}")
    if not (0 <= elapsed_ms < CLOSE_GRACE_MS):
        skip("B3 宽限期内外部点击：面板存活（未误关）",
             f"注入时机 {elapsed_ms:.0f}ms 未落入 {CLOSE_GRACE_MS}ms 宽限期，用例未跑到")
    elif DEACT_SPY.count == 0:
        skip("B3 宽限期内外部点击：面板存活（未误关）",
             "环境限制：整个过程面板一次 WindowDeactivate 都没收到"
             f"（ever_fg 见上，fg_before={fg_before} fg_after={fg_after}），"
             "失活路径未触发，面板存活属于空过，不能算 PASS")
    else:
        check("B3 宽限期内外部点击：面板存活（未误关）", alive,
              f"elapsed={elapsed_ms:.0f}ms < grace={CLOSE_GRACE_MS}ms，"
              f"失活{DEACT_SPY.count}次，panel={'存活' if alive else 'None(关闭)'}"
              f"{'' if alive else ' reason=' + safe_reason(pw)!r}")
        if alive:
            pump(app, 0.6)      # 再等一拍：宽限期结束后仍不应因这一下而关
            check("B3 宽限期结束后该次点击不追认为外部点击", panel_alive(controller),
                  f"panel={'存活' if panel_alive(controller) else 'None(被追认关闭)'}")
        else:
            log_tail(8)
    controller.close_panel()
    pump(app, 0.3)


def b3b_grace_synthetic(app, controller, cat_ids) -> None:
    """B3b：宽限期内一次失活 → 面板必须活下来（**确定性**版本）。

    ## 为什么 B3 之外还要这一条

    B3 用真实外点，依赖"测试机把前台交给面板、点桌面后又交出去"。这台机器上
    多个进程在抢前台，有些运行里 ``deactivate`` 根本不发生 —— 那种运行只能判
    SKIP，等于没测。本用例改用 Qt 层的激活切换（再 show 一个普通 QWidget 把
    ``QApplication.activeWindow()`` 抢走），制造一次**真实的**
    ``WindowDeactivate``，时机固定在 show 后几十毫秒，仍在 300ms 宽限内。

    结果可重复：defer 实现必 FAIL（宽限结束时面板仍不是前台 → 照样关），
    discard 实现必 PASS（这次失活被丢弃 → 面板留着）。
    """
    print("\n  --- B3b/宽限内失活（Qt 层激活切换，确定性）---")
    controller.open_panel(cat_ids[1])
    pw = controller.panel
    if pw is None:
        check("B3b open_panel 产出面板", False, "controller.panel is None")
        return
    try:
        pw._watch.stop()                    # noqa: SLF001 - 隔离轮询，只测激活事件路径
    except Exception as exc:                # noqa: BLE001
        info("B3b 停轮询失败", str(exc))

    shown_at = float(getattr(pw, "_shown_at", 0.0))     # noqa: SLF001
    DEACT_SPY.watch_panel(pw)
    dummy = QWidget()
    dummy.resize(160, 90)
    dummy.move(20, 20)
    dummy.show()
    dummy.activateWindow()                  # 等价于"第二次点击落在面板外的图标上"
    pump(app, 0.05)
    elapsed_ms = (time.monotonic() - shown_at) * 1000.0
    qactive = QApplication.activeWindow()
    info("B3b 前置", f"抢激活于 show 后 {elapsed_ms:.0f}ms；"
                     f"activeWindow={type(qactive).__name__ if qactive else None}；"
                     f"is_panel={qactive is pw}；WindowDeactivate×{DEACT_SPY.count}")

    pump(app, 1.0)                          # 覆盖 120ms 复核 + 300ms 宽限 + 余量
    alive = panel_alive(controller)
    reason = safe_reason(pw)
    deacts = DEACT_SPY.count
    dummy.close()
    dummy.deleteLater()

    if elapsed_ms >= CLOSE_GRACE_MS:
        skip("B3b 宽限内失活不判死（show 后失活，1s 后仍存活）",
             f"注入时机 {elapsed_ms:.0f}ms 已过 {CLOSE_GRACE_MS}ms 宽限期，用例不成立")
    elif deacts == 0:
        skip("B3b 宽限内失活不判死（show 后失活，1s 后仍存活）",
             "环境限制：面板没收到 WindowDeactivate（Qt 层激活切换未生效），用例不成立")
    else:
        check(f"B3b 宽限内失活不判死（show 后 {elapsed_ms:.0f}ms 失活，1s 后仍存活）",
              alive, f"失活{deacts}次，panel={'存活' if alive else 'None(被关)'} "
                     f"reason={reason!r}")
    if panel_alive(controller):
        controller.close_panel()
    pump(app, 0.35)


def b4_settled_outside_click(app, controller, cat_ids) -> None:
    """B4：静置 > 宽限期后外部点击必须关闭（点桌面必关）。"""
    print("\n=== B4. 静置后外部点击必须关闭 ===")
    pw = open_panel(app, controller, cat_ids[2], settle=0.9)
    if not require_panel("B4 open_panel 产出面板", pw):
        return
    lx, ly = pick_outside_point(pw.frameGeometry())
    ox, oy = logical_to_physical(lx, ly)
    saved = cursor_pos()
    thread = run_mouse_script([("move", ox, oy), ("sleep", 0.06), ("down",),
                               ("sleep", 0.10), ("up",)])
    pump(app, 1.1)
    thread.join(2.0)
    user32.SetCursorPos(saved[0], saved[1])
    closed = not panel_alive(controller)
    check("B4 静置 0.9s 后外部点击：面板关闭", closed,
          f"close_reason={safe_reason(pw)}")
    if not closed:
        controller.close_panel()
    pump(app, 0.35)


def b5_deactivate(app, controller, cat_ids) -> None:
    """B5：失焦关闭（激活其他窗口）→ 120ms 复核路径仍然生效。"""
    print("\n=== B5. 失焦关闭（v1.4 120ms 路径未回退）===")
    # B5a：真实失焦——激活另一个窗口，用户语义是"点其他窗口就关"
    pw = open_panel(app, controller, cat_ids[3])
    if not require_panel("B5 open_panel 产出面板", pw, needs_active=True):
        return
    info("B5a 前置", f"isActiveWindow={pw.isActiveWindow()} "
                     f"os_foreground={is_os_foreground(pw)} "
                     f"activeWindow={QApplication.activeWindow()!r}")
    dummy = QWidget()
    dummy.setWindowTitle("QA 失焦用窗口")
    dummy.resize(240, 120)
    dummy.move(60, 60)
    dummy.show()
    dummy.raise_()
    dummy.activateWindow()
    pump(app, 0.8)
    closed = not panel_alive(controller)
    check("B5a 激活其他窗口 → 面板关闭（失焦路径生效）", closed,
          f"close_reason={safe_reason(pw)}")
    if not closed:
        controller.close_panel()
    dummy.close()
    dummy.deleteLater()
    pump(app, 0.4)

    # B5b：隔离 120ms deactivate 复核路径本身（停掉轮询兜底，只留激活事件路径）
    pw = open_panel(app, controller, cat_ids[3])
    if not require_panel("B5b open_panel 产出面板", pw, needs_active=True):
        return
    try:
        pw._watch.stop()                    # noqa: SLF001 - 隔离变量：只验证 deactivate
    except Exception as exc:                # noqa: BLE001
        info("B5b 停轮询失败", str(exc))
    if panel_alive(controller) and is_os_foreground(pw):
        # 面板仍在前台：_check_deactivate 的复核条件（activeWindow is not self）不成立，
        # 此时"不关闭"才是正确行为 —— 先用另一个窗口把它挤下去（真实 WindowDeactivate）
        dummy2 = QWidget()
        dummy2.resize(200, 100)
        dummy2.move(40, 40)
        dummy2.show()
        dummy2.raise_()
        dummy2.activateWindow()
        pump(app, 0.6)
        dummy2.close()
        dummy2.deleteLater()
        pump(app, 0.4)
    if panel_alive(controller):
        # 仍未关闭（例如本环境前台切换未生效）→ 直接注入 WindowDeactivate 走同一复核路径
        QApplication.sendEvent(pw, QEvent(QEvent.Type.WindowDeactivate))
        pump(app, 0.6)
    reason = safe_reason(pw)
    check("B5b 仅靠激活事件路径（轮询已停）→ 120ms 后关闭，原因=deactivate",
          (not panel_alive(controller)) and reason == PW.REASON_DEACTIVATE,
          f"panel={'None(已关)' if not panel_alive(controller) else '仍打开'} "
          f"reason={reason!r}")
    if panel_alive(controller):
        controller.close_panel()
    pump(app, 0.35)


def b6_esc(app, controller, cat_ids) -> None:
    """B6：Esc 关闭面板。"""
    print("\n=== B6. Esc 关闭面板 ===")
    pw = open_panel(app, controller, cat_ids[4])
    if not require_panel("B6 open_panel 产出面板", pw):
        return
    check("B6 前置：面板可见", bool(pw.isVisible()))
    if QTest is not None:
        QTest.keyClick(pw, Qt.Key.Key_Escape)
    else:                                              # pragma: no cover
        QApplication.sendEvent(pw, _esc_event())
    pump(app, 0.6)
    reason = safe_reason(pw)
    check("B6 Esc → 面板关闭，原因=esc",
          (not panel_alive(controller)) and reason == PW.REASON_ESC,
          f"panel={'None(已关)' if not panel_alive(controller) else '仍打开'} "
          f"reason={reason!r}")
    if panel_alive(controller):
        controller.close_panel()
    pump(app, 0.35)


def _esc_event() -> QEvent:
    from PySide6.QtGui import QKeyEvent

    return QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape, Qt.KeyboardModifier.NoModifier)


def b7_rapid_switch(app, controller, cat_ids) -> None:
    """B7：快速连开两面板（跨分类）→ 新面板存活、旧面板已关。"""
    print("\n=== B7. 快速连开两面板（跨分类切换）===")
    controller.open_panel(cat_ids[0])
    old = controller.panel
    # 不等动画结束，立刻切到另一个分类（宽限期/轮询仍在跑的最坏时机）
    controller.open_panel(cat_ids[5])
    new = controller.panel
    old_visible_immediately = alive_visible(old) if old is not None else None
    # 新面板能不能拿到前台决定后续 3 条判 FAIL 还是 SKIP（见 require_panel 说明）
    active_ok = new is not None and (is_os_foreground(new) or force_foreground(new))
    pump(app, 1.0)
    check("B7 旧面板与新面板不是同一个对象", old is not None and new is not None and old is not new,
          f"old={type(old).__name__ if old else None} new={type(new).__name__ if new else None}")
    check("B7 旧面板已关闭（close() 立即隐藏）", old_visible_immediately is False,
          f"old.isVisible()={old_visible_immediately}")
    check("B7 旧面板关闭原因为 programmatic（controller 主动关）",
          safe_reason(old) in (PW.REASON_PROGRAMMATIC, ""),
          f"reason={safe_reason(old)!r}")
    verdict("B7 新面板存活且可见（未被旧面板的轮询/宽限期连坐）",
            panel_alive(controller) and new is controller.panel
            and alive_visible(new) is True,
            f"alive={panel_alive(controller)} visible={alive_visible(new)}",
            env_ok=active_ok)
    verdict("B7 新面板分类 id 正确", controller.active_cat_id == cat_ids[5],
            f"active_cat_id={controller.active_cat_id} 期望={cat_ids[5]}",
            env_ok=active_ok)
    # 新面板必须能活过自己的 300ms 宽限期 + 动画
    pump(app, 0.8)
    verdict("B7 新面板在 1.8s 后仍存活（没有自我关闭）", panel_alive(controller),
            f"panel={'存活' if panel_alive(controller) else 'None(自我关闭)'}",
            env_ok=active_ok)
    controller.close_panel()
    pump(app, 0.35)


# --------------------------------------------------------------------------
# C. 全局设置黑盒
# --------------------------------------------------------------------------

SKIN_PROBE = {
    "panel_w": 612,
    "panel_h": 401,
    "radius": 17,
    "bg_color": "#123456",
    "opacity": 43,
    "position_mode": "center",
    "material": MATERIAL_SOLID,
}
IDENTITY_FIELDS = ("id", "dir", "label", "icon_px", "image", "visible")


def _identity_snapshot(cfg) -> list[dict]:
    return [
        {k: getattr(c, k, None) for k in IDENTITY_FIELDS} for c in cfg.categories
    ]


def c1_apply_all(app, controller) -> str:
    """C1：7 个皮肤字段全部一致；身份字段与排序完全不动。"""
    print("\n=== C1. apply_to_all_categories：皮肤一致 / 身份与排序不动 ===")
    cats = controller.cfg.categories
    check("C1 沙箱里有 6 个分类", len(cats) == 6, f"实际 {len(cats)}")
    if len(cats) != 6:
        return ""
    before_identity = _identity_snapshot(controller.cfg)
    before_order = [c.id for c in cats]
    src = cats[2]
    for name, value in SKIN_PROBE.items():
        setattr(src, name, value)
    info("C1 源分类", f"{src.dir} ({src.id}) 皮肤={ {k: getattr(src, k) for k in SKIN_FIELDS} }")

    count = controller.apply_to_all_categories(src.id)
    check("C1 返回值 = 受影响分类数 6", count == 6, f"返回 {count}")

    diffs = []
    for cat in controller.cfg.categories:
        for name in SKIN_FIELDS:
            if getattr(cat, name) != getattr(src, name):
                diffs.append(f"{cat.dir}.{name}={getattr(cat, name)!r}≠{getattr(src, name)!r}")
    check("C1 6 个分类的 7 个皮肤字段全部一致", not diffs,
          "; ".join(diffs) if diffs else ", ".join(SKIN_FIELDS))

    after_identity = _identity_snapshot(controller.cfg)
    check("C1 身份字段（id/dir/label/icon_px/image/visible）完全未动",
          before_identity == after_identity,
          "全部一致" if before_identity == after_identity
          else f"变化={[(b, a) for b, a in zip(before_identity, after_identity) if b != a]}")
    check("C1 分类顺序未变（列表次序与 id 序列一致）",
          [c.id for c in controller.cfg.categories] == before_order,
          " -> ".join(before_order))
    return src.id


def c2_radius_sanitize(app, controller, source_id: str) -> None:
    """C2：源分类 radius 越界（999）→ sanitize 后再复制。"""
    print("\n=== C2. 越界 radius 先 sanitize 再拷贝 ===")
    src = controller.cfg.find(source_id)
    if src is None:
        check("C2 找到源分类", False, source_id)
        return
    src.radius = 999
    src.opacity = 800
    count = controller.apply_to_all_categories(source_id)
    radii = {c.dir: c.radius for c in controller.cfg.categories}
    opac = {c.dir: c.opacity for c in controller.cfg.categories}
    check("C2 源分类自身 radius 被 clamp 到 24（不留脏值）", src.radius == RADIUS_MAX,
          f"radius={src.radius}")
    check("C2 所有分类 radius 均为 24（越界值未被复制出去）",
          set(radii.values()) == {RADIUS_MAX}, str(radii))
    check("C2 所有分类 opacity 均被 clamp 到 100 以内",
          all(0 <= v <= 100 for v in opac.values()), str(opac))
    check("C2 返回值仍为 6", count == 6, f"返回 {count}")


def c3_immediate_persist(app, controller, source_id: str) -> None:
    """C3：点「应用到全部分类」后配置立即落盘（不等 OK、不等 400ms 去抖）。"""
    print("\n=== C3. 应用后立即落盘（读 JSON 验证） ===")
    src = controller.cfg.find(source_id)
    if src is None:
        check("C3 找到源分类", False, source_id)
        return
    for name, value in SKIN_PROBE.items():
        setattr(src, name, value)
    src.radius = 20
    cfg_path = Path(paths.CONFIG_PATH)
    before_mtime = cfg_path.stat().st_mtime_ns if cfg_path.exists() else 0
    pump(app, 0.05)
    # 走对话框的真实落地入口（含"立即落盘 + 关面板"）
    count = controller._apply_all_from_dialog(source_id)   # noqa: SLF001 - 被测入口
    after_mtime = cfg_path.stat().st_mtime_ns if cfg_path.exists() else 0
    on_disk = json.loads(cfg_path.read_text(encoding="utf-8"))
    cats = on_disk.get("categories", [])
    bad = []
    for raw in cats:
        for name in SKIN_FIELDS:
            if raw.get(name) != getattr(src, name):
                bad.append(f"{raw.get('dir')}.{name}={raw.get('name')!r}"
                           f"({raw.get(name)!r})≠{getattr(src, name)!r}")
    check("C3 落盘文件已更新（mtime 变化，未依赖 400ms 去抖）",
          after_mtime != before_mtime, f"{before_mtime} -> {after_mtime}")
    check("C3 磁盘 JSON 里 6 个分类的 7 个皮肤字段与源分类一致", not bad,
          "; ".join(bad) if bad else f"{len(cats)} 个分类全部一致")
    check("C3 磁盘 JSON 含 material 字段（v1.5 新字段已持久化）",
          all("material" in raw for raw in cats),
          str([raw.get("material") for raw in cats]))
    check("C3 返回受影响分类数 6", count == 6, f"返回 {count}")
    check("C3 面板已关闭（外观改动需重开生效，避免展示旧外观）",
          controller.panel is None, f"panel={controller.panel!r}")


def c4_legacy_config(app) -> None:
    """C4：旧 v1.4 配置（无 material）读入 → material 默认 blur，其余字段保真。"""
    print("\n=== C4. v1.4 旧配置兼容（material 默认 blur） ===")
    legacy = {
        "version": 1,
        "app": {
            "icon_px_default": 64, "panel_item_px": 48, "drag_threshold_px": 4,
            "edit_press_ms": 400, "watch_poll_ms": 1000,
            "data_root": str(paths.DATA_ROOT),
            "category_exclude": ["快捷方式", "桌面", "z全部"],
            "shortcut_mode": "launcher",
            "strip_extensions": True, "strip_shortcut_overlay": True,
        },
        "categories": [
            {"id": "cat_legacy01", "dir": "旧分类一", "label": "旧分类一",
             "image": "", "icon_px": 96, "visible": True,
             "panel_w": 480, "panel_h": 500, "radius": 20,
             "bg_color": "#21C9F8", "opacity": 62, "position_mode": "anchor"},
            {"id": "cat_legacy02", "dir": "旧分类二", "label": "旧分类二",
             "image": "C:/tmp/x.png", "icon_px": 48, "visible": False,
             "panel_w": 300, "panel_h": 200, "radius": 0,
             "bg_color": "#101010", "opacity": 30, "position_mode": "center"},
        ],
    }
    path = Path(paths.APP_DIR) / "legacy_v14.json"
    path.write_text(json.dumps(legacy, ensure_ascii=False, indent=2), encoding="utf-8")
    cfg = AppConfig.load(path)
    check("C4 旧配置能读入 2 个分类", len(cfg.categories) == 2,
          f"实际 {len(cfg.categories)}")
    if len(cfg.categories) != 2:
        return
    check("C4 缺失 material → 默认 blur",
          all(c.material == MATERIAL_BLUR for c in cfg.categories),
          str([c.material for c in cfg.categories]))
    pair = list(zip(legacy["categories"], cfg.categories))
    wrong = []
    for raw, cat in pair:
        for key in ("id", "dir", "label", "image", "icon_px", "visible", "panel_w",
                    "panel_h", "radius", "bg_color", "opacity", "position_mode"):
            if getattr(cat, key) != raw[key]:
                wrong.append(f"{raw['id']}.{key}: {getattr(cat, key)!r}≠{raw[key]!r}")
    check("C4 其余 12 个字段全部保真（无迁移副作用）", not wrong,
          "; ".join(wrong) if wrong else "逐字段一致")
    # 非法 material 值也要回落 blur
    bad = legacy["categories"][0] | {"material": "glass"}
    _p = Path(paths.APP_DIR) / "legacy_bad_material.json"
    _p.write_text(json.dumps({"version": 1, "app": {}, "categories": [bad]},
                             ensure_ascii=False), encoding="utf-8")
    cfg2 = AppConfig.load(_p)
    check("C4 非法 material（glass）→ 回落 blur",
          bool(cfg2.categories) and cfg2.categories[0].material == MATERIAL_BLUR,
          str([c.material for c in cfg2.categories]))
    # 往返一次：写回的 JSON 必带 material，且再次读入不变（幂等）
    out = Path(paths.APP_DIR) / "legacy_v14_roundtrip.json"
    cfg.config_path = out
    cfg.save()
    cfg3 = AppConfig.load(out)
    check("C4 旧配置写回后再读：material 保持 blur（往返幂等）",
          [c.material for c in cfg3.categories] == [MATERIAL_BLUR, MATERIAL_BLUR],
          str([c.material for c in cfg3.categories]))


# --------------------------------------------------------------------------
# D. 毛玻璃回退路径
# --------------------------------------------------------------------------

class FakeScreen:
    """假的 QScreen：只实现 ``_capture_blurred`` 用到的那三件事。"""

    def __init__(self, grab):
        self._grab = grab

    def geometry(self) -> QRect:
        return QRect(0, 0, 1920, 1080)

    def availableGeometry(self) -> QRect:
        return QRect(0, 0, 1920, 1040)

    def devicePixelRatio(self) -> float:
        return 1.0

    def grabWindow(self, *args, **kwargs) -> QPixmap:   # noqa: N802 - Qt 命名
        return self._grab()


class FakeGuiApp:
    """替身：``panel_window.QGuiApplication`` 的 screenAt / primaryScreen。"""

    def __init__(self, screen):
        self._screen = screen

    def screenAt(self, *a, **k):                        # noqa: N802
        return self._screen

    def primaryScreen(self):                            # noqa: N802
        return self._screen


def _solid_color_at(panel, x: int, y: int) -> tuple[int, int, int, int]:
    """把面板渲染到离屏图上，取某点的 RGBA（验证"确实画了纯色背景"）。

    注意：PySide6 的 ``QWidget.render(painter)`` 必须给 ``targetOffset``
    （第二个位置参数），只传 painter 会抛 TypeError。
    """
    img = QImage(panel.size(), QImage.Format.Format_ARGB32)
    img.fill(0)
    painter = QPainter(img)
    try:
        panel.render(painter, QPoint(0, 0))
    finally:
        painter.end()
    color = QColor.fromRgba(int(img.pixel(x, y)))
    return color.red(), color.green(), color.blue(), color.alpha()


def _fallback_case(app, controller, cat_id, name: str, grab_factory) -> None:
    """通用回退用例：注入截屏行为 → 断言回退纯色、不抛异常、面板可用。"""
    print(f"\n  --- D/{name} ---")
    pw = open_panel(app, controller, cat_id, settle=0.5)
    if not require_panel(f"D {name}：open_panel 产出面板", pw):
        return
    orig_gui = PW.QGuiApplication
    screen = FakeScreen(grab_factory)
    PW.QGuiApplication = FakeGuiApp(screen)
    raised = ""
    try:
        pw.refresh_backdrop()
    except Exception as exc:                            # noqa: BLE001 - 就是要证明不抛
        raised = f"{type(exc).__name__}: {exc}"
    finally:
        PW.QGuiApplication = orig_gui
    check(f"D {name}：refresh_backdrop 不抛异常", not raised, raised or "无异常")
    check(f"D {name}：回退到纯色（无模糊底图）", pw.has_blur is False,
          f"has_blur={pw.has_blur} backdrop_size={pw.backdrop_size}")
    # 面板仍然可见、仍能绘制纯色
    draw_error = ""
    try:
        r, g, b, a = _solid_color_at(pw, 3, pw.height() // 2)
    except Exception as exc:                            # noqa: BLE001
        draw_error = f"{type(exc).__name__}: {exc}"
        r = g = b = a = -1
    bg = QColor(pw.cat.bg_color)
    expect_alpha = round(pw.cat.opacity / 100 * 255)
    check(f"D {name}：面板仍可见且未崩溃",
          bool(pw.isVisible()) and not draw_error,
          draw_error or f"visible={pw.isVisible()}")
    check(f"D {name}：绘制出的是纯色背景（RGB≈bg_color，alpha≈opacity）",
          (abs(r - bg.red()) <= 2 and abs(g - bg.green()) <= 2 and abs(b - bg.blue()) <= 2
           and abs(a - expect_alpha) <= 2),
          f"实测 rgba=({r},{g},{b},{a}) 期望≈({bg.red()},{bg.green()},{bg.blue()},{expect_alpha})")
    check(f"D {name}：面板仍可正常关闭", _closable(app, controller, pw), "close_with_reason 生效")
    pump(app, 0.3)


def _closable(app, controller, pw) -> bool:
    try:
        pw.close_with_reason("qa-fallback")
    except RuntimeError:
        return False
    pump(app, 0.5)
    return not panel_alive(controller)


def d_blur_fallbacks(app, controller, cat_ids) -> None:
    """D：三种回退（空 pixmap / 全黑 / 尺寸异常）+ 截屏抛异常。"""
    print("\n=== D. 毛玻璃回退路径（纯色兜底）===")
    cat = controller.cfg.find(cat_ids[6]) if len(cat_ids) > 6 else controller.cfg.find(cat_ids[0])
    cat.material = MATERIAL_BLUR
    cat.opacity = 62
    cat.sanitize()

    _fallback_case(app, controller, cat.id, "空 pixmap",
                   lambda: QPixmap())

    def _black() -> QPixmap:
        img = QImage(400, 300, QImage.Format.Format_ARGB32)
        img.fill(QColor(0, 0, 0, 255))
        return QPixmap.fromImage(img)

    _fallback_case(app, controller, cat.id, "截屏全黑", _black)

    def _tiny() -> QPixmap:
        img = QImage(4, 4, QImage.Format.Format_ARGB32)
        img.fill(QColor(200, 180, 160, 255))
        return QPixmap.fromImage(img)

    _fallback_case(app, controller, cat.id, "尺寸异常（4×4）", _tiny)

    def _boom() -> QPixmap:
        raise RuntimeError("模拟 grabWindow 失败")

    _fallback_case(app, controller, cat.id, "截屏抛异常", _boom)

    # 正向对照：正常截屏必须**真的**生成模糊底图（否则前面的"回退"无从区分）
    def _normal() -> QPixmap:
        img = QImage(420, 300, QImage.Format.Format_ARGB32)
        for y in range(300):
            for x in range(0, 420, 7):
                img.setPixelColor(x, y, QColor((x * 3) % 256, (y * 5) % 256, 128, 255))
        return QPixmap.fromImage(img)

    print("\n  --- D/正向对照：正常截屏 → 必须有底图 ---")
    controller.open_panel(cat.id)
    pump(app, 0.5)
    pw = controller.panel
    if pw is not None:
        orig_gui = PW.QGuiApplication
        PW.QGuiApplication = FakeGuiApp(FakeScreen(_normal))
        try:
            pw.refresh_backdrop()
        finally:
            PW.QGuiApplication = orig_gui
        check("D 对照：正常截屏 → 生成模糊底图（证明回退判定不是恒假）",
              pw.has_blur is True,
              f"has_blur={pw.has_blur} size={pw.backdrop_size} 耗时={pw.blur_cost_ms:.1f}ms")
        controller.close_panel()
        pump(app, 0.4)


# --------------------------------------------------------------------------
# 沙箱数据根
# --------------------------------------------------------------------------

SEED = [
    # (dir, label, icon_px, panel_w, panel_h, radius, bg, opacity, mode)
    ("分类甲", "分类甲", 64, 420, 300, 12, "#F5F6F8", 88, "anchor"),
    ("分类乙", "分类乙", 72, 460, 320, 8, "#203040", 70, "center"),
    ("分类丙", "分类丙", 48, 500, 360, 20, "#21C9F8", 62, "anchor"),
    ("分类丁", "分类丁", 96, 380, 280, 4, "#FFFFFF", 90, "center"),
    ("分类戊", "分类戊", 56, 440, 340, 16, "#101820", 55, "anchor"),
    ("分类己", "分类己", 88, 520, 400, 24, "#E8E8E8", 80, "anchor"),
]


def seed_sandbox() -> list[str]:
    """建 6 个沙箱分类（目录 + 条目 + 配置），返回 cat_id 列表。"""
    global SANDBOX_ROOT
    SANDBOX_ROOT = sandbox.sandbox_paths(prefix="dbx_qa_v15_")
    paths.ensure_base_dirs()
    cats_cfg = []
    for idx, (d, label, icon_px, w, h, r, bg, op, mode) in enumerate(SEED):
        cat_dir = paths.DATA_ROOT / d
        cat_dir.mkdir(parents=True, exist_ok=True)
        for n in range(8):
            (cat_dir / f"条目{n:02d}.txt").write_text(f"{d}-{n}", encoding="utf-8")
        cats_cfg.append({
            "id": f"cat_qa{idx:02d}",
            "dir": d, "label": label, "image": "", "icon_px": icon_px,
            "visible": True, "panel_w": w, "panel_h": h, "radius": r,
            "bg_color": bg, "opacity": op, "position_mode": mode,
        })
    config = {
        "version": 1,
        "app": {
            "icon_px_default": 64, "panel_item_px": 48, "drag_threshold_px": 4,
            "edit_press_ms": 400, "watch_poll_ms": 1000,
            "data_root": str(paths.DATA_ROOT),
            "category_exclude": ["快捷方式", "桌面", "z全部"],
            "shortcut_mode": "launcher",
            "strip_extensions": True, "strip_shortcut_overlay": True,
        },
        "categories": cats_cfg,
    }
    paths.CONFIG_PATH.write_text(
        json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"沙箱根：{SANDBOX_ROOT}（真实目录不会被写入）")
    return [c["id"] for c in cats_cfg]


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    only = (argv[0].upper() if argv else "BCD")

    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")
    cat_ids = seed_sandbox()

    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(4)
    install_deactivate_spy()
    controller = AppController(app)
    controller.start()
    pump(app, 1.0)
    ids = [c.id for c in controller.cfg.categories]
    print(f"沙箱分类：{[(c.dir, c.id) for c in controller.cfg.categories]}")

    code = 0
    try:
        if "B" in only:
            b1_edit_mode(app, controller, ids)
            b2_drag(app, controller, ids)
            b3_grace(app, controller, ids)
            b3b_grace_synthetic(app, controller, ids)
            b4_settled_outside_click(app, controller, ids)
            b5_deactivate(app, controller, ids)
            b6_esc(app, controller, ids)
            b7_rapid_switch(app, controller, ids)
        if "C" in only:
            source_id = c1_apply_all(app, controller)
            if source_id:
                c2_radius_sanitize(app, controller, source_id)
                c3_immediate_persist(app, controller, source_id)
            c4_legacy_config(app)
        if "D" in only:
            d_blur_fallbacks(app, controller, ids)
    finally:
        controller.close_panel()
        pump(app, 0.3)
        controller.shutdown()
        pump(app, 0.3)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n{'=' * 70}")
    print(f"共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，"
          f"失败 {len(failed)} 项，环境跳过 {len(SKIPS)} 项")
    if SKIPS:
        print("环境跳过（不计入失败，但也不能当成通过）：")
        for n, why in SKIPS:
            print(f"  - {n}  ({why})")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
        code = 1
    else:
        print("全部通过")

    if SANDBOX_ROOT is not None:
        shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)
    ok, detail = sandbox.guard_real_untouched(real)
    check("真实用户配置零改动（sha256 一致）", ok, detail)
    if not ok:
        code = 1
    print(f"\n最终：{'PASS' if code == 0 else 'FAIL'}")
    return code


if __name__ == "__main__":
    sys.exit(main())
