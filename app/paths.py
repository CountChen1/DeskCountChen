"""路径集中管理：APP_DIR（程序私有目录）与 DATA_ROOT（分类数据根）。

所有目录创建集中在本模块，其他模块不允许直接调用 mkdir。

v1.4 概念拆分（桌面入口改由 SAO Utils 2 的 HUD 图标承担）：

- ``APP_DIR`` = ``%USERPROFILE%\\DesktopBox``：程序自己的东西（.config.json /
  .icons / .ico / .logs / .launchers），**永远不随数据根漂移**。理由：数据根
  换到 E 盘（甚至外接盘临时未挂载）时，配置与日志不能一起消失——那会让程序
  连"自己上次用哪个数据根"都读不到，直接变成另一套默认配置。
- ``DATA_ROOT`` = 分类数据根，默认等于 ``APP_DIR``（保持向后兼容）。由
  ``.config.json`` 的 ``app.data_root`` 指定，``main.py`` 在创建 controller 之前
  调用 :func:`set_data_root` 注入。

本模块**不读配置**（避免 config ↔ paths 循环依赖），只负责记住并确保目录存在。

兼容：``paths.ROOT`` 仍可用，等价于 ``paths.DATA_ROOT``（过渡别名，
见模块级 ``__getattr__``；新代码一律写 ``paths.DATA_ROOT``）。
"""

from __future__ import annotations

import logging
import os
from pathlib import Path

logger = logging.getLogger(__name__)

# 项目根目录（app 包的上一级：main.py / run.bat 所在处）
APP_ROOT: Path = Path(__file__).resolve().parents[1]

# 程序私有目录：%USERPROFILE%\DesktopBox（**永不随数据根漂移**）
APP_DIR: Path = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "DesktopBox"

# 分类数据根：默认 = APP_DIR；可由 set_data_root() 改写（例如 E:\快捷方式SAO）
DATA_ROOT: Path = APP_DIR

# 全局配置文件（永远在 APP_DIR 内）
CONFIG_PATH: Path = APP_DIR / ".config.json"

# 自定义分类图标存放目录（设置中选入的图片会复制到这里）
ICONS_DIR: Path = APP_DIR / ".icons"

# 分类图标的 ICO 缓存目录（每个分类一张 <cat_id>.ico）
ICO_DIR: Path = ICONS_DIR / "ico"

# 快捷方式同步状态（内容指纹，避免每次启动重写 .lnk 触发图标重排）
LNK_STATE_PATH: Path = ICONS_DIR / "lnk_state.json"

# 日志目录（永远在 APP_DIR 内：数据根不可用时仍要有日志可查）
LOGS_DIR: Path = APP_DIR / ".logs"
LOG_FILE: Path = LOGS_DIR / "app.log"

# 启动器 .lnk 目录（供 SAO Utils 2 的 HUD 图标引用；绝不写到真实桌面）
LAUNCHERS_DIR: Path = APP_DIR / ".launchers"

# 需要创建的基础目录（分类数据目录不在此列，按需创建）
_BASE_DIRS: tuple[Path, ...] = (APP_DIR, ICONS_DIR, ICO_DIR, LOGS_DIR, LAUNCHERS_DIR)


def __getattr__(name: str):
    """过渡别名：``paths.ROOT`` == ``paths.DATA_ROOT``（v1.5 移除）。

    用模块级 ``__getattr__``（PEP 562）而不是 ``ROOT = DATA_ROOT``：后者是
    导入时的值拷贝，``set_data_root()`` 之后别名会指向旧路径，属于"静默错误"。
    """
    if name == "ROOT":
        return DATA_ROOT
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


def set_data_root(path) -> Path:
    """设置分类数据根（幂等；Path 化；拒绝空值）。

    Args:
        path: 数据根路径（str / Path）。None、空串、纯空白一律忽略——
            "忽略"比"清空"安全：调用方传空值时保持上一次的有效值，
            避免把数据根意外打到磁盘根目录。

    Returns:
        当前生效的 DATA_ROOT（便于调用方日志打印）。
    """
    global DATA_ROOT
    if path is None:
        logger.warning("set_data_root 收到 None，保持当前数据根: %s", DATA_ROOT)
        return DATA_ROOT
    try:
        candidate = Path(str(path).strip())
    except (TypeError, ValueError) as exc:  # 防御：非法路径对象
        logger.warning("set_data_root 参数非法（%r），保持当前数据根: %s", path, exc)
        return DATA_ROOT
    if not str(candidate) or str(candidate) == ".":
        logger.warning("set_data_root 收到空路径，保持当前数据根: %s", DATA_ROOT)
        return DATA_ROOT
    DATA_ROOT = candidate
    logger.info("分类数据根 = %s（程序目录 = %s）", DATA_ROOT, APP_DIR)
    return DATA_ROOT


def ensure_base_dirs() -> None:
    """确保基础目录存在（幂等）。

    - APP_DIR 系列（配置 / 图标 / 日志 / 启动器）创建失败只记日志；
    - DATA_ROOT 也尝试创建，但**失败不抛**：外接盘未挂载时程序仍要能启动
        （用户能看到托盘与日志，而不是进程直接消失）。
    """
    for d in _BASE_DIRS:
        try:
            d.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.error("创建目录失败 %s: %s", d, exc)
    try:
        DATA_ROOT.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("创建分类数据根失败（分类目录将不可用）%s: %s", DATA_ROOT, exc)


def category_dir(dir_name: str) -> Path:
    """返回分类数据目录路径（不创建；基于 DATA_ROOT）。"""
    return DATA_ROOT / dir_name


def ensure_category_dir(dir_name: str) -> Path:
    """返回分类数据目录路径，不存在则创建（幂等；基于 DATA_ROOT）。"""
    d = DATA_ROOT / dir_name
    try:
        d.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        logger.error("创建分类目录失败 %s: %s", d, exc)
    return d
