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
"精确构造"，结果可重复。

## 六条用例

| 用例 | 场景 | 正确行为 |
|---|---|---|
| ① / ② / ③ | 失活落在宽限内（100 / 200 / 280ms） | 作废，面板存活 |
| ④ | 失活在宽限之后（420ms） | 判死，面板关闭且 reason=deactivate |
| 流程(连坐) | 连开两块面板 | 第二块不被第一块连坐、拿到干净的宽限起点 |
| 重置(复用) | **同一对象** show 两次 | ``showEvent`` 必须把 ``_deactivate_at`` 清回 0.0 |

## 两条"重置/流程"用例各管什么（踩过的坑，别混）

- **流程(连坐)** 测的是**面板生命周期**：连开两块、第二块单独拿到干净宽限起点。
  它**对 ``showEvent`` 的重置行不敏感**（``--mutant-reset`` 下仍绿）——
  因为 ``controller.open_panel()`` 每次都 ``new PanelWindow``，``__init__`` 里
  ``_deactivate_at`` 就是 0.0。这条曾经标成"重置(流程)"，是**命名层面的 over-claim**：
  名字声称覆盖重置，实际覆盖的是流程。改名就是为这个。
- **重置(复用)** 才是那条护栏：同一对象 show 两次，第二次前人为把 ``_deactivate_at``
  置成非零旧戳，断言被清回 ``0.0``。``showEvent`` 重置行的产品侧覆盖在
  ``test_show_resets_deactivate_at``，本文件提供行为侧的第二道。

## 注入点漂移（本探针最容易误判的地方）

``open_panel`` 之后到第一次 pump 之间，**面板首帧绘制（毛玻璃 ``grabWindow`` +
首次字体/管线初始化）能把主线程堵约 250ms**。实测：① 作为第一条用例时，
目标注入 100ms，实际漂到 **358ms** —— 场景根本没落在"宽限内"，用例却按
``expect_alive=True`` 断言存活，于是把**产品的正确行为判成了红**。

三条对策：

1. **预热**：正式用例之前先开一块面板、``pump(0.35)``、关掉，把首帧开销花在计时窗口之外；
2. **按实测时刻判窗口归属**，不按 ``fire_at_ms`` 判；``expect_alive=True`` 却实测
   ``>= GRACE_MS - 20`` → 这一轮**作废重试**（最多 3 次）；
3. 3 次仍漂 → ``skip()``，理由写明实测漂到多少，**不记 PASS 也不记 FAIL**。

判"漂"的判据是**落错侧**（``expect_alive`` 却实测 ``>= GRACE_MS``）**或严重偏离目标**
（``> fire_at +100ms``），**不是**"距边界不足 20ms"：③ 的目标就是 280ms，用 20ms 余量
会让它永远判漂、永远 SKIP —— 等于为了修 ① 把最贴边界的用例删掉。实测依据：那样跑
③ 三次实测 282ms 全被判漂。

## 死对象守卫（两道，各自都要能单独审）

面板关闭后 ``deleteLater`` 会让 C++ 对象销毁。此时 ``_deactivate_at`` 是**纯 Python
属性**，shiboken 的存活检查只挂在 C++ 调用上 —— 所以 ``getattr`` **不抛、返回真值**，
前置判据全部"通过"，然后在 ``QApplication.sendEvent`` 才炸。崩掉是零计数、零汇总行，
在 CI 里和"环境炸了"长得一样，会被当噪音重跑掉。

- **(d) 守卫**：``controller.panel is pw`` —— 不碰 C++、不抛、也不会在死对象上给假值。
  **不用 ``isVisible()``**，那正是 C++ 调用。
- **(e) 兜底**：``sendEvent`` 外面 ``try/except RuntimeError``，兜住之后判 **FAIL**
  （判词写明"面板对象已销毁，场景未成立"），**不许判 SKIP** —— 那是产品没把生命周期
  管干净才会走到这里。

### ``caught_by`` 只有两个值：``gate`` / ``except``

原来的三值方案里还有个 ``guard``（"引用被换成另一块面板"），**实测后取消**：
``controller.panel`` 的取值域是 ``{None, 当前那块 pw}`` —— ``app/controller.py`` 里
只有 ``318: = pw`` / ``346: = None``（``close_panel`` 先置 None 再 close）/ ``374: = None``
三处赋值，没有一处赋成第三块面板。所以 ``guard``（``is not pw``）与 ``gate``（``is None``）
在产品行为上等价，已由 ``gate`` 吸收。**不为一个产品里走不到的形态造 mutant** ——
用走不到的路径冒充覆盖，比没有覆盖更糟，它会让人相信有一道防线在守。

### 一条诚实的限制：闸与 ``sendEvent`` 之间有微小时窗

闸通过之后、``sendEvent`` 之前，对象仍可能被销毁。**该时窗没有隔离用例**，
``except`` 只作兜底，**不作为已验证的覆盖**（§11.4.1：宁写"没验证"，不假装验证过）。
``--mutant-deadobj`` 造的是"闸之前就已销毁"的形态，证明 ``except`` 这条代码路径可达，
但不等于证明了那个微小时窗。

## 自证矩阵（探针必须能红，且**只红该红的那条**）

| 模式 | ①②③ | ④ | 流程(连坐) | 重置(复用) |
|---|---|---|---|---|
| 正常 | 绿 | 绿 | 绿 | 绿 |
| ``--mutant``（判据退回检查时刻） | 绿※ | 绿 | **红**（首轮闸） | 绿 |
| ``--mutant-reset``（showEvent 不重置） | 绿 | 绿 | 绿 | **红** |
| ``--mutant-collateral``（静置期 ``close_panel`` 掉第二块） | 绿 | 绿 | **红**(caught_by=gate) | 绿 |
| ``--mutant-deadobj``（只 ``deleteLater``：wrapper 活、C++ 已死） | 绿 | 绿 | **红**(caught_by=except) | 绿 |

※ ``--mutant`` 下 ① 是绿的，**这是对的**：mutant 只在"检查时刻"越过 300ms 时才发作，
① 实测注入 ~105ms、检查 ~225ms，还没越过边界。误判阈值是 ``event >= 180ms``，
① 低于阈值，按理论不该红。②③ 与流程(连坐) 的注入都在 180ms 以上，所以红。

后两行是两道死对象防线的**隔离自证**：按 §11.4.1，每道防线都要有"只放行它"的用例，
否则一道失效会被另一道替它扛，看起来全绿其实早废了。

用法：
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py --mutant
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py --mutant-reset
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py --mutant-collateral
    .venv\\Scripts\\python.exe tests\\qa_probe_grace_boundary.py --mutant-deadobj
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

from PySide6.QtCore import QEvent, QObject, QTimer  # noqa: E402
from PySide6.QtWidgets import QApplication, QWidget  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402
from app.controller import AppController  # noqa: E402
from app.windows.panel_window import (  # noqa: E402
    CLOSE_DELAY_MS,
    REASON_DEACTIVATE,
)

GRACE_MS = 300                     # panel_watch.GRACE_MS
GROSS_DRIFT_MS = 100               # 实测事件时刻偏离目标超过这个量 → 判漂、重试
MAX_ATTEMPTS = 3                   # 漂移重试次数上限
FAULT_DELAY_MS = 100               # 故障注入：静置期第几毫秒动手

RESULTS: list[tuple[str, bool, str]] = []
SKIPS: list[tuple[str, str]] = []

FLOW_LABEL = "流程(连坐) 连开两块面板：第二块不被第一块连坐、拿到干净的宽限起点"
REUSE_LABEL = "重置(复用) 同一对象 show 两次：showEvent 必须把 _deactivate_at 清回 0.0"

FAULT_MODE: str | None = None      # None | "collateral" | "deadobj"


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
    """产品记录的事件时刻戳。

    ⚠️ 这是**纯 Python 属性**，对象销毁后 ``getattr`` 不抛、照样返回旧值 ——
    所以它**不能**用来判"面板是否还活着"（见 ``gate_alive``）。
    """
    return float(getattr(pw, "_deactivate_at", 0.0) or 0.0)


def safe_reason(pw) -> str:                     # noqa: ANN001
    """``close_reason`` 的存活安全读取（对象已删时返回空串）。"""
    try:
        return str(getattr(pw, "close_reason", "") or "")
    except RuntimeError:
        return ""


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


# --------------------------------------------------------------------------
# 死对象的两道防线（§11.4.1：每道都要有只放行它的隔离用例）
# --------------------------------------------------------------------------

def gate_alive(name: str, controller, pw, elapsed_ms: float, hint: str = "") -> bool:
    """**(d) 早死闸**：注入之前判面板是否还活着。返回 True 表示可以继续。

    判据用 ``controller.panel is pw`` —— 身份比较，不碰 C++、不抛；
    ``close_panel()`` 会把 ``controller.panel`` 置 None，所以面板一关，这里立刻知道。
    **不用 ``isVisible()`` / ``deactivate_at()``**：前者是 C++ 调用会抛，后者在死对象上
    返回真值（纯 Python 属性），都会把"已经死了"误判成"还活着"。
    """
    if controller.panel is pw:
        return True
    reason = safe_reason(pw) or "（对象已删除，读不到 close_reason）"
    check(name, False,
          f"面板在 {elapsed_ms:.0f}ms 就被关闭 reason={reason}"
          f" caught_by=gate {hint}".rstrip())
    return False


def send_deactivate(pw) -> bool:                # noqa: ANN001
    """**(e) 注入兜底**：``sendEvent`` 外面接住死对象异常。返回 False = 对象已销毁。

    兜住之后由调用方判 **FAIL**（不是 SKIP）：走到这里说明生命周期没管干净，
    那是产品侧的问题，不该记成环境。
    """
    try:
        QApplication.sendEvent(pw, QEvent(QEvent.Type.WindowDeactivate))
        return True
    except RuntimeError:                        # libshiboken: object already deleted
        return False


def forget_dead_panel(controller) -> None:      # noqa: ANN001
    """**仅供故障注入路径**：抹掉 controller 手里那个已销毁的引用。

    产品自身的取值域是 ``{None, 当前 pw}``（``close_panel`` 先置 None 再 close），
    不会出现"指向已删对象"的状态。``--mutant-deadobj`` 是人为 ``deleteLater`` 而不走
    ``close_panel``，才造出这种 **产品里不可能存在**的脏状态；不收拾的话，下一轮
    ``open_panel()`` 里的 ``close_panel()`` 会对着死对象调 ``close()``，整支探针
    就带 traceback 退出、**没有汇总行** —— 那正是我们自己定的 FAIL 形态。
    """
    try:
        controller.panel = None
        controller.active_cat_id = None
    except Exception:                           # noqa: BLE001 - 兜底不该二次抛
        pass


def _safe_close(controller) -> None:             # noqa: ANN001
    try:
        controller.close_panel()
    except RuntimeError:
        pass


def _safe_delete(pw) -> None:                   # noqa: ANN001
    """只 ``deleteLater``，**不动 controller.panel** —— 这是关键区别。"""
    try:
        pw.deleteLater()
    except RuntimeError:
        pass


def arm_fault(controller, pw) -> None:          # noqa: ANN001
    """按当前 mutant 模式，在静置期注入故障。两种形态**分别对应两道防线**：

    - ``collateral``：``close_panel()`` → ``controller.panel`` 变 ``None``
      → **早死闸（gate）**接住。这就是"第二块被第一块连坐"的产品形态。
    - ``deadobj``：只 ``deleteLater()`` → ``controller.panel`` **仍是那个 pw**
      （``is pw`` 为真）→ 闸门**自然放行** → ``sendEvent`` 抛 ``RuntimeError``
      → **except** 接住。这是"wrapper 还活着、C++ 已销毁"的形态。

    两种形态**不 patch 产品、也不 patch 闸门**，所以能单独审。
    """
    if FAULT_MODE is None:
        return
    if FAULT_MODE == "collateral":
        QTimer.singleShot(FAULT_DELAY_MS, lambda: _safe_close(controller))
    elif FAULT_MODE == "deadobj":
        QTimer.singleShot(FAULT_DELAY_MS, lambda: _safe_delete(pw))


# --------------------------------------------------------------------------
# 用例
# --------------------------------------------------------------------------

def warmup(app, controller) -> None:
    """预热：把"面板首帧绘制"那约 250ms 花在不计时的面板上。

    实测教训：① 作为第一条用例时，目标注入 100ms 实际漂到 **358ms**（溢出 258ms），
    场景跑到宽限之外，用例却仍按"宽限内必须存活"断言 → 把产品的**正确行为**判成红。
    预热不记用例。
    """
    controller.open_panel("c0")
    pump(app, 0.35)
    controller.close_panel()
    pump(app, 0.25)


def case(app, controller, label: str, fire_at_ms: float, expect_alive: bool) -> None:
    """在 ``fire_at_ms`` 附近制造一次失活，按**实测**时刻判窗口归属。"""
    last = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        outcome = _case_once(app, controller, label, fire_at_ms, expect_alive, attempt)
        if outcome is None:
            return
        last = outcome
    want = "宽限内" if expect_alive else "宽限外"
    skip(f"{label} 失活@{fire_at_ms:.0f}ms（{want}）",
         f"注入点漂出目标窗口（{MAX_ATTEMPTS} 次实测均未落在{want}：{last}）；"
         "本机这一轮没跑到该窗口 —— 不记通过，也不记失败")


def _case_once(app, controller, label, fire_at_ms, expect_alive, attempt):  # noqa: ANN001
    """跑一轮；返回 ``None`` = 已有结论，返回字符串 = 漂移的实测值（需重试）。"""
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check(f"{label} open_panel 产出面板", False, "controller.panel is None")
        return None
    t0 = shown_at(pw)
    stealer = Stealer()
    try:
        actual_ms = pump_until(app, fire_at_ms / 1000.0, t0)
        if not gate_alive(f"{label} 注入前面板仍在（目标 {fire_at_ms:.0f}ms）",
                          controller, pw, actual_ms):
            return None
        before = deactivate_at(pw)
        stealer.steal()
        if not send_deactivate(pw):
            check(f"{label} 注入成功（目标 {fire_at_ms:.0f}ms）", False,
                  "面板对象已销毁，场景未成立（疑似上一轮连坐），本条按 FAIL 记"
                  " caught_by=except")
            forget_dead_panel(controller)
            return None
        pump(app, 0.03)
        fired = deactivate_at(pw)
        qt_still_panel = QApplication.activeWindow() is pw
        pump(app, (GRACE_MS + CLOSE_DELAY_MS) / 1000.0)
    finally:
        stealer.close()

    alive = controller.panel is not None
    reason = safe_reason(pw)
    event_ms = (fired - t0) * 1000.0 if fired else -1.0

    # —— 漂移检测：先判"这一轮到底落在哪个窗口"，落错了就作废重试 ——
    # 判据是"落错侧"或"严重偏离目标"，**不是**"距宽限边界不足 20ms"：
    # ③ 的目标就是 280ms（刻意贴着边界），用 300-20=280 当阈值会让它永远判漂、
    # 永远 SKIP —— 那等于为了修 ① 的漂移，把最贴边界的用例删掉。
    # 实测依据：改成这条规则前，③ 三次实测 282ms 全部被判漂（见 08eaa84 之后的运行记录）。
    wrong_side = (expect_alive and event_ms >= GRACE_MS) or \
                 ((not expect_alive) and event_ms < GRACE_MS)
    gross_drift = event_ms > fire_at_ms + GROSS_DRIFT_MS
    if wrong_side or gross_drift:
        _cleanup(controller, app)
        why = "落错侧" if wrong_side else f"偏离目标>={GROSS_DRIFT_MS}ms"
        return f"{event_ms:.0f}ms（{why}，目标 {fire_at_ms:.0f}ms，宽限 {GRACE_MS}ms）"

    window = "宽限内" if event_ms < GRACE_MS else "宽限外"
    tail = "作废，面板存活" if expect_alive else "判死，reason=deactivate"
    name = f"{label} 失活@{event_ms:.0f}ms（{window}）→ {tail}"
    detail = (f"注入点 {actual_ms:.0f}ms（第 {attempt} 次）；"
              f"+{CLOSE_DELAY_MS}ms 复核 → {event_ms + CLOSE_DELAY_MS:.0f}ms "
              f"vs 宽限 {GRACE_MS}ms；复核时 activeWindow 仍是面板={qt_still_panel}；"
              f"panel={'存活' if alive else 'None(已关)'} reason={reason!r}")

    if fired <= before:
        skip(name, f"无效：注入点没有产生新的失活事件（戳未变化 {before:.3f}→{fired:.3f}）")
    elif expect_alive:
        check(name, alive, detail)
    elif qt_still_panel:
        skip(name, "无效：复核时 activeWindow 仍是面板，产品按设计就不该关，断言不成立")
    else:
        check(name, (not alive) and reason == REASON_DEACTIVATE, detail)

    _cleanup(controller, app)
    return None


def _cleanup(controller, app) -> None:          # noqa: ANN001
    # close_panel() 先把 self.panel 置 None 再 close()，所以即使对象已销毁，
    # 引用也已经被清掉；这里只兜住 close() 抛的 RuntimeError，别让它炸掉整支探针。
    try:
        if controller.panel is not None:
            controller.close_panel()
    except RuntimeError:
        pass
    pump(app, 0.35)


def case_flow(app, controller) -> None:
    """**流程(连坐)**：连开两块面板，第二块不被第一块连坐、且拿到干净宽限起点。

    ## 它对 ``showEvent`` 的重置行**不敏感**（别给它不该有的信用）

    ``controller.open_panel()`` 每次都 ``new PanelWindow``，``__init__`` 里
    ``_deactivate_at`` 就是 0.0，所以"第二块继承第一块旧戳"这条路走不到 ——
    ``--mutant-reset`` 下本条**仍然绿**。重置行由 ``重置(复用)`` 与产品单测
    ``test_show_resets_deactivate_at`` 守。

    本条真正覆盖的是**生命周期**：第二块在静置期被关掉（连坐）时，必须报 **FAIL**，
    不许落成 SKIP。自证见 ``--mutant-deadobj``（caught_by=gate）。
    """
    # 第一轮：宽限内失活（应被作废，面板存活）
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("流程(连坐) open_panel 产出面板", False, "controller.panel is None")
        return
    t0 = shown_at(pw)
    st = Stealer()
    try:
        elapsed = pump_until(app, 0.20, t0)
        if not gate_alive("流程(连坐) 首轮注入前面板仍在（200ms 窗口）",
                          controller, pw, elapsed):
            return
        before = deactivate_at(pw)
        st.steal()
        if not send_deactivate(pw):
            check("流程(连坐) 首轮注入成功", False,
                  "面板对象已销毁，场景未成立，本条按 FAIL 记 caught_by=except")
            forget_dead_panel(controller)
            return
        pump(app, 0.03)
        first_fired = deactivate_at(pw) > before
        pump(app, 0.30)
    finally:
        st.close()
    first_alive = controller.panel is not None
    _cleanup(controller, app)

    # 第二轮：宽限之后失活（必须判死）。--mutant-deadobj 在这里注入"连坐"
    controller.open_panel("c0")
    pw2 = controller.panel
    if pw2 is None:
        check("流程(连坐) 第二个面板产出", False, "controller.panel is None")
        return
    t1 = shown_at(pw2)
    arm_fault(controller, pw2)
    st2 = Stealer()
    try:
        elapsed2 = pump_until(app, 0.45, t1)
        if not gate_alive("流程(连坐) 次轮注入前面板仍在（450ms 窗口）",
                          controller, pw2, elapsed2, "疑似被首轮连坐"):
            return
        before2 = deactivate_at(pw2)
        st2.steal()
        if not send_deactivate(pw2):
            check("流程(连坐) 次轮注入成功", False,
                  "面板对象已销毁，场景未成立（疑似上一轮连坐），本条按 FAIL 记"
                  " caught_by=except")
            forget_dead_panel(controller)
            return
        pump(app, 0.03)
        second_fired = deactivate_at(pw2) > before2
        qt_still_panel = QApplication.activeWindow() is pw2
        pump(app, 0.35)
    finally:
        st2.close()
    second_closed = controller.panel is None
    reason = safe_reason(pw2)

    if not (first_fired and second_fired):
        skip(FLOW_LABEL,
             f"无效：失活事件没到达（first={first_fired} second={second_fired}）—— "
             "注意这一条不是 FAIL：事件没到就没有判定依据，已在上面说明")
    elif qt_still_panel:
        skip(FLOW_LABEL, "无效：第二轮复核时 activeWindow 仍是面板，产品按设计就不该关")
    else:
        check(FLOW_LABEL,
              first_alive and second_closed and reason == REASON_DEACTIVATE,
              f"首轮存活={first_alive}；次轮关闭={second_closed} reason={reason!r}")
    _cleanup(controller, app)


def case_reset_reuse(app, controller) -> None:
    """**重置（复用）**：同一个对象 show 两次，``showEvent`` 必须把旧戳清回 0.0。

    ## 为什么要单独立这一条

    判据是 ``(_deactivate_at - _shown_at)``。若对象被复用而旧戳没清：
    旧戳早于本次 show → 差值为**负** → 永远落进宽限 → **该关的不关**；
    跨轮次时间差过大 → 永远落出宽限 → **该作废的判死**。两个方向都不可预测。

    ## 断言为什么不会空过

    1. 先断言前置成立：写进旧戳后确实读到非零（否则等于没设）；
    2. 再断言 re-show 后 ``== 0.0``；
    3. 若不为 0，看 ``DEACT_SPY``：有真实失活来过 → SKIP（环境），没有 → FAIL。
       ``--mutant-reset`` 下这里必须 FAIL。
    """
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check(REUSE_LABEL, False, "controller.panel is None")
        return
    pump(app, 0.25)
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

    detail = (f"旧戳={stale:.3f} 写入成功={set_ok}；re-show 后={after:.3f}"
              f"（期望 0.0）；re-show 期间真实失活={fresh_deact} 次")
    if not set_ok:
        check(REUSE_LABEL, False,
              f"前置不成立：旧戳没写进去（读到 {deactivate_at(pw):.3f}），用例无效")
    elif after == 0.0:
        check(REUSE_LABEL, True, detail)
    elif fresh_deact > 0:
        skip(REUSE_LABEL,
             f"环境限制：re-show 期间来了 {fresh_deact} 次真实失活，戳被重新打上，"
             f"无法判定是重置还是新事件；{detail}")
    else:
        check(REUSE_LABEL, False, f"showEvent 没有清空 _deactivate_at（旧戳泄漏）。{detail}")

    _cleanup(controller, app)


# --------------------------------------------------------------------------
# 自证用的 mutant
# --------------------------------------------------------------------------

def install_mutant_patch() -> None:
    """把判定基准退回"检查时刻"。

    这个 mutant 就是产品原本那个缺陷（用 ``time.monotonic() - _shown_at`` 判宽限），
    只是被挪到 monkeypatch 里、不碰 ``app/`` 源码。此时 ①②③ + 流程(连坐) **必须红**。
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
    """把 ``showEvent`` 里的重置"取消"，验证 ``重置(复用)`` 会红。

    做法：记下 show 前的值 → 照常调 ``orig_show``（``_shown_at`` 等都按产品逻辑更新）
    → 把旧值**写回去**。唯一被抹掉的就是那行重置。

    不采用"只 patch 赋值"：那样 ``_shown_at`` 也不会更新，用例可能因为"宽限起点没更新"
    而不是"重置缺失"变红，鉴别力不纯。
    """
    from app.windows import panel_window as PW

    orig_show = PW.PanelWindow.showEvent

    def show_event(self, event) -> None:            # noqa: ANN001
        stale = float(getattr(self, "_deactivate_at", 0.0) or 0.0)
        orig_show(self, event)
        self._deactivate_at = stale                 # ← 模拟"重置那行被删掉"

    PW.PanelWindow.showEvent = show_event           # type: ignore[method-assign]


def install_fault_mode(mode: str) -> None:
    """把故障注入模式挂上（**不 patch 产品、不 patch 闸门**）。

    - ``collateral`` → 产出的形态是"面板被关掉"，由 **早死闸（gate）** 接住；
    - ``deadobj``    → 产出的形态是"wrapper 活、C++ 已销毁"，闸门自然放行，
      由 **sendEvent 的 except** 接住。

    ## 为什么没有 ``guard`` 这个值

    ``controller.panel`` 的取值域是 ``{None, 当前那块 pw}``：``app/controller.py``
    里只有 ``318: = pw`` / ``346: = None``（close_panel 先置 None 再 close）/
    ``374: = None`` 三处赋值，**没有一处赋成第三块面板**。所以"引用被换成另一个对象"
    在产品里走不到，``guard``（``is not pw``）与 ``gate``（``is None``）等价，
    已由 ``gate`` 吸收 —— 不为一个走不到的形态造 mutant（那是"用走不到的路径冒充覆盖"）。
    """
    global FAULT_MODE
    FAULT_MODE = mode


# --------------------------------------------------------------------------
# main
# --------------------------------------------------------------------------

def _by_label(label: str) -> list[tuple[bool, str]]:
    return [(ok, d) for n, ok, d in RESULTS if n.startswith(label)]


def main() -> int:
    argv = sys.argv[1:]
    mutant = "--mutant" in argv
    mutant_reset = "--mutant-reset" in argv
    mutant_collateral = "--mutant-collateral" in argv
    mutant_deadobj = "--mutant-deadobj" in argv

    if mutant:
        install_mutant_patch()
        print("=" * 70)
        print("!!! mutant：判定基准退回「检查时刻」—— ①②③ + 流程(连坐) 必须红")
        print("=" * 70)
    if mutant_reset:
        install_mutant_reset_patch()
        print("=" * 70)
        print("!!! mutant-reset：showEvent 不再重置 _deactivate_at —— 只有 重置(复用) 该红")
        print("=" * 70)
    if mutant_collateral:
        install_fault_mode("collateral")
        print("=" * 70)
        print(f"!!! mutant-collateral：静置期第 {FAULT_DELAY_MS}ms close_panel() 掉第二块"
              " —— 流程(连坐) 必须 FAIL 且 caught_by=gate")
        print("=" * 70)
    if mutant_deadobj:
        install_fault_mode("deadobj")
        print("=" * 70)
        print(f"!!! mutant-deadobj：静置期第 {FAULT_DELAY_MS}ms 只 deleteLater（不动 controller.panel）"
              " —— 必须 FAIL 且 caught_by=except（有汇总行、不崩）")
        print("=" * 70)

    seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    install_deact_spy()
    controller = AppController(app)
    controller.start()
    pump(app, 1.0)

    print(f"宽限 {GRACE_MS}ms，复核延迟 {CLOSE_DELAY_MS}ms → "
          f"误判阈值 deactivate_at >= {GRACE_MS - CLOSE_DELAY_MS}ms；"
          f"漂移判据：落错侧 或 偏离目标>={GROSS_DRIFT_MS}ms\n")

    warmup(app, controller)          # 首帧绘制开销不落在计时窗口里
    case(app, controller, "① ", 100, expect_alive=True)
    case(app, controller, "② ", 200, expect_alive=True)
    case(app, controller, "③ ", 280, expect_alive=True)
    case(app, controller, "④ ", 420, expect_alive=False)
    case_flow(app, controller)
    case_reset_reuse(app, controller)

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS) + len(SKIPS)} 项，通过 {len(RESULTS) - len(failed)} 项，"
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

    if mutant_reset or mutant_collateral or mutant_deadobj:
        print("\n" + "=" * 70)
        if mutant_reset:
            reuse_red = any(not ok for ok, _ in _by_label(REUSE_LABEL))
            others = [n for n in failed if not n.startswith(REUSE_LABEL)]
            if reuse_red and not others:
                print("[SELF-CHECK] 通过：只有「重置(复用)」红 → 这条断言有鉴别力")
            elif reuse_red:
                print(f"[SELF-CHECK] 部分通过：「重置(复用)」红，但另有 {len(others)} 条也红"
                      f"（{others}）→ mutant 不够外科，需查")
            else:
                print("[SELF-CHECK] 失效：「重置(复用)」没红 → 抓不到重置缺失")
            ok_self = reuse_red
        else:
            # 注意：这里要按**族前缀**匹配，不能按 FLOW_LABEL 全名 ——
            # 死对象防线的判词是"流程(连坐) 次轮注入前面板仍在（450ms 窗口）"，
            # 它不以 FLOW_LABEL 开头。按全名匹配会导致自证永远"失效"（假红）。
            details = [d for n, ok, d in RESULTS
                       if n.startswith("流程(连坐)") and not ok]
            want = "caught_by=gate" if mutant_collateral else "caught_by=except"
            hit = [d for d in details if want in d]
            if hit:
                print(f"[SELF-CHECK] 通过：流程(连坐) 报 FAIL，且 {want} 命中 → "
                      "该防线单独有效（有汇总行、没崩）")
            else:
                print(f"[SELF-CHECK] 失效：流程(连坐) 没报出 {want} "
                      f"（details={details or '无失败项，可能被 SKIP 吞了'}）")
            ok_self = bool(hit)
        print("=" * 70)
        return 0 if ok_self else 2

    if mutant:
        print("\n" + "=" * 70)
        print(f"[SELF-CHECK] mutant 下失败 {len(failed)} 项"
              + ("（预期：②③ + 流程(连坐)；① 实测 ~105ms < 180ms 误判阈值，"
                 "按理论**不该**红）" if failed else "—— 全绿说明探针失效"))
        print("=" * 70)
        return 0 if failed else 2

    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
