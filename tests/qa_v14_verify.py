"""QA 独立验证（v1.4）：数据根注入 / 启动器模式 / 图标去箭头 / 名称去扩展名。

为什么单独一个脚本（不是 unittest）：
- 需要在**真实 windows 平台插件**下取 Shell 图标做像素级比对，而
  ``tests/qt_support.py`` 会强行设 ``QT_QPA_PLATFORM=offscreen``，在 offscreen 下
  QFileIconProvider 拿不到真实的 exe/ico 图像，箭头比对会失去意义；
- 需要构造 ``AppController`` 走"自动登记分类"整链路，unittest discover 里跑
  会与其它用例的全局 QApplication 生命周期耦合。

安全约定（与 QA 铁律一致）：
- **绝不写真实桌面 / 绝不写 E:\\ 用户数据根**：所有落盘都在 ``tempfile`` 临时
  目录里完成，桌面目录一律显式注入临时目录；
- ``paths`` 模块的全局量（CONFIG_PATH / ICONS_DIR / LAUNCHERS_DIR / DATA_ROOT…）
  在用例内被重定向到临时目录，``finally`` 无条件还原（含异常路径）；
- 结束删除全部临时目录，不留 .lnk/.ico 到真实目录。

覆盖：
- C 数据根注入：默认根 = APP_DIR、set_data_root 后 ROOT 动态别名跟随、
  category_dir/ensure_category_dir/controller 扫描全部跟随、拒绝空值、
  数据根不可用时 ensure_base_dirs 不抛异常；
- D 启动器模式：只写 launcher 目录（桌面 0 文件）、``启动-<label>.lnk`` 命名与
  重名去重、历史桌面 .lnk 三重判据清理（只删自己的）、state.where=launcher、
  幂等（二次 sync 不重写）；
- E 图标去箭头：与 QFileIconProvider 对目标取图**像素级一致**、与不去箭头结果
  不同、失效 .lnk 仍返回非空图且失败不缓存、目录快捷方式、自定义图标优先、
  ``strip_shortcut_overlay=False`` 与 v1.3 行为一致；
- F 名称去扩展名：显示名/NAME_ROLE/tooltip/关开关/拖拽落盘仍为真实文件名。

用法：.venv\\Scripts\\python.exe tests\\qa_v14_verify.py
"""

from __future__ import annotations

import os
import shutil
import sys
import tempfile
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)   # 必须真实 windows 插件：要取 Shell 图标

from PySide6.QtCore import QFileInfo, QSize, Qt      # noqa: E402
from PySide6.QtGui import QColor, QImage, QPixmap    # noqa: E402
from PySide6.QtWidgets import QApplication, QFileIconProvider  # noqa: E402

from app import paths                                 # noqa: E402
from app.config import CategoryConfig                 # noqa: E402
from app.desktop_shortcuts import DesktopShortcutManager  # noqa: E402
from app.folder_model import Entry, FolderModel       # noqa: E402
from app.icon_provider import IconCache               # noqa: E402
from app.panel_model import NAME_ROLE, PanelModel, display_name  # noqa: E402
from app.win32 import shell_link, shell_resolve       # noqa: E402
from tests import sandbox                             # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
NOTEPAD = Path(r"C:\Windows\System32\notepad.exe")
ICON_PX = 48
# 像素比对容差（0-255 每通道平均绝对差）：同一取图路径应≈0，带/不带箭头应远大于它
SAME_TOL = 2.0
DIFF_MIN = 2.0


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def skip(name: str, reason: str) -> None:
    RESULTS.append((name, True, f"跳过：{reason}"))
    print(f"[SKIP] {name}  ({reason})")


# -- 像素比对 ---------------------------------------------------------------

def _normalized(pm: QPixmap) -> QImage:
    """转成 ICON_PX×ICON_PX 的 ARGB32（消除 devicePixelRatio / 源尺寸差异）。"""
    img = pm.toImage().convertToFormat(QImage.Format.Format_ARGB32)
    if img.width() != ICON_PX or img.height() != ICON_PX:
        img = img.scaled(ICON_PX, ICON_PX,
                         Qt.AspectRatioMode.IgnoreAspectRatio,
                         Qt.TransformationMode.SmoothTransformation)
    return img


def mean_diff(pm_a: QPixmap, pm_b: QPixmap) -> float:
    """两张图逐像素每通道（含 alpha）平均绝对差，范围 0-255。"""
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
    """QFileIconProvider 对某路径取图（= 面板"不去箭头"的参照物）。"""
    prov = QFileIconProvider()
    return prov.icon(QFileInfo(str(path))).pixmap(QSize(ICON_PX, ICON_PX))


# -- 临时环境 ---------------------------------------------------------------

# 沙箱隔离统一走 ``tests/sandbox.py``（同步 9 个 paths 锚点 + ``paths._BASE_DIRS``，
# 并在重定向后自校验"所有锚点都在沙箱根之下"）。本脚本不再自己抄一份重定向逻辑：
# 自己抄的那版漏了 ``_BASE_DIRS``，一旦走到 ``ensure_base_dirs()`` 仍会在真实
# ``%USERPROFILE%\DeskCountChen`` 建目录（"假安全"）。


# =========================================================================
# C. 数据根注入
# =========================================================================

def case_c_pristine() -> None:
    """C1/C2：必须在**任何**重定向/paths 写入之前执行（验的是出厂默认值）。"""
    print("\n=== C. 数据根注入 ===")
    check("C1 默认数据根 == APP_DIR",
          paths.DATA_ROOT == paths.APP_DIR,
          f"DATA_ROOT={paths.DATA_ROOT} APP_DIR={paths.APP_DIR}")
    check("C2 paths.ROOT is paths.DATA_ROOT（同对象）",
          paths.ROOT is paths.DATA_ROOT, str(paths.ROOT))
    check("C2 'ROOT' 不在 paths.__dict__（证明没有静态赋值）",
          "ROOT" not in vars(paths),
          str(sorted(k for k in vars(paths) if k.isupper())))


def case_c(tmp: Path) -> None:
    """C3–C9：数据根注入后 category_dir/ensure/category/controller 全部跟随。"""
    saved = paths.DATA_ROOT          # = 沙箱数据根（由 tests.sandbox 重定向）
    try:
        # C3：set_data_root 后 category_dir / ensure_category_dir 跟随
        target = tmp / "data_root_a"
        ret = paths.set_data_root(target)
        check("C3 set_data_root 返回生效值", ret == target, str(ret))
        check("C3 category_dir 跟随数据根",
              paths.category_dir("游戏加速") == target / "游戏加速",
              str(paths.category_dir("游戏加速")))
        made = paths.ensure_category_dir("影音娱乐")
        check("C3 ensure_category_dir 在数据根下创建目录",
              made == target / "影音娱乐" and made.is_dir(), str(made))

        # C4：ROOT 随 set 变化（不是导入期拷贝）
        check("C4 paths.ROOT 跟随新数据根", paths.ROOT == target, str(paths.ROOT))
        target_b = tmp / "data_root_b"
        paths.set_data_root(target_b)
        check("C4 再次 set 后 ROOT 仍同步（动态别名不是快照）",
              paths.ROOT == target_b and paths.DATA_ROOT == target_b, str(paths.ROOT))

        # C5：拒绝空值（None / "" / 纯空白）——按约定保持上一次有效值
        for bad in (None, "", "   "):
            paths.set_data_root(bad)
            check(f"C5 set_data_root({bad!r}) 被忽略，数据根不变",
                  paths.DATA_ROOT == target_b, str(paths.DATA_ROOT))
        # C5b：非路径对象不抛异常（行为记录：str() 化后生效；配置层已先挡掉非 str，
        #      这里只要求"不崩"，不断言具体取值）
        try:
            paths.set_data_root(12345)
            coerced = str(paths.DATA_ROOT)
            check("C5b set_data_root(非路径对象) 不抛异常（str() 化生效）",
                  bool(coerced), f"被解释为 {coerced}（相对路径，见遗留风险）")
        except Exception as exc:                        # noqa: BLE001
            check("C5b set_data_root(非路径对象) 不抛异常", False, f"{type(exc).__name__}: {exc}")
        paths.set_data_root(target_b)

        # C6：接受 Path 对象并 Path 化
        paths.set_data_root(str(tmp / "data_root_c"))
        check("C6 字符串入参被 Path 化", isinstance(paths.DATA_ROOT, Path),
              type(paths.DATA_ROOT).__name__)
        paths.set_data_root(tmp / "data_root_d")
        check("C6 Path 入参正常生效", paths.DATA_ROOT == tmp / "data_root_d",
              str(paths.DATA_ROOT))

        # C7：数据根不可用（不存在的盘符）时 ensure_base_dirs 不抛异常
        paths.set_data_root(Path("Q:/__qa_no_such_root__/sub"))
        try:
            paths.ensure_base_dirs()
            raised = ""
        except Exception as exc:                       # noqa: BLE001 - 这里就是要抓异常
            raised = f"{type(exc).__name__}: {exc}"
        check("C7 数据根不可用时 ensure_base_dirs 不抛异常", not raised, raised or "无异常")
        check("C7 数据根不可用时仍保留原值（不被清空）",
              str(paths.DATA_ROOT).replace("\\", "/").startswith("Q:/"),
              str(paths.DATA_ROOT))
    finally:
        paths.set_data_root(saved)

    # C8：controller 的分类扫描完全跟随注入的数据根
    _case_c_controller(tmp)

    check("C9 用例结束后数据根已回到沙箱根", paths.DATA_ROOT == saved, str(paths.DATA_ROOT))


def _case_c_controller(tmp: Path) -> None:
    """在沙箱数据根下验证 controller 只扫描该根（paths 已被 sandbox 整体重定向）。"""
    from app.controller import AppController

    real_desktop = shell_link.desktop_dir()
    before_desktop = {p.name for p in Path(real_desktop).glob("*.lnk")} if real_desktop else set()

    data = paths.DATA_ROOT                     # 沙箱数据根
    for name in ("分类A", "分类B", "快捷方式", "桌面", "z全部", ".hidden_dir"):
        (data / name).mkdir(parents=True, exist_ok=True)
    (data / "分类A" / "a.lnk").write_bytes(b"")
    (data / "分类A" / "b.exe").write_bytes(b"")
    (data / "分类A" / "工具").mkdir(exist_ok=True)

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = AppController(app)
    # 显式注入合法桌面目录（比默认更严格：连"解析真实桌面"这一步都不做）
    fake_desktop = tmp / "c8_desktop"
    fake_desktop.mkdir(parents=True, exist_ok=True)
    controller.shortcuts.desktop_dir = fake_desktop
    controller.start()
    try:
        dirs = sorted(c.dir for c in controller.cfg.categories)
        check("C8 controller 只登记数据根下真实目录",
              dirs == ["分类A", "分类B"], str(dirs))
        check("C8 跳过 category_exclude 名单（快捷方式/桌面/z全部）",
              not ({"快捷方式", "桌面", "z全部"} & set(dirs)), str(dirs))
        check("C8 跳过点开头的隐藏目录",
              ".hidden_dir" not in dirs, str(dirs))

        cat_a = controller.cfg.find_by_dir("分类A")
        fm = controller._folder_model(cat_a)
        check("C8 FolderModel 目录跟数据根走",
              fm.dir == data / "分类A", str(fm.dir))
        entries = fm.reload()
        names = sorted(e.name for e in entries)
        check("C8 条目扫描跟数据根走（a.lnk/b.exe/工具）",
              names == ["a.lnk", "b.exe", "工具"], str(names))
        launched = sorted(p.name for p in paths.LAUNCHERS_DIR.glob("*.lnk"))
        check("C8 .launchers 落在沙箱（真实 .launchers 未被写）",
              launched == ["启动-分类A.lnk", "启动-分类B.lnk"], str(launched))
        check("C8 注入桌面目录 0 个文件（launcher 模式绝不碰桌面）",
              not list(fake_desktop.iterdir()), str(list(fake_desktop.iterdir())))
    finally:
        controller.shutdown()

    after_desktop = {p.name for p in Path(real_desktop).glob("*.lnk")} if real_desktop else set()
    check("C8 真实桌面 .lnk 集合未被改动",
          before_desktop == after_desktop,
          f"多出 {sorted(after_desktop - before_desktop)} 少了 {sorted(before_desktop - after_desktop)}")


# =========================================================================
# D. 启动器模式
# =========================================================================

def _make_manager(tmp: Path, mode: str = "launcher"):
    icon_cache = IconCache(strip_shortcut_overlay=True)
    launcher = tmp / "launchers"
    desktop = tmp / "desktop"
    ico_dir = tmp / "ico"
    desktop.mkdir(parents=True, exist_ok=True)
    ico_dir.mkdir(parents=True, exist_ok=True)
    manager = DesktopShortcutManager(
        icon_cache=icon_cache,
        ico_dir=ico_dir,
        state_path=tmp / "state.json",
        project_root=PROJECT_ROOT,
        desktop_dir=desktop,
        python_exe=_pythonw(),
        script_path=PROJECT_ROOT / "main.py",
        mode=mode,
        launcher_dir=launcher,
    )
    return manager, launcher, desktop, ico_dir


def _pythonw() -> str:
    exe = Path(sys.executable)
    candidate = exe.with_name("pythonw.exe")
    return str(candidate if candidate.exists() else exe)


def _cats(*specs) -> list[CategoryConfig]:
    return [CategoryConfig(id=cid, dir=d, label=lbl) for cid, d, lbl in specs]


def _mtime(path: Path) -> int:
    return path.stat().st_mtime_ns


def case_d(tmp: Path) -> None:
    print("\n=== D. 启动器模式（launcher） ===")
    root = tmp / "d"
    manager, launcher, desktop, ico_dir = _make_manager(root)

    cats = _cats(("cat_g", "游戏加速", "游戏加速"), ("cat_y", "影音娱乐", "影音娱乐"))
    manager.sync(cats)

    written = sorted(p.name for p in launcher.glob("*.lnk"))
    check("D1 只在 launcher 目录写 .lnk（桌面 0 文件）",
          written == sorted(["启动-游戏加速.lnk", "启动-影音娱乐.lnk"])
          and not list(desktop.iterdir()),
          f"launcher={written} desktop={[p.name for p in desktop.iterdir()]}")
    check("D2 文件名为「启动-<label>.lnk」",
          all(n.startswith("启动-") for n in written), str(written))

    # D3：重名去重
    dup = _cats(("cat_a", "目录A", "同名"), ("cat_b", "目录B", "同名"))
    dup_root = root / "dup"
    m2, launcher2, desktop2, _ = _make_manager(dup_root)
    m2.sync(dup)
    names2 = sorted(p.name for p in launcher2.glob("*.lnk"))
    check("D2 重名分类追加 (2) 去重",
          names2 == ["启动-同名 (2).lnk", "启动-同名.lnk"], str(names2))
    check("D2 重名时桌面仍 0 文件", not list(desktop2.iterdir()),
          str([p.name for p in desktop2.iterdir()]))

    # D3：state.where = launcher
    import json

    state = json.loads((root / "state.json").read_text(encoding="utf-8"))
    wheres = {rec.get("where") for rec in state["categories"].values()}
    check("D4 state.where 全部为 launcher", wheres == {"launcher"}, str(wheres))
    check("D4 state.lnk 与磁盘文件名一致",
          {rec["lnk"] for rec in state["categories"].values()} == set(written),
          str({rec["lnk"] for rec in state["categories"].values()}))

    # D5：幂等（二次 sync 不重写）
    before = {p.name: _mtime(p) for p in launcher.glob("*.lnk")}
    manager.sync(cats)
    manager.sync(cats)
    after = {p.name: _mtime(p) for p in launcher.glob("*.lnk")}
    check("D5 幂等：二次/三次 sync 不重写 .lnk（mtime 不变）",
          before == after, f"{before} → {after}")

    # D6：迁移清理三重判据（只删指向本项目的）
    legacy_root = root / "legacy"
    m3, launcher3, desktop3, _ = _make_manager(legacy_root)
    own = desktop3 / "游戏加速.lnk"
    alien = desktop3 / "记事本.lnk"
    ok_own = shell_link.create_shortcut(
        dest=own,
        target=_pythonw(),
        arguments=f'"{PROJECT_ROOT / "main.py"}" --open "游戏加速"',
        working_dir=str(PROJECT_ROOT),
    )
    ok_alien = shell_link.create_shortcut(
        dest=alien, target=str(NOTEPAD), arguments="", working_dir="",
    )
    # 造历史 state（v1.3 形态：where=desktop）
    (legacy_root / "state.json").write_text(json.dumps({
        "version": 1,
        "categories": {
            "cat_g": {"lnk": "游戏加速.lnk", "sig": "old", "ico_sig": "x", "where": "desktop"},
            "cat_l": {"lnk": "记事本.lnk", "sig": "old", "ico_sig": "x", "where": "desktop"},
        },
    }, ensure_ascii=False), encoding="utf-8")
    check("D6 伪造 .lnk 就绪（本项目 1 个 + 记事本 1 个）",
          ok_own and ok_alien and own.exists() and alien.exists(),
          f"own={own.exists()} alien={alien.exists()}")

    m3.sync(_cats(("cat_g", "游戏加速", "游戏加速"), ("cat_l", "记事本", "记事本")))
    check("D6 指向本项目的桌面 .lnk 被清理", not own.exists(), str(own))
    check("D6 指向记事本的桌面 .lnk 被保留（不误删用户文件）", alien.exists(), str(alien))
    check("D6 清理后桌面只剩用户自己的 .lnk",
          sorted(p.name for p in desktop3.glob("*.lnk")) == ["记事本.lnk"],
          str(sorted(p.name for p in desktop3.glob("*.lnk"))))
    state3 = json.loads((legacy_root / "state.json").read_text(encoding="utf-8"))
    check("D6 迁移后 state.where 归一为 launcher",
          {rec.get("where") for rec in state3["categories"].values()} == {"launcher"},
          str({rec.get("where") for rec in state3["categories"].values()}))

    # D7：隐藏分类 → 启动器 .lnk 被删除，桌面仍无文件
    hidden = _cats(("cat_g", "游戏加速", "游戏加速"), ("cat_y", "影音娱乐", "影音娱乐"))
    hidden[1].visible = False
    manager.sync(hidden)
    check("D7 隐藏分类后其启动器 .lnk 被删除",
          sorted(p.name for p in launcher.glob("*.lnk")) == ["启动-游戏加速.lnk"],
          str(sorted(p.name for p in launcher.glob("*.lnk"))))
    check("D7 隐藏过程中桌面始终 0 文件", not list(desktop.iterdir()),
          str([p.name for p in desktop.iterdir()]))

    # D8：可回退模式 desktop —— 只写桌面、不写 launcher
    m4, launcher4, desktop4, _ = _make_manager(root / "desk_mode", mode="desktop")
    m4.sync(_cats(("cat_g", "游戏加速", "游戏加速")))
    check("D8 desktop 模式仍可回退：.lnk 落到桌面且无「启动-」前缀",
          sorted(p.name for p in desktop4.glob("*.lnk")) == ["游戏加速.lnk"],
          str(sorted(p.name for p in desktop4.glob("*.lnk"))))
    check("D8 desktop 模式不写 launcher 目录",
          not list(launcher4.glob("*.lnk")) if launcher4.exists() else True,
          str([p.name for p in launcher4.glob("*.lnk")]) if launcher4.exists() else "目录不存在")


# =========================================================================
# E. 图标去箭头
# =========================================================================

def case_e(tmp: Path) -> None:
    print("\n=== E. 图标去快捷方式箭头 ===")
    root = tmp / "e"
    root.mkdir(parents=True, exist_ok=True)
    if not NOTEPAD.exists():
        skip("E 全部用例", f"{NOTEPAD} 不存在")
        return

    plain = root / "记事本快捷方式.lnk"
    made = shell_link.create_shortcut(
        dest=plain, target=str(NOTEPAD), arguments="", working_dir="",
        icon_path="",                       # 刻意不给自定义图标 → 必须回落到目标
    )
    info = shell_resolve.read_link(plain)
    check("E0 临时 .lnk 已生成且能解析出目标",
          made and info.ok and Path(info.target).name.casefold() == "notepad.exe",
          f"target={info.target} icon={info.icon_location!r}")

    ref_exe = provider_pixmap(NOTEPAD)
    stripped = IconCache(strip_shortcut_overlay=True).pixmap(plain, ICON_PX)
    legacy = IconCache(strip_shortcut_overlay=False).pixmap(plain, ICON_PX)

    d_target = mean_diff(stripped, ref_exe)
    check("E1 去箭头后与 QFileIconProvider(notepad.exe) 像素级一致",
          d_target < SAME_TOL, f"平均色差={d_target:.3f} (< {SAME_TOL})")
    check("E1 与不去箭头结果明显不同（证明箭头来自原 .lnk 取图）",
          mean_diff(legacy, stripped) > DIFF_MIN,
          f"平均色差={mean_diff(legacy, stripped):.3f} (> {DIFF_MIN})")
    check("E1 不去箭头时与 QFileIconProvider(.lnk) 一致（v1.3 行为保持）",
          mean_diff(legacy, provider_pixmap(plain)) < SAME_TOL,
          f"平均色差={mean_diff(legacy, provider_pixmap(plain)):.3f}")

    # E2：失效 .lnk（目标不存在）→ 仍返回非空图，且失败不被缓存
    broken = root / "失效快捷方式.lnk"
    shell_link.create_shortcut(
        dest=broken, target=str(root / "__不存在的目标__" / "ghost.exe"),
        arguments="", working_dir="", icon_path="",
    )
    cache = IconCache(strip_shortcut_overlay=True)
    pm1 = cache.pixmap(broken, ICON_PX)
    pm2 = cache.pixmap(broken, ICON_PX)
    check("E2 失效 .lnk 仍返回非空图标（不灰显不隐藏）",
          not pm1.isNull() and not pm2.isNull(),
          f"{pm1.width()}x{pm1.height()} / {pm2.width()}x{pm2.height()}")
    check("E2 失效 .lnk 的图不是内置默认图（走 shell 通用图标）",
          mean_diff(pm1, IconCache().default_icon(ICON_PX)) > DIFF_MIN,
          f"与默认图色差={mean_diff(pm1, IconCache().default_icon(ICON_PX)):.3f}")
    broken_keys = [k for k in cache._cache if "ghost" in str(k).casefold()]
    check("E2 解析失败的结果没有进缓存（下次还会重试）", not broken_keys, str(broken_keys))

    # E3：目录快捷方式 → 文件夹图标（不是空白通用图）
    folder = root / "某个文件夹"
    folder.mkdir(exist_ok=True)
    lnk_dir = root / "文件夹快捷方式.lnk"
    shell_link.create_shortcut(dest=lnk_dir, target=str(folder), arguments="",
                               working_dir="", icon_path="")
    pm_dir = IconCache(strip_shortcut_overlay=True).pixmap(lnk_dir, ICON_PX)
    d_dir = mean_diff(pm_dir, provider_pixmap(folder))
    check("E3 目录快捷方式取到文件夹图标（与 QFileIconProvider(目录) 一致）",
          not pm_dir.isNull() and d_dir < SAME_TOL, f"平均色差={d_dir:.3f}")

    # E4：自定义图标位置优先于目标图标
    ico = root / "custom.ico"
    src = QPixmap(64, 64)
    src.fill(QColor("#C81E5B"))
    wrote_ico = src.save(str(ico), "ICO")
    if wrote_ico:
        lnk_ico = root / "自定义图标快捷方式.lnk"
        shell_link.create_shortcut(dest=lnk_ico, target=str(NOTEPAD), arguments="",
                                   working_dir="", icon_path=str(ico))
        pm_ico = IconCache(strip_shortcut_overlay=True).pixmap(lnk_ico, ICON_PX)
        d_exe = mean_diff(pm_ico, ref_exe)
        check("E4 自定义图标位置优先（与 notepad 图标明显不同）",
              d_exe > DIFF_MIN and not pm_ico.isNull(), f"与 notepad 色差={d_exe:.3f}")
    else:
        skip("E4 自定义图标优先", "本机 Qt 无法写 .ico")

    # E5：非 .lnk 路径不受去箭头分支影响（回归：别把 exe/目录也改了）
    cache5 = IconCache(strip_shortcut_overlay=True)
    check("E5 .exe 取图仍等于 QFileIconProvider 结果",
          mean_diff(cache5.pixmap(NOTEPAD, ICON_PX), ref_exe) < SAME_TOL,
          "")


# =========================================================================
# F. 名称去扩展名
# =========================================================================

def case_f(tmp: Path) -> None:
    print("\n=== F. 面板名称去扩展名 ===")
    check("F1 a.lnk → a", display_name("a.lnk", "link") == "a", display_name("a.lnk", "link"))
    check("F1 b.exe → b", display_name("b.exe", "exe") == "b", display_name("b.exe", "exe"))
    check("F1 文件夹「工具」保持原样", display_name("工具", "dir") == "工具",
          display_name("工具", "dir"))
    check("F1 无扩展名文件兜底为原名", display_name("readme", "exe") == "readme",
          display_name("readme", "exe"))
    check("F1 怪名 .lnk 不返回空串", display_name(".lnk", "link") != "",
          repr(display_name(".lnk", "link")))
    check("F1 strip_extensions=False 时保持原样",
          display_name("a.lnk", "link", strip_extensions=False) == "a.lnk",
          display_name("a.lnk", "link", strip_extensions=False))

    cat_dir = tmp / "f" / "分类A"
    cat_dir.mkdir(parents=True, exist_ok=True)
    for name in ("a.lnk", "b.exe"):
        (cat_dir / name).write_bytes(b"")
    (cat_dir / "工具").mkdir(exist_ok=True)

    fm = FolderModel(cat_dir)
    entries = fm.reload()
    cache = IconCache(strip_shortcut_overlay=True)

    model = PanelModel(strip_extensions=True)
    model.build_slots(fm.slots, entries, cache, 32)
    texts = [model.model.item(r).text() for r in range(model.model.rowCount())]
    roles = [model.model.item(r).data(NAME_ROLE) for r in range(model.model.rowCount())]
    tips = [model.model.item(r).toolTip() for r in range(model.model.rowCount())]
    check("F2 模型行文本去扩展名（.lnk/.exe 去掉，文件夹不动）",
          sorted(texts) == ["a", "b", "工具"], str(texts))
    check("F2 NAME_ROLE 仍是真实文件名",
          sorted(roles) == ["a.lnk", "b.exe", "工具"], str(roles))
    by_text = dict(zip(texts, tips))
    check("F2 显示名≠真实名时才设 tooltip",
          by_text.get("a") == "a.lnk" and by_text.get("b") == "b.exe"
          and by_text.get("工具") == "",
          str(by_text))
    check("F2 serialize() 输出真实文件名（不是去扩展名后的）",
          sorted(model.serialize()) == ["a.lnk", "b.exe", "工具"], str(model.serialize()))
    check("F2 entry_at 按真实名反查（单击启动不会找错文件）",
          model.entry_at(texts.index("a")).name == "a.lnk",
          model.entry_at(texts.index("a")).name if model.entry_at(texts.index("a")) else "None")

    model_off = PanelModel(strip_extensions=False)
    model_off.build_slots(fm.slots, entries, cache, 32)
    texts_off = [model_off.model.item(r).text() for r in range(model_off.model.rowCount())]
    tips_off = [model_off.model.item(r).toolTip() for r in range(model_off.model.rowCount())]
    check("F3 strip_extensions=False 时显示真实文件名",
          sorted(texts_off) == ["a.lnk", "b.exe", "工具"], str(texts_off))
    check("F3 strip_extensions=False 时 tooltip 为空", not any(tips_off), str(tips_off))

    # F4：拖拽落盘仍是真实文件名
    fm.save_slots(["b.exe", "a.lnk", "工具"])
    order_path = cat_dir / ".order.json"
    raw = order_path.read_text(encoding="utf-8")
    check("F4 .order.json 存的是真实文件名（b.exe / a.lnk）",
          '"b.exe"' in raw and '"a.lnk"' in raw, raw.replace("\n", " ")[:180])
    check("F4 .order.json 不含去扩展名后的裸名",
          '"a"' not in raw.replace('"a.lnk"', "") ,
          raw.replace("\n", " ")[:180])


# =========================================================================
# 入口
# =========================================================================

def main() -> int:
    if not Path(sys.executable).samefile(PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"):
        print(f"提示：当前解释器 = {sys.executable}（建议用项目 .venv）")

    # ① 必须在任何 paths.* 使用之前取真实配置指纹（只读，不写任何备份文件）
    real = sandbox.snapshot_real()
    print(f"真实配置（只读指纹）: {real.describe()}")

    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    print(f"Qt 平台插件 = {app.platformName()}（像素比对需要真实 windows 插件）")

    tmp = Path(tempfile.mkdtemp(prefix="qa_v14_verify_"))
    box: Path | None = None
    print(f"临时工作目录: {tmp}（真实桌面/真实数据根全程不写）")
    try:
        case_c_pristine()                    # C1/C2：出厂默认值（重定向前）
        # ② 整体沙箱化：9 个 paths 锚点 + _BASE_DIRS 全部指向临时目录并自校验
        box = sandbox.sandbox_paths(prefix="qa_v14_verify_box_")
        paths.ensure_base_dirs()
        print(f"沙箱根: {box}（DATA_ROOT={paths.DATA_ROOT}）")
        case_c(tmp)
        case_d(tmp)
        case_e(tmp)
        case_f(tmp)
    except Exception:                                   # noqa: BLE001 - 留现场不吞栈
        import traceback

        traceback.print_exc()
        RESULTS.append(("脚本未抛异常", False, "见上方堆栈"))
    finally:
        shutil.rmtree(tmp, ignore_errors=True)
        if box is not None:
            shutil.rmtree(box, ignore_errors=True)
        check("临时目录已清理", not tmp.exists() and (box is None or not box.exists()), str(tmp))
        check("真实用户配置零改动（sha256 一致）", *sandbox.guard_real_untouched(real))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("v1.4 独立验证全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
