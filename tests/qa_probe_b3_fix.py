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

from PySide6.QtCore import QPoint, Qt, QTimer  # noqa: E402
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
RESULTS: list[tuple[str, bool, str]] = []
VARIANT = ""          # 当前候选补丁名（前缀进断言名，便于分辨 v1/v2）

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def check(name: str, ok: bool, detail: str = "") -> None:
    label = f"[{VARIANT}] {name}" if VARIANT else name
    RESULTS.append((label, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {label}" + (f"  ({detail})" if detail else ""))


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


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
        variant: ``"defer"`` = 宽限内**推迟**复核（把 ``_check_deactivate``
            再排一次）；``"discard"`` = 宽限内**丢弃**这次失活事件。

    两个变体的差别是本质的：推迟到宽限结束时面板**仍然不是前台**，于是照样
    关（实测 v1 无效，见探针输出）；只有丢弃才能让"刚 show 的失活churn"不被
    当成用户主动切走。
    """
    orig_show = PanelWindow.showEvent
    orig_check = PanelWindow._check_deactivate       # noqa: SLF001

    def show_event(self, event):                     # noqa: ANN001
        self._shown_at = time.monotonic()
        return orig_show(self, event)

    if variant == "defer":
        def _check_deactivate(self):                 # noqa: N802
            if self.isVisible() and QApplication.activeWindow() is not self:
                remain = SHOW_GRACE_S - (time.monotonic()
                                         - getattr(self, "_shown_at", 0.0))
                if remain > 0:
                    QTimer.singleShot(int(remain * 1000) + 5, self._check_deactivate)
                    return
                self.close_with_reason("deactivate")
    else:                                            # "discard"
        def _check_deactivate(self):                 # noqa: N802
            if not self.isVisible():
                return
            if time.monotonic() - getattr(self, "_shown_at", 0.0) < SHOW_GRACE_S:
                return          # show 后宽限内：这次失活视为"打开动作的 churn"，丢弃
            if QApplication.activeWindow() is not self:
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
    t0 = time.monotonic()
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G1 面板已打开", False)
        return
    pt = pick_outside(pw.frameGeometry())
    px, py = to_phys(pt.x(), pt.y())
    saved = (0, 0)
    mouse([("move", px, py), ("sleep", 0.03), ("down",), ("sleep", 0.08), ("up",)])
    pump(app, 1.2)
    elapsed = (time.monotonic() - t0) * 1000
    alive = controller.panel is not None
    check("G1 宽限期内外点：面板存活（B3 转绿）", alive,
          f"{elapsed:.0f}ms 后 panel={'存活' if alive else 'None(仍被关)'}"
          f" reason={getattr(pw, 'close_reason', '')!r}")
    if alive:
        controller.close_panel()
    pump(app, 0.4)


def g2_settled_click(app, controller) -> None:
    print("\n--- G2. 静置 0.9s 后外部单击 → 期望关闭（不能弄坏「点桌面必关」）---")
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G2 面板已打开", False)
        return
    pump(app, 0.9)
    pt = pick_outside(pw.frameGeometry())
    px, py = to_phys(pt.x(), pt.y())
    mouse([("move", px, py), ("sleep", 0.05), ("down",), ("sleep", 0.10), ("up",)])
    pump(app, 1.1)
    closed = controller.panel is None
    check("G2 静置后外点：面板关闭", closed,
          f"reason={getattr(pw, 'close_reason', '')!r}")
    if not closed:
        controller.close_panel()
    pump(app, 0.4)


def g3_deactivate(app, controller) -> None:
    print("\n--- G3. 静置后激活其他窗口 → 期望 deactivate 关闭（v1.4 路径不回退）---")
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G3 面板已打开", False)
        return
    pump(app, 0.9)          # 越过宽限期
    dummy = QWidget()
    dummy.resize(220, 110)
    dummy.move(50, 50)
    dummy.show()
    dummy.raise_()
    dummy.activateWindow()
    pump(app, 0.8)
    closed = controller.panel is None
    reason = getattr(pw, "close_reason", "")
    check("G3 失焦 → 关闭且原因=deactivate", closed and reason == "deactivate",
          f"panel={'None' if closed else '仍打开'} reason={reason!r}")
    dummy.close()
    dummy.deleteLater()
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.4)


def g4_esc(app, controller) -> None:
    print("\n--- G4. Esc → 期望关闭且原因=esc ---")
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("G4 面板已打开", False)
        return
    pump(app, 0.5)
    if QTest is not None:
        QTest.keyClick(pw, Qt.Key.Key_Escape)
    else:                                             # pragma: no cover
        from PySide6.QtGui import QKeyEvent
        from PySide6.QtCore import QEvent

        QApplication.sendEvent(pw, QKeyEvent(QEvent.Type.KeyPress, Qt.Key.Key_Escape,
                                            Qt.KeyboardModifier.NoModifier))
    pump(app, 0.6)
    closed = controller.panel is None
    reason = getattr(pw, "close_reason", "")
    check("G4 Esc → 关闭且原因=esc", closed and reason == "esc",
          f"panel={'None' if closed else '仍打开'} reason={reason!r}")
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.4)


def main() -> int:
    global VARIANT
    seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
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
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "; ".join(failed))
    root = sandbox.sandbox_root()
    if root:
        shutil.rmtree(root, ignore_errors=True)
    controller.shutdown()
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
