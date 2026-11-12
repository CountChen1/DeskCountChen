"""测试沙箱：把 ``app.paths`` 的整个命名空间重定向到临时目录。

## 为什么必须有这个模块（事故复盘 2026-11-12）

e2e / 回归脚本会写配置、建分类目录、生成 ``.ico`` / ``.lnk``。这些路径一旦指向
真实的 ``%USERPROFILE%\\DesktopBox``，就会覆盖用户的正式配置 —— 实际发生过一次：
6 个 SAO 分类被测试夹具冲掉、``data_root`` 退回默认值。

原来的"跑前备份 + ``finally`` 无条件还原"看起来能救，**但它不是修复**：

- ``finally`` 只在正常退出 / 可捕获异常时执行；``Ctrl-C``、超时被杀、进程崩溃、
  ``os._exit``、断言在 ``finally`` 之前抛错 —— 任何一条都会让备份永远留在 temp 里，
  正式配置已经没了；
- 备份抓取时机也可能错（抓到的是夹具态 / 迁移前的中间态），那就"还原"成错的。

所以正确做法是**测试从一开始就不碰真实路径**：进程内的 ``app.paths`` 全部指向
临时沙箱，真实配置只读一次做指纹，结束时用 sha256 证明"一个字节都没动"。

## 用法

```python
from tests import sandbox

real = sandbox.snapshot_real()      # ① 先记真实配置路径 + sha256（此时还没重定向）
root = sandbox.sandbox_paths()      # ② 整体重定向到临时沙箱（必须早于任何 paths.* 使用）
paths.ensure_base_dirs()            # ③ 之后所有 IO 都落在 root 下
...
ok, detail = sandbox.guard_real_untouched(real)   # ④ 结束时护栏（可喂给 check()）
```

注意：``sandbox_paths()`` 会同步改写 ``paths._BASE_DIRS``（``ensure_base_dirs``
遍历的那个 tuple），否则 ``ensure_base_dirs`` 仍会去真实目录建文件夹。
``paths.ROOT`` 是模块级 ``__getattr__`` 动态别名，会自动跟随 ``DATA_ROOT``，
无需单独设置。
"""

from __future__ import annotations

import hashlib
import tempfile
from dataclasses import dataclass
from pathlib import Path

__all__ = [
    "ANCHOR_ATTRS",
    "RealState",
    "digest",
    "guard_real_untouched",
    "guard_untouched",
    "is_sandboxed",
    "sandbox_paths",
    "snapshot_of",
    "snapshot_real",
]

# 是否已经重定向过（sandbox_paths 里置 True）
_SANDBOXED = False
# 最近一次创建的沙箱根（便于调用方清理）
_SANDBOX_ROOT: Path | None = None

# 需要重定向的 paths 属性（与 app/paths.py 的模块级常量一一对应）
ANCHOR_ATTRS = (
    "APP_DIR",
    "DATA_ROOT",
    "CONFIG_PATH",
    "ICONS_DIR",
    "ICO_DIR",
    "LNK_STATE_PATH",
    "LOGS_DIR",
    "LOG_FILE",
    "LAUNCHERS_DIR",
)


@dataclass(frozen=True)
class RealState:
    """重定向前抓下的真实状态快照（用于最后的零污染护栏）。"""

    config_path: Path
    exists: bool
    digest: str          # 文件不存在时为空串

    def describe(self) -> str:
        return f"{self.config_path} sha256={self.digest[:12]}…"


def digest(path) -> str:
    """文件内容 sha256（不存在返回空串，不抛异常）。"""
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except (OSError, TypeError, ValueError):
        return ""


def snapshot_of(path) -> RealState:
    """记录任意文件的路径 + 指纹（文件不存在时 digest 为空串）。"""
    target = Path(path)
    exists = target.is_file()
    return RealState(
        config_path=target,
        exists=exists,
        digest=digest(target) if exists else "",
    )


def snapshot_real() -> RealState:
    """记录**真实**配置路径与指纹。必须在 :func:`sandbox_paths` 之前调用。"""
    from app import paths

    return snapshot_of(paths.CONFIG_PATH)


def is_sandboxed() -> bool:
    """是否已经调用过 :func:`sandbox_paths`（即 ``paths`` 已被重定向）。"""
    return _SANDBOXED


def sandbox_root() -> Path | None:
    """最近一次创建的沙箱根（未创建时为 None）。"""
    return _SANDBOX_ROOT


def _assert_isolated(root: Path) -> None:
    """自校验：重定向后所有 paths 锚点必须落在沙箱根下，否则立刻炸。

    这是为了防止"看起来沙箱了、其实还有一条路指向真实目录"的假隔离
    —— 例如 ``ensure_base_dirs()`` 走的是 ``paths._BASE_DIRS``，漏同步就会去真实
    ``%USERPROFILE%\\DesktopBox`` 建目录；又例如有人把 ``DATA_ROOT`` 落回真实盘，
    ``AppController._ensure_categories()`` 就会去扫真实目录并把残留的 ``常用``
    登记成分类、进而往落点写 ``启动-常用.lnk``。
    """
    from app import paths

    for attr in ANCHOR_ATTRS:
        value = getattr(paths, attr, None)
        try:
            Path(value).relative_to(root)
        except (TypeError, ValueError):
            raise RuntimeError(f"沙箱隔离失败：paths.{attr}={value!r} 不在 {root} 下")
    for d in paths._BASE_DIRS:  # noqa: SLF001
        try:
            Path(d).relative_to(root)
        except (TypeError, ValueError):
            raise RuntimeError(f"沙箱隔离失败：_BASE_DIRS 含 {d} 不在 {root} 下")


def sandbox_paths(prefix: str = "dbx_sandbox_") -> Path:
    """把 ``app.paths`` 全部重定向到新建的临时目录，返回沙箱根。

    必须在**任何** ``paths.*`` 使用之前调用（模块导入后立即调用最安全）。
    幂等性说明：重复调用会再建一个沙箱并再次重定向（旧沙箱由调用方自行清理），
    因此脚本里只应调用一次。
    """
    global _SANDBOXED, _SANDBOX_ROOT
    from app import paths

    root = Path(tempfile.mkdtemp(prefix=prefix))
    paths.APP_DIR = root
    paths.DATA_ROOT = root / "data"
    paths.CONFIG_PATH = root / ".config.json"
    paths.ICONS_DIR = root / ".icons"
    paths.ICO_DIR = root / ".icons" / "ico"
    paths.LNK_STATE_PATH = root / ".icons" / "lnk_state.json"
    paths.LOGS_DIR = root / ".logs"
    paths.LOG_FILE = root / ".logs" / "app.log"
    paths.LAUNCHERS_DIR = root / ".launchers"
    # ensure_base_dirs() 遍历的是这个 tuple，必须同步替换，否则仍会建真实目录
    paths._BASE_DIRS = (  # noqa: SLF001 - 测试隔离必须动这个内部常量
        paths.APP_DIR,
        paths.ICONS_DIR,
        paths.ICO_DIR,
        paths.LOGS_DIR,
        paths.LAUNCHERS_DIR,
    )
    _SANDBOXED = True
    _SANDBOX_ROOT = root
    _assert_isolated(root)   # 立刻自校验，别把"假隔离"带到用例里
    return root


def guard_real_untouched(real: RealState) -> tuple[bool, str]:
    """零污染护栏：对比真实配置的 sha256 是否仍是快照里的值。

    Returns:
        ``(是否未改动, 说明文本)``，可直接喂给各脚本的 ``check()``。
        说明文本形如 ``7d19cae5… -> 7d19cae5…（未改动）``。
    """
    return guard_untouched(real)


def guard_untouched(real: RealState) -> tuple[bool, str]:
    """同 :func:`guard_real_untouched`，名字更中性（可校验任意文件）。"""
    now_exists = real.config_path.is_file()
    now = digest(real.config_path) if now_exists else ""
    ok = (now == real.digest) and (now_exists == real.exists)
    tail = "（未改动）" if ok else "（**被改动了**）"
    return ok, f"{real.digest[:12]} -> {now[:12]}{tail}"
