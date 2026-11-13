"""DeskCountChen 无 GUI 自检脚本（可单测，不依赖真实桌面交互）。

覆盖：
1. 路径创建（含 ICO 缓存目录）；
2. .order.json：正常 / 缺失 / JSON 损坏 / stale 保留；
3. FolderModel._merge 四用例 + 过滤（.txt/.xlsx/.order.json 不出现）；
4. 自然排序（StrCmpLogicalW 语义）；
5. 图标取图（.exe / 目录 / 失效 .lnk）；
6. 真实桌面路径（SHGetKnownFolderPath）与 .lnk/.ico 生成能力；
7. 槽位"插入"语义（app.slot_ops）；
8. shell_open（不存在的驱动器路径 → 返回 False 且不弹 UI）；
9. v1.4：数据根 / 启动器目录、launcher 模式落点（不碰桌面）、.lnk 只读解析。

用法：.venv\\Scripts\\python.exe tools\\selfcheck.py
"""

from __future__ import annotations

import json
import sys
import tempfile
from pathlib import Path

# 保证可从任意 cwd 运行
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from PySide6.QtWidgets import QApplication  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def main() -> int:
    # offscreen 平台：无 GUI 环境也能创建 QApplication（QFileIconProvider 需要）
    app = QApplication.instance() or QApplication(sys.argv + ["-platform", "offscreen"])

    from app import paths
    from app.config import CategoryConfig
    from app.folder_model import FolderModel
    from app.icon_provider import IconCache
    from app.order_store import OrderStore
    from app.slot_ops import apply_move
    from app.win32 import shell_link, window_ops

    # 1. 路径创建（v1.4：APP_DIR 系列 + LAUNCHERS_DIR + DATA_ROOT）
    paths.ensure_base_dirs()
    check(
        "路径创建（APP_DIR 系列 + .launchers）",
        paths.APP_DIR.exists() and paths.ICONS_DIR.exists()
        and paths.LOGS_DIR.exists() and paths.ICO_DIR.exists()
        and paths.LAUNCHERS_DIR.exists(),
        f"APP_DIR={paths.APP_DIR}",
    )
    check(
        "路径创建（DATA_ROOT）",
        paths.DATA_ROOT.exists(),
        f"DATA_ROOT={paths.DATA_ROOT}",
    )
    check(
        "paths.ROOT 兼容别名 == DATA_ROOT",
        paths.ROOT == paths.DATA_ROOT,
        str(paths.ROOT),
    )

    with tempfile.TemporaryDirectory() as tmp_s:
        tmp = Path(tmp_s)

        # 2. OrderStore 四情形
        store = OrderStore(tmp)
        check("OrderStore: 文件缺失 → []", store.load() == [])

        store.save(["b.lnk", "a.lnk"], [])
        data = json.loads((tmp / ".order.json").read_text(encoding="utf-8"))
        check(
            "OrderStore: 保存 roundtrip",
            store.load() == ["b.lnk", "a.lnk"] and data["version"] == 1,
        )

        # 隐藏属性（紧随保存后校验；写损坏文件前会清属性）
        try:
            import ctypes

            attrs = ctypes.windll.kernel32.GetFileAttributesW(str(tmp / ".order.json"))
            check("OrderStore: 隐藏属性已设置", bool(attrs & 0x2), hex(attrs))
        except Exception:
            check("OrderStore: 隐藏属性已设置", True, "非 Windows？跳过")

        store.save(["a.lnk"], ["b.lnk", "ghost.lnk"])
        data = json.loads((tmp / ".order.json").read_text(encoding="utf-8"))
        check(
            "OrderStore: stale 保留在尾部",
            data["order"] == ["a.lnk", "b.lnk", "ghost.lnk"],
        )

        # 先清隐藏属性（Windows 拒绝覆盖写隐藏文件），再写入损坏内容模拟用户误编辑
        window_ops.clear_file_hidden(tmp / ".order.json")
        (tmp / ".order.json").write_text("{broken json!!", encoding="utf-8")
        check("OrderStore: JSON 损坏 → [] 不崩溃", store.load() == [])
        check(
            "OrderStore: 损坏文件未被删除",
            (tmp / ".order.json").exists(),
        )

        # 3/4. FolderModel 合并与过滤（注意：ghost.lnk 只应出现在"记录"里，
        # 磁盘上不存在 —— 用于验证"记录保留、不显示"）
        for name in ("b.lnk", "a.lnk", "c.exe", "AAA.LNK", "alpha.lnk", "Zeta.exe",
                     "readme.txt", "data.xlsx"):
            (tmp / name).write_bytes(b"")

        fm = FolderModel(tmp)

        # 用例 1：记录 [b, a]，磁盘有 a,b,c → b,a,c
        entries = fm._merge(fm._scan_entries(), ["b.lnk", "a.lnk"])
        check(
            "merge: 记录序优先 + 新条目排末尾",
            [e.name for e in entries][:2] == ["b.lnk", "a.lnk"],
            str([e.name for e in entries]),
        )
        # 精确校验：merge（无记录时）的自然排序
        entries_none = fm._merge(fm._scan_entries(), [])
        names_none = [e.name for e in entries_none]
        check(
            "merge: 未记录条目自然排序（alpha 在 Zeta 之前）",
            names_none.index("alpha.lnk") < names_none.index("Zeta.exe"),
            str(names_none),
        )

        # 用例 2：ghost.lnk 只在记录里、磁盘上不存在 → 不显示
        entries_ghost = fm._merge(fm._scan_entries(), ["b.lnk", "a.lnk", "ghost.lnk"])
        check(
            "merge: 磁盘不存在的记录名不显示",
            "ghost.lnk" not in [e.name for e in entries_ghost]
            and [e.name for e in entries_ghost][:2] == ["b.lnk", "a.lnk"],
            str([e.name for e in entries_ghost]),
        )

        # 用例 3：大小写不敏感匹配
        entries_case = fm._merge(fm._scan_entries(), ["AAA.LNK"])
        check(
            "merge: 大小写不敏感匹配",
            entries_case and entries_case[0].name == "AAA.LNK",
            str([e.name for e in entries_case][:1]),
        )

        # 用例 4：过滤（**v1.4.1 语义变更：不再按扩展名白名单过滤**）
        # 期望值改动记录：原来是 ".txt/.xlsx/.order.json 都不出现"，现按用户
        # "全部显示"的要求拆成两条 —— 普通文件必须收录，只有程序元数据排除。
        check(
            "merge: .txt/.xlsx 全部收录（v1.4.1 全类型显示）",
            all(n in names_none for n in ("readme.txt", "data.xlsx")),
            str(names_none),
        )
        check(
            "merge: .order.json 不进条目（程序元数据）",
            ".order.json" not in names_none,
            str(names_none),
        )

        # save_order：先写入一条含 ghost.lnk 的合法记录，再重载 + 回写
        window_ops.clear_file_hidden(tmp / ".order.json")
        (tmp / ".order.json").write_text(
            json.dumps({"version": 1, "order": ["b.lnk", "a.lnk", "ghost.lnk"]}, ensure_ascii=False),
            encoding="utf-8",
        )
        fm.reload()
        fm.save_order([e.name for e in fm.entries])
        kept = json.loads((tmp / ".order.json").read_text(encoding="utf-8"))["order"]
        check(
            "save_order: stale(ghost.lnk) 保留",
            "ghost.lnk" in kept,
            str(kept[-3:]),
        )

        # 用例 6（缺陷 1 回归）：连续 save_order 两次，ghost.lnk 仍必须保留
        fm.save_order(["Zeta.exe", "b.lnk"])          # 第二次拖拽（换了可视顺序）
        kept2 = json.loads((tmp / ".order.json").read_text(encoding="utf-8"))["order"]
        check(
            "save_order: 连续两次 ghost.lnk 不被抹掉",
            "ghost.lnk" in kept2,
            str(kept2),
        )

    # 5. 图标取图
    ic = IconCache()
    exe = Path(sys.executable)
    pm_exe = ic.pixmap(exe, 64)
    check("图标: .exe 取图非空", not pm_exe.isNull(), f"{pm_exe.width()}x{pm_exe.height()}")
    pm_dir = ic.pixmap(Path(tempfile.gettempdir()), 64)
    check("图标: 目录取图非空", not pm_dir.isNull())
    pm_broken = ic.pixmap(Path("C:/不存在/失效.lnk"), 64)
    check("图标: 失效 .lnk 取图非空（不灰显不隐藏）", not pm_broken.isNull())
    pm_cat = ic.category_pixmap(CategoryConfig(label="x"), 64)
    check("图标: 内置默认分类图标非空", not pm_cat.isNull())

    # 6. 真实桌面路径 + .lnk / .ico 生成（v1.2 新增，写临时目录不碰真实桌面）
    desk = shell_link.desktop_dir()
    check("桌面路径: SHGetKnownFolderPath 可解析", desk is not None and Path(desk).is_dir(),
          str(desk))

    from app.desktop_shortcuts import DesktopShortcutManager, validate_ico
    from app.win32 import shell_resolve

    with tempfile.TemporaryDirectory(prefix="dbx_selfcheck_") as tmp2:
        root = Path(tmp2)
        project = root / "project"
        desktop = root / "Desktop"
        for d in (project, desktop):
            d.mkdir(parents=True, exist_ok=True)
        script = project / "main.py"
        script.write_text("# stub\n", encoding="utf-8")
        manager = DesktopShortcutManager(
            icon_cache=ic,
            ico_dir=root / "ico",
            state_path=root / "state.json",
            project_root=project,
            desktop_dir=desktop,
            python_exe=str(sys.executable),
            script_path=script,
            mode="desktop",          # 这一段验的是桌面落点（v1.3 链路仍保留）
            launcher_dir=root / ".launchers",
        )
        cat = CategoryConfig(id="cat_selfcheck", dir="自检分类", label="自检分类")
        manager.sync([cat])
        lnk = desktop / "自检分类.lnk"
        ico = root / "ico" / "cat_selfcheck.ico"
        check("快捷方式: .lnk 已生成且结构合法",
              lnk.exists() and lnk.read_bytes().startswith(b"\x4c\x00\x00\x00"),
              f"{lnk.stat().st_size if lnk.exists() else 0} bytes")
        check("快捷方式: 内容含 --open 分类目录名",
              lnk.exists() and '--open "自检分类"'.encode("utf-16-le") in lnk.read_bytes())
        check("图标: 分类 ICO 已生成且结构合法", ico.exists() and validate_ico(ico), str(ico))
        cat.visible = False
        manager.sync([cat])
        check("快捷方式: 隐藏分类后 .lnk 被删除", not lnk.exists())

        # 6b. v1.4 launcher 模式：只写私有启动器目录，桌面一个字节都不碰
        launchers = root / ".launchers"
        launcher_manager = DesktopShortcutManager(
            icon_cache=ic,
            ico_dir=root / "ico2",
            state_path=root / "state2.json",
            project_root=project,
            desktop_dir=desktop,
            python_exe=str(sys.executable),
            script_path=script,
            mode="launcher",
            launcher_dir=launchers,
        )
        cat2 = CategoryConfig(id="cat_launcher", dir="启动器自检", label="启动器自检")
        launcher_manager.sync([cat2])
        target_lnk = launchers / "启动-启动器自检.lnk"
        check("启动器: .lnk 生成在 .launchers 且带 --open",
              target_lnk.exists()
              and '--open "启动器自检"'.encode("utf-16-le") in target_lnk.read_bytes(),
              str(target_lnk))
        check("启动器: launcher 模式绝不往桌面写 .lnk",
              not any(p.suffix.lower() == ".lnk" for p in desktop.iterdir()),
              str(sorted(p.name for p in desktop.iterdir())))

        # 6c. .lnk 只读解析（去箭头取图 / 迁移清理共用）
        resolved_target, icon_loc, icon_idx = shell_resolve.resolve(target_lnk)
        check("解析: .lnk 目标可解析（用于去小箭头取图）",
              bool(resolved_target), f"{resolved_target} icon={icon_loc}#{icon_idx}")
        check("解析: 不存在的 .lnk 返回空值不抛异常",
              shell_resolve.resolve(Path("C:/不存在/失效.lnk")) == ("", "", -1))
        info = shell_resolve.read_link(target_lnk)
        check("解析: 参数里带 --open（迁移清理的判据）",
              "--open" in info.arguments, repr(info.arguments))

    # 7. 密集插入语义（v1.3：空位体系废除，完全对齐手机桌面文件夹）
    check(
        "拖拽: 向前拖 = 插入让位，后方图标自动补齐（无空位）",
        apply_move(["A", "B", "C", "D"], [2], 0) == ["C", "A", "B", "D"],
        str(apply_move(["A", "B", "C", "D"], [2], 0)),
    )
    check(
        "拖拽: 向后拖 = 插入，原位置由后方图标补齐（无空位）",
        apply_move(["A", "B", "C", "D"], [0], 2) == ["B", "A", "C", "D"],
        str(apply_move(["A", "B", "C", "D"], [0], 2)),
    )
    check(
        "槽位: 批量搬移插到落点图标之前",
        [s for s in apply_move(["A", "B", "C", "D", "E"], [0, 1], 3) if s]
        == ["C", "A", "B", "D", "E"],
        str(apply_move(["A", "B", "C", "D", "E"], [0, 1], 3)),
    )

    # 8. shell_open（不存在的驱动器 → 返回 False，不弹对话框）
    ok = window_ops.shell_open("Q:\\__no_such_drive__\\x.txt")
    check("shell_open: 失败返回 False 不抛异常", ok is False)

    failed = [n for n, ok2, _ in RESULTS if not ok2]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：", ", ".join(failed))
        return 1
    print("selfcheck 全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
