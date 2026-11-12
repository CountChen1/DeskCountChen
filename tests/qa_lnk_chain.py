"""QA 独立验证：.lnk / .ico 全生命周期（真机 COM 写盘）。

覆盖（落点是一个**真实存在的临时目录**，走真实 COM/IPersistFile 写盘）：
1. 新建分类 → 落点目录出现 `<分类名>.lnk`（目标 pythonw + main.py + --open + .ico）；
2. 幂等：无变化时再次 sync 不重写 .lnk（mtime/大小不变，桌面图标不会被系统重排）；
3. 删掉 .ico → 再 sync 会重建 ico 并重写 .lnk；
4. 隐藏分类 → .lnk 被删除；重新显示 → 重建；
5. 重命名 → 旧名 .lnk 删除、新名生成；
6. 改皮肤（换图片）→ ico 与 .lnk 都重建（大小/内容指纹变化）；
7. 删除分类 → .lnk 与 ico 缓存被清理；
8. 分类目录名含中文 / 空格 / `&` / `#` 时 .lnk 参数引号正确（按 UTF-16LE 在
   .lnk 字节里核对 `--open "<目录名>"`）。

现场保护（2026-11-12 事故后重做）：
- ``app.paths`` 整个命名空间重定向到临时沙箱（配置 / .icons / .ico / 数据根 /
  .launchers），真实 ``%USERPROFILE%\\DesktopBox`` 只读一次取 sha256 指纹；
- 落点不用真实桌面 —— 用沙箱里的临时目录 + 真实 COM（测的是"写 .lnk 的能力"，
  不是"是否恰好叫桌面"）；真实桌面只做**只读**核对：前后 .lnk 集合必须一致；
- 结束时删沙箱，并断言真实配置 sha256 未变；
- **不再使用"备份 + 无条件还原"**：finally 不保证执行，崩溃/被杀会留下污染。

用法：.venv\\Scripts\\python.exe tests\\qa_lnk_chain.py
"""

from __future__ import annotations

import os
import shutil
import sys
import time
import traceback
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtWidgets import QApplication  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前完成重定向
from app.desktop_shortcuts import plan_lnk_names  # noqa: E402
from app.win32 import shell_link  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TEMP_DIR = "QA链接 中文&测试#1"
TEMP_LABEL = "QA链接 中文&测试#1"


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def stat_of(path: Path) -> tuple[int, int] | None:
    try:
        st = path.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def lnk_bytes(path: Path) -> bytes:
    try:
        return path.read_bytes()
    except OSError:
        return b""


def has_utf16(data: bytes, text: str) -> bool:
    return text.encode("utf-16-le") in data


def main() -> int:
    # ---------- ① 只读基线：真实配置 + 真实桌面 .lnk 集合 ----------
    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")

    real_desktop = shell_link.desktop_dir()
    if real_desktop is None or not Path(real_desktop).is_dir():
        print("跳过：真实桌面不可用")
        return 0
    real_desktop = Path(real_desktop)
    existing_lnks = {p.name for p in real_desktop.glob("*.lnk")}
    print(f"真实桌面 {real_desktop}：已有 .lnk {len(existing_lnks)} 个（只读，不写入）")

    # ---------- ② 沙箱：配置 / 图标 / 数据根 / 启动器全部重定向 ----------
    sandbox_root = sandbox.sandbox_paths(prefix="dbx_qa_lnk_chain_")
    paths.ensure_base_dirs()
    print(f"沙箱数据根：{paths.DATA_ROOT}")

    # ---------- ③ 落点用沙箱内的临时目录（真实 NTFS + 真实 COM，不动桌面） ----------
    desktop = sandbox_root / "Desktop"
    desktop.mkdir(parents=True, exist_ok=True)
    print(f"落点目录（临时）: {desktop}")

    shutil.rmtree(paths.ROOT / TEMP_DIR, ignore_errors=True)
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    controller = None
    try:
        from app.controller import AppController

        controller = AppController(app)
        # v1.4：默认落点已是私有 .launchers；本脚本验的是"外部落点"这条仍然保留
        # 的可回退链路（app.shortcut_mode="desktop"），所以显式切回桌面模式并
        # 把 desktop_dir 指向沙箱目录。
        controller.shortcuts.mode = "desktop"
        controller.shortcuts.desktop_dir = desktop
        controller.cfg.categories = [c for c in controller.cfg.categories
                                     if c.dir != TEMP_DIR]  # 防上次残留
        controller.start()
        pump(app, 1.0)

        # ---------- 1. 新建分类 → 生成 .lnk + .ico ----------
        paths.ensure_category_dir(TEMP_DIR)
        cat = controller.cfg.add_category(TEMP_DIR, TEMP_LABEL)
        controller._after_categories_changed()
        pump(app, 0.6)

        plan = plan_lnk_names(controller.cfg.categories)
        lnk_name = plan[cat.id]
        lnk = desktop / lnk_name
        ico = controller.shortcuts.ico_dir / f"{cat.id}.ico"
        print(f"    规划 .lnk 名 = {lnk_name}")
        check("新建分类后落点目录出现 .lnk", lnk.exists(), str(lnk))
        check("分类 .ico 已生成", ico.exists() and ico.stat().st_size > 0, str(ico))

        data = lnk_bytes(lnk)
        check(".lnk 含 main.py 路径", has_utf16(data, "main.py"), f"{len(data)} bytes")
        check(".lnk 含 --open 参数", has_utf16(data, "--open"), "")
        check(".lnk 含带引号的分类目录名（中文/空格/&/# 均正确）",
              has_utf16(data, f'"{TEMP_DIR}"'), repr(f'"{TEMP_DIR}"'))
        check(".lnk 目标为 pythonw.exe", has_utf16(data, "pythonw.exe"),
              repr(data[:0]))
        check(".lnk 图标指向该分类 .ico", has_utf16(data, ico.name), ico.name)

        # ---------- 2. 幂等：内容没变就不重写 ----------
        before_stat = stat_of(lnk)
        ico_before = stat_of(ico)
        controller.shortcuts.sync(controller.cfg.categories)
        controller.shortcuts.sync(controller.cfg.categories)
        pump(app, 0.3)
        check("幂等：重复同步不改写 .lnk（mtime/大小不变）",
              stat_of(lnk) == before_stat, f"{before_stat} → {stat_of(lnk)}")
        check("幂等：重复同步不重写 .ico",
              stat_of(ico) == ico_before, f"{ico_before} → {stat_of(ico)}")

        # ---------- 3. 删掉 ico → 重建 ico 并重写 .lnk ----------
        ico.unlink()
        pump(app, 0.1)
        controller.shortcuts.sync(controller.cfg.categories)
        pump(app, 0.3)
        check("ico 被删后能重建", ico.exists() and ico.stat().st_size > 0, str(ico))
        check("ico 重建后 .lnk 被重写（mtime 变化）",
              stat_of(lnk) != before_stat, f"{before_stat} → {stat_of(lnk)}")

        # ---------- 4. 隐藏 → 删除；显示 → 重建 ----------
        cat.visible = False
        controller._after_categories_changed()
        pump(app, 0.4)
        check("隐藏分类后 .lnk 被删除", not lnk.exists(), str(lnk))
        cat.visible = True
        controller._after_categories_changed()
        pump(app, 0.4)
        check("重新显示后 .lnk 被重建", lnk.exists(), str(lnk))

        # ---------- 5. 重命名 → 旧名删、新名建 ----------
        old_lnk = desktop / plan[cat.id]
        cat.label = "QA重命名 新名"
        cat.sanitize()
        controller._after_categories_changed()
        pump(app, 0.5)
        new_name = plan_lnk_names(controller.cfg.categories)[cat.id]
        new_lnk = desktop / new_name
        print(f"    重命名：{old_lnk.name} → {new_name}")
        check("重命名：旧名 .lnk 被删除", not old_lnk.exists(), old_lnk.name)
        check("重命名：新名 .lnk 已生成", new_lnk.exists(), new_name)
        check("重命名：新 .lnk 仍指向原分类目录",
              has_utf16(lnk_bytes(new_lnk), f'"{TEMP_DIR}"'), TEMP_DIR)

        # ---------- 6. 改皮肤 → ico 与 .lnk 都重建 ----------
        skin_src = sandbox_root / "qa_skin.png"
        from PySide6.QtGui import QColor, QPixmap

        pm = QPixmap(64, 64)
        pm.fill(QColor("#2E86DE"))
        pm.save(str(skin_src), "PNG")
        old_ico_stat = stat_of(ico)
        old_lnk_stat = stat_of(new_lnk)
        if True:
            dest_img = paths.ICONS_DIR / f"{cat.id}.png"
            shutil.copyfile(skin_src, dest_img)
            cat.image = str(dest_img)
            controller._after_categories_changed()
            pump(app, 0.6)
            check("改皮肤后 .ico 被重建",
                  stat_of(ico) != old_ico_stat, f"{old_ico_stat} → {stat_of(ico)}")
            check("改皮肤后 .lnk 被重建",
                  stat_of(new_lnk) != old_lnk_stat, f"{old_lnk_stat} → {stat_of(new_lnk)}")
        else:
            check("改皮肤后 .ico 被重建", False, "截图失败，无法构造皮肤图")

        # ---------- 7. 删除分类 → .lnk / ico 清理 ----------
        removed_id = cat.id
        controller.cfg.categories = [c for c in controller.cfg.categories if c.id != removed_id]
        controller._after_categories_changed()
        pump(app, 0.5)
        check("删除分类后 .lnk 被清理", not new_lnk.exists(), new_name)
        check("删除分类后 .ico 缓存被清理", not ico.exists(), str(ico))

    except Exception:  # noqa: BLE001 - 任何异常都必须留下现场并还原
        traceback.print_exc()
        RESULTS.append(("脚本未抛异常", False, "见上方堆栈"))
    finally:
        try:
            if controller is not None:
                controller.shutdown()
        except Exception:  # noqa: BLE001
            traceback.print_exc()
        # 拆沙箱（落点 .lnk、ico、配置、分类目录都在里面，一并消失）
        shutil.rmtree(sandbox_root, ignore_errors=True)
        check("沙箱（含临时落点目录）已删除", not sandbox_root.exists(), str(sandbox_root))
        # 真实桌面只读核对：一个 .lnk 都不该多、也不该少
        after_lnks = {p.name for p in real_desktop.glob("*.lnk")}
        check("真实桌面零改动（.lnk 集合与运行前一致）",
              after_lnks == existing_lnks,
              f"多出 {sorted(after_lnks - existing_lnks)} 少了 {sorted(existing_lnks - after_lnks)}")
        # 真实配置零改动（不依赖任何"还原"动作）
        check("真实用户配置零改动（sha256 一致）", *sandbox.guard_real_untouched(real))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print(".lnk / .ico 链路全部通过（真实桌面零改动）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
