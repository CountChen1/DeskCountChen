"""Desk CountChen 打包脚本：PyInstaller onedir → dist/DeskCountChen/。

用法（venv 内）::

    .venv/Scripts/python.exe tools/build_dist.py
    .venv/Scripts/python.exe tools/build_dist.py --skip-smoke

做什么：
1. 确保 PyInstaller 已装入 venv（缺则 ``pip install pyinstaller``，不动其他依赖）；
2. 生成 ``app/build_info.py``（git commit + 构建时间）→ 程序「关于」里可见，
   任何人都能拿它和官方 Release 比对，识别改名重编译的版本；
3. 生成 PE 版本资源 ``build/version_info.txt``（CompanyName / ProductName /
   LegalCopyright / OriginalFilename）→ Windows 文件属性里删不掉的身份信息；
4. 确保 ``assets/app.ico`` 存在（缺则调 tools/make_icon.py 现生成）；
5. 清理旧的 build/ dist/，跑 PyInstaller：onedir、windowed、--icon、--version-file；
6. 冒烟验证：``dist/DeskCountChen/DeskCountChen.exe --selfcheck`` 退出码必须为 0，
   且 ``--version`` 能报出「Desk CountChen」+ 版本号。

产物布局（与 docs/deploy.md 一致）：dist/DeskCountChen/DeskCountChen.exe +
_internal/。把整个 DeskCountChen 目录放到 ``E:\\desktopbox\\`` 下即可运行，
数据根自动落在 ``E:\\desktopbox\\data``（见 app/paths.py frozen 分支）。
"""

from __future__ import annotations

import importlib.util
import logging
import shutil
import subprocess
import sys
from datetime import datetime
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

from app import brand  # noqa: E402 - 必须先修好 sys.path

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("build_dist")

VENV_PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
DIST_DIR = PROJECT_ROOT / "dist"
BUILD_DIR = PROJECT_ROOT / "build"
APP_DIR_NAME = brand.PRODUCT_SLUG
EXE_PATH = DIST_DIR / APP_DIR_NAME / f"{APP_DIR_NAME}.exe"
ICON_PATH = PROJECT_ROOT / "assets" / "app.ico"
VERSION_FILE = BUILD_DIR / "version_info.txt"
BUILD_INFO_MODULE = PROJECT_ROOT / "app" / "build_info.py"

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


def git_commit() -> str:
    """当前 commit（短 hash）；有未提交改动时追加 -dirty，不谎报干净。"""
    def _run(*args: str) -> str:
        try:
            out = subprocess.run(
                ["git", *args], capture_output=True, text=True,
                cwd=str(PROJECT_ROOT), timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            return ""
        return out.stdout.strip() if out.returncode == 0 else ""

    commit = _run("rev-parse", "--short=7", "HEAD") or "unknown"
    if _run("status", "--porcelain"):
        commit += "-dirty"
    return commit


def write_build_info() -> None:
    """生成 app/build_info.py（gitignored），供 app.brand 读取构建指纹。"""
    commit = git_commit()
    stamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    BUILD_INFO_MODULE.write_text(
        '"""构建指纹 —— 由 tools/build_dist.py 生成，请勿手改/提交。"""\n\n'
        f'BUILD_COMMIT = "{commit}"\n'
        f'BUILD_TIME = "{stamp}"\n',
        encoding="utf-8",
        newline="",
    )
    log.info("构建指纹：%s @ %s", commit, stamp)


def version_tuple(version: str) -> tuple[int, int, int, int]:
    """'2.0.0' → (2, 0, 0, 0)；缺位补 0，非数字段按 0 处理。"""
    parts: list[int] = []
    for seg in version.split(".")[:4]:
        try:
            parts.append(int(seg))
        except ValueError:
            parts.append(0)
    parts += [0] * (4 - len(parts))
    return (parts[0], parts[1], parts[2], parts[3])


def write_version_file() -> None:
    """写 PyInstaller 的 PE 版本资源（Windows 文件属性 → 详细信息）。"""
    BUILD_DIR.mkdir(parents=True, exist_ok=True)
    v = version_tuple(brand.VERSION)
    filevers = f"({v[0]}, {v[1]}, {v[2]}, {v[3]})"
    VERSION_FILE.write_text(
        f"""VSVersionInfo(
  ffi=FixedFileInfo(
    filevers={filevers},
    prodvers={filevers},
    mask=0x3f,
    flags=0x0,
    OS=0x40004,
    fileType=0x1,
    subtype=0x0,
    date=(0, 0)
  ),
  kids=[
    StringFileInfo([
      StringTable('040904B0', [
        StringStruct('CompanyName', '{brand.AUTHOR}'),
        StringStruct('FileDescription', '{brand.PRODUCT_NAME} - {brand.TAGLINE}'),
        StringStruct('FileVersion', '{brand.VERSION}'),
        StringStruct('InternalName', '{brand.PRODUCT_SLUG}'),
        StringStruct('LegalCopyright', '{brand.COPYRIGHT}'),
        StringStruct('OriginalFilename', '{brand.PRODUCT_SLUG}.exe'),
        StringStruct('ProductName', '{brand.PRODUCT_NAME}'),
        StringStruct('ProductVersion', '{brand.VERSION}'),
        StringStruct('Comments', '{brand.HOMEPAGE}'),
      ])
    ]),
    VarFileInfo([VarStruct('Translation', [1033, 1200])])
  ]
)
""",
        encoding="utf-8",
        newline="",
    )
    log.info("PE 版本资源：%s", VERSION_FILE)


def ensure_icon() -> None:
    """图标缺失时现生成（二进制不入库当唯一来源，脚本可再生）。"""
    if ICON_PATH.is_file():
        log.info("图标已存在：%s", ICON_PATH)
        return
    log.info("图标缺失，生成 %s ...", ICON_PATH)
    subprocess.run(
        [sys.executable, str(PROJECT_ROOT / "tools" / "make_icon.py")],
        check=True, cwd=str(PROJECT_ROOT),
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
        "--icon", str(ICON_PATH),
        "--version-file", str(VERSION_FILE),
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
    """冒烟验证：--selfcheck 退出码 0，且 --version 报出品牌与版本。"""
    if not EXE_PATH.is_file():
        raise SystemExit(f"冒烟失败：找不到产物 {EXE_PATH}")
    log.info("冒烟验证 %s --selfcheck", EXE_PATH)
    # windowed exe 没有 stdout，只验证退出码（selfcheck 不碰互斥量，秒退）
    proc = subprocess.run([str(EXE_PATH), "--selfcheck"], timeout=120)
    if proc.returncode != 0:
        raise SystemExit(f"冒烟失败：--selfcheck 退出码 {proc.returncode}")

    # windowed 下 print() 无控制台可去，重定向到文件才拿得到真值
    out_file = BUILD_DIR / "version_out.txt"
    with open(out_file, "wb") as fh:
        subprocess.run(
            [str(EXE_PATH), "--version"], stdout=fh,
            stderr=subprocess.STDOUT, timeout=120, check=False,
        )
    text = out_file.read_text(encoding="utf-8", errors="replace").strip()
    if brand.PRODUCT_NAME not in text or brand.VERSION not in text:
        raise SystemExit(f"冒烟失败：--version 输出异常 -> {text!r}")
    log.info("冒烟通过：%s | %s", EXE_PATH.name, text.splitlines()[0])


def main(argv: list[str]) -> int:
    if not VENV_PYTHON.is_file():
        raise SystemExit(f"找不到 venv 解释器: {VENV_PYTHON}")
    ensure_pyinstaller()
    write_build_info()
    ensure_icon()
    clean_previous()  # 会清掉 build/，所以版本资源必须在其后重写
    write_version_file()
    run_pyinstaller()
    if "--skip-smoke" not in argv:
        smoke_check()
    log.info("打包完成：%s", EXE_PATH.parent)
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
