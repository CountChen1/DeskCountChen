"""宽限期边界探针：**失活发生在宽限内、复核落在宽限外**时不得判死。

## 为什么单独一个探针

B 段那条 `B3 宽限期结束后该次点击不追认为外部点击` 是间歇红的，根因不是抖动：

``_check_deactivate`` 被 ``QTimer.singleShot(CLOSE_DELAY_MS=120)`` 延迟调用，
如果它用**检查时刻**（``time.monotonic() - _shown_at``）判断"是否还在宽限内"，
那么一次发生在 **190ms** 的失活会在 **310ms** 被检查到 —— 落到 300ms 宽限之外，
于是一次本该作废的失活被判死。

边界很清楚：``deactivate_at + 120 >= 300``，即 **``deactivate_at >= 180ms`` 就误判**。
B3 真实注入的失活落在 62～190ms，正好骑在线上，所以时红时绿。

正确实现必须在**收到失活事件的那一刻**打时间戳（``_deactivate_at``），
判定用 ``_deactivate_at - _shown_at``。本探针把时机从"骑线随机"改成
"精确构造"，结果可重复：

| 用例 | 失活时机 | 正确行为 |
|---|---|---|
| 100ms | 宽限内 | 作废，面板存活 |
| 200ms | 宽限内（**190ms 那条线的复现**） | 作废，面板存活 |
| 280ms | 宽限内（贴着边界） | 作废，面板存活 |
| 420ms | 宽限之后 | 判死，面板关闭且 reason=deactivate |
| 重置 | 上一面板的旧时间戳 | 不得影响下一个面板 |

用法：``.venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py``
"""
from __future__ import annotations

import json
import os
import shutil
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import QEvent  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.windows.panel_window import (  # noqa: E402
    CLOSE_DELAY_MS,
    REASON_DEACTIVATE,
)

GRACE_MS = 300                     # panel_watch.GRACE_MS
RESULTS: list[tuple[str, bool, str]] = []
SKIPS: list[tuple[str, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def pump(app, seconds: float, step: float = 0.005) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(step)


def pump_until(app, target_s: float, t0: float) -> float:
    """推进到"距 ``t0`` 至少 ``target_s`` 秒"，返回实际 elapsed(ms)。"""
    while True:
        elapsed = time.monotonic() - t0
        if elapsed >= target_s:
            return elapsed * 1000.0
        app.processEvents()
        time.sleep(0.002)


def shown_at(pw) -> float:                      # noqa: ANN001
    return float(getattr(pw, "_shown_at", 0.0))


def deactivate_at(pw):                          # noqa: ANN001
    """产品记录的事件时刻戳（未实现时返回 0.0 → 探针判 SKIP 而不是崩）。"""
    return float(getattr(pw, "_deactivate_at", 0.0) or 0.0)


def seed() -> None:
    sandbox.sandbox_paths(prefix="dbx_grace_edge_")
    paths.ensure_base_dirs()
    d = paths.DATA_ROOT / "甲"
    d.mkdir(parents=True, exist_ok=True)
    for n in range(6):
        (d / f"e{n}.txt").write_text("x")
    paths.CONFIG_PATH.write_text(json.dumps({
        "version": 1,
        "app": {},
        "categories": [{"id": "c0", "dir": "甲", "label": "甲",
                        "panel_w": 420, "panel_h": 300, "material": "solid"}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")


class Stealer:
    """把 Qt 层激活从面板手里抢走的普通窗口。"""

    def __init__(self) -> None:
        self.w = QWidget()
        self.w.resize(160, 90)
        self.w.move(20, 20)
        self.w.show()

    def steal(self) -> None:
        self.w.raise_()
        self.w.activateWindow()

    def close(self) -> None:
        self.w.close()
        self.w.deleteLater()


def case(app, controller, label: str, fire_at_ms: float, expect_alive: bool) -> None:
    """在 ``fire_at_ms`` 时刻制造一次失活，看它是否被判死。

    ## 有效性前置（缺一条就 SKIP，不许判 PASS/FAIL）

    1. **失活事件真的到达了** —— ``_deactivate_at > 0``。没到达就什么都证明不了，
       面板"存活"只是因为它压根没被通知（我在这上面栽过一次：``ever_fg=False``
       的面板存活，看着像 PASS，其实场景没发生）。
    2. 判"必须关闭"的用例，还要求 ``QApplication.activeWindow() is not self`` ——
       产品只在"复核时前台确实不是自己"时才关；这条不成立就去断言"没关"，
       等于冤枉产品。
    """
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check(f"{label} open_panel 产出面板", False, "controller.panel is None")
        return
    t0 = shown_at(pw)
    stealer = Stealer()
    try:
        actual_ms = pump_until(app, fire_at_ms / 1000.0, t0)
        before = deactivate_at(pw)
        stealer.steal()                     # ← 尽量制造真实 WindowDeactivate
        # 兜底：真实激活未必发生（本机前台锁），补一发合成的 WindowDeactivate。
        # 这不是"造假"——被测的是**事件时刻 vs 检查时刻**的判定逻辑，
        # 事件从哪来不影响这个逻辑；真窗口那条路径 B 段已经覆盖。
        QApplication.sendEvent(pw, QEvent(QEvent.Type.WindowDeactivate))
        pump(app, 0.03)
        fired = deactivate_at(pw)
        qt_still_panel = QApplication.activeWindow() is pw
        pump(app, (GRACE_MS + CLOSE_DELAY_MS) / 1000.0)
    finally:
        stealer.close()

    alive = controller.panel is not None
    reason = getattr(pw, "close_reason", "")
    event_ms = (fired - t0) * 1000.0 if fired else -1.0
    detail = (f"失活事件于 show 后 {event_ms:.0f}ms"
              f"（+{CLOSE_DELAY_MS}ms 复核 → {event_ms + CLOSE_DELAY_MS:.0f}ms "
              f"vs 宽限 {GRACE_MS}ms）；注入点 {actual_ms:.0f}ms；"
              f"复核时 activeWindow 仍是面板={qt_still_panel}；"
              f"panel={'存活' if alive else 'None(已关)'} reason={reason!r}")

    if fired <= before:
        skip(f"{label} 失活@{fire_at_ms:.0f}ms",
             f"无效：注入点没有产生新的失活事件（戳未变化 {before:.3f}→{fired:.3f}）")
    elif expect_alive:
        check(f"{label} 失活@{event_ms:.0f}ms（宽限内）→ 作废，面板存活", alive, detail)
    elif qt_still_panel:
        skip(f"{label} 失活@{event_ms:.0f}ms（宽限之后）",
             "无效：复核时 activeWindow 仍是面板，产品按设计就不该关，断言不成立")
    else:
        check(f"{label} 失活@{event_ms:.0f}ms（宽限之后）→ 判死，reason=deactivate",
              (not alive) and reason == REASON_DEACTIVATE, detail)

    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.35)


def case_reset(app, controller) -> None:
    """上一个面板的旧 ``_deactivate_at`` 不得泄漏到下一个面板。

    泄漏的后果很阴：旧戳来自上一个面板，``_deactivate_at - _shown_at(新面板)``
    会算成**负数** → 永远落在宽限内 → 新面板从此再也不因失活而关闭。
    """
    # 第一轮：宽限内失活（应被作废，面板存活）
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("重置 open_panel 产出面板", False, "controller.panel is None")
        return
    t0 = shown_at(pw)
    st = Stealer()
    try:
        pump_until(app, 0.20, t0)
        before = deactivate_at(pw)
        st.steal()
        QApplication.sendEvent(pw, QEvent(QEvent.Type.WindowDeactivate))
        pump(app, 0.03)
        first_fired = deactivate_at(pw) > before
        pump(app, 0.30)
    finally:
        st.close()
    first_alive = controller.panel is not None
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.35)

    # 第二轮：宽限之后失活（必须判死）
    controller.open_panel("c0")
    pw2 = controller.panel
    if pw2 is None:
        check("重置 第二个面板产出", False, "controller.panel is None")
        return
    t1 = shown_at(pw2)
    st2 = Stealer()
    try:
        pump_until(app, 0.45, t1)
        before2 = deactivate_at(pw2)
        st2.steal()
        QApplication.sendEvent(pw2, QEvent(QEvent.Type.WindowDeactivate))
        pump(app, 0.03)
        second_fired = deactivate_at(pw2) > before2
        qt_still_panel = QApplication.activeWindow() is pw2
        pump(app, 0.35)
    finally:
        st2.close()
    second_closed = controller.panel is None
    reason = getattr(pw2, "close_reason", "")
    leaked = deactivate_at(pw2) > 0.0 and deactivate_at(pw2) < t1

    name = "重置 上一面板的旧时间戳不影响下一个面板（宽限后失活仍判死）"
    if not (first_fired and second_fired):
        skip(name, f"无效：失活事件没到达（first={first_fired} second={second_fired}）")
    elif qt_still_panel:
        skip(name, "无效：第二轮复核时 activeWindow 仍是面板，产品按设计就不该关")
    else:
        check(name,
              first_alive and second_closed and reason == REASON_DEACTIVATE and not leaked,
              f"首轮存活={first_alive}；次轮关闭={second_closed} reason={reason!r}；"
              f"次轮事件戳是否早于次轮 show={leaked}")
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.35)


def install_mutant_patch() -> None:
    """把判定基准退回"检查时刻"，用来验证**本探针有鉴别力**。

    ## 为什么探针要能自证

    一个永远绿的探针比没有探针更危险 —— 它会让人以为测过了。
    这个 mutant 就是产品原本那个缺陷（用 ``time.monotonic() - _shown_at`` 判宽限），
    只是被挪到 monkeypatch 里、不碰 ``app/`` 源码。

    用法：``qa_probe_grace_boundary.py --mutant`` —— 此时**必须红**。
    如果 mutant 模式也全绿，说明探针失效，不能拿它当放行依据。
    """
    from app.windows import panel_window as PW

    def _check_deactivate(self) -> None:            # noqa: N802
        from PySide6.QtWidgets import QApplication

        if not self.isVisible():
            return
        if float(getattr(self, "_deactivate_at", 0.0) or 0.0) <= 0.0:
            return
        elapsed_ms = (time.monotonic() - self._shown_at) * 1000.0   # ← mutant
        if elapsed_ms < PW.SHOW_GRACE_S * 1000.0:
            return
        if QApplication.activeWindow() is not self:
            self.close_with_reason(PW.REASON_DEACTIVATE)

    PW.PanelWindow._check_deactivate = _check_deactivate      # type: ignore[method-assign]


def main() -> int:
    argv = sys.argv[1:]
    if "--mutant" in argv:
        install_mutant_patch()
        print("=" * 70)
        print("!!! mutant 模式：判定基准退回「检查时刻」—— 本次运行**必须红**")
        print("=" * 70)

    seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)
    controller.start()
    pump(app, 1.0)

    print(f"宽限 {GRACE_MS}ms，复核延迟 {CLOSE_DELAY_MS}ms → "
          f"误判阈值 deactivate_at >= {GRACE_MS - CLOSE_DELAY_MS}ms\n")

    case(app, controller, "① ", 100, expect_alive=True)
    case(app, controller, "② ", 200, expect_alive=True)
    case(app, controller, "③ ", 280, expect_alive=True)
    case(app, controller, "④ ", 420, expect_alive=False)
    case_reset(app, controller)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，"
          f"失败 {len(failed)} 项，环境跳过 {len(SKIPS)} 项")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")

    controller.shutdown()
    root = sandbox.sandbox_root()
    if root:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
