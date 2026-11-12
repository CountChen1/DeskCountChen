"""DesktopBox v1.4.1 独立验收脚本（QA 侧黑盒）。

覆盖两批改动：

  A. 面板显示**全部文件类型**（``.url``/``.txt``/``.pdf``/无扩展名…），
     不再按 ``.lnk``/``.exe`` 白名单过滤；显示名对所有非目录条目去扩展名。
  B. 注销分类的 ``.ico`` 清理判据加固（``removed_ids``）+ 路径安全校验。

设计原则（与工程师的白盒单测**互补不重叠**）：

  - 只走公开入口：``FolderModel.reload()/save_slots()``、
    ``PanelModel.build()``、``DesktopShortcutManager.sync()``；
  - 不 import ``tests/test_desktop_shortcuts.py``，不复用其构造 helper，
    不直接调 ``_prune_orphan_icons``（那是开发自测的白盒面）；
  - 唯一读取的私有观察点是 ``FolderModel._signature``（只读），
    用于验证"扫描口径与签名口径一致"这条内部一致性约束。

铁律遵守情况：所有落盘一律在 ``%TEMP%`` 下；对 ``E:\\快捷方式SAO`` 只做
``os.listdir``/``stat``/只读取图，脚本前后对真实用户配置做 sha256 对拍。

用法::

    .venv/Scripts/python.exe tests/qa_v141_verify.py
    # 退出码 0 = 全通过，1 = 有失败
"""

from __future__ import annotations

import ctypes
import json
import logging
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)   # 必须真实 windows 插件：要取 Shell 图标

from PySide6.QtCore import QEventLoop, QFileInfo, QSize, Qt, QTimer    # noqa: E402
from PySide6.QtGui import QImage, QPixmap                             # noqa: E402
from PySide6.QtWidgets import QApplication, QFileIconProvider          # noqa: E402

from app.config import CategoryConfig                                 # noqa: E402
from app.desktop_shortcuts import DesktopShortcutManager              # noqa: E402
from app.folder_model import FolderModel                              # noqa: E402
from app.icon_provider import IconCache                               # noqa: E402
from app.panel_model import KIND_ROLE, NAME_ROLE, PanelModel, display_name  # noqa: E402
from tests import sandbox                                             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []

ICON_PX = 48
SAME_TOL = 2.0      # 同一取图路径的平均色差上限
DIFF_MIN = 2.0      # "明显不同"的下限

# 真实用户数据（只读；绝不在此目录下写任何东西）
SAO_GAME_DIR = Path(r"E:\快捷方式SAO\游戏加速")
REAL_URL = "Age of Empires II Definitive Edition.url"
REAL_TXT = "帝国时代笔记.txt"

FILE_ATTRIBUTE_HIDDEN = 0x2


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def info(name: str, detail: str) -> None:
    """不参与判定的事实记录（用于报告里举证）。"""
    RESULTS.append((name, True, detail))
    print(f"[INFO] {name}  ({detail})")


def run_case(label: str, fn, *args) -> None:
    """执行一个用例组：单组崩溃不拖垮其余断言（否则一个 TypeError 会隐藏后面几十条）。"""
    try:
        fn(*args)
    except Exception as exc:  # noqa: BLE001 - 验收脚本要跑完
        import traceback
        traceback.print_exc()
        check(f"{label} 用例组未抛异常", False, f"{type(exc).__name__}: {exc}")


# -- 像素工具 ---------------------------------------------------------------

def _normalized(pm: QPixmap) -> QImage:
    """转成 ICON_PX×ICON_PX 的 ARGB32，消除源尺寸 / DPR 差异。"""
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    if img.width() != ICON_PX or img.height() != ICON_PX:
        img = img.scaled(ICON_PX, ICON_PX,
                         Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img


def mean_diff(pm_a: QPixmap, pm_b: QPixmap) -> float:
    """两张图逐像素每通道（含 alpha）平均绝对差，0-255。"""
    a, b = _normalized(pm_a), _normalized(pm_b)
    total = 0
    count = 0
    for y in range(ICON_PX):
        for x in range(ICON_PX):
            ca, cb = a.pixelColor(x, y), b.pixelColor(x, y)
            total += (abs(ca.red() - cb.red()) + abs(ca.green() - cb.green())
                      + abs(ca.blue() - cb.blue()) + abs(ca.alpha() - cb.alpha()))
            count += 4
    return total / count if count else 999.0


def provider_pixmap(path) -> QPixmap:
    """QFileIconProvider 对某路径取图（系统关联图标 = 产品"回退"路径的参照物）。"""
    prov = QFileIconProvider()
    return prov.icon(QFileInfo(str(path))).pixmap(QSize(ICON_PX, ICON_PX))


def alpha_stats(pm: QPixmap) -> tuple[int, int]:
    """(不透明像素数, 总像素数) —— 用于"非纯透明"判定。"""
    img = _normalized(pm)
    opaque = 0
    for y in range(ICON_PX):
        for x in range(ICON_PX):
            if img.pixelColor(x, y).alpha() > 0:
                opaque += 1
    return opaque, ICON_PX * ICON_PX


# -- 文件系统工具 -----------------------------------------------------------

_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.GetFileAttributesW.argtypes = [ctypes.c_wchar_p]
_k32.GetFileAttributesW.restype = ctypes.c_uint32
_k32.SetFileAttributesW.argtypes = [ctypes.c_wchar_p, ctypes.c_uint32]
_k32.SetFileAttributesW.restype = ctypes.c_int
_INVALID_ATTRS = 0xFFFFFFFF


def set_hidden(path: Path) -> bool:
    """给路径设置 Windows 隐藏属性（测试用；产品代码里 ctypes 只出现在 win32/）。"""
    attrs = _k32.GetFileAttributesW(str(path))
    if attrs == _INVALID_ATTRS:
        return False
    return _k32.SetFileAttributesW(str(path), attrs | FILE_ATTRIBUTE_HIDDEN) != 0


def write(path: Path, text: str = "x") -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
    return path


def snapshot_tree(root: Path) -> list[tuple[str, int, int]]:
    """目录树 (相对路径, 大小, mtime_ns) 清单，用于证"零改动"。"""
    if not root.exists():
        return []
    out = []
    for p in sorted(root.rglob("*")):
        try:
            st = p.stat()
        except OSError:
            continue
        out.append((str(p.relative_to(root)), st.st_size, st.st_mtime_ns))
    return out


# =========================================================================
# A. 全类型显示（批次 A）
# =========================================================================

def _build_matrix(box: Path) -> Path:
    """造一个覆盖全部文件类型的目录。返回目录路径。"""
    box.mkdir(parents=True, exist_ok=True)
    write(box / "a.lnk")
    write(box / "b.exe")
    write(box / "c.txt", "hello")
    write(box / "d.url", "[InternetShortcut]\r\nURL=https://example.com\r\n")
    write(box / "e.pdf")
    write(box / "f")                      # 无扩展名
    write(box / "empty.txt", "")          # 0 字节
    (box / "dir1").mkdir()
    # 以下 7 项必须被排除
    write(box / "desktop.ini", "[.ShellClassInfo]")
    write(box / "thumbs.db")
    write(box / ".ds_store")
    write(box / ".order.json", '{"version": 1, "order": []}')
    write(box / ".order.json.tmp4711", "{}")
    hidden_file = write(box / "hidden.txt")
    hidden_dir = box / "hiddendir"
    hidden_dir.mkdir()
    set_hidden(hidden_file)
    set_hidden(hidden_dir)
    return box


def case_a_scan(tmp: Path) -> None:
    print("\n=== A1/A2/A3. 扫描：全部类型进来，只有元数据/系统文件被排除 ===")
    box = _build_matrix(tmp / "a" / "box")
    model = FolderModel(box)
    entries = model.reload()
    names = sorted(e.name for e in entries)
    expected = sorted(["a.lnk", "b.exe", "c.txt", "d.url", "e.pdf", "f",
                       "empty.txt", "dir1"])
    check("A1 全类型条目全部进入面板（.url/.txt/.pdf/无扩展名/0字节/目录）",
          names == expected, f"实际={names}")
    check("A1 条目数 == 8", len(entries) == 8, f"len={len(entries)}")

    by_name = {e.name: e for e in entries}
    kinds = {n: by_name[n].kind for n in by_name}
    ok_kind = (kinds.get("a.lnk") == "link" and kinds.get("b.exe") == "exe"
               and kinds.get("dir1") == "dir"
               and all(kinds.get(n) == "file"
                       for n in ("c.txt", "d.url", "e.pdf", "f", "empty.txt")))
    check("A2 kind 判定（link/exe/dir/file）", ok_kind, str(kinds))

    excluded = ["desktop.ini", "thumbs.db", ".ds_store", ".order.json",
                ".order.json.tmp4711", "hidden.txt", "hiddendir"]
    leaked = [n for n in excluded if n in by_name]
    check("A3 程序元数据 / 系统文件 / 隐藏项均不进面板", not leaked,
          f"泄漏={leaked}")
    check("A3 0 字节文件照常显示（不被 size>0 之类判据误杀）",
          "empty.txt" in by_name, str(by_name.get("empty.txt")))


def case_a_display_name() -> None:
    print("\n=== A4. 显示名去扩展名规则 ===")
    cases = [
        ("c.txt", "file", "c"),
        ("d.url", "file", "d"),
        ("e.pdf", "file", "e"),
        ("f", "file", "f"),
        ("empty.txt", "file", "empty"),
        ("a.lnk", "link", "a"),
        ("b.exe", "exe", "b"),
        (REAL_TXT, "file", "帝国时代笔记"),
        (REAL_URL, "file", "Age of Empires II Definitive Edition"),
    ]
    bad = [(n, k, display_name(n, k), want) for n, k, want in cases
           if display_name(n, k) != want]
    check("A4 非目录条目一律去扩展名（含 .txt/.url/.pdf 与真实文件名）",
          not bad, f"不符={bad}")

    check("A4 目录保留原名", display_name("工具", "dir") == "工具",
          display_name("工具", "dir"))
    check("A4 点开头且无扩展名（.gitignore）不被截断",
          display_name(".gitignore", "file") == ".gitignore",
          display_name(".gitignore", "file"))
    check("A4 多段扩展名只去最后一段（a.tar.gz → a.tar）",
          display_name("a.tar.gz", "file") == "a.tar",
          display_name("a.tar.gz", "file"))
    # 已登记的取舍：名字里带点但无扩展名会被截断；NAME_ROLE/tooltip 仍是真名
    info("A4 已知取舍：v1.4 → 显示为 v1（原文 v1.4）",
         f"display_name('v1.4','file') = {display_name('v1.4', 'file')!r}")
    check("A4 strip_extensions=False 保留原名",
          display_name("c.txt", "file", strip_extensions=False) == "c.txt",
          display_name("c.txt", "file", strip_extensions=False))


def case_a_panel_model(tmp: Path) -> None:
    print("\n=== A5. 面板行：显示名 / NAME_ROLE / tooltip 三者的关系 ===")
    box = tmp / "a" / "panel"
    box.mkdir(parents=True, exist_ok=True)
    write(box / REAL_TXT, "笔记")
    write(box / "c.txt", "hello")
    (box / "工具").mkdir()
    model = FolderModel(box)
    entries = model.reload()

    cache = IconCache(strip_shortcut_overlay=True)
    pm = PanelModel(strip_extensions=True)
    pm.build(entries, cache, ICON_PX)

    rows = {}
    for r in range(pm.model.rowCount()):
        item = pm.model.item(r)
        rows[item.data(NAME_ROLE)] = {
            "text": item.text(),
            "kind": item.data(KIND_ROLE),
            "tip": item.toolTip(),
        }

    target = rows.get(REAL_TXT, {})
    check("A5 .txt 行显示名去掉扩展名",
          target.get("text") == "帝国时代笔记", str(target.get("text")))
    check("A5 .txt 行 NAME_ROLE 仍是真实文件名（带扩展名）",
          REAL_TXT in rows, str(list(rows)))
    check("A5 显示名与真实名不同时才设 tooltip，且 tooltip == 真实名",
          target.get("tip") == REAL_TXT, repr(target.get("tip")))

    folder = rows.get("工具", {})
    check("A5 目录行显示原名且不设 tooltip",
          folder.get("text") == "工具" and not folder.get("tip"),
          f"text={folder.get('text')!r} tip={folder.get('tip')!r}")

    pm2 = PanelModel(strip_extensions=False)
    pm2.build(entries, cache, ICON_PX)
    raw = {pm2.model.item(r).data(NAME_ROLE): pm2.model.item(r)
           for r in range(pm2.model.rowCount())}
    item = raw.get(REAL_TXT)
    check("A5 strip_extensions=False 时显示真实名、不设 tooltip",
          item is not None and item.text() == REAL_TXT and not item.toolTip(),
          f"text={item.text() if item else None!r}")


def case_a_order_roundtrip(tmp: Path) -> None:
    print("\n=== A6. 拖拽落盘：.order.json 里必须是真实文件名（带扩展名） ===")
    box = tmp / "a" / "order"
    box.mkdir(parents=True, exist_ok=True)
    write(box / REAL_TXT, "笔记")
    write(box / "a.lnk")
    write(box / "b.exe")

    model = FolderModel(box)
    model.reload()
    # 模拟"把 .txt 拖到第一位"后的落盘（面板 serialize() 输出的即真实名序列）
    wanted = [REAL_TXT, "b.exe", "a.lnk"]
    model.save_slots(wanted)               # 公开入口无返回值；内存顺序以落盘结果为准

    raw = json.loads((box / ".order.json").read_text(encoding="utf-8"))
    order = raw.get("order") or []
    slots = raw.get("slots") or []
    check("A6 .order.json 的 order 存真实文件名（带 .txt 扩展名）",
          REAL_TXT in order and "b.exe" in order and "a.lnk" in order, str(order))
    check("A6 .order.json 的 slots 同为真实文件名",
          REAL_TXT in slots, str(slots))
    check("A6 落盘内容不含被截断的裸名「帝国时代笔记」",
          "帝国时代笔记" not in order and "帝国时代笔记" not in slots,
          str(order))
    check("A6 保存后内存顺序与文件内容一致（无漂移）",
          list(model.slots) == wanted, f"slots={model.slots}")

    reloaded = FolderModel(box)
    reloaded.reload()
    check("A6 重新加载后顺序与写入一致（真实名可反查）",
          list(reloaded.slots) == wanted, str(reloaded.slots))


def case_a_signature_consistency(tmp: Path) -> None:
    print("\n=== A7. 扫描口径 vs 签名口径必须一致（否则\"加了文件不刷新\"） ===")
    box = tmp / "a" / "sig"
    box.mkdir(parents=True, exist_ok=True)
    write(box / "desktop.ini", "[.ShellClassInfo]")
    write(box / ".order.json", '{"version": 1, "order": []}')

    model = FolderModel(box)
    model.reload()
    check("A7 只有忽略项时签名 == ()（忽略项不污染签名）",
          model._signature == (), str(model._signature))

    before = model._signature
    write(box / ".order.json.tmp9999", "{}")
    entries = model.reload()
    check("A7 新增 .order.json.tmp* → 签名不变、条目不变（不触发无效刷新）",
          model._signature == before and entries == [],
          f"sig_changed={model._signature != before} entries={len(entries)}")

    before = model._signature
    write(box / "desktop.ini", "[.ShellClassInfo]\r\nIconResource=x.ico")
    entries = model.reload()
    check("A7 修改 desktop.ini → 签名不变、条目不变",
          model._signature == before and entries == [],
          f"sig_changed={model._signature != before}")

    hidden = write(box / "hidden.txt")
    set_hidden(hidden)
    before = model._signature
    entries = model.reload()
    sig_changed = model._signature != before
    check("A7 新增隐藏文件 → 条目不变（签名会变，只多一次无害刷新）",
          entries == [],
          f"sig_changed={sig_changed}（不对称但无害：签名是超集探测器）")


def case_a_icons(tmp: Path) -> None:
    print("\n=== A8. 非 .lnk/.exe 条目必须有真实图标（不能是内置占位图） ===")
    box = tmp / "a" / "icons"
    box.mkdir(parents=True, exist_ok=True)
    write(box / "c.txt", "hello")
    write(box / "d.url", "[InternetShortcut]\r\nURL=https://example.com\r\n")
    write(box / "e.pdf")
    write(box / "f")
    write(box / "empty.txt", "")

    cache = IconCache(strip_shortcut_overlay=True)
    fallback = cache.default_icon(ICON_PX)

    for name in ("c.txt", "d.url", "e.pdf", "f", "empty.txt"):
        pm = cache.pixmap(box / name, ICON_PX)
        opaque, total = alpha_stats(pm)
        diff = mean_diff(pm, fallback)
        ok = (not pm.isNull()) and opaque > 0 and diff > DIFF_MIN
        check(f"A8 {name} 图标非空/非纯透明/非内置占位图", ok,
              f"{pm.width()}x{pm.height()} 不透明={opaque}/{total} 与内置图色差={diff:.3f}")


def _wait_changed(model: FolderModel, action, cap_ms: int = 3000) -> tuple[int, int]:
    """执行 action 后等 ``changed`` 信号：返回 (触发次数, 实际耗时 ms)。

    真实的"加了文件面板会不会刷新"链路：QFileSystemWatcher(150ms 去抖) 或
    1s 轮询签名比对 → reload → changed。这里跑真事件循环，不用 sleep 猜。
    """
    hits = {"n": 0}
    loop = QEventLoop()

    def _on_changed() -> None:
        hits["n"] += 1
        loop.quit()

    model.changed.connect(_on_changed)
    cap = QTimer()
    cap.setSingleShot(True)
    cap.timeout.connect(loop.quit)
    QTimer.singleShot(60, action)
    start = time.monotonic()
    cap.start(cap_ms)
    loop.exec()
    elapsed = int((time.monotonic() - start) * 1000)
    model.changed.disconnect(_on_changed)
    cap.stop()
    return hits["n"], elapsed


def case_a_live_refresh(tmp: Path) -> None:
    print("\n=== A10. 端到端实时刷新：新增/删除 .txt 面板是否会刷新 ===")
    box = tmp / "a" / "live"
    box.mkdir(parents=True, exist_ok=True)
    write(box / "starter.txt", "x")
    model = FolderModel(box)
    model.reload()
    model.start_watch()
    try:
        new_file = box / "新增笔记.txt"
        hits, ms = _wait_changed(model, lambda: write(new_file, "hello"))
        check("A10 新增 .txt 后触发 changed（面板会刷新）", hits > 0,
              f"changed={hits} 用时={ms}ms")
        names = [e.name for e in model.entries]
        check("A10 新增的 .txt 真正出现在条目里", "新增笔记.txt" in names, str(names))

        hits2, ms2 = _wait_changed(model, lambda: new_file.unlink())
        names2 = [e.name for e in model.entries]
        check("A10 删除 .txt 后条目随之消失", "新增笔记.txt" not in names2,
              f"changed={hits2} 用时={ms2}ms 剩余={names2}")
    finally:
        model.stop_watch()


NOTEPAD = Path(r"C:\Windows\System32\notepad.exe")


def case_a_url_icon(tmp: Path) -> None:
    """A11：``.url`` 的新增分支（解析 INI 的 ``IconFile=``）。

    这是 v1.4.1 新写的代码路径，四条支路都要过一遍：绝对路径 / 带引号 /
    环境变量 / 相对路径（图片）/ 指向不存在 / GBK 编码文本。
    """
    print("\n=== A11. .url 图标：IconFile 绝对/引号/环境变量/相对/缺失/GBK ===")
    box = tmp / "a" / "urlicon"
    box.mkdir(parents=True, exist_ok=True)
    cache = IconCache(strip_shortcut_overlay=True)
    notepad_pm = cache.pixmap(NOTEPAD, ICON_PX)
    red = tmp / "a" / "urlicon" / "red.png"
    img = QImage(32, 32, QImage.Format.Format_ARGB32)
    img.fill(Qt.GlobalColor.red)
    img.save(str(red), "PNG")

    def make(name: str, icon_line: str, encoding: str = "utf-8") -> Path:
        body = "[InternetShortcut]\r\nURL=https://example.com\r\n" + icon_line + "\r\n"
        path = box / name
        path.write_bytes(body.encode(encoding))
        return path

    cases = [
        ("绝对路径 IconFile", make("abs.url", rf"IconFile={NOTEPAD}"), "notepad"),
        ("带引号的 IconFile", make("quoted.url", rf'IconFile="{NOTEPAD}"'), "notepad"),
        ("环境变量 %SystemRoot%",
         make("envvar.url", r"IconFile=%SystemRoot%\System32\notepad.exe"), "notepad"),
        ("相对路径（图片同目录）", make("rel.url", "IconFile=red.png"), "red"),
        ("GBK 编码的中文 .url",
         # 注意：这里**不能**用 rf""——原始字符串会把 \r\n 变成字面量，
         # 导致 IconFile 的值被污染成非法路径（我第一次就踩了这个坑）。
         make("gbk.url", f"IconFile={NOTEPAD}\r\nComment=中文注释", "gbk"), "notepad"),
        ("IconFile 指向不存在的文件", make("missing.url",
                                     r"IconFile=C:\no\such\dir\x.ico"), "fallback"),
    ]
    retried: list[str] = []
    for label, path, kind in cases:
        attempts = 0
        while True:
            pm = cache.pixmap(path, ICON_PX)
            attempts += 1
            if kind == "notepad":
                diff = mean_diff(pm, notepad_pm)
                ok = (not pm.isNull()) and diff < SAME_TOL
                detail = f"与 notepad 色差={diff:.3f} (< {SAME_TOL})"
            elif kind == "red":
                diff = mean_diff(pm, notepad_pm)
                ok = (not pm.isNull()) and diff > DIFF_MIN
                detail = f"与 notepad 色差={diff:.3f} (> {DIFF_MIN}，说明用的是图标本身)"
            else:
                diff = mean_diff(pm, provider_pixmap(path))
                ok = (not pm.isNull()) and diff < SAME_TOL
                detail = f"回退系统图标，与 QFileIconProvider 色差={diff:.3f}"
            if ok or attempts >= 3:
                break
            # Shell 图标提取偶发失败是真实存在的外部抖动：换一个干净实例重试。
            # **重试必须留痕**（下面单独统计），否则会变成"吞掉真实失败"。
            cache = IconCache(strip_shortcut_overlay=True)
            notepad_pm = cache.pixmap(NOTEPAD, ICON_PX)
        if ok and attempts > 1:
            retried.append(f"{label}×{attempts}")
            detail += f"；重试 {attempts - 1} 次后通过（Shell 抖动）"
        if not ok:
            try:
                ini = IconCache._read_url_ini(path)
            except Exception as exc:  # noqa: BLE001
                ini = f"<读取异常 {exc}>"
            detail += f"；INI 解析结果={ini}（ini 正确→Shell 抖动；ini 错→解析缺陷）"
        check(f"A11 {label} → 图标符合预期", ok, detail)
    info("A11 重试统计",
         f"需要重试的用例={retried or '无'}（>0 表示 Shell 取图不稳，非产品缺陷，但有留痕）")


def case_a_real_data(tmp: Path) -> None:
    print("\n=== A9. 真实数据 E:\\快捷方式SAO\\游戏加速（只读） ===")
    if not SAO_GAME_DIR.is_dir():
        check("A9 真实目录可访问", False, str(SAO_GAME_DIR))
        return
    before = snapshot_tree(SAO_GAME_DIR)
    real_names = sorted(os.listdir(SAO_GAME_DIR))
    info("A9 目录实际条目数", f"{len(real_names)} 项")

    model = FolderModel(SAO_GAME_DIR)      # reload() 只读：listdir/stat/read_text
    entries = model.reload()
    shown = sorted(e.name for e in entries)
    check("A9 27 项全部进入面板（.url/.txt 不再被过滤）",
          shown == real_names,
          f"面板={len(shown)} 磁盘={len(real_names)} 差集={sorted(set(real_names) - set(shown))}")

    by_name = {e.name: e for e in entries}
    check("A9 .url 条目 kind == file（不是被整类丢弃）",
          by_name.get(REAL_URL) is not None and by_name[REAL_URL].kind == "file",
          str(by_name.get(REAL_URL)))
    check("A9 .txt 条目 kind == file",
          by_name.get(REAL_TXT) is not None and by_name[REAL_TXT].kind == "file",
          str(by_name.get(REAL_TXT)))

    cache = IconCache(strip_shortcut_overlay=True)
    pm = PanelModel(strip_extensions=True)
    pm.build(entries, cache, ICON_PX)
    texts = {pm.model.item(r).data(NAME_ROLE): pm.model.item(r).text()
             for r in range(pm.model.rowCount())}
    check("A9 真实 .url 显示名 = 「Age of Empires II Definitive Edition」",
          texts.get(REAL_URL) == "Age of Empires II Definitive Edition",
          repr(texts.get(REAL_URL)))
    check("A9 真实 .txt 显示名 = 「帝国时代笔记」",
          texts.get(REAL_TXT) == "帝国时代笔记", repr(texts.get(REAL_TXT)))

    for name in (REAL_URL, REAL_TXT):
        pmx = cache.pixmap(SAO_GAME_DIR / name, ICON_PX)
        opaque, total = alpha_stats(pmx)
        check(f"A9 真实项图标非纯透明 {name}", (not pmx.isNull()) and opaque > 0,
              f"{pmx.width()}x{pmx.height()} 不透明={opaque}/{total}")

    info("A9 面板完整清单（顺序）",
         " | ".join(f"{r + 1}.{pm.model.item(r).text()}" for r in range(pm.model.rowCount())))
    info("A9 NAME_ROLE 完整清单",
         " | ".join(str(pm.model.item(r).data(NAME_ROLE))
                    for r in range(pm.model.rowCount())))

    after = snapshot_tree(SAO_GAME_DIR)
    check("A9 只读验证未改动真实目录（清单/大小/mtime 全一致）",
          before == after, f"before={len(before)} after={len(after)}")


# =========================================================================
# B. 注销 ico 清理（批次 B）
# =========================================================================

def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _make_manager(root: Path):
    root.mkdir(parents=True, exist_ok=True)
    launcher = root / "launchers"
    desktop = root / "desktop"
    ico_dir = root / "ico"
    desktop.mkdir(parents=True, exist_ok=True)
    ico_dir.mkdir(parents=True, exist_ok=True)
    manager = DesktopShortcutManager(
        icon_cache=IconCache(strip_shortcut_overlay=True),
        ico_dir=ico_dir,
        state_path=root / "state.json",
        project_root=PROJECT_ROOT,
        desktop_dir=desktop,
        python_exe=_pythonw(),
        script_path=PROJECT_ROOT / "main.py",
        mode="launcher",
        launcher_dir=launcher,
    )
    return manager, launcher, ico_dir


def _cat(cid: str, label: str, visible: bool = True) -> CategoryConfig:
    return CategoryConfig(id=cid, dir=label, label=label, visible=visible)


def _icos(ico_dir: Path) -> set[str]:
    return {p.name for p in ico_dir.glob("*.ico")}


class _WarnCatcher(logging.Handler):
    """只收 WARNING 及以上，用于断言"熔断必须留痕"。"""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.messages: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        if record.levelno >= logging.WARNING:
            self.messages.append(record.getMessage())


def _sync_capture(manager, cats) -> list[str]:
    """执行一次 sync 并返回期间的 WARNING 文本（含格式化后的参数）。"""
    logger = logging.getLogger("app.desktop_shortcuts")
    catcher = _WarnCatcher()
    prev_level = logger.level
    logger.addHandler(catcher)
    logger.setLevel(logging.DEBUG)          # 确保记录一定发出，由 handler 过滤
    try:
        manager.sync(cats)
    finally:
        logger.removeHandler(catcher)
        logger.setLevel(prev_level)
    return catcher.messages


# P2 熔断的边界表（team-lead 定案，逐行独立断言）
# 阈值 = max(2, ceil(50% × managed_before))；managed_before 必须是**两个 pop 循环之前**
# 的 state 条数，否则"正常删 2 个"会被误熔断（本轮最容易写错的地方）。
PRUNE_TABLE = [
    # tag, 原有分类数, 保留分类数, 期望删除的 ico 数, 是否应熔断
    ("B③-1 cfg=[]（配置不可读/掉盘）",        6, 0, 0, True),
    ("B③-2 cfg 覆盖成 1 个（16:58 真实形态）", 6, 1, 0, True),
    ("B③-3 正常删 1 个（清理能力保持）",        6, 5, 1, False),
    ("B③-4 正常删 2 个（managed_before 陷阱）", 6, 4, 2, False),
    ("B③-5 批量删 3 个（达阈值，熔断）",        6, 3, 0, True),
    ("B③-6 单分类配置被清空",                  1, 0, 0, True),
]


def case_b_prune_table(tmp: Path) -> None:
    print("\n=== B③. P2 熔断边界表（6 行独立断言 + WARNING 留痕） ===")
    for idx, (tag, n_before, n_keep, n_del, braked) in enumerate(PRUNE_TABLE):
        ids = [f"cat_0000000{i}" for i in range(1, n_before + 1)]
        cats_all = [_cat(cid, f"分类{i + 1}") for i, cid in enumerate(ids)]
        cats_keep = cats_all[:n_keep]
        root = tmp / "b" / f"tbl_{idx}"
        manager, launcher, ico_dir = _make_manager(root)

        manager.sync(cats_all)                       # 让产品自己写出 state / ico / lnk
        assert len(_icos(ico_dir)) >= n_before, (tag, _icos(ico_dir))
        warnings = _sync_capture(manager, cats_keep)

        present = _icos(ico_dir)
        if n_del:
            gone = {f"cat_0000000{i}.ico" for i in range(n_keep + 1, n_before + 1)}
            check(f"{tag} → 注销的 {n_del} 个 .ico 被删除", gone.isdisjoint(present),
                  f"应删={sorted(gone)} 现存={sorted(present)}")
        else:
            keep_all = {f"{cid}.ico" for cid in ids}
            check(f"{tag} → 一个 .ico 都不删", keep_all <= present,
                  f"缺失={sorted(keep_all - present)}")

        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        check(f"{tag} → state 里只剩 {n_keep} 条记录",
              len(state.get("categories", {})) == n_keep,
              str(sorted(state.get("categories", {}))))
        lnks = sorted(p.name for p in launcher.glob("*.lnk"))
        check(f"{tag} → .lnk 仍与配置对齐（熔断不影响 .lnk 清理）",
              len(lnks) == n_keep, str(lnks))

        has_warn = any("注销" in m for m in warnings)
        check(f"{tag} → {'留痕 WARNING' if braked else '不产生注销 WARNING'}",
              has_warn == braked,
              f"warnings={warnings}")
        if braked:
            check(f"{tag} → WARNING 含注销数量与总数（N/M 数字）",
                  any("注销" in m and str(n_before - n_keep) in m
                      and str(n_before) in m for m in warnings),
                  f"warnings={warnings}")


def case_b_fresh_state(tmp: Path) -> None:
    """坑 1：进程刚启动时 ``self._state`` 还是 {}。

    ``_load_state()`` 只返回值、不写 ``self._state``（那是 ``sync()`` 末尾才赋的），
    所以 ``managed_before`` 若取 ``len(self._state)`` 会恒为 0 → 熔断整条失效。
    本用例用**全新 manager 实例** + **预置 state.json**（上一次运行留下的现场），
    只跑**一次** sync —— 这才是"重启后配置被覆盖成 1 个"的真实形态（16:58 之后重启）。
    """
    print("\n=== B④. 全新实例 + 预置 state（managed_before 取值陷阱） ===")
    ids = [f"cat_0000000{i}" for i in range(1, 7)]
    root = tmp / "b" / "fresh"
    manager, launcher, ico_dir = _make_manager(root)
    # 预置"上一次运行"留下的现场：6 条 state 记录 + 6 个 ico
    seed = {"version": 1, "categories": {
        cid: {"lnk": f"启动-分类{i + 1}.lnk", "sig": "old", "ico_sig": "old",
              "where": "launcher"}
        for i, cid in enumerate(ids)
    }}
    (root / "state.json").write_text(json.dumps(seed), encoding="utf-8")
    for cid in ids:
        write(ico_dir / f"{cid}.ico", "seed")

    assert manager._state == {}, "前提：新实例的 self._state 必须是空（陷阱成立的条件）"
    warnings = _sync_capture(manager, [_cat(ids[0], "分类1")])

    present = _icos(ico_dir)
    missing = {f"{cid}.ico" for cid in ids} - present
    check("B④ 重启后 cfg 缩成 1/6 → 6 个 .ico 一个都不删（熔断生效）",
          not missing, f"缺失={sorted(missing)} 现存={sorted(present)}")
    check("B④ 熔断留痕 WARNING（含注销数与总数）",
          any("注销" in m and "5" in m and "6" in m for m in warnings),
          f"warnings={warnings}")
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    check("B④ state 仍按配置收敛到 1 条（熔断不动记录）",
          len(state.get("categories", {})) == 1,
          str(sorted(state.get("categories", {}))))
    check("B④ .lnk 仍与配置对齐（只剩 1 个）",
          len(list(launcher.glob("*.lnk"))) == 1,
          str(sorted(p.name for p in launcher.glob("*.lnk"))))


def case_b_prune(tmp: Path) -> None:
    print("\n=== B. 未管理文件保护 + 隐藏语义 + 同名接管 + 路径安全 ===")
    # id 统一用 8 位十六进制，兼容"只处理 cat_[0-9a-f]{8}.ico"这类实现
    A = _cat("cat_0a0a0a01", "分类甲")
    B = _cat("cat_0b0b0b02", "分类乙")

    # ---- ①b：从未被本程序管理过的 ico → 必须保留 ------------------------
    root = tmp / "b" / "case1b"
    manager, launcher, ico_dir = _make_manager(root)
    write(ico_dir / "cat_99999999.ico", "not-ours")
    manager.sync([A])
    check("B①b 从未管理过的 .ico 不被删除（不误删非我方文件）",
          "cat_99999999.ico" in _icos(ico_dir), str(sorted(_icos(ico_dir))))
    check("B①b 我方分类的 .ico 正常生成",
          "cat_0a0a0a01.ico" in _icos(ico_dir), str(sorted(_icos(ico_dir))))

    # ---- ②a：分类注销（cfg 里消失了） → ico 删除 ------------------------
    root = tmp / "b" / "case2a"
    manager, launcher, ico_dir = _make_manager(root)
    manager.sync([A, B])          # 让产品自己写出 state.json
    check("B②a 首次同步后 A/B 的 .ico 都存在",
          {"cat_0a0a0a01.ico", "cat_0b0b0b02.ico"} <= _icos(ico_dir),
          str(sorted(_icos(ico_dir))))
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    check("B②a 状态文件 schema: {version, categories} 且记录键集齐",
          isinstance(state.get("categories"), dict)
          and set(state["categories"]) == {"cat_0a0a0a01", "cat_0b0b0b02"}
          and all(set(v) == {"lnk", "sig", "ico_sig", "where"}
                  for v in state["categories"].values()),
          str(sorted(state.get("categories", {}))))

    manager.sync([B])             # A 被注销
    check("B②a 注销分类的 .ico 被删除", "cat_0a0a0a01.ico" not in _icos(ico_dir),
          str(sorted(_icos(ico_dir))))
    check("B②a 存活分类的 .ico 不受影响", "cat_0b0b0b02.ico" in _icos(ico_dir),
          str(sorted(_icos(ico_dir))))
    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    check("B②a 注销分类的记录已从 state 中移除",
          "cat_0a0a0a01" not in state.get("categories", {}),
          str(sorted(state.get("categories", {}))))

    # ---- ②b：隐藏分类 → .lnk 删、.ico 保留 ------------------------------
    root = tmp / "b" / "case2b"
    manager, launcher, ico_dir = _make_manager(root)
    manager.sync([A, B])
    hidden_a = _cat("cat_0a0a0a01", "分类甲", visible=False)
    manager.sync([hidden_a, B])
    check("B②b 隐藏分类的 .lnk 被删除",
          not (launcher / "启动-分类甲.lnk").exists(),
          str(sorted(p.name for p in launcher.glob("*.lnk"))))
    check("B②b 隐藏分类的 .ico 保留在磁盘（隐藏≠注销）",
          "cat_0a0a0a01.ico" in _icos(ico_dir), str(sorted(_icos(ico_dir))))
    # 隐藏 → 再显示：ico 会重建（既有行为，本轮不改），此处只记录事实
    manager.sync([A, B])

    # ---- ⑤：同名 .lnk 被新分类接管 → 旧 ico 删、新 ico 独立 --------------
    root = tmp / "b" / "case5"
    manager, launcher, ico_dir = _make_manager(root)
    manager.sync([A])
    old_ico = "cat_0a0a0a01.ico"
    new_cat = _cat("cat_0c0c0c03", "分类甲")     # 同 label、不同 id（重建场景）
    manager.sync([new_cat])
    check("B⑤ 同名 .lnk 被新分类接管后文件仍在",
          (launcher / "启动-分类甲.lnk").exists(),
          str(sorted(p.name for p in launcher.glob("*.lnk"))))
    check("B⑤ 旧分类 .ico 被删除", old_ico not in _icos(ico_dir),
          str(sorted(_icos(ico_dir))))
    check("B⑤ 新分类 .ico 独立存在",
          "cat_0c0c0c03.ico" in _icos(ico_dir), str(sorted(_icos(ico_dir))))

    # ---- ⑥：路径安全 —— 恶意 id 不得越出 ico_dir -------------------------
    # 注意 ⑥ 的构造要点：state 里只放 **1 条**恶意记录 + 1 条存活记录，
    # 使 managed_before=2 / removed=1 < 阈值 2 → 不触发熔断，
    # 否则"熔断提前 return"会让本用例白白通过（等于没测到路径判据）。
    print("\n--- B⑥ 路径安全（两道防线：正则 + resolve 后的真实归属） ---")

    def _traversal_case(tag: str, evil_id: str, target: Path, expect_alive: bool) -> None:
        root = tmp / "b" / f"case6_{tag}"
        manager, launcher, ico_dir = _make_manager(root)
        write(target, "must-survive")
        seed = {"version": 1, "categories": {
            evil_id: {"lnk": "", "sig": "x", "ico_sig": "x", "where": "launcher"},
            A.id: {"lnk": "", "sig": "x", "ico_sig": "x", "where": "launcher"},
        }}
        (root / "state.json").write_text(json.dumps(seed), encoding="utf-8")
        manager.sync([A])                     # removed={evil_id} → 不熔断 → 走路径判据
        survived = target.exists()
        check(f"B⑥-{tag} 恶意 id {evil_id!r} 不越界删 {target.name}",
              survived == expect_alive,
              f"存活={survived} 期望={expect_alive} ico_dir={sorted(_icos(ico_dir))}")
        state = json.loads((root / "state.json").read_text(encoding="utf-8"))
        check(f"B⑥-{tag} 前置条件成立：注销循环确实执行了（state 只剩存活分类）",
              set(state.get("categories", {})) == {A.id},
              str(sorted(state.get("categories", {}))))

    # ⑥a：反斜杠/斜杠穿越 + 文件名不符合 ICO_NAME_RE → 靠正则挡住
    _traversal_case("a-backslash", "..\\evil", tmp / "b" / "case6_a-backslash" / "evil.ico",
                    True)
    # ⑥b：**关键陷阱** —— id 让目标既是"词法上在 ico_dir 内"、文件名又完全符合
    #      cat_[0-9a-f]{8}.ico，两道老防线同时失效，只有 resolve() 后的真实归属能挡住。
    #      目标 = ico_dir 的兄弟目录里的真实 ico 名。
    _traversal_case("b-lexical-relative-to", "../cat_0a0a0a01",
                    tmp / "b" / "case6_b-lexical-relative-to" / "cat_0a0a0a01.ico", True)

    # 负向对照：证明 ⑥b 不是空断言 —— 对这条 id，"词法 is_relative_to" + "ICO_NAME_RE"
    # 两道老防线**都是放行**，唯一挡住它的是 resolve() 之后的真实归属判定。
    # 若哪天有人把判据改回 is_relative_to，这里会立刻提示"探针失效/兜不住"。
    from app.desktop_shortcuts import ICO_NAME_RE
    base = tmp / "b" / "case6_negctl" / "ico"
    base.mkdir(parents=True, exist_ok=True)
    probe = base / "../cat_0a0a0a01.ico"
    lex_inside = probe.is_relative_to(base)
    name_match = bool(ICO_NAME_RE.fullmatch(probe.name))
    real_outside = probe.resolve().parent != base.resolve()
    check("B⑥-c 对照：两道老防线对 ../cat_0a0a0a01 均放行，故 ⑥b 的断言非空",
          lex_inside and name_match and real_outside,
          f"lexical_inside={lex_inside} name_match={name_match} "
          f"resolved_parent={probe.resolve().parent.name}")


# =========================================================================
# main
# =========================================================================

def main() -> int:
    app = QApplication.instance() or QApplication([])
    real = sandbox.snapshot_real()
    box = None
    tmp = Path(tempfile.mkdtemp(prefix="qa_v141_"))
    try:
        box = sandbox.sandbox_paths(prefix="qa_v141_box_")
        from app import paths
        paths.ensure_base_dirs()

        run_case("A1-A3 扫描", case_a_scan, tmp)
        run_case("A4 显示名", case_a_display_name)
        run_case("A5 面板行", case_a_panel_model, tmp)
        run_case("A6 落盘", case_a_order_roundtrip, tmp)
        run_case("A7 口径一致", case_a_signature_consistency, tmp)
        run_case("A8 图标", case_a_icons, tmp)
        run_case("A11 .url 图标", case_a_url_icon, tmp)
        run_case("A10 实时刷新", case_a_live_refresh, tmp)
        run_case("A9 真实数据", case_a_real_data, tmp)
        run_case("B 未管理文件/隐藏/同名/路径安全", case_b_prune, tmp)
        run_case("B③ 熔断边界表", case_b_prune_table, tmp)
        run_case("B④ 全新实例+预置state", case_b_fresh_state, tmp)
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if box is not None:
            shutil.rmtree(box, ignore_errors=True)
        check("临时目录已清理", not tmp.exists() and not (box and box.exists()),
              f"{tmp}")
        ok, detail = sandbox.guard_real_untouched(real)
        check("真实用户配置零改动（sha256 一致）", ok, detail)

    failed = [r for r in RESULTS if not r[1]]
    total = len(RESULTS)
    print("\n" + "=" * 70)
    print(f"总计 {total} 项，通过 {total - len(failed)}，失败 {len(failed)}")
    if failed:
        for name, _ok, detail in failed:
            print(f"  [FAIL] {name}  ({detail})")
    print("=" * 70)
    del app
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
