"""分类快捷方式（.lnk + .ico）全生命周期单元测试。

用临时目录当"桌面"，所以**不会**动用户真实桌面；真正落盘的 .lnk 用
Shell 的 IShellLinkW 写，因此这里连 COM 写盘链路一起验证。

覆盖：
- 文件名清洗与重名去重；
- desktop 模式（v1.3 行为）：可见分类 → 建 .lnk + 建 .ico，内容里能看到
  pythonw / main.py / --open；
- launcher 模式（v1.4 默认）：只写 ``launcher_dir/启动-<分类>.lnk``，**桌面目录
  一个字节都不碰**；
- 隐藏分类 → 删 .lnk；重命名 → 删旧名建新名；皮肤变化 → 重建 .ico 与 .lnk；
  无变化 → 完全不碰文件（避免图标重排）；
- 迁移清理：桌面上的本项目 .lnk 会被删；**不是本项目的不删**；
- ICO 结构校验（Qt 单图路径与自研多尺寸路径都验一遍）。
"""

from __future__ import annotations

import json
import logging
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PySide6.QtCore import QSize  # noqa: E402
from PySide6.QtGui import QColor, QPixmap  # noqa: E402

from app.config import CategoryConfig  # noqa: E402
from app.desktop_shortcuts import (  # noqa: E402
    DesktopShortcutManager,
    _write_ico,
    _write_ico_multi,
    launcher_file_name,
    plan_lnk_names,
    sanitize_stem,
    validate_ico,
)
from app.icon_provider import IconCache  # noqa: E402
from app.win32 import shell_link, shell_resolve  # noqa: E402
from tests.qt_support import qt_app  # noqa: E402

LNK_MAGIC = b"\x4c\x00\x00\x00"  # ShellLinkHeader 的 HeaderSize = 0x0000004C


def utf16(text: str) -> bytes:
    """UTF-16LE 字节（.lnk 里的字符串都是这个编码，便于断言内容）。"""
    return text.encode("utf-16-le")


class ShortcutManagerTestBase(unittest.TestCase):
    @classmethod
    def setUpClass(cls) -> None:
        qt_app()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_lnk_")
        root = Path(self._tmp.name)
        self.desktop = root / "Desktop"
        self.launchers = root / ".launchers"
        self.ico_dir = root / ".icons" / "ico"
        self.state = root / ".icons" / "lnk_state.json"
        self.project = root / "project"
        for path in (self.desktop, self.ico_dir, self.project):
            path.mkdir(parents=True, exist_ok=True)
        self.script = self.project / "main.py"
        self.script.write_text("# stub\n", encoding="utf-8")
        self.python_exe = str(Path(sys.executable))
        # 默认按 v1.3 的 desktop 模式建 manager：本文件的 LnkLifecycleTests 验的是
        # 桌面落点这条老链路（仍然保留、可回退）；launcher 模式另有专门用例。
        self.manager = DesktopShortcutManager(
            icon_cache=IconCache(),
            ico_dir=self.ico_dir,
            state_path=self.state,
            project_root=self.project,
            desktop_dir=self.desktop,
            python_exe=self.python_exe,
            script_path=self.script,
            mode="desktop",
            launcher_dir=self.launchers,
        )

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- 辅助 ---------------------------------------------------------------

    def make_cat(self, label: str = "工作", cat_id: str = "cat_test01",
                 visible: bool = True, image: str = "") -> CategoryConfig:
        cat = CategoryConfig(id=cat_id, dir=label, label=label, visible=visible, image=image)
        cat.sanitize()
        return cat

    def lnk_files(self) -> list[str]:
        return sorted(p.name for p in self.desktop.iterdir() if p.suffix.lower() == ".lnk")

    def make_png(self, name: str = "skin.png") -> Path:
        pm = QPixmap(64, 64)
        pm.fill(QColor("#3366CC"))
        path = Path(self._tmp.name) / name
        self.assertTrue(pm.save(str(path), "PNG"))
        return path


class NamePlanTests(unittest.TestCase):
    def test_sanitize_stem_replaces_illegal_chars(self) -> None:
        self.assertEqual(sanitize_stem('a\\b/c:d*e?f"g<h>i|j'), "a_b_c_d_e_f_g_h_i_j")

    def test_sanitize_stem_fallbacks(self) -> None:
        self.assertEqual(sanitize_stem(""), "分类")
        self.assertEqual(sanitize_stem("   "), "分类")
        self.assertEqual(sanitize_stem("..."), "分类")
        self.assertLessEqual(len(sanitize_stem("长" * 200)), 60)

    def test_plan_names_dedup_case_insensitive(self) -> None:
        cats = [CategoryConfig(id="a", dir="d1", label="工作"),
                CategoryConfig(id="b", dir="d2", label="工作"),
                CategoryConfig(id="c", dir="d3", label="WORK")]
        plan = plan_lnk_names(cats)
        self.assertEqual(plan["a"], "工作.lnk")
        self.assertEqual(plan["b"], "工作 (2).lnk")
        self.assertEqual(plan["c"], "WORK.lnk")
        self.assertEqual(len({v.casefold() for v in plan.values()}), 3)


class LnkLifecycleTests(ShortcutManagerTestBase):
    def test_sync_creates_lnk_and_ico(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])

        self.assertEqual(self.lnk_files(), ["工作.lnk"])
        lnk = self.desktop / "工作.lnk"
        data = lnk.read_bytes()
        self.assertTrue(data.startswith(LNK_MAGIC), "ShellLinkHeader 魔数不对")
        # 目标解释器：IShellLink 常把目标存成"相对路径 + ANSI 全路径"，
        # 因此这里既看 UTF-16 也看 ANSI 两种编码
        self.assertTrue(utf16(self.python_exe) in data or b"python.exe" in data
                        or b"pythonw.exe" in data, ".lnk 目标必须是 python( w ).exe")
        self.assertIn(utf16(str(self.script)), data, ".lnk 参数里必须带 main.py 全路径")
        self.assertIn(utf16('--open "工作"'), data, ".lnk 参数必须是 --open <分类目录名>")
        self.assertIn(utf16(str(self.project)), data, ".lnk 工作目录必须是项目根")

        ico = self.ico_dir / "cat_test01.ico"
        self.assertTrue(ico.exists(), "分类 ICO 未生成")
        self.assertTrue(validate_ico(ico), "生成的 ICO 结构非法")
        self.assertIn(utf16(str(ico)), data, ".lnk 图标必须指向生成的 ICO")

    def test_hidden_category_removes_lnk(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        self.assertTrue((self.desktop / "工作.lnk").exists())

        cat.visible = False
        self.manager.sync([cat])
        self.assertEqual(self.lnk_files(), [], "隐藏分类必须删除桌面 .lnk")

    def test_show_again_recreates_lnk(self) -> None:
        cat = self.make_cat("工作", visible=False)
        self.manager.sync([cat])
        self.assertEqual(self.lnk_files(), [])
        cat.visible = True
        self.manager.sync([cat])
        self.assertEqual(self.lnk_files(), ["工作.lnk"])

    def test_rename_replaces_old_lnk(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        cat.label = "工作区"
        self.manager.sync([cat])
        self.assertEqual(self.lnk_files(), ["工作区.lnk"], "重命名后旧 .lnk 必须删除")

    def test_second_sync_is_noop(self) -> None:
        """内容没变时不得重写桌面文件（否则 Windows 会重排图标位置）。"""
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        lnk = self.desktop / "工作.lnk"
        ico = self.ico_dir / "cat_test01.ico"
        lnk_stat = lnk.stat()
        ico_stat = ico.stat()

        self.manager.sync([cat])
        again = lnk.stat()
        self.assertEqual(again.st_mtime_ns, lnk_stat.st_mtime_ns, ".lnk 被无谓重写了")
        self.assertEqual(ico.stat().st_mtime_ns, ico_stat.st_mtime_ns, ".ico 被无谓重写了")

    def test_image_change_rebuilds_ico_and_lnk(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        ico = self.ico_dir / "cat_test01.ico"
        old_ico = ico.read_bytes()
        lnk = self.desktop / "工作.lnk"
        old_mtime = lnk.stat().st_mtime_ns

        cat.image = str(self.make_png())
        self.manager.sync([cat])
        self.assertNotEqual(ico.read_bytes(), old_ico, "皮肤变化后 ICO 必须重建")
        self.assertNotEqual(lnk.stat().st_mtime_ns, old_mtime,
                            "ICO 变化后 .lnk 必须重建（否则 Windows 显示旧图标）")

    def test_removed_category_lnk_is_deleted(self) -> None:
        cat_a = self.make_cat("工作", cat_id="cat_a")
        cat_b = self.make_cat("娱乐", cat_id="cat_b")
        self.manager.sync([cat_a, cat_b])
        self.assertEqual(len(self.lnk_files()), 2)
        self.manager.sync([cat_a])                      # cat_b 从配置里消失
        self.assertEqual(self.lnk_files(), ["工作.lnk"])

    def test_default_icon_also_produces_ico(self) -> None:
        cat = self.make_cat("无皮肤")
        self.manager.sync([cat])
        ico = self.ico_dir / "cat_test01.ico"
        self.assertTrue(ico.exists())
        self.assertTrue(validate_ico(ico))

    def test_missing_desktop_dir_does_not_raise(self) -> None:
        manager = DesktopShortcutManager(
            icon_cache=IconCache(),
            ico_dir=self.ico_dir,
            state_path=self.state,
            project_root=self.project,
            desktop_dir=self.desktop / "不存在",
            python_exe=self.python_exe,
            script_path=self.script,
            mode="desktop",
            launcher_dir=self.launchers,   # 必须显式给：默认值会写到真实 .launchers
        )
        manager.sync([self.make_cat("工作")])           # 目录不存在：只记日志
        self.assertEqual(self.lnk_files(), [])


class LauncherModeTests(ShortcutManagerTestBase):
    """v1.4：launcher 模式只写私有启动器目录，绝不触碰桌面。"""

    def setUp(self) -> None:
        super().setUp()
        self.manager = DesktopShortcutManager(
            icon_cache=IconCache(),
            ico_dir=self.ico_dir,
            state_path=self.state,
            project_root=self.project,
            desktop_dir=self.desktop,          # 仍然注入，用来断言"没被碰过"
            python_exe=self.python_exe,
            script_path=self.script,
            mode="launcher",
            launcher_dir=self.launchers,
        )

    def launcher_files(self) -> list[str]:
        if not self.launchers.is_dir():
            return []
        return sorted(p.name for p in self.launchers.iterdir() if p.suffix.lower() == ".lnk")

    def test_sync_writes_launcher_and_not_desktop(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])

        self.assertEqual(self.launcher_files(), ["启动-工作.lnk"])
        self.assertEqual(self.lnk_files(), [], "launcher 模式绝不能往桌面写 .lnk")

        lnk = self.launchers / "启动-工作.lnk"
        data = lnk.read_bytes()
        self.assertTrue(data.startswith(LNK_MAGIC), "ShellLinkHeader 魔数不对")
        self.assertIn(utf16(str(self.script)), data)
        self.assertIn(utf16('--open "工作"'), data, "启动器 .lnk 必须带 --open 分类目录名")
        self.assertTrue(validate_ico(self.ico_dir / "cat_test01.ico"))

    def test_state_records_where_launcher(self) -> None:
        self.manager.sync([self.make_cat("工作")])
        state = json.loads(self.state.read_text(encoding="utf-8"))
        record = state["categories"]["cat_test01"]
        self.assertEqual(record["where"], "launcher")
        self.assertEqual(record["lnk"], "启动-工作.lnk")

    def test_second_sync_is_noop_in_launcher_mode(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        lnk = self.launchers / "启动-工作.lnk"
        before = lnk.stat().st_mtime_ns
        self.manager.sync([cat])
        self.assertEqual(lnk.stat().st_mtime_ns, before, "启动器 .lnk 被无谓重写了")

    def test_launcher_rename_replaces_old_file(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        cat.label = "工作区"
        self.manager.sync([cat])
        self.assertEqual(self.launcher_files(), ["启动-工作区.lnk"])

    def test_hidden_category_removes_launcher(self) -> None:
        cat = self.make_cat("工作")
        self.manager.sync([cat])
        cat.visible = False
        self.manager.sync([cat])
        self.assertEqual(self.launcher_files(), [])

    def test_removed_category_launcher_is_deleted(self) -> None:
        cat_a = self.make_cat("工作", cat_id="cat_a")
        cat_b = self.make_cat("娱乐", cat_id="cat_b")
        self.manager.sync([cat_a, cat_b])
        self.assertEqual(len(self.launcher_files()), 2)
        self.manager.sync([cat_a])
        self.assertEqual(self.launcher_files(), ["启动-工作.lnk"])

    def test_launcher_paths_matches_plan(self) -> None:
        cats = [self.make_cat("工作", cat_id="cat_a"), self.make_cat("娱乐", cat_id="cat_b")]
        paths = self.manager.launcher_paths(cats)
        self.assertEqual(paths["cat_a"], self.launchers / "启动-工作.lnk")
        self.assertEqual(paths["cat_b"], self.launchers / "启动-娱乐.lnk")

    def test_launcher_file_name_is_idempotent(self) -> None:
        self.assertEqual(launcher_file_name("工作.lnk"), "启动-工作.lnk")
        self.assertEqual(launcher_file_name("启动-工作.lnk"), "启动-工作.lnk")


class LegacyDesktopCleanupTests(ShortcutManagerTestBase):
    """迁移清理：只删"确认指向本项目"的旧桌面 .lnk。"""

    def _write_launcher_manager(self) -> DesktopShortcutManager:
        return DesktopShortcutManager(
            icon_cache=IconCache(),
            ico_dir=self.ico_dir,
            state_path=self.state,
            project_root=self.project,
            desktop_dir=self.desktop,
            python_exe=self.python_exe,
            script_path=self.script,
            mode="launcher",
            launcher_dir=self.launchers,
        )

    def _seed_state(self, records: dict) -> None:
        self.state.parent.mkdir(parents=True, exist_ok=True)
        self.state.write_text(
            json.dumps({"version": 1, "categories": records}, ensure_ascii=False),
            encoding="utf-8",
        )

    def test_own_desktop_lnk_is_removed_on_migration(self) -> None:
        own = self.desktop / "常用.lnk"
        self.assertTrue(shell_link.create_shortcut(
            dest=own, target=self.python_exe,
            arguments=f'"{self.script}" --open "常用"',
            working_dir=str(self.project),
        ))
        self._seed_state({"cat_old": {"lnk": "常用.lnk", "sig": "x", "ico_sig": "default"}})

        self._write_launcher_manager().sync([self.make_cat("工作")])

        self.assertFalse(own.exists(), "指向本项目的旧桌面 .lnk 应被清理")
        state = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertNotIn("cat_old", state["categories"], "旧记录应被同步逻辑淘汰")

    def test_foreign_desktop_lnk_is_never_deleted(self) -> None:
        """安全红线：桌面上不是本项目创建的 .lnk 一个都不能删。"""
        foreign = self.desktop / "常用.lnk"
        system32 = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32"
        self.assertTrue(shell_link.create_shortcut(
            dest=foreign,
            target=str(system32 / "notepad.exe"),
            arguments="",
            working_dir=str(system32),
        ))
        self._seed_state({"cat_old": {"lnk": "常用.lnk", "sig": "x", "ico_sig": "default"}})

        self._write_launcher_manager().sync([self.make_cat("工作")])

        self.assertTrue(foreign.exists(), "目标不是 python(w).exe 的 .lnk 必须保留")

    def test_unparseable_desktop_file_is_kept(self) -> None:
        """同名但内容不是 .lnk（无法解析）→ 一律按用户文件保留。"""
        bogus = self.desktop / "常用.lnk"
        bogus.write_bytes(b"not a shell link")
        self._seed_state({"cat_old": {"lnk": "常用.lnk", "sig": "x", "ico_sig": "default"}})

        self._write_launcher_manager().sync([self.make_cat("工作")])

        self.assertTrue(bogus.exists(), "解析失败必须跳过（不能误删用户文件）")


class ShellResolveTests(ShortcutManagerTestBase):
    """v1.4：.lnk 只读解析（去箭头取图与迁移清理都依赖它）。"""

    def test_resolve_reads_target_and_arguments(self) -> None:
        dest = Path(self._tmp.name) / "probe.lnk"
        self.assertTrue(shell_link.create_shortcut(
            dest=dest,
            target=self.python_exe,
            arguments=f'"{self.script}" --open "自检"',
            working_dir=str(self.project),
        ))
        target, icon_location, icon_index = shell_resolve.resolve(dest)
        self.assertTrue(target, "GetPath 应解析出目标")
        self.assertEqual(Path(target).name.casefold(), Path(self.python_exe).name.casefold())
        # 未设置自定义图标时 GetIconLocation 给空路径（序号由 Shell 决定，不做断言）
        self.assertEqual(icon_location, "")
        self.assertIsInstance(icon_index, int)
        info = shell_resolve.read_link(dest)
        self.assertIn("--open", info.arguments)
        self.assertIn('"自检"', info.arguments)

    def test_resolve_reports_icon_location_when_set(self) -> None:
        dest = Path(self._tmp.name) / "icon_probe.lnk"
        icon_file = self.ico_dir / "probe.ico"
        self.ico_dir.mkdir(parents=True, exist_ok=True)
        pm = QPixmap(64, 64)
        pm.fill(QColor("#445566"))
        self.assertTrue(_write_ico(icon_file, pm))
        self.assertTrue(shell_link.create_shortcut(
            dest=dest, target=self.python_exe, arguments="", working_dir=str(self.project),
            icon_path=str(icon_file),
        ))
        target, icon_location, icon_index = shell_resolve.resolve(dest)
        self.assertTrue(target)
        self.assertEqual(Path(icon_location).name.casefold(), icon_file.name.casefold())
        self.assertEqual(icon_index, 0)

    def test_resolve_missing_file_returns_empty(self) -> None:
        self.assertEqual(shell_resolve.resolve(Path(self._tmp.name) / "无.lnk"), ("", "", -1))

    def test_resolve_non_lnk_suffix_returns_empty(self) -> None:
        plain = Path(self._tmp.name) / "a.txt"
        plain.write_text("x", encoding="utf-8")
        self.assertEqual(shell_resolve.resolve(plain), ("", "", -1))


class IcoWriterTests(ShortcutManagerTestBase):
    def test_qt_ico_writer_output_is_valid(self) -> None:
        pm = QPixmap(256, 256)
        pm.fill(QColor("#228844"))
        dest = Path(self._tmp.name) / "single.ico"
        self.assertTrue(_write_ico(dest, pm))
        self.assertTrue(validate_ico(dest))

    def test_default_writer_packs_multiple_sizes(self) -> None:
        """默认写出必须走多尺寸容器：小图标靠预渲染而不是缩放，体积也小一个量级。"""
        pm = QPixmap(256, 256)
        pm.fill(QColor("#3366cc"))
        dest = Path(self._tmp.name) / "sizes.ico"
        self.assertTrue(_write_ico(dest, pm))
        count = int.from_bytes(dest.read_bytes()[4:6], "little")
        self.assertGreaterEqual(count, 3, f"应打包多个尺寸，实际 {count}")
        self.assertLess(dest.stat().st_size, 200 * 1024,
                        "多尺寸 PNG 负载不该超过 200KB（Qt 单图 BMP 负载约 270KB）")

    def test_multi_size_writer_produces_multiple_entries(self) -> None:
        pm = QPixmap(256, 256)
        pm.fill(QColor("#884422"))
        dest = Path(self._tmp.name) / "multi.ico"
        self.assertTrue(_write_ico_multi(dest, pm, (256, 48, 32, 16)))
        self.assertTrue(validate_ico(dest))
        data = dest.read_bytes()
        reserved, kind, count = int.from_bytes(data[0:2], "little"), \
            int.from_bytes(data[2:4], "little"), int.from_bytes(data[4:6], "little")
        self.assertEqual((reserved, kind), (0, 1))
        self.assertEqual(count, 4, "多尺寸 ICO 应有 4 个目录项")

    def test_validate_ico_rejects_garbage(self) -> None:
        bad = Path(self._tmp.name) / "bad.ico"
        bad.write_bytes(b"not an ico at all")
        self.assertFalse(validate_ico(bad))
        self.assertFalse(validate_ico(Path(self._tmp.name) / "missing.ico"))


class PruneOrphanIconsTests(ShortcutManagerTestBase):
    """``_prune_orphan_icons`` 的安全边界（2026-11-12 误删事故后的回归）。

    事故：旧判据是"当前配置里没有这个 id 就删"。配置被整体替换/缩小（迁移换
    数据根、手工编辑 .config.json、读到损坏配置）时，用户其余分类的 .ico 会被
    当"孤儿缓存"删掉 —— 真实日志里 16:58:17 误删过 5 个。

    新语义：只删 **本次真正注销** 的分类（``sync()`` 收集的 ``removed_ids``）。
    下面 4 条是四条**语义不同**的用例，缺一条都等于没验到行为变更：

    ============ ========= ========= ================ ==============
    用例          state     cfg       磁盘             期望
    ============ ========= ========= ================ ==============
    注销         有 C      无 C      cat_C.ico        **删**
    从未管理过   无 B      无 B      cat_B.ico        **保留**
    cfg 为空     空        空        cat_A/B.ico      **都不动**
    隐藏         有 H      有 H(隐)  cat_H.ico        **文件保留**
    ============ ========= ========= ================ ==============
    """

    def _ico(self, cat_id: str) -> Path:
        path = self.ico_dir / f"{cat_id}.ico"
        path.write_bytes(b"\x00\x00\x01\x00stub")
        return path

    # -- 用例一：注销的分类 → 删 -------------------------------------------------

    def test_removed_category_icon_deleted(self) -> None:
        victim = self._ico("cat_deadbad1")
        survivor = self._ico("cat_alive001")
        # 走真实 sync 的注销路径：state 里有记录、cfg 里没有
        self.manager._save_state({
            "cat_deadbad1": {"lnk": "启动-旧分类.lnk", "sig": "s", "ico_sig": "i",
                             "where": "desktop"},
            "cat_alive001": {"lnk": "启动-在用的.lnk", "sig": "s", "ico_sig": "i",
                             "where": "desktop"},
        })
        self.manager.sync([self.make_cat(label="在用的", cat_id="cat_alive001")])
        self.assertFalse(victim.exists(), "已注销分类的 .ico 应被清理（prune 的立身功能）")
        self.assertTrue(survivor.exists(), "仍在配置里的分类 .ico 必须保留")

    # -- 用例二：从未管理过的残留 → 保留（这就是行为变更点）----------------------

    def test_orphan_not_managed_before_is_kept(self) -> None:
        """state 里没有、cfg 里也没有 → **不删**（旧判据会删掉，这是本次修复的核心）。"""
        leftover = self._ico("cat_0ldcache")
        self.manager.sync([self.make_cat(label="工作", cat_id="cat_test01")])
        self.assertTrue(
            leftover.exists(),
            "配置文件里没出现的 id，若 lnk_state 里也从未记录过，就不能判定为我们管理过的"
            "缓存 —— 必须保留（否则换数据根/手改配置会误删用户图标）",
        )

    # -- 用例三：cfg 为空 → 一个都不动 -------------------------------------------

    def test_empty_config_keeps_every_icon(self) -> None:
        """配置为空（读到损坏/空配置、数据根掉盘）时，绝不能清空 ico 目录。

        真实风险路径：坏配置 → _ensure_categories() 自动补登记残留目录 →
        alive 里只剩那一两个 → 旧判据会把其余全部当孤儿删掉。
        """
        icons = [self._ico("cat_aaaaaaaa"), self._ico("cat_bbbbbbbb")]
        self.manager.sync([])
        for icon in icons:
            self.assertTrue(icon.exists(), f"cfg=[] 时不允许删任何 ico：{icon.name}")

    # -- 用例四：隐藏分类 → ico 文件保留（.lnk 才删）-----------------------------

    def test_hidden_category_icon_file_kept(self) -> None:
        """隐藏走 ② 分支（只 pop state + 删 lnk），文件必须保留。

        注意措辞：**"保留"只针对文件本身**。重新显示时 state 记录已丢，
        ``ensure_ico`` 判 ``ico_sig`` 失配 → 会重建 ico 并重写 .lnk
        （与旧行为一致，不是零成本）。
        """
        cat = self.make_cat(label="隐藏的", cat_id="cat_hidden01")
        self.manager.sync([cat])
        icon = self.ico_dir / "cat_hidden01.ico"
        self.assertTrue(icon.exists(), "首次同步应生成 ico")

        cat.visible = False
        self.manager.sync([cat])
        self.assertTrue(icon.exists(), "隐藏分类只删 .lnk，ico 文件必须保留")
        self.assertEqual(self.lnk_files(), [], "隐藏后桌面 .lnk 应被删除")

    # -- 纵深防御：路径穿越 / 命名规范 / 不存在的目标 -----------------------------

    def test_traversal_ids_are_ignored(self) -> None:
        """id 里带 ``..``、分隔符、绝对路径时，一律不删、不抛、不越界。

        断言的是**文件在系统上确实还在**（不是"没抛异常"）：越界删除是唯一
        会造成用户文件永久丢失的路径，必须看磁盘。
        """
        # 目标文件放在 ico_dir 的**上一级**：`base / "../evil.ico"` 正好命中它
        outside = Path(self._tmp.name) / ".icons" / "evil.ico"
        outside.parent.mkdir(parents=True, exist_ok=True)
        outside.write_bytes(b"keep me")
        absolute = Path(self._tmp.name) / "absolute.ico"
        absolute.write_bytes(b"keep me too")
        # 这条是"词法陷阱"探针：`base/"../cat_deadbeef.ico"` 的 parts 以 base 开头，
        # `Path.is_relative_to` 会误判为"在 ico_dir 内"，而文件名又完全符合
        # `cat_[0-9a-f]{8}.ico` —— 只有 resolve 后的判据能挡住它。
        lexical_trap = Path(self._tmp.name) / ".icons" / "cat_deadbeef.ico"
        lexical_trap.write_bytes(b"keep me three")
        for bad_id in ("../evil", "..", ".", "C:\\Windows\\win.ini",
                       "../cat_deadbeef", "sub/../../evil", "  "):
            self.manager._prune_orphan_icons(alive={"cat_alive001"},
                                             removed_ids={bad_id}, managed_before=1)
        self.assertTrue(outside.exists(), "越界路径被误删会删到 ico_dir 之外的文件")
        self.assertTrue(absolute.exists(), "绝对路径入参不得删到 ico_dir 之外")
        self.assertTrue(lexical_trap.exists(),
                        "`..` 穿越 + 合法文件名必须一并挡住（is_relative_to 是词法比较，"
                        "只看它会被绕过）")
        # 反向确认：越界 id 不能顺带把目录里的合法文件也带走
        self.assertEqual(sorted(p.name for p in self.ico_dir.iterdir()
                                if p.suffix == ".ico"), [])

    def test_non_standard_names_are_skipped(self) -> None:
        """命名不符合 ``cat_[0-9a-f]{8}.ico`` 的文件一律跳过（多留几 KB 换安全）。"""
        odd = self.ico_dir / "cat_qa_a.ico"
        odd.write_bytes(b"stub")
        self.manager._prune_orphan_icons(alive={"cat_alive001"},
                                         removed_ids={"cat_qa_a"}, managed_before=1)
        self.assertTrue(odd.exists(), "非本程序命名规范的文件不得删除")

    def test_missing_file_is_silently_skipped(self) -> None:
        """记录里有、磁盘上已不存在 → 静默跳过（不抛）。"""
        self.manager._prune_orphan_icons(alive={"cat_alive001"},
                                         removed_ids={"cat_ffffffff"}, managed_before=1)

    def test_empty_removed_ids_is_noop(self) -> None:
        icon = self._ico("cat_cccccccc")
        self.manager._prune_orphan_icons(alive={"cat_alive001"},
                                         removed_ids=set(), managed_before=1)
        self.assertTrue(icon.exists())

    # -- 交错五：旧分类注销、同名 .lnk 被新分类接管 -----------------------------

    def test_same_name_lnk_taken_over_by_new_category(self) -> None:
        """旧分类注销、同名 .lnk 被**新分类**接管时，三者各自落位正确。

        这是 ``sync()`` 里最隐蔽的交错：注销循环里"同名 .lnk 跳过删除"（保留文件）
        与"仍然 ``state.pop``"（清记录）同时发生。它是"prune 会不会误伤刚建好的
        分类"的唯一直接证据，四条语义用例都覆盖不到。
        """
        old = CategoryConfig(id="cat_0a0a0a01", dir="旧目录", label="工作")
        old.sanitize()
        new = CategoryConfig(id="cat_0a0a0a02", dir="新目录", label="工作")
        new.sanitize()

        self.manager.sync([old])
        lnk = self.desktop / "工作.lnk"
        old_ico = self.ico_dir / "cat_0a0a0a01.ico"
        self.assertTrue(lnk.exists())
        self.assertTrue(old_ico.exists())

        self.manager.sync([new])          # old 从配置里消失，new 用同一个 label

        self.assertTrue(lnk.exists(), "同名 .lnk 已归属新分类，不得删除")
        self.assertIn(utf16('--open "新目录"'), lnk.read_bytes(),
                      ".lnk 内容必须指向新分类（旧参数不能残留）")
        self.assertFalse(old_ico.exists(), "旧分类已注销，其 .ico 应被清理")
        new_ico = self.ico_dir / "cat_0a0a0a02.ico"
        self.assertTrue(new_ico.exists(), "新分类的 .ico 必须独立存在（不能复用旧 id）")
        self.assertTrue(validate_ico(new_ico), "新分类 .ico 结构必须合法")
        state = json.loads(self.state.read_text(encoding="utf-8"))["categories"]
        self.assertEqual(set(state), {"cat_0a0a0a02"},
                         "state 里只应剩新分类的记录")

    # -- 结构兼容：手写的 state 不能比产品写出的更宽松 ---------------------------

    def test_state_structure_matches_product_output(self) -> None:
        """产品 ``sync()`` 写出的 state 结构 == 测试手写用的结构（防止测试自嗨）。"""
        from app.desktop_shortcuts import STATE_VERSION

        cat = self.make_cat(label="结构", cat_id="cat_struct1")
        self.manager.sync([cat])
        raw = json.loads(self.state.read_text(encoding="utf-8"))
        self.assertEqual(raw.get("version"), STATE_VERSION)
        self.assertIsInstance(raw.get("categories"), dict)
        record = raw["categories"]["cat_struct1"]
        self.assertEqual(set(record), {"lnk", "sig", "ico_sig", "where"},
                         "手写 state 的键集必须与产品一致，否则测试会掩盖问题")
        # 产品写出的 state 能被 _load_state 原样读回
        self.assertEqual(set(self.manager._load_state()["cat_struct1"]),
                         {"lnk", "sig", "ico_sig", "where"})


class _RecordHandler(logging.Handler):
    """把日志记录收进列表（用来断言"有没有 WARNING"和"警告文本里有没有数字"）。"""

    def __init__(self) -> None:
        super().__init__(level=logging.DEBUG)
        self.records: list[logging.LogRecord] = []

    def emit(self, record: logging.LogRecord) -> None:
        self.records.append(record)


class PruneBreakerTests(ShortcutManagerTestBase):
    """``_prune_orphan_icons`` 的三道闸（alive 空保底 + 比例熔断）。

    事故形态（2026-11-12 16:58）：配置被夹具覆盖成 **1 个**分类、``lnk_state.json``
    里仍有 **6 条**记录 → ``alive={A}``、``removed_ids`` = 其余 5 条 → 5 个用户的
    ``.ico`` 被当"注销残留"批量删掉。``removed_ids`` 判据单独无法区分这种情形与
    "用户主动删了 5 个分类"，因此按"一次同步里注销占比过半 = 配置可疑"熔断。

    边界表（"本次注销" = 配置里消失的分类个数）：

    ================ ====== ======= ========== ==========================
    用例              state  保留    删 ico     走的闸
    ================ ====== ======= ========== ==========================
    ``cfg=[]``       6      0       **0**      闸 1 + WARNING
    ``1-of-6``       6      1       **0**      闸 3 熔断 + WARNING
    正常删 1         6      5       1          ——（核心能力，必须保持）
    正常删 2         6      4       2          ——（**结构性探针**，见下）
    批量删 3         6      3       **0**      闸 3 熔断 + WARNING
    单分类被清空     1      0       **0**      闸 1 + WARNING
    首次同步         0      —       —          闸 2 早退（不告警）
    ================ ====== ======= ========== ==========================

    "正常删 2"是唯一的结构性探针：``managed_before`` 若在 ``state.pop`` **之后**才
    取（会得到 4 → 阈值 2 → ``2 >= 2`` 误熔断），其余六行看起来都照样通过。
    """

    def _seed(self, count: int) -> list[str]:
        """造 ``count`` 个"已管理过"的分类：state 有记录 + 磁盘有 .ico。"""
        ids = [f"cat_{i:08x}" for i in range(1, count + 1)]
        self.manager._save_state({
            cid: {"lnk": f"启动-{cid}.lnk", "sig": "s", "ico_sig": "i", "where": "desktop"}
            for cid in ids
        })
        for cid in ids:
            (self.ico_dir / f"{cid}.ico").write_bytes(b"\x00\x00\x01\x00stub")
        return ids

    def _cats(self, ids: list[str]) -> list[CategoryConfig]:
        return [self.make_cat(label=f"保留{i}", cat_id=cid) for i, cid in enumerate(ids)]

    def _sync_capture(self, cats) -> list[logging.LogRecord]:
        """跑一次 sync 并收集 ``app.desktop_shortcuts`` 的日志记录。"""
        handler = _RecordHandler()
        target = logging.getLogger("app.desktop_shortcuts")
        target.addHandler(handler)
        previous_level = target.level
        target.setLevel(logging.DEBUG)
        try:
            self.manager.sync(cats)
        finally:
            target.removeHandler(handler)
            target.setLevel(previous_level)
        return handler.records

    @staticmethod
    def _logout_warnings(records: list[logging.LogRecord]) -> list[str]:
        """只取含"注销"字样的 WARNING 文本（熔断/闸 1 的告警才算）。"""
        return [r.getMessage() for r in records
                if r.levelno >= logging.WARNING and "注销" in r.getMessage()]

    def _state_ids(self) -> set[str]:
        raw = json.loads(self.state.read_text(encoding="utf-8"))
        return set(raw["categories"])

    def _ico_exists(self, cid: str) -> bool:
        return (self.ico_dir / f"{cid}.ico").exists()

    # -- 闸 1：配置里一个分类都没有 ----------------------------------------------

    def test_empty_config_keeps_every_icon_and_warns(self) -> None:
        """``cfg=[]``：一个 .ico 都不删 + WARNING（这是 QA 复现的 B③ FAIL）。"""
        ids = self._seed(6)
        records = self._sync_capture([])

        for cid in ids:
            self.assertTrue(self._ico_exists(cid),
                            f"cfg=[] 时不允许删任何 .ico（{cid} 被删了）")
        warns = self._logout_warnings(records)
        self.assertTrue(warns, "闸 1 必须留 WARNING，否则用户日志里看不出跳过了清理")
        self.assertIn("6", warns[0], f"告警要带注销数与总数，实际: {warns[0]}")
        # 熔断/闸 1 **不影响** state 的清理：配置里没有的记录照样 pop
        self.assertEqual(self._state_ids(), set(),
                         "配置里没有的分类，state 记录应照常清理（否则永远进不了 removed_ids）")

    def test_single_category_config_cleared_keeps_icon(self) -> None:
        """state 只有 1 条、配置被清空：同样一个都不删（闸 1 兜住，不是"注销最后一个"）。"""
        ids = self._seed(1)
        records = self._sync_capture([])

        self.assertTrue(self._ico_exists(ids[0]), "单分类被清空时也不能删它的 .ico")
        self.assertTrue(self._logout_warnings(records), "闸 1 必须留 WARNING")

    # -- 闸 3：比例熔断 ----------------------------------------------------------

    def test_config_shrunk_to_one_keeps_all_and_warns(self) -> None:
        """16:58 的真实形态：cfg 只剩 1 个、state 仍有 6 条 → 熔断，一个都不删。"""
        ids = self._seed(6)
        keep = ids[0]
        records = self._sync_capture(self._cats([keep]))

        for cid in ids:
            self.assertTrue(self._ico_exists(cid),
                            f"注销占比 5/6 应熔断，{cid}.ico 被删了")
        warns = self._logout_warnings(records)
        self.assertTrue(warns, "熔断必须留 WARNING")
        self.assertIn("5", warns[0], f"告警要带注销数，实际: {warns[0]}")
        self.assertIn("6", warns[0], f"告警要带总数，实际: {warns[0]}")
        self.assertEqual(self._state_ids(), {keep}, "熔断不影响 state 记录的清理")
        self.assertEqual(self.lnk_files(), ["保留0.lnk"],
                         ".lnk 必须跟着配置走（**不熔断**），否则启动器会指向不存在的文件")

    def test_three_removals_trip_breaker(self) -> None:
        """6 个里注销 3 个 = 恰达阈值 → 熔断（多留 3 个垃圾 ico，可接受）。"""
        ids = self._seed(6)
        keep = ids[:3]
        records = self._sync_capture(self._cats(keep))

        for cid in ids:
            self.assertTrue(self._ico_exists(cid), f"熔断后 {cid}.ico 必须保留")
        self.assertTrue(self._logout_warnings(records), "达阈值必须留 WARNING")
        self.assertEqual(self._state_ids(), set(keep))
        self.assertEqual(len(self.lnk_files()), 3, ".lnk 不熔断，必须与配置的 3 个分类对齐")

    # -- 正常注销：核心能力不能因为加闸而失效 ------------------------------------

    def test_single_removal_still_prunes(self) -> None:
        """6 个里注销 1 个：**必须照常删**那 1 个 ico，且不能有"注销"告警。"""
        ids = self._seed(6)
        victim = ids[-1]
        keep = ids[:-1]
        records = self._sync_capture(self._cats(keep))

        self.assertFalse(self._ico_exists(victim),
                         "正常注销 1 个分类的 .ico 清理能力必须保持（prune 的立身功能）")
        for cid in keep:
            self.assertTrue(self._ico_exists(cid), f"存活分类 {cid}.ico 不能被动")
        self.assertEqual(self._logout_warnings(records), [],
                         "正常删分类不能出现'注销'告警（会吓到用户）")

    def test_two_removals_still_prune(self) -> None:
        """6 个里注销 2 个（2 < 阈值 3）：照删。

        **结构性探针**：``managed_before`` 若在 pop 之后取（=4），阈值会变成 2，
        这条就会误熔断 —— 其余用例都发现不了这个错。
        """
        ids = self._seed(6)
        victims = ids[4:]
        keep = ids[:4]
        self._sync_capture(self._cats(keep))

        for cid in victims:
            self.assertFalse(self._ico_exists(cid),
                             f"注销 2/6 不该熔断（2 < 3），{cid}.ico 应被删除")
        for cid in keep:
            self.assertTrue(self._ico_exists(cid), f"存活分类 {cid}.ico 不能被动")

    # -- 闸 2：无事可做 ----------------------------------------------------------

    def test_first_sync_with_empty_state_is_noop(self) -> None:
        """首次同步（state 为空）：不删文件、不告警。"""
        records = self._sync_capture([self.make_cat(label="工作", cat_id="cat_11111111")])

        self.assertTrue(self._ico_exists("cat_11111111"))
        self.assertEqual(self._logout_warnings(records), [], "无注销动作时不该有告警")


if __name__ == "__main__":
    unittest.main(verbosity=2)
