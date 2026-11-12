"""B3 修复方案可行性预验证（**不改 app/ 源码**，全部用 monkeypatch 模拟）。

## 为什么要做这件事

``tests/qa_v15_verify.py B`` 的 B3 判 FAIL：面板 ``show()`` 后 71ms 的一次外点
会通过 **deactivate 路径**（``_check_deactivate``）把面板关掉，300ms 宽限期
只作用于 ``OutsideClickWatcher`` 轮询那一半。

在把 bug 报给工程师之前，先把"提议的补丁能不能真的修好、且不把 B4/B5 弄坏"
验证掉，避免给出一个看起来对、实际有副作用的修法。

## 提议的补丁（挂在 ``PanelWindow`` 上）

```python
SHOW_GRACE_S = 0.3                      # 与 panel_watch.GRACE_MS 对齐

def showEvent(self, event):
    ...
    self._shown_at = time.monotonic()

def _check_deactivate(self):
    if self.isVisible() and QApplication.activeWindow() is not self:
        remain = SHOW_GRACE_S - (time.monotonic() - self._shown_at)
        if remain > 0:                  # show 后仍在宽限内 → 推迟复核，不判死
            QTimer.singleShot(int(remain * 1000) + 5, self._check_deactivate)
            return
        self.close_with_reason(REASON_DEACTIVATE)
```

## 本探针断言（打完补丁后）

| 用例 | 期望 | 说明 |
|---|---|---|
| G1 show()+70ms 外点 | 面板**存活** | B3 必须转绿 |
| G2 宽限过后再外点 | 面板**关闭** | 不能把"点桌面必关"弄坏（v1.5 主诉求） |
| G3 静置后激活其他窗口 | 关闭且 reason=deactivate | 不能把 v1.4 失焦路径弄坏 |
| G4 Esc | 关闭且 reason=esc | 不受影响 |

## 两个曾经让这个探针失效的坑（都修了，别改回去）

**坑 1：G2 的 use-after-free。** 第一版在 ``pump(app, 0.9)`` 之后直接拿
``pw.frameGeometry()``，``pw`` 是 ``open_panel`` 时的局部变量。面板若在这 0.9s
里被关掉、``deleteLater`` 已执行，C++ 对象已销毁而 Python wrapper 还活着 →
``RuntimeError: libshiboken: Internal C++ object (PanelWindow) already deleted``。
崩在 ``[defer]`` 组，于是后面的 ``[discard]`` 组**一次都没执行过** —— 这个探针
在真正跑 discard 之前就先把自己崩掉了。现在全程用 ``controller.panel`` 重新
取，且所有对面板属性的读取都过 ``safe_reason()`` / ``alive_visible()``（try/except）。

**坑 2：G2 曾经是空过（vacuous pass）。** 修完崩溃后第一版 G2 只要
"面板最后是 None"就算 PASS，可面板可能在这 0.9s 静置期间就被**别的东西**
关掉了，点击动作根本没发生 —— 详情写「静置前已被自动关闭」而断言却记 PASS。
这正是主理人点名的假绿。现在 G2 必须给出**注入确实生效**的证据
（失活事件计数增加，或关闭原因是 ``click-outside``），给不出证据记 SKIP，
既不算通过也不算失败。

## SKIP 与 PASS 分开打印

``[SKIP]`` 表示"用例前提在这台机器上不成立"（前台被第三方进程夺走、面板从未
收到失活事件、注入未生效），**不进通过数**。汇总行会把三项分别报出来。

用法：``.venv\\Scripts\\python.exe tests\\qa_probe_b3_fix.py``
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
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QEvent, QObject, QPoint, Qt, QTimer  # noqa: E402
from PySide6.QtGui import QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.windows.panel_window import PanelWindow  # noqa: E402

try:
    from PySide6.QtTest import QTest
except ImportError:                                   # pragma: no cover
    QTest = None

SHOW_GRACE_S = 0.3
CLOSE_DELAY_MS = 120          # panel_window.CLOSE_DELAY_MS（判边界用）
RESULTS: list[tuple[str, bool, str]] = []
SKIPS: list[tuple[str, str]] = []
CLOSE_LOG: list[tuple] = []   # [(面板, 关闭原因, 关闭那一刻的前台 pid)]
VARIANT = ""          # 当前候选补丁名（前缀进断言名，便于分辨 v1/v2）

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004

user32.GetForegroundWindow.restype = wintypes.HWND
user32.GetWindowThreadProcessId.argtypes = [wintypes.HWND,
                                            ctypes.POINTER(wintypes.DWORD)]
user32.GetWindowThreadProcessId.restype = wintypes.DWORD
user32.SetForegroundWindow.argtypes = [wintypes.HWND]
user32.SetForegroundWindow.restype = wintypes.BOOL
user32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
user32.AttachThreadInput.restype = wintypes.BOOL
user32.BringWindowToTop.argtypes = [wintypes.HWND]
user32.SwitchToThisWindow.argtypes = [wintypes.HWND, wintypes.BOOL]
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short

VK_LBUTTON = 0x01

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
kernel32.GetCurrentThreadId.restype = wintypes.DWORD
kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
kernel32.OpenProcess.restype = wintypes.HANDLE
kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
kernel32.CloseHandle.restype = wintypes.BOOL
kernel32.QueryFullProcessImageNameW.argtypes = [wintypes.HANDLE, wintypes.DWORD,
                                               ctypes.c_wchar_p,
                                               ctypes.POINTER(wintypes.DWORD)]
kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL
kernel32.CreateToolhelp32Snapshot.argtypes = [wintypes.DWORD, wintypes.DWORD]
kernel32.CreateToolhelp32Snapshot.restype = wintypes.HANDLE
kernel32.Process32FirstW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32FirstW.restype = wintypes.BOOL
kernel32.Process32NextW.argtypes = [wintypes.HANDLE, ctypes.c_void_p]
kernel32.Process32NextW.restype = wintypes.BOOL

PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
MAX_PATH = 260


class PROCESSENTRY32W(ctypes.Structure):
    """``PROCESSENTRY32W``（只用到 pid 与 exe 名，其余字段占位）。"""

    _fields_ = [
        ("dwSize", wintypes.DWORD),
        ("cntUsage", wintypes.DWORD),
        ("th32ProcessID", wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.c_size_t),
        ("th32ModuleID", wintypes.DWORD),
        ("cntThreads", wintypes.DWORD),
        ("th32ParentProcessID", wintypes.DWORD),
        ("pcPriClassBase", ctypes.c_long),
        ("dwFlags", wintypes.DWORD),
        ("szExeFile", ctypes.c_wchar * MAX_PATH),
    ]


def check(name: str, ok: bool, detail: str = "") -> None:
    label = f"[{VARIANT}] {name}" if VARIANT else name
    RESULTS.append((label, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    """用例前提在这台机器上不成立：不进通过数，也不算失败。"""
    label = f"[{VARIANT}] {name}" if VARIANT else name
    SKIPS.append((label, detail))
    print(f"[SKIP] {label}" + (f"  ({detail})" if detail else ""))


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


# --------------------------------------------------------------------------
# 存活安全访问：面板关掉后 C++ 对象已销毁，直接读属性会抛 RuntimeError
# --------------------------------------------------------------------------

def safe_reason(pw) -> str:
    if pw is None:
        return ""
    try:
        return str(getattr(pw, "close_reason", "") or "")
    except RuntimeError:                     # C++ 对象已删除
        return ""


def alive_visible(pw):
    """``isVisible()`` 的存活安全版；对象已删除返回 ``None``。"""
    if pw is None:
        return False
    try:
        return bool(pw.isVisible())
    except RuntimeError:
        return None


# --------------------------------------------------------------------------
# 前台归属（独立实现，不借用被测代码）
# --------------------------------------------------------------------------

def foreground_hwnd() -> int:
    return int(user32.GetForegroundWindow() or 0)


def foreground_owner_pid() -> int:
    hwnd = foreground_hwnd()
    if not hwnd:
        return 0
    pid = wintypes.DWORD()
    user32.GetWindowThreadProcessId(wintypes.HWND(hwnd), ctypes.byref(pid))
    return int(pid.value)


def process_image_name(pid: int) -> str:
    """按下 pid 取可执行文件名（用于把"谁抢了前台"说清楚）。

    只报名字不报命令行：``wmic``/``Get-CimInstance`` 在本机受策略限制，
    ``QueryFullProcessImageNameW`` 是唯一稳定可用的路径。
    """
    if not pid:
        return "?"
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, int(pid))
    if not handle:
        return f"pid={pid}(取不到)"
    try:
        buf = ctypes.create_unicode_buffer(512)
        size = wintypes.DWORD(512)
        if kernel32.QueryFullProcessImageNameW(handle, 0, buf, ctypes.byref(size)):
            return Path(buf.value).name
        return f"pid={pid}(? )"
    finally:
        kernel32.CloseHandle(handle)


def concurrent_python_pids() -> list[int]:
    """本进程之外还有几个 python.exe 在跑。

    同一台机器上并行跑另一个 GUI 测试套件时，两边会互相抢前台、互相注入鼠标
    点击 —— B 段的"面板应存活"、本探针的 G1/G2 都会成片报错，而且**看起来像
    产品 bug**。实测抓到过 ``python.exe tests/qa_v15_verify.py B`` 在对拍。

    注意排除两类"假并发"：venv 的 ``python.exe`` 是启动器，会再 fork 一个
    python.exe 子进程跑真正的解释器；shell 也是 python 的父进程。这两类都是
    自己人，算进去会每次开局就打一条假的 WARN（实测踩过）。
    """
    pids: list[int] = []
    me = os.getpid()
    parent = os.getppid()
    snap = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)     # TH32CS_SNAPPROCESS
    if snap in (0, -1):
        return pids
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        ok = kernel32.Process32FirstW(snap, ctypes.byref(entry))
        while ok:
            pid = int(entry.th32ProcessID)
            if (str(entry.szExeFile).lower() == "python.exe"
                    and pid not in (me, parent)
                    and int(entry.th32ParentProcessID) != me):
                pids.append(pid)
            ok = kernel32.Process32NextW(snap, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snap)
    return pids


def is_os_foreground(pw) -> bool:
    """面板是不是**操作系统层面**的前台窗口（不是 ``isActiveWindow()``）。"""
    try:
        hwnd = int(pw.winId())
    except (RuntimeError, AttributeError):
        return False
    return hwnd > 0 and foreground_hwnd() == hwnd


def force_foreground(pw) -> bool:
    """尽最大努力把面板抢到前台，返回是否成功（用 ``GetForegroundWindow`` 验证）。"""
    if not is_os_foreground(pw):
        try:
            hwnd = int(pw.winId())
        except (RuntimeError, AttributeError):
            return False
        target = wintypes.HWND(hwnd)
        target_tid = user32.GetWindowThreadProcessId(target, None)
        self_tid = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(self_tid, target_tid, True)
        try:
            user32.BringWindowToTop(target)
            user32.SetForegroundWindow(target)
            user32.SwitchToThisWindow(target, True)
        finally:
            user32.AttachThreadInput(self_tid, target_tid, False)
    return is_os_foreground(pw)


def left_button_down() -> bool:
    """物理左键当前是否处于按下状态（``GetAsyncKeyState`` 高位）。

    历史用例在两次注入之间如果不小心留下"只按没抬"的状态，面板的
    ``OutsideClickWatcher`` 会认为用户正按在外面 —— 于是下一条用例里面板
    一过宽限期就以 ``click-outside`` 关掉，看起来像是 Esc/失焦路径坏了。
    这不是产品缺陷，是上一轮注入没收拾干净，所以注入前后都要查一次。
    """
    return bool(user32.GetAsyncKeyState(VK_LBUTTON) & 0x8000)


def release_stuck_button() -> bool:
    """若左键卡在按下状态，补一次抬起；返回是否补过。"""
    if not left_button_down():
        return False
    user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)
    time.sleep(0.05)
    return True


def install_close_observer() -> None:
    """记录每次 ``close_with_reason`` 的「原因 + 那一刻的前台 pid」。

    只记录、不改变产品行为。为什么不用事后查：外部进程夺走前台 → 面板关闭 →
    等我们检查时前台可能已经换回本进程的另一个窗口，事后查会把"外部夺走"
    误判成"自己关的"，把环境噪音记成 FAIL。关掉那一刻的归属是唯一不漂的证据。
    """
    original = PanelWindow.close_with_reason

    def close_with_reason(self, reason):         # noqa: ANN001
        # ⚠️ 存**对象本身**，不存 id(self)：Python 的 id 会被回收复用，
        # 上一条用例的面板对象被 GC 后，新面板可能拿到同一个 id，
        # 于是"上一条的关闭记录"会被当成本条的记录（实测把 focus-lost
        # 误判成 deactivate+外部进程）。持有强引用反而保证了 id 不复用。
        CLOSE_LOG.append((self, str(reason), foreground_owner_pid()))
        return original(self, reason)

    PanelWindow.close_with_reason = close_with_reason      # type: ignore[method-assign]


def close_record(pw) -> tuple[str, int]:
    """取面板关闭时的「原因, 前台 pid」；没有记录返回 ``("", 0)``。"""
    if pw is None:
        return "", 0
    for panel, reason, pid in reversed(CLOSE_LOG):
        if panel is pw:
            return reason, pid
    return "", 0


def closed_by_external_foreground(pw) -> bool:
    """面板被 ``deactivate`` 关掉，且关闭那一瞬间前台由**别的进程**持有。"""
    reason, pid = close_record(pw)
    return reason == "deactivate" and pid not in (0, os.getpid())


def fg_desc(pid: int | None = None) -> str:
    """把"当前前台是谁"渲染成一句话（pid + 可执行名）。"""
    pid = foreground_owner_pid() if pid is None else int(pid)
    return f"pid={pid}({process_image_name(pid)})"


def settle_close_verdict(name: str, pw, stage_ms: float) -> None:
    """静置期间面板被关掉时的定性。

    - 关闭那一刻前台被**别的进程**持有 → 环境噪音（确实是别人把焦点拿走了）→ SKIP
    - 关闭那一刻前台仍在本进程 → 面板**自己**走的，静置状态下不该发生 → FAIL

    后者是 v1.5 这一类"打不开/一闪而过"缺陷的信号，绝不能记 SKIP 放过。
    """
    reason, pid = close_record(pw)
    if pid not in (0, os.getpid()):
        skip(name, f"环境限制：静置 {stage_ms:.0f}ms 内前台被外部进程 {fg_desc(pid)} "
                   f"拿走，面板按 {reason!r} 关闭")
    else:
        check(name, False, f"面板在静置 {stage_ms:.0f}ms 内自行关闭 reason={reason!r}"
                           "（关闭瞬间前台仍属本进程）—— 静置状态不该自我关闭")


class DeactivateSpy(QObject):
    """统计面板真的收到过几次 ``WindowDeactivate``。

    没有这个计数器，G1/G2 就分不清"面板活着"和"面板压根没收到失活事件"——
    后者是空过。``count == 0`` 时 G1 记 SKIP，不许记 PASS。
    """

    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.watch = None

    def watch_panel(self, panel) -> None:        # noqa: ANN001
        self.watch = panel
        self.count = 0

    def eventFilter(self, obj, event) -> bool:   # noqa: N802
        if event.type() == QEvent.Type.WindowDeactivate and obj is self.watch:
            self.count += 1
        return False


DEACT_SPY = DeactivateSpy()


def install_deactivate_spy() -> None:
    app = QApplication.instance()
    if app is not None:
        app.installEventFilter(DEACT_SPY)



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


def install_proposed_fix(variant: str = "discard") -> tuple:
    """把候选补丁 monkeypatch 到 ``PanelWindow`` 上（不改源码）。

    Args:
        variant: ``"defer"`` = 宽限内**推迟**复核（反面教材，见下）；
            ``"discard"`` = **不注入补丁**，直接跑产品当前实现。

    ## 为什么 discard 变体改成"不 patch"

    早先两个变体都是手写实现，靠"丢弃 vs 推迟"的差别做对照。产品定版之后
    （宽限内作废 **且** 用事件发生时刻 ``_deactivate_at`` 判宽限），手写的
    ``discard`` 反而**落后于产品**：它拿"检查时刻"判宽限，于是 `show()+70ms`
    的外点会因 ``+120ms`` 复核越过 300ms 而被关 —— 那是同一个 180ms 边界，
    只是搬到了探针自己身上。

    把 ``discard`` 改成"直接跑产品"，对照关系反而更硬：
    **同一个 G1 用例，产品绿、defer 红** —— 这才是"探针能鉴别错误设计"的证据。

    ``defer`` 仍然保留手写实现，因为它就是这个探针存在的理由：把"推迟复核"
    这个看起来对、实际必死的写法钉在这儿，防止有人再改回去。
    """
    orig_show = PanelWindow.showEvent
    orig_check = PanelWindow._check_deactivate       # noqa: SLF001

    if variant == "discard":
        return orig_show, orig_check                 # 不注入：跑产品当前实现

    def show_event(self, event):                     # noqa: ANN001
        self._shown_at = time.monotonic()
        return orig_show(self, event)

    def _check_deactivate(self):                     # noqa: N802
        if self.isVisible() and QApplication.activeWindow() is not self:
            remain = SHOW_GRACE_S - (time.monotonic()
                                     - getattr(self, "_shown_at", 0.0))
            if remain > 0:
                QTimer.singleShot(int(remain * 1000) + 5, self._check_deactivate)
                return
            self.close_with_reason("deactivate")

    PanelWindow.showEvent = show_event               # type: ignore[method-assign]
    PanelWindow._check_deactivate = _check_deactivate  # type: ignore[method-assign]
    return orig_show, orig_check


def uninstall(orig: tuple) -> None:
    PanelWindow.showEvent = orig[0]                  # type: ignore[method-assign]
    PanelWindow._check_deactivate = orig[1]          # type: ignore[method-assign]


def pick_outside(rect) -> QPoint:
    avail = QGuiApplication.primaryScreen().availableGeometry()
    for cx, cy in [(avail.right() - 90, avail.top() + 90),
                   (avail.left() + 90, avail.bottom() - 90),
                   (avail.right() - 90, avail.bottom() - 90)]:
        if not rect.contains(QPoint(cx, cy)):
            return QPoint(cx, cy)
    return QPoint(avail.left() + 40, avail.top() + 40)


def seed() -> None:
    root = sandbox.sandbox_paths(prefix="dbx_b3fix_")
    paths.ensure_base_dirs()
    d = paths.DATA_ROOT / "甲"
    d.mkdir(parents=True, exist_ok=True)
    for n in range(6):
        (d / f"e{n}.txt").write_text("x")
    paths.CONFIG_PATH.write_text(json.dumps({
        "version": 1,
        "app": {"data_root": str(paths.DATA_ROOT)},
        "categories": [{"id": "c0", "dir": "甲", "label": "甲",
                        "panel_w": 420, "panel_h": 300, "material": "solid"}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"沙箱：{root}")


def g1_grace_click(app, controller) -> None:
    print("\n--- G1. show()+70ms 外部单击（B3 场景）→ 期望存活 ---")
    release_stuck_button()
    t0 = time.monotonic()
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G1 面板已打开", False, "open_panel 没产出面板")
        return
    DEACT_SPY.watch_panel(pw)
    # 面板必须真的拿到前台，否则不会有 WindowDeactivate —— "存活"就是空过。
    # 先尽力抢（比直接 SKIP 好），抢不到才认环境限制。
    if not is_os_foreground(pw) and not force_foreground(pw):
        skip("G1 宽限期内外点：面板存活（B3 转绿）",
             f"环境限制：面板未取得前台（前台={fg_desc()}），失活路径不会触发")
        controller.close_panel()
        pump(app, 0.4)
        return
    pt = pick_outside(pw.frameGeometry())
    px, py = to_phys(pt.x(), pt.y())
    inj_ms = (time.monotonic() - t0) * 1000
    pre_pid = foreground_owner_pid()
    mouse([("move", px, py), ("sleep", 0.03), ("down",), ("sleep", 0.08), ("up",)])
    pump(app, 1.2)
    elapsed = (time.monotonic() - t0) * 1000
    alive = controller.panel is not None
    deact = DEACT_SPY.count
    reason = safe_reason(pw)
    if deact == 0:
        skip("G1 宽限期内外点：面板存活（B3 转绿）",
             f"注入于 {inj_ms:.0f}ms，{elapsed:.0f}ms 后仍无 WindowDeactivate（失活0次）"
             "—— 失活路径没被触发，判通过属于空过")
    elif not alive and pre_pid not in (0, os.getpid()) and not closed_by_external_foreground(pw):
        # 注入瞬间前台已被第三方进程占着：这一轮判不了，别把账算到产品头上
        skip("G1 宽限期内外点：面板存活（B3 转绿）",
             f"注入瞬间前台属外部进程 pid={pre_pid}，失活归属不可控")
    else:
        check("G1 宽限期内外点：面板存活（B3 转绿）", alive,
              f"注入 {inj_ms:.0f}ms，{elapsed:.0f}ms 后 panel={'存活' if alive else 'None(仍被关)'}"
              f" reason={reason!r} 失活{deact}次")
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.4)


def g2_settled_click(app, controller) -> None:
    print("\n--- G2. 静置 0.9s 后外部单击 → 期望关闭（不能弄坏「点桌面必关」）---")
    release_stuck_button()
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G2 面板已打开", False, "open_panel 没产出面板")
        return
    DEACT_SPY.watch_panel(pw)
    pump(app, 0.9)
    # ★ use-after-free 防线：这 0.9s 里面板可能已被关掉且 deleteLater 已执行。
    #   此时 pw 的 C++ 对象已销毁（Python wrapper 还在），碰 frameGeometry()
    #   就是 RuntimeError → 整支探针在这里崩掉，后面的 [discard] 组一条都跑不到。
    if controller.panel is None:
        settle_close_verdict("G2 静置 0.9s 后面板应存活（否则点击无从发生）", pw, 900)
        return
    live = controller.panel                   # 每次重新取，不缓存旧 wrapper
    if not is_os_foreground(live) and not force_foreground(live):
        skip("G2 静置后外点：面板关闭",
             "环境限制：面板未取得前台，外部点击能否生效不可控")
        controller.close_panel()
        pump(app, 0.4)
        return
    pt = pick_outside(live.frameGeometry())
    px, py = to_phys(pt.x(), pt.y())
    before = DEACT_SPY.count
    mouse([("move", px, py), ("sleep", 0.05), ("down",), ("sleep", 0.10), ("up",)])
    pump(app, 1.1)
    closed = controller.panel is None
    reason = safe_reason(pw)
    deact_delta = DEACT_SPY.count - before
    # 注入生效的证据：面板收到失活，或者被轮询判成 click-outside。
    # 两者都没有 = 点击动作根本没发生（历史上这条 PASS 就是这种空过）。
    evidence = deact_delta >= 1 or reason == "click-outside"
    if not evidence:
        skip("G2 静置后外点：面板关闭",
             f"注入未生效：失活计数+{deact_delta} 且 reason={reason!r}，"
             "点击动作没有发生 —— 不算通过")
        if controller.panel is not None:
            controller.close_panel()
        pump(app, 0.4)
        return
    check("G2 静置后外点：面板关闭", closed,
          f"reason={reason!r} 失活+{deact_delta}")
    if not closed:
        controller.close_panel()
    pump(app, 0.4)


def g3_deactivate(app, controller) -> None:
    print("\n--- G3. 静置后激活其他窗口 → 期望 deactivate 关闭（v1.4 路径不回退）---")
    release_stuck_button()
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G3 面板已打开", False, "open_panel 没产出面板")
        return
    if not is_os_foreground(pw) and not force_foreground(pw):
        skip("G3 失焦 → 关闭（deactivate/focus-lost 皆可）",
             "环境限制：面板未取得前台，无法制造「别人抢走前台」这个前提")
        controller.close_panel()
        pump(app, 0.4)
        return
    pump(app, 0.9)          # 越过宽限期
    if controller.panel is None:
        settle_close_verdict("G3 静置 0.9s 后面板应存活（否则失焦前提不成立）", pw, 900)
        return
    dummy = QWidget()
    dummy.resize(220, 110)
    dummy.move(50, 50)
    dummy.show()
    dummy.raise_()
    dummy.activateWindow()
    pump(app, 0.8)
    closed = controller.panel is None
    reason = safe_reason(pw)
    stolen = not is_os_foreground(pw)
    if not closed and not stolen:
        # dummy 没抢到前台（Windows 前台锁把 activateWindow 吃了），
        # 面板根本没有失活过 —— 这条用例的前提不存在，判 FAIL 是冤枉产品。
        skip("G3 失焦 → 关闭（deactivate/focus-lost 皆可）",
             f"环境限制：替身窗口抢不到前台（前台={fg_desc()}），"
             "面板未失活，判通过属于空过")
    else:
        # 原因允许 deactivate 或 focus-lost：设计中这两条都表示"用户切走了"，
        # 谁先判定取决于事件派发时机（120ms 复核 vs 180ms 持续失焦），是竞争不是缺陷。
        # 断言的是**行为**（失焦后面板关闭），不是**哪条路径先跑**。
        check("G3 失焦 → 关闭（deactivate/focus-lost 皆可）",
              closed and reason in ("deactivate", "focus-lost"),
              f"panel={'None' if closed else '仍打开'} reason={reason!r} 前台已让出={stolen}")
    dummy.close()
    dummy.deleteLater()
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.4)


def g4_esc(app, controller) -> None:
    print("\n--- G4. Esc → 期望关闭且原因=esc ---")
    release_stuck_button()
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G4 面板已打开", False, "open_panel 没产出面板")
        return
    pump(app, 0.5)
    if controller.panel is None:
        skip("G4 Esc → 关闭且原因=esc",
             f"环境限制：按下 Esc 之前面板已被关闭 reason={safe_reason(pw)!r}")
        return
    # Esc 是键盘路径，前提是面板真的持有前台（没焦点时 true 用户也按不到它）。
    if not is_os_foreground(pw):
        force_foreground(pw)
    if not is_os_foreground(pw):
        skip("G4 Esc → 关闭且原因=esc",
             f"环境限制：面板未取得前台（前台={fg_desc()}），"
             "Esc 无法送达面板，判通过属于空过")
        controller.close_panel()
        pump(app, 0.4)
        return
    info(f"[{VARIANT}] G4 注入前：os_foreground=True "
         f"左键按下={left_button_down()} 失活计数={DEACT_SPY.count}")
    if QTest is not None:
        QTest.keyClick(controller.panel, Qt.Key.Key_Escape)
    else:                                             # pragma: no cover
        from PySide6.QtGui import QKeyEvent

        QApplication.sendEvent(controller.panel,
                               QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                         Qt.KeyboardModifier.NoModifier))
    pump(app, 0.6)
    closed = controller.panel is None
    reason = safe_reason(pw)
    check("G4 Esc → 关闭且原因=esc", closed and reason == "esc",
          f"panel={'None' if closed else '仍打开'} reason={reason!r}"
          f" 失活{DEACT_SPY.count}次")
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.4)


def main() -> int:
    global VARIANT
    others = concurrent_python_pids()
    if others:
        print(f"[WARN] 本进程之外还有 {len(others)} 个 python.exe 在跑：{others}")
        print("[WARN] 两个 GUI 测试进程会互相抢前台、互相注入鼠标点击，"
              "本探针的 G1/G2 结论不可信。建议先停掉另一个再跑。")
    seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    install_deactivate_spy()          # 失活事件计数：判"空过"用
    install_close_observer()          # 关闭瞬间的前台归属：判"环境噪音"用
    controller = AppController(app)
    controller.start()
    pump(app, 1.0)
    info(f"沙箱分类：{[c.dir for c in controller.cfg.categories]}")
    for variant, title in (("defer", "v1 宽限内推迟复核"),
                           ("discard", "v2 宽限内丢弃失活事件")):
        VARIANT = variant
        print(f"\n{'=' * 70}\n=== 候选补丁 {title} ===\n{'=' * 70}")
        orig = install_proposed_fix(variant)
        try:
            g1_grace_click(app, controller)
            g2_settled_click(app, controller)
            g3_deactivate(app, controller)
            g4_esc(app, controller)
        finally:
            uninstall(orig)
            controller.close_panel()
            pump(app, 0.4)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS) + len(SKIPS)} 项，通过 {len(RESULTS) - len(failed)} 项，"
          f"失败 {len(failed)} 项，环境跳过 {len(SKIPS)} 项")
    if failed:
        print("失败项：" + "; ".join(failed))
    if SKIPS:
        print("跳过项（不计入通过数）：")
        for n, why in SKIPS:
            print(f"  - {n}  ({why})")
    root = sandbox.sandbox_root()
    if root:
        shutil.rmtree(root, ignore_errors=True)
    controller.shutdown()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
