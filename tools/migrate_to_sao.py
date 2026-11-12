"""配置迁移脚本：把 DesktopBox 切到"SAO Utils 2 入口 + E 盘数据根"。

背景（v1.4）：桌面图标入口交给 SAO Utils 2 的 HUD 图标。DesktopBox 不再往
桌面写 .lnk，改为在私有目录 ``%USERPROFILE%\\DesktopBox\\.launchers`` 生成
``启动-<分类>.lnk``，供 SAO 的 "Change Action" 引用。

本脚本做三件事（**幂等，可重复执行**）：
1. 备份现有 ``%USERPROFILE%\\DesktopBox\\.config.json`` 到同目录
   ``.config.json.bak-<YYYYmmdd-HHMMSS>``（备份失败立即中止，绝不先改配置）；
2. 写入新配置：``app.data_root = E:\\快捷方式SAO``、``app.shortcut_mode = launcher``、
   ``category_exclude`` 用默认值；分类集合 = 数据根下真实存在的这 6 个目录
   （游戏加速 / 影音娱乐 / 工作学习 / 社交通讯 / 系统硬件 / 文档下载），
   皮肤沿用旧"常用"分类（radius=12 / #21C9F8 / opacity=62 / anchor / 479×522）；
   旧"常用"分类不再登记（旧配置已备份，随时可回退）；
3. 用 offscreen QApplication 调 ``DesktopShortcutManager.sync()`` 生成启动器
   .lnk 并打印路径。

**安全红线**：只写 ``%USERPROFILE%\\DesktopBox``（配置 + .launchers + .icons），
绝不写 / 改 / 删 ``E:\\快捷方式SAO`` 下的任何文件（只读取目录名判断分类）。

**执行顺序至关重要**：DesktopBox **必须先退出**。运行中的旧实例内存里是旧配置，
它退出时会把旧配置写回磁盘，迁移成果会被静默覆盖。脚本会用单实例互斥量探测，
发现有实例在跑就中止（除非显式 ``--force``）。

用法：
    .venv\\Scripts\\python.exe tools\\migrate_to_sao.py
    .venv\\Scripts\\python.exe tools\\migrate_to_sao.py --dry-run   # 只看计划
    .venv\\Scripts\\python.exe tools\\migrate_to_sao.py --force     # 明知实例在跑仍继续
"""

from __future__ import annotations

import os
import shutil
import sys
from datetime import datetime
from pathlib import Path

# 保证可从任意 cwd 运行（脚本在 tools/ 下）
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

# 必须在导入 QtWidgets 之前：无桌面环境（服务器 / CI）也要能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

TARGET_DATA_ROOT = r"E:\快捷方式SAO"
CATEGORY_DIRS = ("游戏加速", "影音娱乐", "工作学习", "社交通讯", "系统硬件", "文档下载")
LEGACY_SKIN_SOURCE_DIR = "常用"     # 皮肤模板来源（旧"常用"分类）
MUTEX_NAME = r"Local\DesktopBox.SingleInstance.Mutex"
ERROR_ALREADY_EXISTS = 183
DEFAULT_SKIN = {                    # 旧"常用"不存在时的兜底（与用户当前皮肤一致）
    "icon_px": 64,
    "panel_w": 479,
    "panel_h": 522,
    "radius": 12,
    "bg_color": "#21C9F8",
    "opacity": 62,
    "position_mode": "anchor",
}


def desktopbox_running() -> bool:
    """是否已有 DesktopBox 实例在跑（用单实例互斥量探测，只读不打断）。

    为什么必须检测：正在运行的旧实例内存里是**旧配置**，它退出时会把自己的
    配置写回磁盘 —— 迁移成果会被静默覆盖，用户下次启动拿到的是旧数据根。
    所以正确顺序永远是"先退出 DesktopBox → 再迁移 → 再启动新版"。
    """
    try:
        import ctypes

        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.CreateMutexW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_wchar_p]
        kernel32.CreateMutexW.restype = ctypes.c_void_p
        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = ctypes.c_int
        handle = kernel32.CreateMutexW(None, False, MUTEX_NAME)
        err = ctypes.get_last_error()
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return err == ERROR_ALREADY_EXISTS
    except Exception:  # noqa: BLE001 - 探测失败不该阻断迁移
        return False



def backup_config(config_path: Path) -> Path | None:
    """把现有配置复制成 ``.config.json.bak-<时间戳>``；返回备份路径。

    配置不存在时返回 None（没什么可备份，属于首次安装场景）。
    备份失败直接抛 OSError：**没备份就不许改配置**。
    """
    config_path = Path(config_path)
    if not config_path.is_file():
        return None
    stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    backup = config_path.with_name(f"{config_path.name}.bak-{stamp}")
    # 同一秒内重复执行：追加序号，绝不复用/覆盖已有备份
    index = 2
    while backup.exists():
        backup = config_path.with_name(f"{config_path.name}.bak-{stamp}-{index}")
        index += 1
    shutil.copyfile(config_path, backup)
    return backup


def skin_template(old_cfg) -> dict:
    """从旧配置里取"常用"分类作为皮肤模板（缺失字段用 DEFAULT_SKIN 兜底）。"""
    template = dict(DEFAULT_SKIN)
    try:
        legacy = old_cfg.find_by_dir(LEGACY_SKIN_SOURCE_DIR)
    except Exception:  # noqa: BLE001 - 旧配置结构异常不该拖垮迁移
        legacy = None
    if legacy is None:
        return template
    template.update({
        "icon_px": int(legacy.icon_px),
        "panel_w": int(legacy.panel_w),
        "panel_h": int(legacy.panel_h),
        "radius": int(legacy.radius),
        "bg_color": str(legacy.bg_color),
        "opacity": int(legacy.opacity),
        "position_mode": str(legacy.position_mode),
    })
    if getattr(legacy, "image", ""):
        template["image"] = str(legacy.image)
    return template


def plan_categories(old_cfg, data_root: Path) -> tuple[list, list[str], list[str]]:
    """规划新分类集合。

    Returns:
        (新分类列表, 实际登记到的目录名, 缺失的目录名)。

    幂等要点：目录已登记过 → **沿用原分类对象**（id / 皮肤 / 尺寸都被保留），
    这样重复执行脚本不会把用户调过的皮肤重置，也不会让启动器 .lnk 反复重建。
    """
    import uuid

    from app.config import CategoryConfig  # 延迟导入：--help 场景不依赖项目包

    template = skin_template(old_cfg)
    cats: list = []
    registered: list[str] = []
    missing: list[str] = []
    for dir_name in CATEGORY_DIRS:
        if not (data_root / dir_name).is_dir():
            missing.append(dir_name)
            continue
        existing = old_cfg.find_by_dir(dir_name)
        if existing is not None:
            cats.append(existing)      # 已有分类：原样保留（含 id / 皮肤）
            registered.append(dir_name)
            continue
        cat = CategoryConfig(
            # id 必须显式生成：CategoryConfig 直接构造不会自动分配 id，
            # 空 id 会让 6 个分类在 plan/state 里互相覆盖（踩过）
            id=f"cat_{uuid.uuid4().hex[:8]}",
            dir=dir_name,
            label=dir_name,
            icon_px=template["icon_px"],
            panel_w=template["panel_w"],
            panel_h=template["panel_h"],
            radius=template["radius"],
            bg_color=template["bg_color"],
            opacity=template["opacity"],
            position_mode=template["position_mode"],
            image=str(template.get("image", "")),
            visible=True,
        )
        cat.sanitize()
        cats.append(cat)
        registered.append(dir_name)
    return cats, registered, missing


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    dry_run = "--dry-run" in argv
    force = "--force" in argv

    from app import paths
    from app.config import AppConfig, DEFAULT_CATEGORY_EXCLUDE, SHORTCUT_MODE_LAUNCHER

    config_path = paths.CONFIG_PATH
    print(f"配置文件      : {config_path}")
    print(f"启动器目录    : {paths.LAUNCHERS_DIR}")
    print(f"目标数据根    : {TARGET_DATA_ROOT}")

    data_root = Path(TARGET_DATA_ROOT)
    if not data_root.is_dir():
        print(f"[中止] 数据根不存在或不是目录：{data_root}")
        print("      分类目录无法确认，脚本不做任何改动。")
        return 1

    # 运行中的旧实例会在退出时把旧配置写回磁盘 → 必须先退出再迁移
    if desktopbox_running() and not dry_run:
        if not force:
            print("[中止] 检测到 DesktopBox 正在运行。")
            print("      运行中的实例内存里是旧配置，它退出时会把旧配置写回，")
            print("      迁移结果会被静默覆盖。请先：托盘图标 → 退出，然后重跑本脚本。")
            print("      （确知风险仍要继续可加 --force）")
            return 2
        print("[警告] DesktopBox 正在运行：迁移后请**立即**让它退出并重启新版，")
        print("       否则旧实例退出时可能覆盖新配置。")

    # ---- 1. 备份（必须先成功） -----------------------------------------
    backup = None
    if dry_run:
        print(f"[1/3] (dry-run) 将先备份配置到 {config_path.name}.bak-<YYYYmmdd-HHMMSS>")
    else:
        try:
            backup = backup_config(config_path)
        except OSError as exc:
            print(f"[中止] 备份配置失败（未改动任何配置）：{exc}")
            return 1
        if backup is None:
            print("提示：配置文件不存在（首次安装场景），无需备份。")
        else:
            print(f"[1/3] 配置已备份 → {backup}")
            if not backup.is_file() or backup.stat().st_size <= 0:
                print("[中止] 备份文件校验失败，脚本不做任何改动。")
                return 1

    # ---- 2. 生成新配置 --------------------------------------------------
    old_cfg = AppConfig.load(config_path)
    new_cfg = AppConfig.from_dict(old_cfg.to_dict())   # 深拷贝：不动 old_cfg 的对象
    new_cfg.app.data_root = TARGET_DATA_ROOT
    new_cfg.app.shortcut_mode = SHORTCUT_MODE_LAUNCHER
    new_cfg.app.category_exclude = list(DEFAULT_CATEGORY_EXCLUDE)

    cats, registered, missing = plan_categories(old_cfg, data_root)
    new_cfg.categories = cats
    new_cfg.config_path = config_path

    print(f"[2/3] 分类登记：{len(registered)} 个 → {', '.join(registered) or '（无）'}")
    if missing:
        print(f"      数据根下不存在的目录（跳过）：{', '.join(missing)}")
    print(f"      旧\"{LEGACY_SKIN_SOURCE_DIR}\"分类不再登记（旧配置已备份）")
    for cat in cats:
        print(f"      - {cat.label:<8} dir={cat.dir:<8} id={cat.id} "
              f"radius={cat.radius} bg={cat.bg_color} opacity={cat.opacity} "
              f"{cat.panel_w}x{cat.panel_h} {cat.position_mode}")

    if dry_run:
        print("[dry-run] 未写入配置、未生成启动器 .lnk。")
        return 0

    new_cfg.save()
    print(f"      新配置已写入：{config_path}")

    # ---- 3. 生成启动器 .lnk（offscreen QApplication） -------------------
    from PySide6.QtWidgets import QApplication

    from app.desktop_shortcuts import DesktopShortcutManager
    from app.icon_provider import IconCache

    app = QApplication.instance() or QApplication(sys.argv[:1])
    paths.set_data_root(TARGET_DATA_ROOT)     # 只影响内存中的路径常量，不产生 IO
    try:
        paths.ensure_base_dirs()              # 只创建 APP_DIR 系列（.launchers 等）
    except Exception as exc:  # noqa: BLE001 - 目录问题不该中断迁移
        print(f"      警告：创建程序目录时出错（已忽略）：{exc}")

    manager = DesktopShortcutManager(
        icon_cache=IconCache(strip_shortcut_overlay=new_cfg.app.strip_shortcut_overlay),
        ico_dir=paths.ICO_DIR,
        state_path=paths.LNK_STATE_PATH,
        project_root=paths.APP_ROOT,
        mode=new_cfg.app.shortcut_mode,
        launcher_dir=paths.LAUNCHERS_DIR,
    )
    manager.sync(new_cfg.categories)

    print(f"[3/3] 启动器 .lnk（共 {len(new_cfg.categories)} 个）→ {paths.LAUNCHERS_DIR}")
    print("      在 SAO Utils 2 里给每个 HUD 图标：Change Action → 选择对应 .lnk")
    missing_lnk: list[str] = []
    for cat_id, lnk in manager.launcher_paths(new_cfg.categories).items():
        flag = "OK " if lnk.is_file() else "缺失"
        if not lnk.is_file():
            missing_lnk.append(str(lnk))
        label = next((c.label for c in new_cfg.categories if c.id == cat_id), cat_id)
        print(f"      [{flag}] {label:<8} → {lnk}")

    print("\n配置关键字段：")
    print(f"      app.data_root         = {new_cfg.app.data_root!r}")
    print(f"      app.shortcut_mode     = {new_cfg.app.shortcut_mode!r}")
    print(f"      app.category_exclude  = {new_cfg.app.category_exclude}")
    print(f"      app.strip_extensions  = {new_cfg.app.strip_extensions}")
    print(f"      app.strip_shortcut_overlay = {new_cfg.app.strip_shortcut_overlay}")
    print(f"      分类数                = {len(new_cfg.categories)}")

    if missing_lnk:
        print("\n[警告] 以下启动器 .lnk 未能生成（检查日志 %s）：" % paths.LOG_FILE)
        for item in missing_lnk:
            print(f"      {item}")
        return 1
    print("\n迁移完成：桌面上的旧 DesktopBox 快捷方式已按「确认指向本项目」规则清理。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
