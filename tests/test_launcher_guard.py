"""启动器目录安全防线单测：**启动器绝不允许落在用户数据根上**。

## 背景（为什么加这条防线）

``E:\\快捷方式SAO`` 是用户自己的分类数据根，v1.4 测试期的某次运行之后，该目录
**根下**出现了 6 个 ``启动-*.lnk``（与 ``%USERPROFILE%\\DeskCountChen\\.launchers``
里那 6 个 sha256 逐字节相同）。那几个文件不是本仓库任何代码路径写出来的
（全历史 ``git grep`` 没有任何一处把 ``DATA_ROOT`` 当 ``launcher_dir``），
但**代码里也没有任何一道防线**阻止这种调用错误。

因此 v1.5 追加：``_target_dir()`` / ``_dir_for_mode()`` 在返回启动器目录前先校验，
命中"用户数据根 / 程序目录根 / 分类数据目录"一律**拒绝写入 + 只告警一次**。

## 覆盖点

- 白名单：``paths.LAUNCHERS_DIR`` 正常放行并真的写出 .lnk；
- 拒绝：``launcher_dir == DATA_ROOT`` / ``== APP_DIR`` / ``== DATA_ROOT/<分类>``；
- 拒绝时不落任何文件、不创建目录；
- 告警**只打一次**（不每 tick 刷屏）；
- 清理路径 ``_dir_for_mode`` 同样被挡（它会**删文件**，比写入处更危险）。
"""

from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402
from app.config import CategoryConfig  # noqa: E402
from app.desktop_shortcuts import DesktopShortcutManager  # noqa: E402
from app.icon_provider import IconCache  # noqa: E402
from tests.qt_support import qt_app  # noqa: E402


class LauncherDirGuardTestBase(unittest.TestCase):
    """每个用例一套隔离的临时目录树，并把它注入 ``app.paths``。"""

    @classmethod
    def setUpClass(cls) -> None:
        qt_app()

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory(prefix="dbx_guard_")
        root = Path(self._tmp.name)
        self.root = root
        self.app_dir = root / "app"
        self.data_root = root / "data"
        self.launchers = self.app_dir / ".launchers"
        self.ico_dir = self.app_dir / ".icons" / "ico"
        self.state = self.app_dir / ".icons" / "lnk_state.json"
        self.project = root / "project"
        for p in (self.app_dir, self.data_root, self.project):
            p.mkdir(parents=True, exist_ok=True)
        self.script = self.project / "main.py"
        self.script.write_text("# stub\n", encoding="utf-8")

        # 把 app.paths 的三个关键全局量替换成临时路径（用完即还原）
        self._patches = [
            mock.patch.object(paths, "APP_DIR", self.app_dir),
            mock.patch.object(paths, "DATA_ROOT", self.data_root),
            mock.patch.object(paths, "LAUNCHERS_DIR", self.launchers),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self) -> None:
        for p in reversed(self._patches):
            p.stop()
        self._tmp.cleanup()

    # -- 辅助 ---------------------------------------------------------------

    def make_manager(self, launcher_dir: Path) -> DesktopShortcutManager:
        return DesktopShortcutManager(
            icon_cache=IconCache(),
            ico_dir=self.ico_dir,
            state_path=self.state,
            project_root=self.project,
            python_exe=str(Path(sys.executable)),
            script_path=self.script,
            mode="launcher",
            launcher_dir=launcher_dir,
        )

    def make_cat(self, label: str = "工作", cat_id: str = "cat_guard") -> CategoryConfig:
        cat = CategoryConfig(id=cat_id, dir=label, label=label)
        cat.sanitize()
        return cat

    def lnks_in(self, d: Path) -> list[str]:
        return sorted(p.name for p in d.glob("*.lnk")) if d.is_dir() else []


class PathsInvariantTests(LauncherDirGuardTestBase):
    """先钉住前提：DATA_ROOT 与 LAUNCHERS_DIR 是两个不同的量。"""

    def test_data_root_and_launchers_dir_are_distinct(self) -> None:
        """``LAUNCHERS_DIR`` 恒等于 ``APP_DIR/.launchers``，与 ``DATA_ROOT`` 无关。"""
        self.assertNotEqual(paths.DATA_ROOT, paths.LAUNCHERS_DIR)
        self.assertEqual(paths.LAUNCHERS_DIR, paths.APP_DIR / ".launchers")

    def test_set_data_root_does_not_move_launchers_dir(self) -> None:
        """``set_data_root()`` 只动 DATA_ROOT，**绝不**动 LAUNCHERS_DIR。"""
        before = paths.LAUNCHERS_DIR
        try:
            paths.set_data_root(self.root / "another_root")
            self.assertEqual(paths.LAUNCHERS_DIR, before)
            self.assertNotEqual(paths.DATA_ROOT, paths.LAUNCHERS_DIR)
        finally:
            paths.set_data_root(self.data_root)

    def test_set_data_root_does_not_move_launchers_dir_across_many_calls(self) -> None:
        before = paths.LAUNCHERS_DIR
        try:
            for name in ("r1", "r2", "r3"):
                paths.set_data_root(self.root / name)
                self.assertEqual(paths.LAUNCHERS_DIR, before)
        finally:
            paths.set_data_root(self.data_root)


class ForbiddenLauncherDirTests(LauncherDirGuardTestBase):
    """三类必须拒绝的落点。"""

    def test_data_root_is_rejected(self) -> None:
        """``launcher_dir == DATA_ROOT`` → 拒绝（正是 E:\\快捷方式SAO 那种情形）。"""
        mgr = self.make_manager(self.data_root)
        self.assertTrue(mgr.launcher_dir_blocked_reason())
        self.assertIsNone(mgr._target_dir())          # noqa: SLF001 - 直接验内部落点

    def test_category_dir_under_data_root_is_rejected(self) -> None:
        """``launcher_dir == DATA_ROOT/<分类>`` → 拒绝（不能往分类数据目录里塞 .lnk）。"""
        cat_dir = self.data_root / "工作"
        cat_dir.mkdir(parents=True, exist_ok=True)
        mgr = self.make_manager(cat_dir)
        self.assertTrue(mgr.launcher_dir_blocked_reason())
        self.assertIsNone(mgr._target_dir())          # noqa: SLF001

    def test_app_dir_root_is_rejected(self) -> None:
        """``launcher_dir == APP_DIR`` → 拒绝（别把 .lnk 撒到程序目录根）。"""
        mgr = self.make_manager(self.app_dir)
        self.assertTrue(mgr.launcher_dir_blocked_reason())
        self.assertIsNone(mgr._target_dir())          # noqa: SLF001

    def test_normal_launchers_dir_is_allowed(self) -> None:
        """白名单：``paths.LAUNCHERS_DIR`` 放行。"""
        mgr = self.make_manager(self.launchers)
        self.assertEqual(mgr.launcher_dir_blocked_reason(), "")
        self.assertEqual(mgr._target_dir(), self.launchers)   # noqa: SLF001

    def test_relative_equivalent_path_is_still_rejected(self) -> None:
        """绕过尝试：``data_root/../data`` 这类等价路径也必须被 resolve 后拦住。"""
        sneaky = self.data_root / ".." / "data"
        mgr = self.make_manager(sneaky)
        self.assertTrue(mgr.launcher_dir_blocked_reason())


class NoWriteWhenBlockedTests(LauncherDirGuardTestBase):
    """被拒绝时：一个文件都不写、目录都不建。"""

    def test_sync_writes_nothing_when_data_root(self) -> None:
        mgr = self.make_manager(self.data_root)
        mgr.sync([self.make_cat("工作"), self.make_cat("影音", "cat_guard2")])

        self.assertEqual(self.lnks_in(self.data_root), [])   # 数据根根下没有 .lnk
        self.assertEqual(self.lnks_in(self.launchers), [])
        self.assertFalse((self.data_root / "启动-工作.lnk").exists())

    def test_sync_writes_nothing_when_category_dir(self) -> None:
        cat_dir = self.data_root / "工作"
        cat_dir.mkdir(parents=True, exist_ok=True)
        mgr = self.make_manager(cat_dir)
        mgr.sync([self.make_cat("工作")])
        self.assertEqual(self.lnks_in(cat_dir), [])

    def test_normal_path_still_writes(self) -> None:
        """反证：合法路径下同步照旧写出 .lnk（防线不能误伤正常流程）。"""
        mgr = self.make_manager(self.launchers)
        mgr.sync([self.make_cat("工作")])
        self.assertEqual(self.lnks_in(self.launchers), ["启动-工作.lnk"])


class WarnOnceTests(LauncherDirGuardTestBase):
    """告警只打一次，不能每 tick 刷屏。"""

    def test_warning_logged_only_once(self) -> None:
        mgr = self.make_manager(self.data_root)
        with self.assertLogs("app.desktop_shortcuts", level="WARNING") as captured:
            for _ in range(5):
                mgr._target_dir()                     # noqa: SLF001
        hits = [line for line in captured.output if "拒绝把启动器写入" in line]
        self.assertEqual(len(hits), 1, f"告警应只出现 1 次，实际 {len(hits)} 次")

    def test_warning_text_mentions_correct_target(self) -> None:
        mgr = self.make_manager(self.data_root)
        with self.assertLogs("app.desktop_shortcuts", level="WARNING") as captured:
            mgr._target_dir()                         # noqa: SLF001
        blob = "\n".join(captured.output)
        self.assertIn("拒绝把启动器写入", blob)
        self.assertIn(".launchers", blob)             # 提示正确落点


class CleanupPathGuardTests(LauncherDirGuardTestBase):
    """``_dir_for_mode`` 也要挡：它会删文件。"""

    def test_dir_for_mode_launcher_is_blocked(self) -> None:
        mgr = self.make_manager(self.data_root)
        self.assertIsNone(mgr._dir_for_mode("launcher"))     # noqa: SLF001

    def test_unregister_does_not_delete_user_files_in_data_root(self) -> None:
        """注销分类时，数据根里的用户文件绝不能被当成本程序的 .lnk 删掉。"""
        victim = self.data_root / "启动-工作.lnk"
        victim.write_bytes(b"user's own file, must survive")

        mgr = self.make_manager(self.data_root)
        cat = self.make_cat("工作")
        mgr.sync([cat])
        mgr.sync([])                                   # 空分类 → 触发注销清理

        self.assertTrue(victim.is_file(), "数据根里的用户文件被误删了")


if __name__ == "__main__":
    unittest.main()
