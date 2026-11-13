"""v1.6 部署态单测：frozen 路径解析 + .lnk 目标改产品 exe。

源码运行态的行为不变由既有 295 项测试守护；这里只覆盖 frozen 分支
（用 ``patch.object(sys, "frozen", True, create=True)`` 模拟打包态）：

| 覆盖点 | 断言方式 |
|---|---|
| APP_DIR 解析 | :func:`app.paths.resolve_app_dir` 纯函数断言（源码态 / frozen 默认 / 环境变量优先） |
| .lnk 目标 | frozen 态 DesktopShortcutManager 默认目标 = sys.executable、无脚本参数 |
| 参数形态 | frozen 态 ``--open "分类"``（不含 main.py） |
| 自有判定 | frozen 态 ``_is_own_shortcut`` 认得 DeskCountChen.exe 目标的 .lnk |
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import paths  # noqa: E402
from app.desktop_shortcuts import DesktopShortcutManager  # noqa: E402
from app.win32 import shell_link, shell_resolve  # noqa: E402


class ResolveAppDirTests(unittest.TestCase):
    """APP_DIR 解析规则（纯函数，不碰真实环境变量）。"""

    def test_source_mode_uses_userprofile(self) -> None:
        out = paths.resolve_app_dir(False, "whatever/python.exe", userprofile=r"C:\Users\Z")
        self.assertEqual(out, Path(r"C:\Users\Z") / "DeskCountChen")

    def test_source_mode_home_fallback(self) -> None:
        out = paths.resolve_app_dir(False, "whatever/python.exe", userprofile=None)
        self.assertEqual(out.name, "DeskCountChen")
        self.assertTrue(out.is_absolute())

    def test_frozen_default_is_deploy_root_data(self) -> None:
        """E:\\desktopbox\\DeskCountChen\\DeskCountChen.exe → E:\\desktopbox\\data。"""
        out = paths.resolve_app_dir(
            True, r"E:\desktopbox\DeskCountChen\DeskCountChen.exe", desktopbox_home=None)
        self.assertEqual(out, Path(r"E:\desktopbox\DeskCountChen").parent / "data")
        self.assertEqual(out, Path(r"E:\desktopbox") / "data")

    def test_frozen_env_var_wins(self) -> None:
        out = paths.resolve_app_dir(
            True, r"E:\desktopbox\DeskCountChen\DeskCountChen.exe",
            desktopbox_home=r"D:\ anywhere\DeskCountChenHome")
        self.assertEqual(out, Path(r"D:\ anywhere\DeskCountChenHome"))

    def test_frozen_blank_env_ignored(self) -> None:
        out = paths.resolve_app_dir(
            True, r"E:\desktopbox\DeskCountChen\DeskCountChen.exe", desktopbox_home="   ")
        self.assertEqual(out, Path(r"E:\desktopbox") / "data")

    def test_frozen_deep_exe_still_parent_parent(self) -> None:
        """exe 被放进深层目录（如 .venv\\Scripts\\）时兜底 parent.parent，
        绝不向上无限爬（避免把数据写进盘根）。"""
        out = paths.resolve_app_dir(
            True, r"E:\x\.venv\Scripts\python.exe", desktopbox_home=None)
        self.assertEqual(out, Path(r"E:\x\.venv") / "data")


class _StubIcons:
    """最小 IconCache 替身（本文件不真正生成图标）。"""


class FrozenShortcutTargetTests(unittest.TestCase):
    """frozen 态 .lnk 目标 = 产品 exe（DeskCountChen.exe）+ --open 参数。"""

    FROZEN_EXE = Path(r"E:\desktopbox\DeskCountChen\DeskCountChen.exe")

    def _manager(self, tmp: Path) -> DesktopShortcutManager:
        return DesktopShortcutManager(
            icon_cache=_StubIcons(),
            ico_dir=tmp / "ico",
            state_path=tmp / "state.json",
            launcher_dir=tmp / "launchers",
            desktop_dir=tmp / "desktop",
        )

    def test_frozen_defaults(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(self.FROZEN_EXE)):
                mgr = self._manager(Path(td))

                self.assertEqual(Path(mgr.python_exe), self.FROZEN_EXE)
                self.assertIsNone(mgr.script_path)
                self.assertEqual(mgr.project_root, self.FROZEN_EXE.parent)

    def test_frozen_arguments_have_no_script(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(self.FROZEN_EXE)):
                mgr = self._manager(Path(td))
                self.assertEqual(
                    mgr._arguments(type("C", (), {"dir": "常用"})()),  # noqa: SLF001
                    '--open "常用"',
                )

    def test_frozen_target_override_in_source_process(self) -> None:
        """B4 迁移工具场景：源码进程里显式 frozen_target=True 必须生效。

        实测踩过：迁移脚本从源码 venv 运行，按进程检测 _frozen=False，
        script_path 被自动补成 main.py，错误参数写进了 .lnk。
        """
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            mgr = DesktopShortcutManager(
                icon_cache=_StubIcons(),
                ico_dir=Path(td) / "ico",
                state_path=Path(td) / "state.json",
                launcher_dir=Path(td) / "launchers",
                desktop_dir=Path(td) / "desktop",
                python_exe=str(self.FROZEN_EXE),
                script_path=None,
                frozen_target=True,
            )
            self.assertIsNone(mgr.script_path)
            self.assertEqual(Path(mgr.python_exe), self.FROZEN_EXE)
            self.assertEqual(mgr.project_root, self.FROZEN_EXE.parent)

    def test_source_mode_arguments_keep_script(self) -> None:
        """源码态回归：参数必须仍带 main.py（不被 frozen 分支波及）。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            mgr = self._manager(Path(td))
            self.assertEqual(
                mgr._arguments(type("C", (), {"dir": "常用"})()),  # noqa: SLF001
                f'"{mgr.script_path}" --open "常用"',
            )

    def test_frozen_write_lnk_target_is_product_exe(self) -> None:
        """真实写一个 .lnk 再解析回来：目标/参数必须与 frozen 约定一致。"""
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            cat = type("C", (), {"dir": "常用", "label": "常用"})()
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(self.FROZEN_EXE)):
                mgr = self._manager(tmp)
                dest = tmp / "launchers" / "启动-常用.lnk"
                dest.parent.mkdir(parents=True, exist_ok=True)
                self.assertTrue(mgr._write_lnk(dest, cat, None))  # noqa: SLF001
            info = shell_resolve.read_link(dest)
            self.assertTrue(info.ok)
            self.assertEqual(Path(info.target), self.FROZEN_EXE)
            self.assertEqual(info.arguments, '--open "常用"')

    def test_frozen_is_own_shortcut_recognizes_product_exe(self) -> None:
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            tmp = Path(td)
            dest = tmp / "own.lnk"
            self.assertTrue(shell_link.create_shortcut(
                dest=dest, target=str(self.FROZEN_EXE),
                arguments='--open "常用"', working_dir=str(self.FROZEN_EXE.parent),
                icon_path=str(self.FROZEN_EXE), description="DeskCountChen 分类：常用",
            ))
            with patch.object(sys, "frozen", True, create=True), \
                 patch.object(sys, "executable", str(self.FROZEN_EXE)):
                mgr = self._manager(tmp)
                self.assertTrue(mgr._is_own_shortcut(dest))   # noqa: SLF001
            # 源码态（未 patch frozen）下同一个 .lnk 不是"python 目标"——
            # 但源码态运行时 sys.executable 是 python.exe，own_exes 不含
            # DeskCountChen.exe → 判 False（保守：不确定就不删）。
            mgr2 = self._manager(tmp)
            self.assertFalse(mgr2._is_own_shortcut(dest))    # noqa: SLF001


if __name__ == "__main__":
    unittest.main()
