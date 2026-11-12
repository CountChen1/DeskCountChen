"""开场宽限单测：deactivate 路径也必须享受 ``GRACE_MS``（QA 的 B3）。

## 为什么有这个文件

v1.5 的 P0-1 修好了"关不掉"，但宽限期（红线 1）当初只做在
``panel_watch.OutsideClickWatcher`` 里，``_check_deactivate`` 这条**激活事件**
路径完全没有宽限——留下了一个镜像缺陷"打不开"：

    用户连点 SAO HUD 图标唤起面板时，第二次点击常落在**图标**上（图标在面板之外），
    面板瞬间失活 → 120ms 后 ``_check_deactivate`` 判死 → 面板一闪而过。
    实测（QA 取证到行）：show() 后 71ms 失活 → 190ms 被关。

本文件锁死三件事：

1. ``show()`` 之后 ``GRACE_MS`` 内触发失活复核 → 面板**存活**；
2. 超出 ``GRACE_MS`` 后触发失活复核 → 正常关闭，且原因仍是 ``deactivate``；
3. 宽限内那次复核会**自动重排**，宽限结束后自己把面板关掉（不需要外部再踢一脚）。

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

    def test_shown_at_is_zero_before_show(self) -> None:
        """未 show 过时 ``_shown_at`` 为 0.0：宽限必然早已过期，不会误推迟关闭。"""
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
        """show() 之后宽限内触发失活复核 → 面板**不得**关闭（B3 的核心）。"""
        pw, other = self._shown_panel_with_other_active()
        try:
            self.assertFalse(pw.isActiveWindow())      # 前置：确实"失活"了
            self.assertTrue(pw.isVisible())

            pw._check_deactivate()                     # noqa: SLF001 - 直接触发复核

            self.assertEqual(pw.close_reason, "")
            self.assertFalse(pw._closing)              # noqa: SLF001
            self.assertTrue(pw.isVisible())
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


if __name__ == "__main__":
    unittest.main()
