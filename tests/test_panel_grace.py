"""开场宽限单测：deactivate 路径也必须享受开场宽限（QA 的 B3）。

## 为什么有这个文件

v1.5 的 P0-1 修好了"关不掉"，但宽限期（红线 1）当初只做在
``panel_watch.OutsideClickWatcher`` 里，``_check_deactivate`` 这条**激活事件**
路径完全没有宽限——留下了一个镜像缺陷"打不开"：

    用户连点 SAO HUD 图标唤起面板时，第二次点击常落在**图标**上（图标在面板之外），
    面板瞬间失活 → 120ms 后 ``_check_deactivate`` 判死 → 面板一闪而过。
    实测（QA 取证到行）：show() 后 71ms 失活 → 190ms 被关。

本文件锁死三件事：

1. ``show()`` 之后宽限内（``panel_window.SHOW_GRACE_S``，由
   ``panel_watch.GRACE_MS`` 派生）触发失活复核 → 面板**存活**；
2. 超出宽限后触发失活复核 → 正常关闭，且原因仍是 ``deactivate``；
3. 宽限内那次失活**作废**：不重排定时器、也不延迟爆雷，宽限结束后**不得**因为
   **这一次**失活而关闭。
4. 宽限判据用的是**失活事件的发生时刻**（``_deactivate_at``），不是
   ``_check_deactivate`` 被调用时的 ``time.monotonic()``。``singleShot(120ms)``
   保证"检查时刻晚于事件时刻"，而 ``show()`` 之后主线程被截屏/淡入/建模压住时，
   复核可能迟到 300ms 以上——用检查时刻判就会把"打开手势的失活"误判成
   "用户走开了"，面板一闪而过。

> ⚠️ **反例，勿照做**：第 3 条**曾经**是"宽限内那次复核会自动重排、300ms 后把面板
> 关掉"（defer 方案），已被两轮实测否掉（关闭时刻从 190ms 只挪到 ~305ms，缺陷
> 不变：宽限结束时 ``activeWindow()`` 依然不是面板，再判必判死）。现在的实现是
> **discard**：宽限内直接 ``return``。改这里之前请先读
> ``app/windows/panel_window.py::_check_deactivate`` 的 docstring 和
> ``tests/qa_probe_b3_fix.py``（那里 ``[defer]`` 组是刻意保留的反面教材）。

## 测试手法

offscreen 平台下 ``show()`` 会让面板成为 ``QApplication.activeWindow()``，
而 ``_check_deactivate`` 判的是 ``QApplication.activeWindow() is not self``。
因此这里再 show 一个**普通 QWidget** 来"抢走"激活——这正好模拟真实场景里
"点图标 → 别的窗口拿到激活"，而且不依赖任何 mock。

同时把 ``_watch``（外部点击轮询）停掉：本文件只测激活事件这一半，避免
Win32 真实光标状态把用例搅成不稳定。
"""

from __future__ import annotations

import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtGui import QColor, QPixmap  # noqa: E402
from PySide6.QtWidgets import QWidget  # noqa: E402

from app.config import CategoryConfig  # noqa: E402
from app.windows import panel_watch  # noqa: E402
from app.windows.panel_watch import GRACE_MS  # noqa: E402
from app.windows.panel_window import (  # noqa: E402
    REASON_DEACTIVATE,
    PanelWindow,
)
from tests.qt_support import qt_app  # noqa: E402


def pump(seconds: float) -> None:
    """跑事件循环若干秒（``QTimer.singleShot`` 需要真实时间推进）。"""
    app = qt_app()
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.005)


class StubIcons:
    """最小 IconCache 替身：任意路径返回一张纯色 pixmap。"""

    def pixmap(self, path, px: int) -> QPixmap:  # noqa: ANN001 - 接口对齐
        pm = QPixmap(int(px), int(px))
        pm.fill(QColor("#4C8DFF"))
        return pm

    def default_icon(self, px: int) -> QPixmap:
        return self.pixmap("", px)

    def invalidate_dir(self, directory) -> None:  # noqa: ANN001 - 接口对齐
        return None


def make_panel() -> PanelWindow:
    # 必须先拿到 QApplication：没有实例时构造 QWidget 会**直接硬崩**
    # （Windows 上进程秒退、连 traceback 都没有——踩过一次，别再省这一行）。
    qt_app()
    cat = CategoryConfig(id="cat_grace", dir="宽限", label="宽限",
                         panel_w=320, panel_h=240)
    return PanelWindow(cat, 48, StubIcons())


class GraceConstantTests(unittest.TestCase):
    """宽限期必须与轮询那一半引用**同一个常量**，不能各写一个数。"""

    def test_grace_ms_is_shared_with_watcher(self) -> None:
        """panel_window.GRACE_MS 就是 panel_watch.GRACE_MS（同一个对象/同一个值）。"""
        from app.windows import panel_window

        self.assertIs(panel_window.GRACE_MS, panel_watch.GRACE_MS)
        self.assertEqual(GRACE_MS, 300)

    def test_show_grace_seconds_is_derived_not_duplicated(self) -> None:
        """``SHOW_GRACE_S`` 必须由 ``GRACE_MS`` 派生，不能是另一个字面量 0.3。

        单侧防漂移：将来有人把 GRACE_MS 改成 250，SHOW_GRACE_S 会自动跟着变；
        如果这里断言失败，说明有人在 panel_window 里又写死了一个 0.3。
        """
        from app.windows import panel_window

        self.assertEqual(panel_window.SHOW_GRACE_S, panel_watch.GRACE_MS / 1000.0)
        self.assertEqual(panel_window.SHOW_GRACE_S * 1000.0, panel_watch.GRACE_MS)

    def test_shown_at_is_zero_before_show(self) -> None:
        """未 show 过时 ``_shown_at`` 为 0.0：宽限必然早已过期 → 走原有判死逻辑。"""
        pw = make_panel()
        try:
            self.assertEqual(pw._shown_at, 0.0)  # noqa: SLF001 - 断言初始状态
        finally:
            pw.close()
            pw.deleteLater()

    def test_show_records_monotonic_timestamp(self) -> None:
        """``showEvent`` 写入单调时钟戳（不是 ``time.time()``，时钟回拨也不受影响）。"""
        pw = make_panel()
        try:
            before = time.monotonic()
            pw.show()
            qt_app().processEvents()
            after = time.monotonic()
            self.assertGreaterEqual(pw._shown_at, before)  # noqa: SLF001
            self.assertLessEqual(pw._shown_at, after)      # noqa: SLF001
        finally:
            pw.close()
            pw.deleteLater()


class DeactivateGraceTests(unittest.TestCase):
    """B3：deactivate 路径的开场宽限。"""

    def _shown_panel_with_other_active(self) -> tuple[PanelWindow, QWidget]:
        """返回 (面板, 抢走激活的窗口)；两者都已 show。"""
        pw = make_panel()
        pw.show()
        qt_app().processEvents()
        pw._watch.stop()           # noqa: SLF001 - 隔离轮询，只测激活事件这一半
        other = QWidget()
        other.show()
        qt_app().processEvents()
        return pw, other

    def test_deactivate_within_grace_keeps_panel_alive(self) -> None:
        """show() 之后宽限内触发失活复核 → 面板**不得**关闭（B3 的核心）。

        两段断言：立刻不关，**以及**等过整个宽限期之后仍然不关。
        只有前者的话，defer 实现（宽限内先不判、到期再判死）也能"通过"——
        那正是被否掉的写法，所以必须补上后半段。
        """
        pw, other = self._shown_panel_with_other_active()
        try:
            self.assertFalse(pw.isActiveWindow())      # 前置：确实"失活"了
            self.assertTrue(pw.isVisible())

            # ⚠️ 必须打戳：宽限判据用的是**事件发生时刻**，不打戳就走
            # ``_deactivate_at <= 0.0`` 那条"从未失活"分支直接 return，
            # 用例会假绿（空过当通过，踩过的坑）。
            pw._deactivate_at = pw._shown_at + 0.06    # noqa: SLF001 - 60ms 时失活
            pw._check_deactivate()                     # noqa: SLF001 - 直接触发复核

            self.assertEqual(pw.close_reason, "")
            self.assertFalse(pw._closing)              # noqa: SLF001
            self.assertTrue(pw.isVisible())

            pump((GRACE_MS + 250) / 1000.0)            # 等过宽限期
            self.assertTrue(pw.isVisible(), "宽限结束后被追认关闭了（defer 的典型症状）")
            self.assertEqual(pw.close_reason, "")
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    def test_deactivate_after_grace_closes_normally(self) -> None:
        """宽限结束后再复核 → 正常关闭，原因仍是 ``deactivate``（行为不变）。"""
        pw, other = self._shown_panel_with_other_active()
        try:
            # 把起点往前拨 400ms：等价于"面板已经开了 400ms"
            pw._shown_at -= 0.4                        # noqa: SLF001 - 构造"已过宽限"
            pw._deactivate_at = time.monotonic()       # noqa: SLF001 - 此刻发生失活
            pw._check_deactivate()                     # noqa: SLF001

            self.assertEqual(pw.close_reason, REASON_DEACTIVATE)
            self.assertTrue(pw._closing)               # noqa: SLF001
            pump(0.30)                                 # 等淡出 80ms 走完
            self.assertFalse(pw.isVisible())
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    def test_grace_discards_instead_of_deferring(self) -> None:
        """宽限内那次失活是**作废**，不是"推迟到 300ms 再判一次"。

        这是被 QA 的黑盒用例反推出来的关键区别：推迟会把同一个事件挪到更晚、
        更不可靠的时刻重新判（那时 ``activeWindow()`` 早已不是面板）→ 必然判死，
        等于面板开出来 0.8s 后自己关掉。作废之后，宽限过了也不该再关。
        """
        pw, other = self._shown_panel_with_other_active()
        try:
            pw._shown_at = time.monotonic()            # noqa: SLF001 - 宽限从"现在"起算
            pw._deactivate_at = pw._shown_at           # noqa: SLF001 - 0ms 时失活
            pw._check_deactivate()                     # noqa: SLF001
            self.assertTrue(pw.isVisible())

            pump((GRACE_MS + 250) / 1000.0)            # 等过整个宽限期

            self.assertTrue(pw.isVisible(), "宽限内的失活被推迟执行了，应作废")
            self.assertEqual(pw.close_reason, "")
            self.assertFalse(pw._closing)              # noqa: SLF001
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    def test_no_residual_timer_chain_after_discard(self) -> None:
        """作废之后不留下任何 self-schedule 定时器（不可能泄漏或延迟爆雷）。"""
        pw, other = self._shown_panel_with_other_active()
        try:
            pw._shown_at = time.monotonic()            # noqa: SLF001
            pw._deactivate_at = pw._shown_at           # noqa: SLF001 - 必须先打戳
            for _ in range(3):
                pw._check_deactivate()                 # noqa: SLF001
            pump(0.10)
            self.assertTrue(pw.isVisible())
            self.assertEqual(pw.close_reason, "")
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    # -- 判据用"事件时刻"而非"检查时刻"（主线程繁忙时定时器会迟到） -------------

    def test_late_check_still_uses_event_time_within_grace(self) -> None:
        """事件时刻在宽限内、检查时刻远在宽限外 → **作废**（不关）。

        场景来源：``show()`` 之后主线程并不空闲（毛玻璃截屏 6–12ms + 140ms 淡入 +
        首次建 ``PanelModel`` ~293ms），``singleShot(120ms)`` 的复核完全可能被压到
        300ms 之后才执行。若用检查时刻判，那次属于"打开手势"的失活会被误判成
        "用户走开了" → 面板一闪而过（P0-1 的镜像）。
        """
        pw, other = self._shown_panel_with_other_active()
        try:
            now = time.monotonic()
            pw._shown_at = now - 1.0                   # noqa: SLF001 - 检查时刻已过 1s
            pw._deactivate_at = pw._shown_at + 0.06    # noqa: SLF001 - 事件在 60ms
            elapsed_hint = (now - pw._shown_at) * 1000.0
            self.assertGreater(elapsed_hint, GRACE_MS,
                               "前置：检查时刻必须已经超出宽限期，否则测不到这个缺陷")

            pw._check_deactivate()                     # noqa: SLF001

            self.assertEqual(pw.close_reason, "")
            self.assertFalse(pw._closing)              # noqa: SLF001
            self.assertTrue(pw.isVisible())
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    def test_late_check_after_event_beyond_grace_closes(self) -> None:
        """事件时刻在宽限外（900ms）→ **判死**（B4「点桌面必关」不受影响）。"""
        pw, other = self._shown_panel_with_other_active()
        try:
            pw._shown_at = time.monotonic() - 1.0      # noqa: SLF001
            pw._deactivate_at = pw._shown_at + 0.9     # noqa: SLF001 - 事件在 900ms

            pw._check_deactivate()                     # noqa: SLF001

            self.assertEqual(pw.close_reason, REASON_DEACTIVATE)
            self.assertTrue(pw._closing)               # noqa: SLF001
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    def test_no_deactivate_event_is_a_noop(self) -> None:
        """从未收到过失活事件时复核是空操作（**不能**当成"面板该关"的依据）。

        这条同时也是对本文件其余用例的护栏：它们靠显式给 ``_deactivate_at``
        打戳来"武装"自己，忘了打戳就会走这条分支而假绿。

        注：``_shown_panel_with_other_active()`` 里 ``other.show()`` 会真的派发一次
        ``WindowDeactivate``（实测确实打上了戳），所以这里显式清零来构造"从未失活"。
        """
        pw, other = self._shown_panel_with_other_active()
        try:
            pw._deactivate_at = 0.0                    # noqa: SLF001 - 构造"从未失活"
            pw._shown_at = time.monotonic() - 5.0      # noqa: SLF001 - 早已过宽限
            pw._check_deactivate()                     # noqa: SLF001
            self.assertEqual(pw.close_reason, "")
            self.assertTrue(pw.isVisible())
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()

    def test_real_deactivate_event_stamps_deactivate_at(self) -> None:
        """真实 ``WindowDeactivate`` 必须在**事件里**打戳（不是靠轮询补）。

        这条是「判据换时刻」这项改动的接线检查：``event()`` / ``changeEvent()``
        里漏了 ``self._deactivate_at = time.monotonic()`` 时，前一条单测会因为
        ``<= 0.0`` 直接 return 而假绿，只有这条会红。
        """
        pw = make_panel()
        other = QWidget()
        try:
            pw.show()
            qt_app().processEvents()
            pw._watch.stop()                           # noqa: SLF001
            self.assertEqual(pw._deactivate_at, 0.0)   # noqa: SLF001 - 尚未失活

            before = time.monotonic()
            other.show()                               # 抢走激活 → 真实失活事件
            qt_app().processEvents()
            after = time.monotonic()

            self.assertFalse(pw.isActiveWindow())
            self.assertGreaterEqual(pw._deactivate_at, before)  # noqa: SLF001
            self.assertLessEqual(pw._deactivate_at, after)      # noqa: SLF001
        finally:
            pw.close()
            other.close()
            pw.deleteLater()
            other.deleteLater()


if __name__ == "__main__":
    unittest.main()
