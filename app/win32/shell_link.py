"""真实桌面路径 + Windows 快捷方式（.lnk）写盘 —— ctypes / COM 边界。

为什么不用 pywin32：项目硬约束"不引入任何第三方新依赖"，本模块用 ctypes
直接调 Shell 的 COM 接口：``CoCreateInstance(CLSID_ShellLink, IID_IShellLinkW)``
→ 设置目标/参数/工作目录/图标 → ``QueryInterface(IID_IPersistFile)`` →
``IPersistFile::Save`` 落盘。手写 .lnk 二进制（MS-SHLLINK）风险更高，不采用。

为什么用 SHGetKnownFolderPath(FOLDERID_Desktop)：
桌面可能被 OneDrive / 企业策略重定向到 ``%USERPROFILE%\\OneDrive\\桌面`` 之类，
用注册表或 ``%USERPROFILE%\\Desktop`` 猜测会在这些机器上把图标建到错误位置。

vtable 调用约定（务必照此实现，PySide6/ctypes 都没有现成的 COM 绑定）：
``this`` 指针指向"指向 vtable 的指针"，vtable 是函数指针数组，第 i 项即第 i 个
虚函数；每个方法第一个隐式参数是 ``this``。
"""

from __future__ import annotations

import ctypes
import logging
import os
from pathlib import Path

from app.win32 import consts

logger = logging.getLogger(__name__)

__all__ = ["desktop_dir", "create_shortcut"]

# COM 是否已在本线程初始化（幂等；Qt 的 GUI 线程通常已由 OLE 初始化）
_com_ready = False


def _com_call(this, index: int, restype, argtypes, *args):
    """调用 COM 接口 vtable 上第 index 个方法（第一个隐式参数为 this）。"""
    vtbl_ptr = ctypes.cast(this, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    entry = vtbl_ptr.contents[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(entry)(this, *args)


def _ensure_com() -> bool:
    """保证当前线程已初始化 COM（STA）。

    返回 True 表示"可以继续用 COM"。若本线程已被初始化为另一种 apartment
    （RPC_E_CHANGED_MODE），ShellLink 是 in-proc 组件，依旧可以创建，故仍返回 True。
    """
    global _com_ready
    if _com_ready:
        return True
    try:
        hr = int(consts._CoInitializeEx(None, consts.COINIT_APARTMENTTHREADED))
    except Exception as exc:  # noqa: BLE001
        logger.warning("CoInitializeEx 失败: %s", exc)
        return False
    # S_OK(0) 首次初始化；S_FALSE(1) 已初始化；RPC_E_CHANGED_MODE 也可继续
    if hr in (consts.S_OK, consts.S_FALSE) or hr == consts.RPC_E_CHANGED_MODE:
        _com_ready = True
        return True
    logger.warning("CoInitializeEx 返回 0x%08X，COM 不可用", hr & 0xFFFFFFFF)
    return False


def _release(this) -> None:
    """释放 COM 接口（引用计数 -1），失败只记日志。"""
    if not this:
        return
    try:
        _com_call(this, consts.VTBL_RELEASE, ctypes.c_ulong, [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("COM Release 失败: %s", exc)


def desktop_dir() -> Path | None:
    """返回**真实**桌面目录（解析 OneDrive / 企业重定向）；失败返回 None。

    绝不使用注册表或 ``%USERPROFILE%\\Desktop`` 猜测；只有在
    SHGetKnownFolderPath 完全不可用时才退化为"用户目录下的 Desktop（若存在）"，
    并记 warning，便于排查。
    """
    try:
        out = ctypes.c_wchar_p()
        hr = int(consts._SHGetKnownFolderPath(
            ctypes.byref(consts.FOLDERID_Desktop),
            consts.KF_FLAG_DEFAULT,
            None,
            ctypes.byref(out),
        ))
        try:
            if hr == consts.S_OK and out.value:
                return Path(out.value)
        finally:
            if out:
                consts._CoTaskMemFree(ctypes.cast(out, ctypes.c_void_p))
        logger.warning("SHGetKnownFolderPath(FOLDERID_Desktop) 返回 0x%08X", hr & 0xFFFFFFFF)
    except Exception as exc:  # noqa: BLE001
        logger.warning("获取真实桌面路径失败: %s", exc)

    # 最后兜底（仅当已知文件夹 API 不可用）：用户目录下的 Desktop
    profile = os.environ.get("USERPROFILE", "")
    if profile:
        fallback = Path(profile) / "Desktop"
        if fallback.is_dir():
            logger.warning("已退化为 %s（非 SHGetKnownFolderPath 结果）", fallback)
            return fallback
    logger.error("无法定位桌面目录，桌面快捷方式功能不可用")
    return None


def create_shortcut(
    dest: Path,
    target: str,
    arguments: str = "",
    working_dir: str = "",
    icon_path: str = "",
    description: str = "",
) -> bool:
    """创建/重建一个 .lnk 快捷方式，成功返回 True。

    重建（先删后建）是刻意设计：Windows 会按 文件路径 + 时间戳 缓存 .lnk 图标，
    原地覆写常常"图标不刷新"，删掉再建才能让新 ICO 立即生效。
    """
    if not _ensure_com():
        return False
    dest = Path(dest)
    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
    ok = False
    try:
        hr = int(consts._CoCreateInstance(
            ctypes.byref(consts.CLSID_ShellLink),
            None,
            consts.CLSCTX_INPROC_SERVER,
            ctypes.byref(consts.IID_IShellLinkW),
            ctypes.byref(link),
        ))
        if hr != consts.S_OK or not link:
            logger.warning("CoCreateInstance(ShellLink) 失败: 0x%08X", hr & 0xFFFFFFFF)
            return False

        steps = (
            (consts.VTBL_SHELLLINK_SET_PATH, [ctypes.c_wchar_p], (str(target),), "SetPath"),
            (consts.VTBL_SHELLLINK_SET_ARGUMENTS, [ctypes.c_wchar_p], (arguments,), "SetArguments"),
            (consts.VTBL_SHELLLINK_SET_WORKING_DIRECTORY, [ctypes.c_wchar_p],
             (working_dir,), "SetWorkingDirectory"),
            (consts.VTBL_SHELLLINK_SET_ICON_LOCATION, [ctypes.c_wchar_p, ctypes.c_int],
             (icon_path, 0), "SetIconLocation"),
            (consts.VTBL_SHELLLINK_SET_DESCRIPTION, [ctypes.c_wchar_p],
             (description,), "SetDescription"),
        )
        for index, argtypes, args, name in steps:
            ret = int(_com_call(link, index, ctypes.c_long, argtypes, *args))
            if ret != consts.S_OK:
                logger.warning("IShellLinkW::%s 失败: 0x%08X", name, ret & 0xFFFFFFFF)
                return False

        hr = int(_com_call(
            link,
            consts.VTBL_QUERY_INTERFACE,
            ctypes.c_long,
            [ctypes.POINTER(consts.GUID), ctypes.POINTER(ctypes.c_void_p)],
            ctypes.byref(consts.IID_IPersistFile),
            ctypes.byref(persist),
        ))
        if hr != consts.S_OK or not persist:
            logger.warning("QueryInterface(IPersistFile) 失败: 0x%08X", hr & 0xFFFFFFFF)
            return False

        # 先删旧文件：让 Windows 的图标缓存失效，新图标立即生效
        try:
            dest.unlink(missing_ok=True)
        except OSError as exc:
            logger.warning("删除旧快捷方式失败 %s: %s", dest, exc)

        hr = int(_com_call(
            persist,
            consts.VTBL_PERSISTFILE_SAVE,
            ctypes.c_long,
            [ctypes.c_wchar_p, ctypes.c_int],
            str(dest),
            1,  # fRemember = TRUE
        ))
        if hr != consts.S_OK:
            logger.warning("IPersistFile::Save(%s) 失败: 0x%08X", dest, hr & 0xFFFFFFFF)
            return False
        ok = dest.exists() and dest.stat().st_size > 0
        if not ok:
            logger.warning("快捷方式写盘后校验失败: %s", dest)
        return ok
    except Exception as exc:  # noqa: BLE001 - 上层不希望为 COM 失败崩掉主流程
        logger.warning("创建快捷方式异常 %s: %s", dest, exc)
        return False
    finally:
        _release(persist)
        _release(link)
        if ok:
            logger.info("快捷方式已写入: %s → %s %s", dest, target, arguments)
