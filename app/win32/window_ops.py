"""Win32 通用能力封装（DesktopBox 唯一的 ctypes 边界，除 shell_link 外）。

约定：
- 除 ``shell_open`` 外，所有函数不抛异常，失败返回安全默认值并记录日志。
- ``shell_open`` 不做存在性预检、不捕获异常，由系统 Shell 负责错误 UI
  （失效快捷方式会弹出 Windows 原生的"找不到程序"对话框）。

v1.2 清理（悬浮窗体系移除）：``progman_hwnd / ensure_desktop_flags /
sink_to_desktop / is_hidden_or_iconic / restore_noactivate / clamp_to_screens``
全部删除——桌面图标已是 Windows 真实快捷方式，位置与 z 序由系统管理，
不再需要任何置底/看门狗/坐标 clamp 逻辑。
"""

from __future__ import annotations

import ctypes
import logging
from pathlib import Path

from app.win32 import consts

logger = logging.getLogger(__name__)

__all__ = [
    "set_file_hidden",
    "clear_file_hidden",
    "logical_name_cmp",
    "shell_open",
    "acquire_single_instance_mutex",
    "set_app_user_model_id",
    "release_single_instance_mutex",
    "ctypes_get_last_error",
]

# 单实例互斥量句柄（进程退出时由 OS 自动回收，但主动 Close 更干净）
_mutex_handle = None

# 单实例互斥量名称（Local 命名空间 = 每个登录会话一份）
MUTEX_NAME = "Local\\DesktopBox.SingleInstance.Mutex"


def set_file_hidden(path: Path) -> None:
    """把文件设为 Windows 隐藏属性（Windows 不以点前缀隐藏文件）。"""
    try:
        consts._SetFileAttributesW(str(path), consts.FILE_ATTRIBUTE_HIDDEN)
    except Exception as exc:  # noqa: BLE001 - window_ops 约定不抛异常
        logger.warning("set_file_hidden(%s) 失败: %s", path, exc)


def clear_file_hidden(path: Path) -> None:
    """清除文件的隐藏属性。

    实测边界：open(path,'w') 覆写已有隐藏文件会 PermissionError（CREATE_ALWAYS
    + FILE_ATTRIBUTE_NORMAL 打开隐藏文件被 Win32 拒绝）；os.replace() 不受限。
    当前保存路径走 os.replace 本可不调用本函数，保留作防御——写入方式将来
    变更时不必重新踩坑。注意：SetFileAttributesW(path, 固定值) 会一并清掉
    其它属性位（如 ARCHIVE），对纯数据文件无实际影响（P3 记录在案）。
    """
    try:
        consts._SetFileAttributesW(str(path), consts.FILE_ATTRIBUTE_NORMAL)
    except Exception as exc:  # noqa: BLE001
        logger.warning("clear_file_hidden(%s) 失败: %s", path, exc)


def logical_name_cmp(a, b) -> int:
    """自然排序比较（StrCmpLogicalW，与资源管理器一致）。

    失败时退化为 casefold 的字典序比较。返回负/零/正。
    """
    try:
        return int(consts._StrCmpLogicalW(str(a), str(b)))
    except Exception as exc:  # noqa: BLE001
        logger.warning("StrCmpLogicalW 失败(%s, %s): %s", a, b, exc)
        sa, sb = str(a).casefold(), str(b).casefold()
        return (sa > sb) - (sa < sb)


def shell_open(path) -> bool:
    """把路径交给系统 Shell 打开（os.startfile 同款语义）。

    - 不预检目标是否存在；
    - 失效快捷方式由 Shell 弹出 Windows 原生"找不到程序"对话框；
    - 仅把结果写入日志并返回布尔值，不吞错、不弹自有提示框。
    """
    ret = consts._ShellExecuteW(None, "open", str(path), None, None, consts.SW_SHOWNORMAL)
    code = 0 if ret is None else int(ret)
    if code > 32:
        return True
    logger.warning("shell_open 失败(%s): ShellExecute 返回 %s", path, code)
    return False


def acquire_single_instance_mutex(name: str = MUTEX_NAME) -> bool:
    """尝试获取单实例互斥量；已有实例在运行时返回 False。"""
    global _mutex_handle
    try:
        handle = consts._CreateMutexW(None, False, name)
        err = ctypes_get_last_error()
        if not handle:
            logger.warning("CreateMutexW 失败 (err=%s)", err)
            return False
        if err == consts.ERROR_ALREADY_EXISTS:
            consts._CloseHandle(handle)
            return False
        _mutex_handle = handle
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("acquire_single_instance_mutex 失败: %s", exc)
        return False


def release_single_instance_mutex() -> None:
    """释放单实例互斥量（正常退出路径调用；异常退出由 OS 回收）。"""
    global _mutex_handle
    if _mutex_handle:
        try:
            consts._CloseHandle(_mutex_handle)
        except Exception as exc:  # noqa: BLE001
            logger.warning("CloseHandle 失败: %s", exc)
        _mutex_handle = None


def set_app_user_model_id(app_id: str = "WorkBuddy.DesktopBox") -> None:
    """设置进程 AppUserModelID（让任务栏归属正确）。"""
    try:
        consts._SetCurrentProcessExplicitAppUserModelID(app_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("SetCurrentProcessExplicitAppUserModelID 失败: %s", exc)


def ctypes_get_last_error() -> int:
    """读取 ctypes 的 last error（配合 use_last_error=True 的 DLL）。"""
    return ctypes.get_last_error()
