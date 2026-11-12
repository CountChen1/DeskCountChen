"""验收脚本：面板"全部显示"（v1.4.1 需求 A）。

用户原话："文件夹里有什么就能在面板里点到"。起因：``E:\\快捷方式SAO\\游戏加速``
实际 27 项，面板只显示 25 项，漏掉 ``Age of Empires II Definitive Edition.url``
与 ``帝国时代笔记.txt``（旧实现用 ``ALLOWED_SUFFIXES = {".lnk", ".exe"}`` 白名单）。

本脚本做三件事：
1. **真实数据只读取证**：直接扫真实数据根里的分类目录，断言条目数、条目名、
   显示名（去扩展名）、图标非空（像素级：存在非全透明像素）；
2. **启动链路**：``.txt`` 条目的启动走 ``window_ops.shell_open``（打桩断言参数），
   不真的弹记事本；
3. **拖拽落盘**：把真实目录**复制**到临时目录再拖，断言顺序写入临时目录的
   ``.order.json``，真实目录**零改动**。

现场保护（与其它脚本同一套）：``app.paths`` 重定向到临时沙箱；真实配置与真实
数据目录都只读一次取指纹，结束时断言未变。

用法：.venv\\Scripts\\python.exe tests\\verify_all_file_types.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path
from unittest import mock

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtGui import QColor, QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import paths  # noqa: E402
from app.panel_model import NAME_ROLE, PanelModel  # noqa: E402
from tests import sandbox  # noqa: E402
from app.folder_model import FolderModel  # noqa: E402
from app.icon_provider import IconCache  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
TARGET_CAT = "游戏加速"
IGNORED = (".order.json", "desktop.ini", "thumbs.db", ".ds_store")


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def dir_fingerprint(root: Path) -> list[tuple]:
    """目录指纹（名/大小/mtime），用于证明我们没有往真实目录写任何东西。"""
    out = []
    try:
        for p in sorted(root.iterdir()):
            try:
                st = p.stat()
                out.append((p.name, st.st_size, st.st_mtime_ns))
            except OSError:
                out.append((p.name, -1, -1))
    except OSError:
        pass
    return out


def visible_alpha_pixels(pm) -> int:
    """统计非全透明像素数（用来证伪"空白格"）。"""
    img = pm.toImage()
    count = 0
    for y in range(0, img.height(), 1):
        for x in range(0, img.width(), 1):
            if QColor(img.pixel(x, y)).alpha() > 8:
                count += 1
    return count


def main() -> int:
    # ---------- ① 只读基线：真实配置 + 真实数据目录 ----------
    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")
    try:
        real_cfg = json.loads(Path(real.config_path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        print(f"读取真实配置失败，无法验收：{exc}")
        return 1
    data_root = Path(str((real_cfg.get("app") or {}).get("data_root") or ""))
    if not data_root.is_dir():
        print(f"真实数据根不可用：{data_root!r}")
        return 1
    target_dir = data_root / TARGET_CAT
    if not target_dir.is_dir():
        print(f"目标分类目录不存在：{target_dir}")
        return 1
    files_on_disk = [p.name for p in target_dir.iterdir() if p.is_file()]
    fp_before = dir_fingerprint(target_dir)
    print(f"真实数据根：{data_root}")
    print(f"验收目录：{target_dir}（磁盘上 {len(files_on_disk)} 个文件）")

    # ---------- ② 沙箱：之后所有写入都落在临时目录 ----------
    sandbox_root = sandbox.sandbox_paths(prefix="dbx_alltypes_")
    paths.ensure_base_dirs()
    app = QApplication.instance() or QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)

    code = 0
    try:
        # ---------- ③ 真实目录扫描（只读） ----------
        fm = FolderModel(target_dir)
        entries = fm.reload()
        names = [e.name for e in entries]
        print(f"    面板条目（{len(names)}）：{names}")
        expected = [n for n in files_on_disk if n.casefold() not in IGNORED]
        check("面板条目数 == 目录实际文件数（全部显示）",
              len(names) == len(expected),
              f"{len(names)} vs 磁盘 {len(expected)}（磁盘原始 {len(files_on_disk)}）")
        check("条目名集合与磁盘一致（无遗漏/无多余）",
              sorted(names) == sorted(expected),
              f"缺 {sorted(set(expected) - set(names))} 多 {sorted(set(names) - set(expected))}")

        url_name = "Age of Empires II Definitive Edition.url"
        txt_name = "帝国时代笔记.txt"
        check(f"用户报告的漏项已收录：{url_name}", url_name in names)
        check(f"用户报告的漏项已收录：{txt_name}", txt_name in names)

        kinds = {e.name: e.kind for e in entries}
        check("'.url' 的 kind = file", kinds.get(url_name) == "file", str(kinds.get(url_name)))
        check("'.txt' 的 kind = file", kinds.get(txt_name) == "file", str(kinds.get(txt_name)))

        # ---------- ④ 面板模型：显示名去扩展名 ----------
        icons = IconCache()
        model = PanelModel(strip_extensions=True)
        model.build_slots(fm.slots, entries, icons, 48)
        shown = [str(model.model.item(r).data(NAME_ROLE)) for r in range(model.model.rowCount())]
        texts = [model.model.item(r).text() for r in range(model.model.rowCount())]
        by_real = dict(zip(shown, texts))
        check("'.url' 显示为去掉扩展名的名字",
              by_real.get(url_name) == "Age of Empires II Definitive Edition",
              repr(by_real.get(url_name)))
        check("'.txt' 显示为去掉扩展名的名字",
              by_real.get(txt_name) == "帝国时代笔记", repr(by_real.get(txt_name)))
        check("NAME_ROLE 仍是真实文件名（启动/落盘不受影响）",
              url_name in shown and txt_name in shown)

        # ---------- ⑤ 图标：非空 + 像素级证据 ----------
        for name in (url_name, txt_name):
            path = target_dir / name
            pm = icons.pixmap(path, 48)
            pixels = visible_alpha_pixels(pm) if not pm.isNull() else 0
            check(f"图标非空且非空白格：{name}", (not pm.isNull()) and pixels > 0,
                  f"{pm.width()}x{pm.height()} 可见像素={pixels}")

        # ---------- ⑥ 启动链路（打桩，不真弹记事本） ----------
        from app import controller as controller_mod

        txt_entry = next(e for e in entries if e.name == txt_name)
        with mock.patch.object(controller_mod.window_ops, "shell_open") as fake_open:
            controller_mod.AppController._launch_entry(object(), txt_entry)
        check("双击 .txt 走 shell_open(真实路径)", fake_open.call_count == 1,
              f"调用 {fake_open.call_count} 次")
        if fake_open.call_count == 1:
            check("传给启动链路的正是真实文件名（含扩展名）",
                  Path(fake_open.call_args[0][0]) == target_dir / txt_name,
                  str(fake_open.call_args[0][0]))

        # ---------- ⑦ 拖拽落盘：复制到临时目录再做，真实目录零改动 ----------
        from app.windows.panel_grid import PanelGrid

        copy_dir = Path(tempfile.mkdtemp(prefix="dbx_alltypes_copy_", dir=sandbox_root)) / TARGET_CAT
        shutil.copytree(target_dir, copy_dir)
        fm2 = FolderModel(copy_dir)
        entries2 = fm2.reload()
        model2 = PanelModel(strip_extensions=True)
        model2.build_slots(fm2.slots, entries2, icons, 48)
        grid = PanelGrid(item_px=48, icon_cache=icons)
        grid.set_model(model2)
        # 与 controller._do_save_order 等价（生产里 200ms 去抖由 controller 负责）
        grid.order_changed.connect(lambda: fm2.save_slots(model2.serialize_slots()))
        before_slots = list(model2.serialize_slots())
        target_row = min(2, len(before_slots) - 1)
        grid._apply_slot_move([0], target_row)      # 把第一个条目拖到第 3 行位置
        after_slots = list(model2.serialize_slots())
        order_file = copy_dir / ".order.json"
        window_ops_clear = getattr(sys.modules.get("app.win32.window_ops"), "clear_file_hidden", None)
        if window_ops_clear is not None:
            window_ops_clear(order_file)   # 隐藏属性会让读取失败
        saved = (json.loads(order_file.read_text(encoding="utf-8"))["slots"]
                 if order_file.exists() else [])
        check("非 .lnk/.exe 条目可参与拖拽排序（落盘顺序生效）",
              saved == after_slots and saved != before_slots,
              f"{before_slots[:3]}… → {saved[:3]}…")
        check("拖拽后布局仍密集（无空位）", None not in saved and "" not in saved)
        grid.deleteLater()

        # ---------- ⑧ 系统文件不进面板 ----------
        for ignored in ("desktop.ini", "thumbs.db", ".order.json"):
            check(f"{ignored} 不出现在面板里", ignored not in names)

    finally:
        app.quit()
        fp_after = dir_fingerprint(target_dir)
        check("真实分类目录零改动（未写入 .order.json / 未改任何文件）",
              fp_before == fp_after,
              f"{len(fp_before)} → {len(fp_after)} 个文件"
              + ("" if fp_before == fp_after else "（有变化！）"))
        shutil.rmtree(sandbox_root, ignore_errors=True)
        check("沙箱目录已删除", not sandbox_root.exists(), str(sandbox_root))
        ok, detail = sandbox.guard_real_untouched(real)
        check("真实用户配置零改动（sha256 一致）", ok, detail)
        if not ok:
            code = 1

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("全部文件类型显示验收通过")
    return code


if __name__ == "__main__":
    sys.exit(main())
