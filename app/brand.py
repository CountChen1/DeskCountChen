"""品牌与版本元数据 —— 全项目唯一事实来源。

规则（与 docs/design.md §12.8.1 一致）：**同一事实只留一个副本，其余必须是
它的投影**。产品名、作者、版权、版本号只在这里写一次，其他模块一律引用，
不许再写第二份字面量。改名/改版权时只动本文件，全项目自动跟随。

两个名字刻意分开：

- ``PRODUCT_NAME`` = ``Desk CountChen`` —— 人看的（窗口标题、托盘提示、关于框）；
- ``PRODUCT_SLUG`` = ``DeskCountChen`` —— 机器用的（exe 名、目录名、互斥量、
  QLocalServer 名）。不带空格，避免路径、命令行、.lnk 目标里出现引号歧义。
"""

from __future__ import annotations

from app import __version__

# -- 人看的 -------------------------------------------------------------------
PRODUCT_NAME = "Desk CountChen"
AUTHOR = "CountChen"
COPYRIGHT = "Copyright (c) 2026 CountChen"
LICENSE_NAME = "Apache-2.0"
HOMEPAGE = "https://github.com/CountChen/DeskCountChen"
TAGLINE = "Windows 桌面图标收纳面板"

# -- 机器用的 -----------------------------------------------------------------
PRODUCT_SLUG = "DeskCountChen"
APP_USER_MODEL_ID = f"CountChen.{PRODUCT_SLUG}"

# 版本号唯一来源是 app/__init__.py 的 __version__，这里只做投影
VERSION = __version__

# 打包时由 tools/build_dist.py 生成 app/build_info.py；源码态没有该文件
try:  # pragma: no cover - 两条分支由不同运行形态覆盖
    from app.build_info import BUILD_COMMIT as _COMMIT, BUILD_TIME as _TIME
except ImportError:  # pragma: no cover
    _COMMIT = "dev"
    _TIME = "未打包（源码运行）"

BUILD_COMMIT: str = _COMMIT
BUILD_TIME: str = _TIME


def version_line() -> str:
    """单行版本指纹：``Desk CountChen v2.0.0 (build abc1234, 2026-11-13 10:00)``。

    打包态带真实 commit 与构建时间；源码态显示 dev。盗版改名重编译出来的
    commit 必然与官方 Release 不同，任何人可据此比对辨伪。
    """
    return f"{PRODUCT_NAME} v{VERSION} (build {BUILD_COMMIT}, {BUILD_TIME})"


def about_text() -> str:
    """「关于」对话框正文。"""
    return (
        f"{PRODUCT_NAME} v{VERSION}\n"
        f"{TAGLINE}\n\n"
        f"{COPYRIGHT}\n"
        f"许可：{LICENSE_NAME}\n"
        f"构建：{BUILD_COMMIT} / {BUILD_TIME}\n"
        f"主页：{HOMEPAGE}"
    )
