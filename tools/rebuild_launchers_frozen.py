"""B4 迁移辅助：用打包产物重建启动器 .lnk（并可选发布到 SAO 目录）。

用法（venv 内，QT 走 offscreen，不弹任何窗口）::

    .venv/Scripts/python.exe tools/rebuild_launchers_frozen.py \\
        --exe "E:\\desktopbox\\DeskCountChen\\DeskCountChen.exe" \\
        --app-dir "C:\\Users\\Z\\DeskCountChen" \\
        --publish-dir "E:\\快捷方式SAO"

做什么：
1. 从 ``--app-dir``（源码态 APP_DIR）读 .config.json 拿到分类清单与同步设置；
2. 构造 DesktopShortcutManager：python_exe = 产品 exe、script_path = None、
   project_root = exe 所在目录（等价于 frozen 态产品自己 sync 的结果，见
   app/desktop_shortcuts.py 的 frozen 分支），落点仍是 ``--app-dir`` 的
   ``.launchers``（源码态 paths.LAUNCHERS_DIR 的白名单语义不变）；
3. ``sync(categories)``：幂等重建全部启动器 .lnk（目标变 → sig 变 → 重写）；
4. ``--publish-dir`` 给定时，把 ``启动-*.lnk`` 覆盖复制过去（SAO HUD 引用）；
5. 打印新旧目标对照（read_link 反解析验证）。

注意：跑之前确认产品实例已退出（.config.json / lnk_state.json 不被并发写）。
"""

from __future__ import annotations

import argparse
import logging
import shutil
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

import os  # noqa: E402

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtWidgets import QApplication  # noqa: E402

from app.config import AppConfig  # noqa: E402
from app.desktop_shortcuts import DesktopShortcutManager  # noqa: E402
from app.icon_provider import IconCache  # noqa: E402
from app.win32.shell_resolve import read_link  # noqa: E402

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("rebuild_launchers")


def describe(lnk: Path) -> str:
    info = read_link(lnk)
    if not info.ok:
        return "<解析失败>"
    return f"{info.target} {info.arguments}"


def main() -> int:
    ap = argparse.ArgumentParser(description="用打包产物重建启动器 .lnk（B4）")
    ap.add_argument("--exe", required=True, help="产品 exe 路径（.lnk 的目标）")
    ap.add_argument("--app-dir", required=True,
                    help="源码态 APP_DIR（读 .config.json；.lnk 写到它的 .launchers）")
    ap.add_argument("--publish-dir", default=None,
                    help="可选：把 启动-*.lnk 覆盖复制到该目录（SAO HUD 引用处）")
    args = ap.parse_args()

    exe = Path(args.exe).resolve()
    if not exe.is_file():
        raise SystemExit(f"产品 exe 不存在: {exe}")
    app_dir = Path(args.app_dir).resolve()
    config_path = app_dir / ".config.json"
    if not config_path.is_file():
        raise SystemExit(f"配置不存在: {config_path}")

    cfg = AppConfig.load(config_path)
    publish_dir = Path(args.publish_dir).resolve() if args.publish_dir else None
    if publish_dir is not None and not publish_dir.is_dir():
        raise SystemExit(f"发布目录不存在: {publish_dir}")

    QApplication.instance() or QApplication(sys.argv[:1])
    icon_cache = IconCache(strip_shortcut_overlay=cfg.app.strip_shortcut_overlay)

    mgr = DesktopShortcutManager(
        icon_cache=icon_cache,
        ico_dir=app_dir / ".icons" / "ico",
        state_path=app_dir / ".icons" / "lnk_state.json",
        project_root=exe.parent,
        python_exe=str(exe),
        script_path=None,          # frozen 语义：参数只有 --open
        frozen_target=True,        # 源码进程里强制 frozen 语义（否则会被补成 main.py）
        mode=cfg.app.shortcut_mode,
        launcher_dir=app_dir / ".launchers",
    )

    before = {lnk.name: describe(lnk)
              for lnk in sorted((app_dir / ".launchers").glob("启动-*.lnk"))
              if (app_dir / ".launchers").is_dir()}
    mgr.sync(cfg.categories)
    after_paths = mgr.launcher_paths(cfg.categories)

    print("\n== 新旧 .lnk 目标对照 ==")
    for cat_id, lnk in sorted(after_paths.items(), key=lambda kv: kv[1].name):
        label = Path(lnk).name
        old = before.get(label, "<新建>")
        print(f"[{label}]\n  旧: {old}\n  新: {describe(lnk)}")

    published: list[Path] = []
    if publish_dir is not None:
        print(f"\n== 覆盖发布到 {publish_dir} ==")
        for lnk in after_paths.values():
            dest = publish_dir / Path(lnk).name
            shutil.copyfile(lnk, dest)
            published.append(dest)
            print(f"  {dest}  ←  {describe(dest)}")

    log.info("完成：重建 %d 个启动器，发布 %d 个", len(after_paths), len(published))
    return 0


if __name__ == "__main__":
    sys.exit(main())
