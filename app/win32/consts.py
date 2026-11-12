"""全部 ctypes 常量与 Win32 / COM 函数声明（64 位句柄安全）。

v1.2 变更（悬浮窗体系移除后的声明面）：
- **不再**声明置底/贴桌面/可见性/窗口样式相关函数（Progman、SetWindowPos、
  IsWindowVisible、ShowWindowAsync、GWL_EXSTYLE ... 随 CategoryIconWindow 一起删除）；
- 新增：真实桌面路径（SHGetKnownFolderPath + FOLDERID_Desktop）与快捷方式 COM
  写盘（CoCreateInstance / IShellLinkW / IPersistFile）。

硬性约束（历史踩坑，违反必炸）：
- 所有涉及 HWND / HANDLE / 接口指针的函数必须显式设置 argtypes / restype，
  否则 64 位下句柄会被截断成 int32；
- 返回值用 `ctypes.c_long` 承载 HRESULT，**不要**用 `ctypes.HRESULT`——
  后者在失败时直接抛异常，与 window_ops「不抛异常、只记日志」的约定冲突。
"""

from __future__ import annotations

import ctypes
from ctypes import wintypes

kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
shell32 = ctypes.WinDLL("shell32", use_last_error=True)
shlwapi = ctypes.WinDLL("shlwapi", use_last_error=True)
ole32 = ctypes.WinDLL("ole32", use_last_error=True)

# ---------------------------------------------------------------------------
# 常量
# ---------------------------------------------------------------------------

SW_SHOWNORMAL = 1

FILE_ATTRIBUTE_HIDDEN = 0x00000002
FILE_ATTRIBUTE_NORMAL = 0x00000080
ERROR_ALREADY_EXISTS = 183

HANDLE = wintypes.HANDLE
BOOL = wintypes.BOOL
DWORD = wintypes.DWORD
LPCWSTR = wintypes.LPCWSTR

# COM 初始化 / 激活上下文
COINIT_APARTMENTTHREADED = 0x2           # STA（Qt 在 GUI 线程已初始化 OLE，重复调用返回 S_FALSE）
CLSCTX_INPROC_SERVER = 0x1
S_OK = 0
S_FALSE = 1
RPC_E_CHANGED_MODE = -2147417850        # 0x80010106：线程已是另一种 apartment
E_NOINTERFACE = -2147467262             # 0x80004002

# SHGetKnownFolderPath 的 KF_FLAG
KF_FLAG_DEFAULT = 0x00000000

# IUnknown 三个方法在所有 COM 接口的 vtable 中固定占 0..2
VTBL_QUERY_INTERFACE = 0
VTBL_ADD_REF = 1
VTBL_RELEASE = 2

# IShellLinkW 的 vtable 索引（0 基，IUnknown 占 0..2）
VTBL_SHELLLINK_GET_PATH = 3
VTBL_SHELLLINK_SET_DESCRIPTION = 7
VTBL_SHELLLINK_SET_WORKING_DIRECTORY = 9
VTBL_SHELLLINK_GET_ARGUMENTS = 10
VTBL_SHELLLINK_SET_ARGUMENTS = 11
VTBL_SHELLLINK_SET_ICON_LOCATION = 17
VTBL_SHELLLINK_GET_ICON_LOCATION = 16
VTBL_SHELLLINK_SET_PATH = 20

# IShellLinkW::GetPath 的 fFlags
SLGP_SHORTPATH = 0x1
SLGP_UNCPRIORITY = 0x2
SLGP_RAWPATH = 0x4      # 解析后不展开环境变量、不改写成短名（读取 .lnk 用这个）

# IPersistFile 的 vtable 索引（0 基，IUnknown 占 0..2）
VTBL_PERSISTFILE_LOAD = 5
VTBL_PERSISTFILE_SAVE = 6

# 从 .lnk 读字符串的缓冲区长度（MAX_PATH=260 不够：长路径 + 参数可能超长）
LNK_STRING_MAX = 1024

# ---------------------------------------------------------------------------
# 结构体 / GUID
# ---------------------------------------------------------------------------


class GUID(ctypes.Structure):
    """Windows GUID（16 字节，按值传给 COM 时必须是结构体而不是字符串）。"""

    _fields_ = [
        ("Data1", wintypes.DWORD),
        ("Data2", wintypes.WORD),
        ("Data3", wintypes.WORD),
        ("Data4", ctypes.c_ubyte * 8),
    ]


def _guid(d1: int, d2: int, d3: int, tail: tuple[int, ...]) -> GUID:
    """由分段整数构造 GUID（避免依赖 uuid.UUID 的字符串解析）。"""
    return GUID(d1, d2, d3, (ctypes.c_ubyte * 8)(*tail))


# {00021401-0000-0000-C000-000000000046}  ShellLink 组件类
CLSID_ShellLink = _guid(0x00021401, 0x0000, 0x0000, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
# {000214F9-0000-0000-C000-000000000046}  IShellLinkW
IID_IShellLinkW = _guid(0x000214F9, 0x0000, 0x0000, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
# {0000010B-0000-0000-C000-000000000046}  IPersistFile
IID_IPersistFile = _guid(0x0000010B, 0x0000, 0x0000, (0xC0, 0, 0, 0, 0, 0, 0, 0x46))
# {B4BFCC3A-DB2C-424C-B029-7FE99A87C641}  FOLDERID_Desktop
FOLDERID_Desktop = _guid(0xB4BFCC3A, 0xDB2C, 0x424C, (0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))

# ---------------------------------------------------------------------------
# 函数声明（argtypes / restype 必须显式设置）
# ---------------------------------------------------------------------------

# CreateMutexW（SECURITY_ATTRIBUTES 在 3.13 的 wintypes 中已移除，用 void* 代替）
_CreateMutexW = kernel32.CreateMutexW
_CreateMutexW.argtypes = [ctypes.c_void_p, BOOL, LPCWSTR]
_CreateMutexW.restype = HANDLE

_CloseHandle = kernel32.CloseHandle
_CloseHandle.argtypes = [HANDLE]
_CloseHandle.restype = BOOL

# SetFileAttributesW
_SetFileAttributesW = kernel32.SetFileAttributesW
_SetFileAttributesW.argtypes = [LPCWSTR, DWORD]
_SetFileAttributesW.restype = BOOL

# ShellExecuteW
_ShellExecuteW = shell32.ShellExecuteW
_ShellExecuteW.argtypes = [wintypes.HWND, LPCWSTR, LPCWSTR, LPCWSTR, LPCWSTR, ctypes.c_int]
_ShellExecuteW.restype = wintypes.HINSTANCE

# SetCurrentProcessExplicitAppUserModelID
_SetCurrentProcessExplicitAppUserModelID = shell32.SetCurrentProcessExplicitAppUserModelID
_SetCurrentProcessExplicitAppUserModelID.argtypes = [LPCWSTR]
_SetCurrentProcessExplicitAppUserModelID.restype = ctypes.c_long

# StrCmpLogicalW（资源管理器同款自然排序）
_StrCmpLogicalW = shlwapi.StrCmpLogicalW
_StrCmpLogicalW.argtypes = [LPCWSTR, LPCWSTR]
_StrCmpLogicalW.restype = ctypes.c_int

# SHGetKnownFolderPath（真实桌面路径，含 OneDrive / 企业重定向）
_SHGetKnownFolderPath = shell32.SHGetKnownFolderPath
_SHGetKnownFolderPath.argtypes = [
    ctypes.POINTER(GUID),
    DWORD,
    HANDLE,
    ctypes.POINTER(ctypes.c_wchar_p),
]
_SHGetKnownFolderPath.restype = ctypes.c_long

# COM 基础
_CoInitializeEx = ole32.CoInitializeEx
_CoInitializeEx.argtypes = [ctypes.c_void_p, DWORD]
_CoInitializeEx.restype = ctypes.c_long

_CoUninitialize = ole32.CoUninitialize
_CoUninitialize.argtypes = []
_CoUninitialize.restype = None

_CoCreateInstance = ole32.CoCreateInstance
_CoCreateInstance.argtypes = [
    ctypes.POINTER(GUID),
    ctypes.c_void_p,
    DWORD,
    ctypes.POINTER(GUID),
    ctypes.POINTER(ctypes.c_void_p),
]
_CoCreateInstance.restype = ctypes.c_long

_CoTaskMemFree = ole32.CoTaskMemFree
_CoTaskMemFree.argtypes = [ctypes.c_void_p]
_CoTaskMemFree.restype = None
