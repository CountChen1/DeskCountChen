"""前台窗口与鼠标按键状态查询（``app/win32`` 内的 ctypes 边界）。

## 为什么需要这个模块（P0-1 根因）

面板由 SAO Utils 的 HUD 图标唤起 ``pythonw`` 后台进程。Windows 有前台锁定
（``SPI_GETFOREGROUNDLOCKTIMEOUT``），后台进程的 ``SetForegroundWindow`` 常被
拒绝 → 面板**从未真正成为前台窗口** → 用户点桌面时不产生
``WindowDeactivate`` / ``ActivationChange(False)`` → 依赖 Qt 激活事件的关闭逻辑
永远不被调用 → 面板关不掉。用户先点一次面板内部（真实鼠标点击让 Windows
真正激活它）之后，Deactivate 才恢复正常。

因此需要一套**不依赖 Qt 激活事件**的原始信号：

| 函数 | Win32 来源 | 用途 |
|---|---|---|
| :func:`left_button_down` | ``GetAsyncKeyState(VK_LBUTTON)`` | 全局左键是否按下（面板外按下即关闭） |
| :func:`cursor_pos_physical` | ``GetCursorPos`` | 物理像素鼠标位置（取证 / 换算） |
| :func:`foreground_hwnd` | ``GetForegroundWindow`` | 当前前台窗口 |
| :func:`is_foreground` | 同上 + ``GetAncestor`` | 面板是否真的在前台 |
| :func:`force_foreground` | ``AttachThreadInput`` + ``SetForegroundWindow`` + ``SwitchToThisWindow`` | 尽力抢前台（成功则问题自然消失） |

约定与 ``window_ops`` 一致：**所有函数不抛异常**，失败返回安全默认值并记日志。

DPI 注意：``GetCursorPos`` 返回**物理像素**，而 Qt 的 ``frameGeometry()`` 是
**逻辑坐标**。本机 DPI=120（125%），直接比较会差 25%，落在边缘时会误判。
所以落点判断统一走 Qt 的 ``QCursor.pos()``（它内部就是 ``GetCursorPos`` +
Qt 自己的 DPI 换算），本模块只提供物理坐标用于取证/日志。
"""

from __future__ import annotations

import ctypes
import logging
from ctypes import wintypes

from app.win32 import consts

logger = logging.getLogger(__name__)

__all__ = [
    "cursor_pos_physical",
    "left_button_state",
    "force_foreground",
    "foreground_hwnd",
    "is_foreground",
    "left_button_down",
]


def left_button_down() -> bool:
    """鼠标左键当前是否按下（进程无关的全局状态）。

    ``GetAsyncKeyState`` 的最高位（0x8000）表示"此刻是否按下"，最低位表示
    "自上次查询以来是否被按过"；这里只关心最高位。
    """
    try:
        state = int(consts._GetAsyncKeyState(consts.VK_LBUTTON))
    except Exception as exc:  # noqa: BLE001 - 本模块约定不抛异常
        logger.warning("GetAsyncKeyState 失败: %s", exc)
        return False
    return bool(state & 0x8000)


def left_button_state() -> tuple[bool, bool]:
    """``(当前是否按下, 自上次查询以来是否按下过)``。

    调用方**必须一次查询同时取走两个值**：``GetAsyncKeyState`` 的返回值是
    "一次性"的，分两次调用会让第二次污染第一次的低位。

    为什么同时给出低位（**实测踩坑，与文档语义不符**）：按 MSDN，低位（0x0001）
    语义是"自上次调用以来被按过"，本该能兜住短按。但**实测对鼠标左键，低位
    从来不置位** —— 消费一次低位后，20ms 内按下再抬起，再查询 raw 仍是 0x0000。
    所以短按**只能靠高频轮询高位**（见 ``panel_watch.CLICK_POLL_MS = 40``：
    120ms 一轮会整次漏掉 50–100ms 的单击，探针连续 3 轮未检出）。

    低位仍按"谁先看到算谁的"取或保留：万一在某些驱动/环境下它真的置位，
    我们不会漏；它不可靠时，高频高位轮询已经兜住了。
    """
    try:
        state = int(consts._GetAsyncKeyState(consts.VK_LBUTTON))
    except Exception as exc:  # noqa: BLE001
        logger.warning("GetAsyncKeyState 失败: %s", exc)
        return False, False
    return bool(state & 0x8000), bool(state & 0x0001)


def cursor_pos_physical() -> tuple[int, int]:
    """鼠标位置（**物理像素**；失败返回 ``(0, 0)``）。"""
    try:
        pt = wintypes.POINT()
        if not consts._GetCursorPos(ctypes.byref(pt)):
            return 0, 0
        return int(pt.x), int(pt.y)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GetCursorPos 失败: %s", exc)
        return 0, 0


def foreground_hwnd() -> int:
    """当前前台窗口句柄（失败返回 0）。"""
    try:
        return int(consts._GetForegroundWindow() or 0)
    except Exception as exc:  # noqa: BLE001
        logger.warning("GetForegroundWindow 失败: %s", exc)
        return 0


def is_foreground(hwnd: int) -> bool:
    """该窗口（或其根窗口）是否是当前前台窗口。

    面板是无边框顶层窗口，Qt 的 ``winId()`` 就是接收激活的 HWND；仍用
    ``GetAncestor(GA_ROOT)`` 兜住"前台窗口其实是它的子窗口"的形态。
    """
    target = int(hwnd or 0)
    if target <= 0:
        return False
    try:
        fg = foreground_hwnd()
        if fg == 0:
            return False
        if fg == target:
            return True
        root = int(consts._GetAncestor(wintypes.HWND(fg), consts.GA_ROOT) or 0)
        return root == target
    except Exception as exc:  # noqa: BLE001
        logger.warning("is_foreground 失败: %s", exc)
        return False


def force_foreground(hwnd: int) -> bool:
    """尽力把窗口抢到前台（可选兜底，**不能**作为唯一依赖）。

    三步走：
    1. 已是前台 → 直接 True；
    2. ``AttachThreadInput`` 让自己和前台线程共享输入队列，绕过前台锁定；
    3. ``SetForegroundWindow`` 失败再试 ``SwitchToThisWindow``。

    抢到了 → 面板会有真实的激活事件，Qt 的既有两条关闭路径自然生效；
    抢不到 → 由外部点击轮询兜底（那是用户的直觉路径）。
    """
    target = int(hwnd or 0)
    if target <= 0:
        return False
    try:
        if not consts._IsWindow(wintypes.HWND(target)):
            return False
        if foreground_hwnd() == target:
            return True
        cur = int(consts._GetCurrentThreadId())
        fg = wintypes.HWND(foreground_hwnd())
        fg_thread = int(consts._GetWindowThreadProcessId(fg, None)) if fg else 0
        attached = False
        try:
            if fg_thread and fg_thread != cur:
                attached = bool(consts._AttachThreadInput(cur, fg_thread, True))
            ok = bool(consts._SetForegroundWindow(wintypes.HWND(target)))
            if not ok:
                consts._SwitchToThisWindow(wintypes.HWND(target), True)
                ok = foreground_hwnd() == target
        finally:
            if attached:
                consts._AttachThreadInput(cur, fg_thread, False)
        if not ok:
            logger.info("抢前台被系统拒绝（前台锁定），改由外部点击轮询兜底")
        return ok
    except Exception as exc:  # noqa: BLE001
        logger.warning("force_foreground 失败: %s", exc)
        return False
