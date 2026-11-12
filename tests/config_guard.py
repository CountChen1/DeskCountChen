"""用户配置护栏（纯标准库，不导入 Qt）。

单独成文件的原因：`tests/qt_support.py` 在导入时会 `setdefault
QT_QPA_PLATFORM=offscreen`，而真实窗口探针必须在 windows 平台插件下运行。
探针若从 qt_support 借这个上下文管理器，会把进程强行切到 offscreen，
导致所有 Win32 窗口断言（hwnd/扩展样式/z-order）全部失真。

## 2026-11-12 事故后的语义变更（重要）

旧实现叫 ``preserve_user_config``：进入时把真实 ``.config.json`` 复制到
``%TEMP%\\dbx_config_backup.json``，退出时**无条件写回**。这看起来能救，实际不是修复：

- ``finally`` 只在正常退出 / 可捕获异常时执行。``Ctrl-C``、超时被杀、进程崩溃、
  ``os._exit`` —— 任何一种情况下备份都留在 temp 里，真实配置已被改名/覆盖；
- 进入时抓到的"备份"也可能已经是**错误的字节态**（例如上一次测试污染后的夹具配置），
  那样"还原"等于把错误固化下来。真实事故正是这样丢掉了 6 个 SAO 分类。

现在的能力边界被刻意缩小为**只读护栏**：**一个字节都不写**，只在进入/退出时对真实
配置取 sha256 指纹，退出时比对并在被改动时发出告警。真正的防污染手段是
``tests/sandbox.py`` —— 测试从一开始就不把路径指向真实目录。
"""

from __future__ import annotations

import contextlib
import logging
from pathlib import Path

from tests import sandbox

__all__ = [
    "VIOLATIONS",
    "preserve_user_config",
    "real_config_path",
]

logger = logging.getLogger(__name__)

# 发生过污染的记录（每条形如 ".config.json sha256 abc123 -> def456"）。
# 测试编排可以在结束时断言它为空的，以此作为"零污染"判据。
VIOLATIONS: list[str] = []


def real_config_path() -> Path:
    """真实 ``.config.json`` 路径（**未**被沙箱重定向时的值）。

    注意：如果调用方已经执行过 ``sandbox.sandbox_paths()``，``paths.CONFIG_PATH``
    就指向沙箱了。这个函数只在重定向之前有意义，因此这里直接按 USERPROFILE 拼，
    不依赖可变的模块属性。
    """
    import os

    return Path(os.environ.get("USERPROFILE", str(Path.home()))) / "DesktopBox" / ".config.json"


@contextlib.contextmanager
def preserve_user_config(strict: bool = False):
    """只读护栏：保证并证明"真实配置在代码块执行期间没有被改动"。

    Args:
        strict: 为 True 时，一旦检测到真实配置被改动就抛 ``RuntimeError``；
            默认 False 只记录到 :data:`VIOLATIONS` 并打印告警（避免掩盖首个异常）。

    Yields:
        进入时的真实配置字节（文件不存在则为 ``None``）。**仅供读取比对**，
        不要拿它写回任何地方 —— 那正是旧实现犯的错。
    """
    from app import paths

    # 已经沙箱化时，paths.CONFIG_PATH 指向 temp；此时护栏按真实路径单独取指纹。
    target = paths.CONFIG_PATH if not sandbox.is_sandboxed() else real_config_path()
    snapshot = sandbox.snapshot_of(target)
    payload: bytes | None = None
    try:
        payload = target.read_bytes() if target.is_file() else None
    except OSError:  # 防御：权限/占用导致读不到时不影响被测流程
        payload = None
    try:
        yield payload
    finally:
        ok, detail = sandbox.guard_untouched(snapshot)
        if not ok:
            message = f"【污染告警】真实配置被改动：{target} {detail}"
            VIOLATIONS.append(f"{target.name} {detail}")
            print(message)
            logger.error(message)
            if strict:
                raise RuntimeError(message)
