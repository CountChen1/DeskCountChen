"""测试公共辅助：offscreen Qt 应用、隐藏属性读写、用户配置保护。"""

from __future__ import annotations

import ctypes
import os
from pathlib import Path

from tests import config_guard as _GUARD

# 必须在创建 QApplication 之前设置，保证无桌面也能跑
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

_QT_APP = None

FILE_ATTRIBUTE_HIDDEN = 0x2
FILE_ATTRIBUTE_NORMAL = 0x80


def qt_app():
    """返回进程内唯一的 QApplication（offscreen）。"""
    global _QT_APP
    if _QT_APP is not None:
        return _QT_APP
    from PySide6.QtWidgets import QApplication

    _QT_APP = QApplication.instance() or QApplication([])
    return _QT_APP


def _kernel32():
    return ctypes.WinDLL("kernel32", use_last_error=True)


def get_attrs(path: Path) -> int:
    """读取文件属性位（Win32）。"""
    return int(_kernel32().GetFileAttributesW(str(path)))


def set_attrs(path: Path, attrs: int) -> None:
    """写入文件属性位（Win32）。"""
    _kernel32().SetFileAttributesW(str(path), attrs)


def make_hidden(path: Path) -> None:
    """把文件/目录设为隐藏属性（模拟资源管理器里"隐藏"）。"""
    set_attrs(path, get_attrs(path) | FILE_ATTRIBUTE_HIDDEN)


def clear_hidden(path: Path) -> None:
    """清掉隐藏属性。"""
    set_attrs(path, get_attrs(path) & ~FILE_ATTRIBUTE_HIDDEN)


def is_hidden(path: Path) -> bool:
    """是否带隐藏属性。"""
    return bool(get_attrs(path) & FILE_ATTRIBUTE_HIDDEN)


# 只读护栏（不再写任何备份文件；详见 tests/config_guard.py 的说明）
preserve_user_config = _GUARD.preserve_user_config
