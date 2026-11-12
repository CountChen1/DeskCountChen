"""ctypes 只读解析 .lnk（Shell Link）—— 取目标路径 / 参数 / 图标位置。

用途（v1.4）：
1. **图标去"快捷方式"小箭头**：解析 .lnk 拿到真实目标（.exe / 目录）后，
   对这个目标取图标，就不会带系统叠加的快捷方式箭头；
2. **迁移期安全带**：改数据根前要把旧桌面 .lnk 清掉，删之前必须确认
   "这个 .lnk 确实指向本项目的 pythonw + --open"，否则一个都不能删。

设计（与 ``shell_link.py`` 同源的 COM 基建，不引入任何第三方依赖）：
``CoCreateInstance(CLSID_ShellLink, IID_IShellLinkW)`` →
``QueryInterface(IID_IPersistFile)`` → ``Load(<.lnk 路径>)`` →
``GetPath(SLGP_RAWPATH)`` / ``GetArguments`` / ``GetIconLocation``。

硬约定：
- **只读**，绝不写盘、绝不 Resolve()（Resolve 会修改 .lnk 的相对路径缓存）；
- 任何失败（文件不存在、不是 .lnk、COM 不可用、目标为空）一律返回空值，
  **不抛异常**——调用方在 GUI 线程上取图，异常外泄会让 pythonw 静默崩溃。
"""

from __future__ import annotations

import ctypes
import logging
from dataclasses import dataclass
from pathlib import Path

from app.win32 import consts

logger = logging.getLogger(__name__)

__all__ = ["LinkTarget", "read_link", "resolve"]


@dataclass(frozen=True)
class LinkTarget:
    """一个 .lnk 的只读解析结果（解析失败时各字段为空值）。"""

    target: str = ""            # 目标路径（SLGP_RAWPATH，未展开环境变量）
    arguments: str = ""         # 命令行参数（不含目标本身）
    icon_location: str = ""     # 自定义图标文件（.ico/.exe/.dll）
    icon_index: int = -1        # 图标序号；-1 = 未解析/无

    @property
    def ok(self) -> bool:
        """是否解析出至少一个可用信息。"""
        return bool(self.target or self.icon_location)


def _com_call(this, index: int, restype, argtypes, *args):
    """调用 COM 接口 vtable 上第 index 个方法（第一个隐式参数为 this）。"""
    vtbl_ptr = ctypes.cast(this, ctypes.POINTER(ctypes.POINTER(ctypes.c_void_p)))
    entry = vtbl_ptr.contents[index]
    proto = ctypes.WINFUNCTYPE(restype, ctypes.c_void_p, *argtypes)
    return proto(entry)(this, *args)


def _release(this) -> None:
    """释放 COM 接口（引用计数 -1），失败只记日志。"""
    if not this:
        return
    try:
        _com_call(this, consts.VTBL_RELEASE, ctypes.c_ulong, [])
    except Exception as exc:  # noqa: BLE001
        logger.warning("COM Release 失败（shell_resolve）: %s", exc)


def _ensure_com() -> bool:
    """保证当前线程已初始化 COM（复用 shell_link 的状态，幂等）。"""
    from app.win32 import shell_link  # 延迟导入：避免循环依赖

    return shell_link._ensure_com()  # noqa: SLF001 - 同包内部基建，刻意复用


def read_link(path) -> LinkTarget:
    """解析 .lnk，返回 :class:`LinkTarget`（失败返回全空实例，绝不抛异常）。

    Args:
        path: .lnk 文件路径（str / Path）；不存在或非 .lnk 直接返回空结果。

    Returns:
        LinkTarget。``ok`` 为 False 表示没解析出任何可用信息。
    """
    empty = LinkTarget()
    try:
        link_path = Path(str(path))
    except (TypeError, ValueError):
        return empty
    # 先做极轻量的文件检查：避免为"不存在的路径"建 COM 对象（面板里失效
    # 快捷方式很常见，每个都建一次 COM 实例是纯浪费）
    try:
        if not link_path.is_file() or link_path.suffix.lower() != ".lnk":
            return empty
    except OSError:
        return empty

    if not _ensure_com():
        return empty

    link = ctypes.c_void_p()
    persist = ctypes.c_void_p()
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
            return empty

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
            return empty

        # IPersistFile::Load(pszFileName, dwMode=STGM_READ)
        hr = int(_com_call(
            persist,
            consts.VTBL_PERSISTFILE_LOAD,
            ctypes.c_long,
            [ctypes.c_wchar_p, ctypes.c_int],
            str(link_path),
            0,  # STGM_READ
        ))
        if hr != consts.S_OK:
            logger.warning("IPersistFile::Load(%s) 失败: 0x%08X", link_path, hr & 0xFFFFFFFF)
            return empty

        target = _get_path(link)
        arguments = _get_arguments(link)
        icon_location, icon_index = _get_icon_location(link)
        return LinkTarget(
            target=target,
            arguments=arguments,
            icon_location=icon_location,
            icon_index=icon_index,
        )
    except Exception as exc:  # noqa: BLE001 - COM/ctypes 问题不得外泄
        logger.warning("解析快捷方式异常 %s: %s", link_path, exc)
        return empty
    finally:
        _release(persist)
        _release(link)


def resolve(path) -> tuple[str, str, int]:
    """解析 .lnk → ``(target, icon_location, icon_index)``。

    解析失败一律返回 ``("", "", -1)``，调用方无需做 None 判断。
    """
    info = read_link(path)
    return info.target, info.icon_location, info.icon_index


# -- IShellLinkW 的三个 Getter --------------------------------------------


def _get_path(link) -> str:
    """``GetPath(SLGP_RAWPATH)``：目标路径（空串 = 目标不是文件/未设置）。"""
    buf = ctypes.create_unicode_buffer(consts.LNK_STRING_MAX)
    try:
        # GetPath(pszFile, cch, WIN32_FIND_DATAW *pfd = NULL, DWORD fFlags)
        hr = int(_com_call(
            link,
            consts.VTBL_SHELLLINK_GET_PATH,
            ctypes.c_long,
            [ctypes.c_wchar_p, ctypes.c_int, ctypes.c_void_p, ctypes.c_ulong],
            buf,
            consts.LNK_STRING_MAX,
            None,
            consts.SLGP_RAWPATH,
        ))
        if hr != consts.S_OK:
            return ""
        return str(buf.value or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("IShellLinkW::GetPath 失败: %s", exc)
        return ""


def _get_arguments(link) -> str:
    """``GetArguments``：命令行参数（空串 = 无参数）。"""
    buf = ctypes.create_unicode_buffer(consts.LNK_STRING_MAX)
    try:
        hr = int(_com_call(
            link,
            consts.VTBL_SHELLLINK_GET_ARGUMENTS,
            ctypes.c_long,
            [ctypes.c_wchar_p, ctypes.c_int],
            buf,
            consts.LNK_STRING_MAX,
        ))
        if hr != consts.S_OK:
            return ""
        return str(buf.value or "").strip()
    except Exception as exc:  # noqa: BLE001
        logger.warning("IShellLinkW::GetArguments 失败: %s", exc)
        return ""


def _get_icon_location(link) -> tuple[str, int]:
    """``GetIconLocation``：``(图标文件, 序号)``；未设置时返回 ("", -1)。"""
    buf = ctypes.create_unicode_buffer(consts.LNK_STRING_MAX)
    index = ctypes.c_int(-1)
    try:
        hr = int(_com_call(
            link,
            consts.VTBL_SHELLLINK_GET_ICON_LOCATION,
            ctypes.c_long,
            [ctypes.c_wchar_p, ctypes.c_int, ctypes.POINTER(ctypes.c_int)],
            buf,
            consts.LNK_STRING_MAX,
            ctypes.byref(index),
        ))
        if hr != consts.S_OK:
            return "", -1
        return str(buf.value or "").strip(), int(index.value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("IShellLinkW::GetIconLocation 失败: %s", exc)
        return "", -1
