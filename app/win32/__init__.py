"""Win32 能力层的唯一对外入口。

用法：
    from app.win32 import window_ops, shell_link

所有 ctypes 细节都封装在本包内部，其他模块不得直接 import ctypes。
"""

from app.win32 import shell_link, window_ops  # noqa: F401  (re-export)

__all__ = ["window_ops", "shell_link"]
