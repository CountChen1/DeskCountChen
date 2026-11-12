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
| ① / ② / ③ | 100 / 200 / 280ms（宽限内） | 作废，面板存活 |
| ④ | 420ms（宽限之后） | 判死，面板关闭且 reason=deactivate |
| 重置（流程） | 连开两块面板 | 第二块拿到干净的宽限起点 |
| 重置（复用） | 同一对象 show 两次 | ``showEvent`` 必须把 ``_deactivate_at`` 清回 0.0 |

## 两条"重置"用例各管什么（别搞混，这里是踩过的坑）

- **重置（流程）** 验证的是**流程**：连开两块面板、第二块单独拿到干净的宽限起点，
  不会因为第一块的失活而"该关的不关"。它**不**验证 ``showEvent`` 里的重置行 ——
  因为 ``controller.open_panel()`` 每次都 ``new PanelWindow``，``__init__`` 里
  ``_deactivate_at`` 就是 ``0.0``，把 ``showEvent`` 那行删掉它照样绿（**空过**）。
  ``showEvent`` 重置行的覆盖在白盒 ``test_show_resets_deactivate_at`` 与本探针的
  **重置（复用）** 身上。
- **重置（复用）** 才是那条护栏：**同一个对象** show 两次，第二次之前人为把
  ``_deactivate_at`` 置成一个非零的旧戳，断言 ``showEvent`` 会把它清回 ``0.0``。
  这是 ``PanelWindow`` 将来改成"缓存 + 重新 show"时的唯一防线。

## 自证（探针必须能红，否则不许拿它当放行依据）

- ``--mutant``：把判定基准退回"检查时刻" → ①②③ + 流程重置 **必须红**。
- ``--mutant-reset``：把 ``showEvent`` 里的重置"取消"（show 之后把值写回 show 前的旧值）
  → **重置（复用）必须红**，且 ①～④ 与流程重置应仍绿（证明这个 mutant 是外科手术式的，
  只打那一条）。若它没红，说明本探针失效，此时退出码为 2 并打印 ``[SELF-CHECK] 失效``。

用法：
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py --mutant
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py --mutant-reset
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

from PySide6.QtCore import QEvent, QObject  # noqa: E402
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

RESET_FLOW_LABEL = "重置(流程) 连开两块面板：第二块拿到干净的宽限起点"
RESET_REUSE_LABEL = "重置(复用) 同一对象 show 两次：showEvent 必须把 _deactivate_at 清回 0.0"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def skip(name: str, detail: str = "") -> None:
    """用例前提不成立：既不算通过也不算失败，汇总时单列。"""
    SKIPS.append((name, detail))
    print(f"[SKIP] {name}" + (f"  ({detail})" if detail else ""))


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


class DeactSpy(QObject):
    """数面板收到过几次 ``WindowDeactivate``。

    "重置(复用)" 用到它：如果断言 ``_deactivate_at == 0.0`` 失败，要能区分
    "重置那行被删了"（真缺陷）和"刚好有一发真实失活把戳又打上了"（环境噪音）。
    没有这个计数器，后者会被记成 FAIL，冤枉产品。
    """

    def __init__(self) -> None:
        super().__init__()
        self.count = 0
        self.watch = None

    def reset(self, panel) -> None:                # noqa: ANN001
        self.watch = panel
        self.count = 0

    def eventFilter(self, obj, event) -> bool:     # noqa: N802
        if event.type() == QEvent.Type.WindowDeactivate and obj is self.watch:
            self.count += 1
        return False


DEACT_SPY = DeactSpy()


def install_deact_spy() -> None:
    app = QApplication.instance()
    if app is not None:
        app.installEventFilter(DEACT_SPY)


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
    """**重置（流程）**：连开两块面板，第二块拿到干净的宽限起点。

    ## 这条**不**验证 ``showEvent`` 里的重置行（重要，别给它不该有的信用）

    ``controller.open_panel()`` 每次都 ``new PanelWindow``（见 ``app/controller.py``
    里 ``pw = PanelWindow(...)``），``__init__`` 里 ``_deactivate_at`` 就是 ``0.0``。
    所以"第二块拿到上一块的旧戳"这条路**结构上走不到** —— 把 ``showEvent`` 里
    ``self._deactivate_at = 0.0`` 删掉，本条照样绿（实测确认，属于**空过**）。
    ``showEvent`` 重置行由白盒 ``test_show_resets_deactivate_at`` 与本文件的
    ``case_reset_reuse`` 覆盖。

    本条真正能抓的是**流程级回归**：第一块的失活把第二块的宽限起点带歪，
    导致第二块"该关的不关"（例如将来 ``open_panel`` 加缓存、或宽限起点忘记重置）。
    """
    # 第一轮：宽限内失活（应被作废，面板存活）
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("重置(流程) open_panel 产出面板", False, "controller.panel is None")
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
        check("重置(流程) 第二个面板产出", False, "controller.panel is None")
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

    if not (first_fired and second_fired):
        skip(RESET_FLOW_LABEL,
             f"无效：失活事件没到达（first={first_fired} second={second_fired}）")
    elif qt_still_panel:
        skip(RESET_FLOW_LABEL, "无效：第二轮复核时 activeWindow 仍是面板，产品按设计就不该关")
    else:
        check(RESET_FLOW_LABEL,
              first_alive and second_closed and reason == REASON_DEACTIVATE,
              f"首轮存活={first_alive}；次轮关闭={second_closed} reason={reason!r}")
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.35)


def case_reset_reuse(app, controller) -> None:
    """**重置（复用）**：同一个对象 show 两次，``showEvent`` 必须把旧戳清回 0.0。

    ## 为什么要单独立这一条

    判据是 ``(_deactivate_at - _shown_at)``。若对象被复用而旧戳没清：
    旧戳早于本次 show → 差值为**负** → 永远落进宽限 → **该关的不关**；
    跨轮次时间差过大 → 永远落出宽限 → **该作废的判死**。两个方向都不可预测。

    现在 ``open_panel`` 每次都 new，所以只能靠"同一对象 show 两次"把这条路走通 ——
    这也正是它与"重置(流程)"的分工：流程那条测行为，这条测那行代码本身。

    ## 断言为什么不会空过

    1. 先断言**前置真的成立**：写进旧戳后 ``_deactivate_at`` 确实非零（否则等于没设）；
    2. 再断言 re-show 后 ``== 0.0``；
    3. 若不为 0，还要看 ``DEACT_SPY``：有真实失活来过 → SKIP（环境），没有 → FAIL。
       ``--mutant-reset`` 下这里必须是 FAIL，否则探针失效。
    """
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check(RESET_REUSE_LABEL, False, "controller.panel is None")
        return
    pump(app, 0.25)                     # 让它稳定，别在淡入动画中途折腾
    try:
        DEACT_SPY.reset(pw)
        try:
            pw._watch.stop()            # noqa: SLF001 - 隔离轮询，只测 showEvent
        except Exception:               # noqa: BLE001 - 策略变了也不该让用例崩
            pass
        pw.hide()
        pump(app, 0.05)

        stale = time.monotonic() - 10.0
        pw._deactivate_at = stale       # noqa: SLF001 - 伪造"上一轮残留的旧戳"
        set_ok = abs(deactivate_at(pw) - stale) < 1e-6

        DEACT_SPY.count = 0
        pw.show()                       # ← 同一个对象第二次 show
        app.processEvents()
        app.processEvents()
        after = deactivate_at(pw)
        fresh_deact = DEACT_SPY.count
    finally:
        pass

    detail = (f"旧戳={stale:.3f} 写入成功={set_ok}；re-show 后={after:.3f}"
              f"（期望 0.0）；re-show 期间真实失活={fresh_deact} 次")
    if not set_ok:
        check(RESET_REUSE_LABEL, False,
              f"前置不成立：旧戳没写进去（读到 {deactivate_at(pw):.3f}），用例无效")
    elif after == 0.0:
        check(RESET_REUSE_LABEL, True, detail)
    elif fresh_deact > 0:
        skip(RESET_REUSE_LABEL,
             f"环境限制：re-show 期间来了 {fresh_deact} 次真实失活，戳被重新打上，"
             f"无法判定是重置还是新事件；{detail}")
    else:
        check(RESET_REUSE_LABEL, False,
              f"showEvent 没有清空 _deactivate_at（旧戳泄漏）。{detail}")

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


def install_mutant_reset_patch() -> None:
    """把 ``showEvent`` 里的重置"取消"，验证 ``case_reset_reuse`` 会红。

    ## 为什么是"包住整个 showEvent"而不是只 patch 赋值

    只 patch ``self._deactivate_at = 0.0`` 这一句，``_shown_at`` 也不会更新，
    用例就可能因为"宽限起点没更新"而不是"重置缺失"变红 —— 鉴别力不纯。
    这里的写法：先记下 show 之前的值，照常调 ``orig_show``（``_shown_at`` 等
    全部按产品逻辑更新），然后把旧值**写回去** —— 唯一被抹掉的就是那行重置。

    用法：``qa_probe_grace_boundary.py --mutant-reset``。
    ``case_reset_reuse`` 此时**必须红**；①②③④ 与"重置(流程)"应仍绿
    （证明这个 mutant 是外科手术式的）。
    """
    from app.windows import panel_window as PW

    orig_show = PW.PanelWindow.showEvent

    def show_event(self, event) -> None:            # noqa: ANN001
        stale = float(getattr(self, "_deactivate_at", 0.0) or 0.0)
        orig_show(self, event)
        self._deactivate_at = stale                 # ← 模拟"重置那行被删掉"

    PW.PanelWindow.showEvent = show_event           # type: ignore[method-assign]


def main() -> int:
    argv = sys.argv[1:]
    mutant_reset = "--mutant-reset" in argv
    if "--mutant" in argv:
        install_mutant_patch()
        print("=" * 70)
        print("!!! mutant 模式：判定基准退回「检查时刻」—— 本次运行**必须红**")
        print("=" * 70)
    if mutant_reset:
        install_mutant_reset_patch()
        print("=" * 70)
        print("!!! mutant-reset 模式：showEvent 不再重置 _deactivate_at")
        print("!!! 期望：重置(复用) 红；①～④ 与 重置(流程) 仍绿")
        print("=" * 70)

    seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    install_deact_spy()
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
    case_reset_reuse(app, controller)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，"
          f"失败 {len(failed)} 项，环境跳过 {len(SKIPS)} 项")
    if failed:
        print("失败项：")
        for n in failed:
            print(f"  - {n}")
    if SKIPS:
        print("跳过项（不计入通过数）：")
        for n, why in SKIPS:
            print(f"  - {n}  ({why})")

    controller.shutdown()
    root = sandbox.sandbox_root()
    if root:
        shutil.rmtree(root, ignore_errors=True)

    if mutant_reset:
        # 自证：只有"重置(复用)"红，才算这个探针有鉴别力
        reuse_red = any(n == RESET_REUSE_LABEL for n in failed)
        others_red = [n for n in failed if n != RESET_REUSE_LABEL]
        print("\n" + "=" * 70)
        if reuse_red and not others_red:
            print("[SELF-CHECK] 通过：mutant-reset 下「重置(复用)」红，其余全绿"
                  " → 这条断言有鉴别力")
        elif reuse_red:
            print(f"[SELF-CHECK] 部分通过：「重置(复用)」红，但另有 {len(others_red)} 条也红"
                  f"（{others_red}）→ mutant 不够外科，需查")
        else:
            print("[SELF-CHECK] 失效：「重置(复用)」没红 → 这条断言抓不到重置缺失，"
                  "不能当护栏用")
        print("=" * 70)
        return 0 if reuse_red else 2

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
