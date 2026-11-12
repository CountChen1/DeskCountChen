"""【P0-1】面板关闭兜底判定单测：外部点击 / 焦点丢失（不碰真实鼠标）。

被测对象是 :class:`app.windows.panel_watch.OutsideClickWatcher`。判定逻辑与
Win32 完全解耦（只依赖 ``InputSource`` 三个方法），因此这里注入"假输入源"，
用一个可手动推进的假时钟逐拍断言，结果完全确定，不需要真实点击或真实前台窗口。

覆盖的红线：
1. 面板外按下 → ``click-outside``；
2. 面板内按下 → **绝不**触发；
3. show() 后 300ms 宽限期内 → 只记录不判死；
4. 从未成为前台的面板 → ``focus-lost`` **永不**触发（否则会变成"打不开"）；
5. 曾是前台再丢失 ≥180ms → ``focus-lost``；
6. 前后两拍任一落在面板内 → 不判外部点击（防短按漏采导致误关）；
7. 没有输入源 / 定时器停止时不炸、不泄漏。
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtCore import QPoint, QRect  # noqa: E402

from app.windows.panel_watch import (  # noqa: E402
    CLICK_POLL_MS,
    FOCUS_LOST_MS,
    GRACE_MS,
    REASON_CLICK_OUTSIDE,
    REASON_FOCUS_LOST,
    OutsideClickWatcher,
)
from tests.qt_support import qt_app  # noqa: E402

PANEL = QRect(100, 100, 200, 150)
INSIDE = QPoint(150, 150)
OUTSIDE = QPoint(900, 700)


class FakeSource:
    """可编程输入源：按键状态、光标位置、前台归属全部由用例指定。"""

    def __init__(self) -> None:
        self.down = False
        self.since = False        # "自上次查询以来按过"（系统实现里恒 False，见下）
        self.foreground = False
        self.point = QPoint(INSIDE)
        self.button_calls = 0

    # -- InputSource ------------------------------------------------------
    def left_button_state(self) -> tuple[bool, bool]:
        self.button_calls += 1
        return self.down, self.since

    def cursor_pos(self) -> QPoint:
        return QPoint(self.point)

    def panel_is_foreground(self) -> bool:
        return bool(self.foreground)

    # -- 用例用 ------------------------------------------------------------
    def press(self, point: QPoint) -> None:
        self.point = QPoint(point)
        self.down = True
        self.since = True

    def release(self) -> None:
        self.down = False
        self.since = False


class WatcherCase(unittest.TestCase):
    """公共装置：假时钟 + 已 start 的 watcher。"""

    def setUp(self) -> None:
        qt_app()
        self.src = FakeSource()
        self.w = OutsideClickWatcher(
            lambda: QRect(PANEL), source=self.src, grace_ms=GRACE_MS,
            interval_ms=CLICK_POLL_MS, focus_lost_ms=FOCUS_LOST_MS,
        )
        self.fired: list[str] = []
        self.w.triggered.connect(self.fired.append)
        self.w.start()
        self.now = self.w._grace_until + 0.001     # noqa: SLF001 - 直接越过宽限期
        self.assertEqual(self.fired, [])

    def tick(self, dt_ms: float = 0.0) -> str | None:
        """推进假时钟并做一次判定（同时模拟定时器的触发路径）。"""
        self.now += dt_ms / 1000.0
        reason = self.w.poll_once(now=self.now)
        if reason:
            self.w._fire(reason)               # noqa: SLF001 - 走真实发信号路径
        return reason


class ClickOutsideTests(WatcherCase):
    def test_click_outside_triggers(self) -> None:
        # 先把光标移出去（真实操作必然有移动过程，至少走一拍），再按下
        self.src.point = QPoint(OUTSIDE)
        self.assertIsNone(self.tick())
        self.src.press(OUTSIDE)
        self.assertEqual(self.tick(), REASON_CLICK_OUTSIDE)
        self.assertEqual(self.fired, [REASON_CLICK_OUTSIDE])

    def test_click_outside_right_after_open_needs_one_move_tick(self) -> None:
        """防误关的代价：光标"瞬移"出去后立刻按下，第一拍按内部处理。

        这是刻意的保守取舍（宁漏一次，不误关一次）：真实操作里鼠标从面板内
        移到面板外至少经过一拍（40ms），所以不影响正常使用。
        """
        self.src.press(OUTSIDE)                    # 上一拍仍在面板内
        self.assertIsNone(self.tick())
        self.src.release()
        self.assertIsNone(self.tick())             # 这一拍刷新"在外"
        self.src.press(OUTSIDE)
        self.assertEqual(self.tick(), REASON_CLICK_OUTSIDE)

    def test_click_inside_never_triggers(self) -> None:
        self.src.press(INSIDE)
        self.assertIsNone(self.tick())
        self.src.release()
        for _ in range(5):
            self.assertIsNone(self.tick(50))
        self.assertEqual(self.fired, [])

    def test_click_during_grace_is_ignored(self) -> None:
        """宽限期内（面板刚 show 的那一下点击）绝不算外部点击。"""
        self.now = self.w._grace_until - 0.05      # noqa: SLF001
        self.src.press(OUTSIDE)
        self.assertIsNone(self.tick())
        self.assertEqual(self.fired, [])

    def test_prev_tick_inside_suppresses_short_press(self) -> None:
        """上一拍还在面板内 → 本拍即便"按下 + 落点在外"也不算外部点击。

        这是防误关的关键：短按靠系统状态补回来时，按下瞬间的落点已被漏采，
        面板内拖拽（按下在内、拖到外面松手）不能把面板误关掉。
        """
        self.src.point = QPoint(INSIDE)
        self.assertIsNone(self.tick())             # 上一拍：在面板内
        self.src.down = True
        self.src.since = True
        self.src.point = QPoint(OUTSIDE)            # 本拍：已拖到面板外
        self.assertIsNone(self.tick())
        self.assertEqual(self.fired, [])

    def test_only_press_edge_reacts(self) -> None:
        """按住不放只在"抬起→按下"沿判一次，长按不会反复触发。"""
        self.src.point = QPoint(OUTSIDE)
        self.assertIsNone(self.tick(0))            # 先建立"在外"的上一拍
        self.src.down = True
        self.src.since = True
        self.assertEqual(self.tick(), REASON_CLICK_OUTSIDE)
        # 继续按住（since 已被消费），不应再次触发
        self.src.since = False
        self.assertIsNone(self.tick(50))
        self.assertEqual(self.fired, [REASON_CLICK_OUTSIDE])

    def test_signal_fires_once_only(self) -> None:
        self.w._fire(REASON_CLICK_OUTSIDE)         # noqa: SLF001
        self.w._fire(REASON_CLICK_OUTSIDE)         # noqa: SLF001
        self.assertEqual(len(self.fired), 1)


class FocusLostTests(WatcherCase):
    def test_never_foreground_never_triggers(self) -> None:
        """核心安全红线：面板一次都没成为前台时，focus-lost 永不触发。

        否则"抢不到前台的机器"上会变成面板打开 0.5 秒就自己消失。
        """
        self.src.foreground = False
        for _ in range(40):
            self.assertIsNone(self.tick(50))
        self.assertFalse(self.w.ever_foreground)
        self.assertEqual(self.fired, [])

    def test_focus_lost_after_being_foreground(self) -> None:
        self.src.foreground = True
        self.assertIsNone(self.tick())
        self.assertTrue(self.w.ever_foreground)
        self.src.foreground = False
        self.assertIsNone(self.tick(100))                # 刚丢失：开始计时
        self.assertIsNone(self.tick(100))                # 100ms < 180ms 阈值
        self.assertEqual(self.tick(100), REASON_FOCUS_LOST)

    def test_focus_regained_resets_timer(self) -> None:
        self.src.foreground = True
        self.tick()
        self.src.foreground = False
        self.assertIsNone(self.tick(150))
        self.src.foreground = True                       # 又回来了
        self.assertIsNone(self.tick(50))
        self.src.foreground = False
        self.assertIsNone(self.tick(50))                 # 重新从 0 计时
        self.assertIsNone(self.tick(100))
        self.assertEqual(self.tick(100), REASON_FOCUS_LOST)


class LifecycleTests(unittest.TestCase):
    def setUp(self) -> None:
        qt_app()

    def test_listen_without_source_is_safe(self) -> None:
        w = OutsideClickWatcher(lambda: QRect(PANEL), source=None)
        w.start()
        self.assertFalse(w.is_running())
        self.assertIsNone(w.poll_once())      # 没有源 → 永不判定
        w.stop()

    def test_timer_runs_and_stops(self) -> None:
        src = FakeSource()
        w = OutsideClickWatcher(lambda: QRect(PANEL), source=src)
        self.assertFalse(w.is_running())
        w.start()
        self.assertTrue(w.is_running())
        w.stop()
        self.assertFalse(w.is_running())

    def test_rect_provider_failure_is_conservative(self) -> None:
        """矩形取不到（面板正在销毁）→ 按"在面板内"处理，绝不误关。"""
        src = FakeSource()
        w = OutsideClickWatcher(lambda: None, source=src, grace_ms=0)
        w.start()
        src.press(OUTSIDE)
        self.assertIsNone(w.poll_once(now=w._grace_until + 1))  # noqa: SLF001

    def test_system_source_returns_two_bools(self) -> None:
        """真实输入源必须一次返回两个布尔值（低位被分两次查询会丢失）。"""
        from app.windows.panel_watch import SystemInputSource

        src = SystemInputSource(lambda: 0)
        state = src.left_button_state()
        self.assertEqual(len(state), 2)
        self.assertIsInstance(state[0], bool)
        self.assertIsInstance(state[1], bool)


if __name__ == "__main__":
    unittest.main()
