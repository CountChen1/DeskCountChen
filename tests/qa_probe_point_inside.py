"""【v1.5 补充验收】``_point_inside()`` 边界判定 / DPI 一致性压测。

## 为什么专压这一点（工程师主动提示的薄弱点）

``OutsideClickWatcher._point_inside()`` 的兜底语义是"判定不了就按**在面板内**
处理（宁可不关，绝不误关）"。代价是：**一旦坐标系算错**（物理像素 vs Qt 逻辑
坐标、DPI 非 100%、多屏负坐标），它会把"面板外"误判成"面板内"，
症状就是 **"点桌面关不掉"** —— 与 v1.4 的 P0-1 缺陷**表现完全一样**，但
根因不同，排查成本极高。

本机实测 **DPI = 125%（dpr=1.25，逻辑 1536×864）**，正好是最容易踩坑的档位。

## 判定链条（两个坐标必须同一个空间）

| 环节 | 取值 | 坐标系 |
|---|---|---|
| 面板矩形 | ``PanelWindow._panel_rect()`` → ``frameGeometry()`` | 全局 **逻辑** |
| 光标位置 | ``SystemInputSource.cursor_pos()`` → ``QCursor.pos()`` | 全局 **逻辑** |

只要两边都是逻辑坐标，DPI 就不是问题；任一边被换成物理像素（例如误用
``GetCursorPos`` 而不做换算），在 125% 下就会偏 25%，边界附近必然误判。

## 用例

- **H1** 坐标往返精度：逻辑点 → ``SetCursorPos(物理)`` → ``QCursor.pos()``
  必须回到原逻辑点（±1px）。若这条不成立，后面全部无意义。
- **H2** 边界格点判定：沿面板矩形采样（中心 / 四边 ±1,2,3px / 四角），
  逐点断言 ``_point_inside() == rect.contains(QCursor.pos())``。
- **H3** 真实端到端边界：点**右边缘外 2px** 必须关闭（这是"关不掉"最易暴露
  的位置）；点**右边缘内 2px** 必须不关。
- **H4** 降级语义：输入源取光标抛异常 / 矩形非法 → 必须返回 True（宁可不关），
  这是**设计如此**，断言它别被"顺手改坏"。

用法：``.venv\\Scripts\\python.exe tests\\qa_probe_point_inside.py``
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

from PySide6.QtCore import QPoint, QRect  # noqa: E402
from PySide6.QtGui import QCursor, QGuiApplication  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402
from app.controller import AppController  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

user32 = ctypes.WinDLL("user32", use_last_error=True)
user32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
user32.GetCursorPos.argtypes = [ctypes.POINTER(wintypes.POINT)]
user32.mouse_event.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.DWORD,
                               wintypes.DWORD, ctypes.c_size_t]
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.01)


def to_phys(x: float, y: float) -> tuple[int, int]:
    """Qt 逻辑坐标 → 物理像素（DPI 125% 下必须换算）。"""
    screen = QGuiApplication.primaryScreen()
    dpr = float(screen.devicePixelRatio()) or 1.0
    geo = screen.geometry()
    return int(round(geo.x() * dpr + x * dpr)), int(round(geo.y() * dpr + y * dpr))


def click_at(x: int, y: int, hold: float = 0.08) -> threading.Thread:
    def _run() -> None:
        user32.SetCursorPos(int(x), int(y))
        time.sleep(0.05)
        user32.mouse_event(MOUSEEVENTF_LEFTDOWN, 0, 0, 0, 0)
        time.sleep(hold)
        user32.mouse_event(MOUSEEVENTF_LEFTUP, 0, 0, 0, 0)

    t = threading.Thread(target=_run, daemon=True)
    t.start()
    return t


def seed() -> None:
    root = sandbox.sandbox_paths(prefix="dbx_point_inside_")
    paths.ensure_base_dirs()
    d = paths.DATA_ROOT / "甲"
    d.mkdir(parents=True, exist_ok=True)
    for n in range(10):
        (d / f"e{n}.txt").write_text("x")
    paths.CONFIG_PATH.write_text(json.dumps({
        "version": 1,
        "app": {"data_root": str(paths.DATA_ROOT)},
        "categories": [{"id": "c0", "dir": "甲", "label": "甲",
                        "panel_w": 420, "panel_h": 300, "material": "solid"}],
    }, ensure_ascii=False, indent=2), encoding="utf-8")
    print(f"沙箱：{root}")


def h1_roundtrip(app, rect: QRect) -> None:
    print("\n--- H1. 逻辑→物理→逻辑 坐标往返精度 ---")
    probes = [
        QPoint(rect.center()),
        QPoint(rect.left() + 1, rect.top() + 1),
        QPoint(rect.right() - 1, rect.bottom() - 1),
        QPoint(rect.right() + 2, rect.center().y()),
        QPoint(rect.left() - 2, rect.center().y()),
    ]
    saved = QCursor.pos()
    worst = 0
    bad: list[str] = []
    for pt in probes:
        px, py = to_phys(pt.x(), pt.y())
        user32.SetCursorPos(px, py)
        pump(app, 0.06)
        back = QCursor.pos()
        dx, dy = abs(back.x() - pt.x()), abs(back.y() - pt.y())
        worst = max(worst, dx, dy)
        if dx > 1 or dy > 1:
            bad.append(f"{pt.x()},{pt.y()} → {back.x()},{back.y()} (Δ{dx},{dy})")
    user32.SetCursorPos(saved.x(), saved.y())
    check("H1 逻辑点经 SetCursorPos 往返误差 ≤ 1px（DPI 换算自洽）", not bad,
          f"最大误差 {worst}px" + ("" if not bad else "；异常: " + "; ".join(bad)))


def h2_grid(app, pw, rect: QRect) -> None:
    print("\n--- H2. 边界格点：_point_inside() vs rect.contains(QCursor.pos()) ---")
    watch = pw._watch                                # noqa: SLF001 - 被测对象
    src = watch.source
    cx, cy = rect.center().x(), rect.center().y()
    pts: list[tuple[str, QPoint]] = [("中心", QPoint(cx, cy))]
    for d in (-3, -2, -1, 1, 2, 3):
        pts.append((f"左边{d:+d}", QPoint(rect.left() + d, cy)))
        pts.append((f"右边{d:+d}", QPoint(rect.right() + d, cy)))
        pts.append((f"上边{d:+d}", QPoint(cx, rect.top() + d)))
        pts.append((f"下边{d:+d}", QPoint(cx, rect.bottom() + d)))
    for dx in (-1, 1):
        for dy in (-1, 1):
            pts.append((f"角({dx},{dy})", QPoint(rect.right() + dx, rect.bottom() + dy)))

    saved = QCursor.pos()
    mismatch: list[str] = []
    for name, pt in pts:
        px, py = to_phys(pt.x(), pt.y())
        user32.SetCursorPos(px, py)
        pump(app, 0.05)
        actual = QCursor.pos()
        expect = bool(rect.contains(actual))
        got = bool(watch._point_inside())            # noqa: SLF001 - 被测函数
        if got != expect:
            mismatch.append(f"{name}@{pt.x()},{pt.y()} 实际光标({actual.x()},"
                            f"{actual.y()}) 期望inside={expect} 实测={got}")
        # 顺带核对 source.cursor_pos() 与 QCursor.pos() 是同一个值（同源）
        if src.cursor_pos() != actual:
            mismatch.append(f"{name}: source.cursor_pos()={src.cursor_pos()} "
                            f"≠ QCursor.pos()={actual}")
    user32.SetCursorPos(saved.x(), saved.y())
    check(f"H2 {len(pts)} 个边界格点全部判定正确（含 ±1px 紧贴边缘）",
          not mismatch,
          "全部一致" if not mismatch else f"{len(mismatch)} 处偏差: " + "; ".join(mismatch[:5]))


def h3_edge_click(app, controller, rect: QRect) -> None:
    print("\n--- H3. 真实端到端边界点击 ---")
    # H3a：右边缘**外** 2px 单击 → 必须关闭（"关不掉"最易暴露的位置）
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("H3 面板已打开", False)
        return
    pump(app, 1.0)
    rect = QRect(pw.frameGeometry())
    out = QPoint(rect.right() + 2, rect.center().y())
    px, py = to_phys(out.x(), out.y())
    saved = QCursor.pos()
    info(f"H3a 面板几何 {rect.x()},{rect.y()} {rect.width()}x{rect.height()}；"
         f"点击点 {out.x()},{out.y()}（边缘外 2px，逻辑）")
    click_at(px, py)
    pump(app, 1.1)
    closed = controller.panel is None
    check("H3a 面板右边缘外 2px 单击 → 面板关闭（边界未误判为“内”）", closed,
          f"panel={'None(已关)' if closed else '仍打开(疑似坐标误判)'} "
          f"reason={getattr(pw, 'close_reason', '')!r}")

    # H3b：右边缘**内** 2px 单击 → 必须不关
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("H3b 面板已打开", False)
        return
    pump(app, 1.0)
    rect = QRect(pw.frameGeometry())
    inside = QPoint(rect.right() - 2, rect.center().y())
    px, py = to_phys(inside.x(), inside.y())
    info(f"H3b 点击点 {inside.x()},{inside.y()}（边缘内 2px，逻辑）")
    click_at(px, py)
    pump(app, 1.1)
    kept = controller.panel is not None
    check("H3b 面板右边缘内 2px 单击 → 面板不关（不得误关）", kept,
          f"panel={'存活' if kept else 'None(被误关)'} "
          f"reason={getattr(pw, 'close_reason', '')!r}")
    user32.SetCursorPos(saved.x(), saved.y())
    if controller.panel is not None:
        controller.close_panel()
    pump(app, 0.4)


def h4_degrade(app, controller) -> None:
    print("\n--- H4. 降级语义：判定不了必须按“在面板内”处理 ---")
    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        check("H4 面板已打开", False)
        return
    pump(app, 0.8)
    watch = pw._watch                                # noqa: SLF001
    src = watch.source
    orig_cursor = src.cursor_pos
    orig_rect = watch._rect_provider                 # noqa: SLF001

    def _boom_cursor():
        raise OSError("模拟 GetCursorPos 失败")

    try:
        src.cursor_pos = _boom_cursor                # type: ignore[assignment]
        check("H4 取光标抛异常 → _point_inside() 返回 True（宁可不关）",
              bool(watch._point_inside()) is True,   # noqa: SLF001
              "设计如此：判定不了就按面板内处理")
    finally:
        src.cursor_pos = orig_cursor                 # type: ignore[assignment]

    try:
        watch._rect_provider = lambda: QRect()       # noqa: SLF001 - 非法矩形
        check("H4 矩形非法（QRect()）→ _point_inside() 返回 True",
              bool(watch._point_inside()) is True,   # noqa: SLF001
              "设计如此")
    finally:
        watch._rect_provider = orig_rect             # noqa: SLF001
    controller.close_panel()
    pump(app, 0.4)


def main() -> int:
    seed()
    app = QApplication(sys.argv[:1])
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)
    controller.start()
    pump(app, 0.6)

    print("=== 屏幕/DPI 实况 ===")
    for i, s in enumerate(QGuiApplication.screens()):
        g, a = s.geometry(), s.availableGeometry()
        print(f"  [{i}] {s.name()} dpr={s.devicePixelRatio()} "
              f"geometry=({g.x()},{g.y()}) {g.width()}x{g.height()} "
              f"avail=({a.x()},{a.y()}) {a.width()}x{a.height()}")
    if len(QGuiApplication.screens()) < 2:
        info("本机只有 1 块屏幕 → **多屏负坐标分支无法在本环境覆盖**（已如实标注，不冒充覆盖）")

    controller.open_panel("c0")
    pw = controller.panel
    if pw is None:
        print("面板未打开，无法验证")
        return 1
    pump(app, 1.0)
    rect = QRect(pw.frameGeometry())
    info(f"面板 frameGeometry（逻辑）= ({rect.x()},{rect.y()}) "
         f"{rect.width()}x{rect.height()}")
    try:
        h1_roundtrip(app, rect)
        h2_grid(app, pw, rect)
        h3_edge_click(app, controller, rect)
        h4_degrade(app, controller)
    finally:
        controller.close_panel()
        pump(app, 0.3)
        controller.shutdown()

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "; ".join(failed))
    root = sandbox.sandbox_root()
    if root:
        shutil.rmtree(root, ignore_errors=True)
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
