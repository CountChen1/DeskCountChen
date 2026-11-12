"""DesktopBox 打包脚本（B3）：PyInstaller onedir → dist/DesktopBox/。

用法（venv 内）::

    .venv/Scripts/python.exe tools/build_dist.py

做什么：
1. 确保 PyInstaller 已装入 venv（缺则 ``pip install pyinstaller``，不动其他依赖）；
2. 清理旧的 build/ dist/，跑 PyInstaller：onedir、windowed（无控制台）、
   name=DesktopBox、entry=main.py；排除 tests/docs/tools/.venv 等非运行时模块；
3. 冒烟验证：``dist/DesktopBox/DesktopBox.exe --selfcheck`` 退出码必须为 0
   （main.py 的 --selfcheck 打印"基础设施就绪"后立即退出，不碰互斥量）。

产物布局（与 docs/deploy.md 一致）：dist/DesktopBox/DesktopBox.exe +
_internal/。把整个 DesktopBox 目录放到 ``E:\\desktopbox\\`` 下即可运行，
数据根自动落在 ``E:\\desktopbox\\data``（见 app/paths.py frozen 分支）。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from pathlib import Path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("build_dist")

PROJECT_ROOT = Path(__file__).resolve().parents[1]
VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
APP_DIR_NAME = "DesktopBox"
EXE_PATH = DIST_DIR / APP_DIR_NAME / f"{APP_DIR_NAME}.exe"

# 打包排除的本地模块/目录（都不属于运行时；.venv 由 PyInstaller 自动识别排除）
EXCLUDE_MODULES = ("tests", "docs", "tools", "pytest", "pyinstaller")


def ensure_pyinstaller() -> None:
    """PyInstaller 缺失时装入 venv（已装则跳过，不改其他依赖）。"""
    if importlib.util.find_spec("PyInstaller") is not None:
        log.info("PyInstaller 已安装，跳过安装")
        return
    log.info("安装 PyInstaller ...")
    subprocess.run(
        [sys.executable, "-m", "pip", "install", "pyinstaller"],
        check=True,
        cwd=str(PROJECT_ROOT),
    )


def clean_previous() -> None:
    """清掉上次产物，避免旧文件混进新包。"""
    for d in (BUILD_DIR, DIST_DIR):
        if d.exists():
            log.info("清理旧目录 %s", d)
            shutil.rmtree(d, ignore_errors=True)


def run_pyinstaller() -> None:
    """PyInstaller onedir/windowed 打包 main.py。"""
    cmd = [
        sys.executable, "-m", "PyInstaller",
        "--noconfirm", "--clean",
        "--onedir", "--windowed",
        "--name", APP_DIR_NAME,
        "--distpath", str(DIST_DIR),
        "--workpath", str(BUILD_DIR),
        "--specpath", str(BUILD_DIR),
    ]
    for mod in EXCLUDE_MODULES:
        cmd += ["--exclude-module", mod]
    cmd.append(str(PROJECT_ROOT / "main.py"))
    log.info("运行 PyInstaller ...")
    subprocess.run(cmd, check=True, cwd=str(PROJECT_ROOT))


def smoke_check() -> None:
    """冒烟验证：产品 exe --selfcheck 退出码 0。"""
    if not EXE_PATH.is_file():
        raise SystemExit(f"冒烟失败：找不到产物 {EXE_PATH}")
    log.info("冒烟验证 %s --selfcheck", EXE_PATH)
    # windowed exe 没有 stdout，只验证退出码（selfcheck 不碰互斥量，秒退）
    proc = subprocess.run([str(EXE_PATH), "--selfcheck"], timeout=120)
    if proc.returncode != 0:
        raise SystemExit(f"冒烟失败：--selfcheck 退出码 {proc.returncode}")
    log.info("冒烟通过：%s 退出码 0", EXE_PATH)


def main() -> int:
    if not VENV_PYTHON.is_file():
        raise SystemExit(f"找不到 venv 解释器: {VENV_PYTHON}")
    ensure_pyinstaller()
    clean_previous()
    run_pyinstaller()
    smoke_check()
    log.info("打包完成：%s", EXE_PATH.parent)
    return 0


if __name__ == "__main__":
    sys.exit(main())
