"""面板关闭兜底：外部点击 / 前台丢失轮询（**不依赖 Qt 激活事件**）。

## 为什么需要它（P0-1 根因，实测取证见 ``tests/probe_close_bug.py``）

面板由 SAO Utils 的 HUD 图标唤起 ``pythonw`` 后台进程，Windows 前台锁定常拒绝
``SetForegroundWindow`` → 面板**从未真正成为前台窗口** → 点桌面不会产生
``WindowDeactivate`` / ``ActivationChange(False)`` → 依赖激活事件的关闭路径完全
不触发 → 面板关不掉（实测 100% 复现）。

因此这里提供一条**纯轮询**兜底，语义直接对齐用户直觉「点桌面就关」：

| 信号 | 判据 | 关闭原因 |
|---|---|---|
| 外部按下 | 左键由"抬起→按下"的瞬间（40ms 轮询），光标落在面板 ``frameGeometry()`` 之外 | ``click-outside`` |
| 前台丢失 | 面板**曾经**是前台，之后前台持续 ≥180ms 不是面板 | ``focus-lost`` |

### 四条安全红线（违反即返工，``tests/test_panel_watch.py`` 逐条覆盖）

1. **前 300ms 宽限期**（``grace_ms``）：面板刚 ``show()`` 时只记录按键状态、不做
   判定。否则缩放/淡入动画期间的鼠标状态、以及"打开面板的那一下点击"会误关。
2. **面板内部按下绝不触发**：判据是"落点在面板矩形之外"，所以内部点击、拖拽、
   编辑态长按、拖出窗口的 ``QDrag``（按下点仍在面板内）都不会关闭；
   且前后两拍任一落在面板内就不判外部点击（短按漏采时按下的落点已丢）。
3. **``focus-lost`` 必须"曾经是前台"才生效**（``_ever_foreground``）：如果面板
   自始至终抢不到前台（正是本缺陷的形态），该规则**永不**触发 —— 否则面板会在
   打开 0.5 秒后自己消失，把"关不掉"换成"打不开"这种更糟的缺陷。
4. **判定不了就按"不关"处理**：输入源/矩形/落点任何一步失败，一律按
   "面板内"处理（宁可不关，绝不误关）。

### 可测性

判定逻辑与 Win32 完全解耦：
- :class:`InputSource` 只要求 3 个方法（``left_button_state`` / ``cursor_pos`` /
  ``panel_is_foreground``），单测用假源即可逐条覆盖；
- :meth:`OutsideClickWatcher.poll_once` 是纯函数式的一步判定（返回原因或 None），
  不依赖定时器，便于在无消息循环的环境里断言。
"""

from __future__ import annotations

import logging
import time
from typing import Callable, Protocol

from PySide6.QtCore import QObject, QPoint, QRect, QTimer, Signal
from PySide6.QtGui import QCursor

from app.win32 import input_watch

logger = logging.getLogger(__name__)

# 轮询间隔。设计初稿写的是 120–150ms，**实测必须下调到 40ms**：
# GetAsyncKeyState 的低位（"自上次查询以来按过"）对鼠标左键**从不置位**
# （实测 t0 消费 → 20ms 内按下抬起 → 再查，raw=0x0000），只有高位在"按住期间"
# 有效；而真实单击的按下时长普遍 50–100ms，120ms 一轮会整次漏掉（探针连续 3 轮
# 未检出）。40ms 能覆盖绝大多数单击，成本只是每 40ms 两个轻量 API 调用。
CLICK_POLL_MS = 40
GRACE_MS = 300           # show() 后的宽限期：只记录不判死
FOCUS_LOST_MS = 180      # 前台持续非面板多久后判为"焦点丢失"

REASON_CLICK_OUTSIDE = "click-outside"
REASON_FOCUS_LOST = "focus-lost"


class InputSource(Protocol):
    """轮询所需的三路输入（系统实现 :class:`SystemInputSource`，单测可注入假源）。"""

    def left_button_state(self) -> tuple[bool, bool]:
        """``(当前是否按下, 自上次查询以来是否按下过)``。

        **必须一次查询同时给出两个值**：``GetAsyncKeyState`` 的低位是"自上次
        调用以来按过"，分两次调用会让第一次把低位吃掉，第二次永远拿到 False。
        """
        ...

    def cursor_pos(self) -> QPoint:
        ...

    def panel_is_foreground(self) -> bool:
        ...


class SystemInputSource:
    """系统输入源：按键状态走 Win32，坐标走 Qt（DPI 正确），前台走 Win32。"""

    def __init__(self, hwnd_provider: Callable[[], int]) -> None:
        """
        Args:
            hwnd_provider: 返回面板 HWND 的可调用对象（面板可能被重建，
                所以每次查询都重新取，不缓存）。
        """
        self._hwnd_provider = hwnd_provider

    def left_button_state(self) -> tuple[bool, bool]:
        """全局左键 (是否按下, 自上次查询以来是否按下过)。"""
        return input_watch.left_button_state()

    def cursor_pos(self) -> QPoint:
        """光标位置（**Qt 逻辑坐标**，与 ``frameGeometry()`` 同一坐标系）。

        ``GetCursorPos`` 返回物理像素，本机 DPI=120（125%）时直接和逻辑坐标
        比较会差 25%，边缘会误判；``QCursor.pos()`` 内部就是
        ``GetCursorPos`` + Qt 的 DPI 换算，是唯一安全的比较基准。
        """
        return QCursor.pos()

    def panel_is_foreground(self) -> bool:
        """面板是否是当前前台窗口。"""
        try:
            hwnd = int(self._hwnd_provider() or 0)
        except Exception as exc:  # noqa: BLE001 - 面板已销毁
            logger.debug("取面板 hwnd 失败: %s", exc)
            return False
        return input_watch.is_foreground(hwnd)


class OutsideClickWatcher(QObject):
    """面板可见期间的关闭兜底轮询器。

    Attributes:
        triggered: 判定应当关闭时发出，参数为原因（``click-outside`` /
            ``focus-lost``）。订阅者负责真正 ``close()``。
    """

    triggered = Signal(str)

    def __init__(
        self,
        rect_provider: Callable[[], QRect],
        source: InputSource | None = None,
        parent: QObject | None = None,
        interval_ms: int = CLICK_POLL_MS,
        grace_ms: int = GRACE_MS,
        focus_lost_ms: int = FOCUS_LOST_MS,
    ) -> None:
        """
        Args:
            rect_provider: 返回面板全局矩形（**逻辑坐标**）的可调用对象。
            source: 输入源；``None`` 时必须由调用方稍后 :meth:`set_source`
                或直接传 ``SystemInputSource``。
            parent: Qt 父对象（保证生命周期）。
            interval_ms: 轮询间隔。
            grace_ms: show() 之后的宽限期。
            focus_lost_ms: 前台持续非面板多久后判定焦点丢失。
        """
        super().__init__(parent)
        self._rect_provider = rect_provider
        self.source: InputSource | None = source
        self.interval_ms = max(40, int(interval_ms))
        self.grace_ms = max(0, int(grace_ms))
        self.focus_lost_ms = max(0, int(focus_lost_ms))

        self._timer = QTimer(self)
        self._timer.setInterval(self.interval_ms)
        self._timer.timeout.connect(self._tick)

        self._prev_down = False
        self._grace_until = 0.0
        self._fg_since: float | None = None
        self._ever_foreground = False
        self._last_inside = True      # 保守初值：首次判定前按"在面板内"处理
        self._fired = False

    # -- 生命周期 -----------------------------------------------------------

    def start(self) -> None:
        """开始轮询（重新进入宽限期；幂等）。"""
        if self.source is None:
            logger.warning("OutsideClickWatcher 没有输入源，兜底检测不会生效")
            return
        self._fired = False
        self._grace_until = time.monotonic() + self.grace_ms / 1000.0
        self._fg_since = None
        self._ever_foreground = False
        # 忽略"启动瞬间就已经按下"的状态：打开面板的那一下点击不能算外部点击
        try:
            down, _ = self.source.left_button_state()
        except Exception:  # noqa: BLE001
            down = False
        self._prev_down = bool(down)
        # 首拍的真实落点：宽限期内每拍都会刷新，这里给个准确初值，
        # 避免"面板外单击恰好落在宽限期结束后的第一拍"被防误关逻辑吃掉。
        self._last_inside = self._point_inside()
        self._timer.start()

    def stop(self) -> None:
        """停止轮询（面板关闭 / 隐藏时必须调用，避免定时器泄漏）。"""
        self._timer.stop()
        self._fg_since = None

    def is_running(self) -> bool:
        """轮询是否在跑（探针/单测用）。"""
        return self._timer.isActive()

    @property
    def ever_foreground(self) -> bool:
        """面板本次显示期间是否曾经是前台窗口。"""
        return self._ever_foreground

    # -- 判定 ---------------------------------------------------------------

    def _tick(self) -> None:
        reason = self.poll_once()
        if reason:
            self._fire(reason)

    def _fire(self, reason: str) -> None:
        if self._fired:
            return
        self._fired = True
        logger.info("外部点击兜底触发关闭：原因=%s", reason)
        self.triggered.emit(reason)

    def poll_once(self, now: float | None = None) -> str | None:
        """做一次判定，返回关闭原因或 ``None``（不依赖定时器，便于单测）。

        Args:
            now: 单调时钟（测试注入用）；``None`` = 取当前时间。
        """
        if self.source is None:
            return None
        now = time.monotonic() if now is None else float(now)

        try:
            down, pressed_since = self.source.left_button_state()
        except Exception as exc:  # noqa: BLE001 - 取不到按键状态就当没按
            logger.debug("读取鼠标按键状态失败: %s", exc)
            down, pressed_since = False, False
        pressed = bool(pressed_since or (down and not self._prev_down))
        self._prev_down = bool(down)

        if now < self._grace_until:
            # 宽限期：只记录按键状态，不判死
            self._fg_since = None
            self._remember_pos()
            return None

        is_fg = self._safe_foreground()
        if is_fg:
            self._ever_foreground = True
            self._fg_since = None

        inside_now = self._point_inside()
        # 防误关：短按是靠"低位"补回来的，此时按下瞬间的落点可能已经被漏采。
        # 只要**本次或上一次轮询**的落点还在面板内，就按"内部操作"处理
        # （宁可漏关一次，也绝不把面板内拖拽/长按误判成点外面）。
        inside_before = self._last_inside
        self._remember_pos(inside_now)
        if pressed and not inside_now and not inside_before:
            return REASON_CLICK_OUTSIDE

        # 只有"曾经真的成为过前台"才启用 focus-lost：否则面板一次都没激活
        # （正是本缺陷的形态）时会自我关闭，把"关不掉"换成"打不开"。
        if self._ever_foreground and not is_fg:
            if self._fg_since is None:
                self._fg_since = now
            elif (now - self._fg_since) * 1000.0 >= self.focus_lost_ms:
                return REASON_FOCUS_LOST
        return None

    def _safe_foreground(self) -> bool:
        """``panel_is_foreground()`` 的安全包装（失败按"不是前台"处理）。"""
        if self.source is None:
            return False
        try:
            return bool(self.source.panel_is_foreground())
        except Exception as exc:  # noqa: BLE001
            logger.debug("前台判定失败: %s", exc)
            return False

    def _remember_pos(self, inside: bool | None = None) -> None:
        """记录本拍光标是否在面板内（下一拍用于"前后两拍"防误关）。"""
        if inside is None:
            inside = self._point_inside()
        self._last_inside = bool(inside)

    def _point_inside(self) -> bool:
        """按下点是否落在面板矩形内；判断不了时按"在内"处理（宁可不关）。"""
        if self.source is None:
            return True
        try:
            rect = self._rect_provider()
            if rect is None or not rect.isValid():
                return True
            return bool(rect.contains(self.source.cursor_pos()))
        except Exception as exc:  # noqa: BLE001
            logger.debug("落点判定失败（按面板内处理）: %s", exc)
            return True
