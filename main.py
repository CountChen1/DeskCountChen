"""Desk CountChen 进程入口（``Desk CountChen`` by CountChen）。

流程：logging → 解析 --version / --open / --selfcheck → 单实例（互斥量 / 唤醒已有实例）
→ QApplication → 数据根注入 → AppController → QLocalServer → app.exec()。

命令行参数：
- ``--open <分类目录名>``：由启动器 .lnk（SAO 的 HUD 图标）传入，打开该分类的面板；
- ``--console``：带控制台窗口**真实运行**（日志同时进 stderr，便于实机排查）；
- ``--selfcheck``：仅做基础设施自检，打印"基础设施就绪"后退出（不碰互斥量）；
- ``--version``：打印产品名 + 版本 + 构建指纹后退出（零副作用，用于验真伪）。

v1.2 关键变化（桌面图标改为 Windows 真实 .lnk 快捷方式）：
- 快捷方式的目标就是 ``pythonw.exe main.py --open <分类目录名>``，
  所以点图标 = 起第二个进程；
- 第二进程若发现互斥量已被持有，**通过 QLocalSocket 把"分类目录名 + 当前鼠标
  落点"发给第一实例，然后静默退出**（绝不再弹"已在运行"对话框）；
- 第一实例启动 QLocalServer 收消息 → 直接打开该分类的面板。

v1.4 关键变化（入口改为 SAO Utils 2 的 HUD 图标 + 数据根可配置）：
- 日志初始化仍在 ``paths.set_data_root()`` **之前**，日志永远写 ``APP_DIR``
  （数据根在 E 盘且未挂载时，也必须有日志可查）；
- QApplication 之后、构造 controller 之前读取 ``app.data_root`` 并注入 paths，
  再 ``ensure_base_dirs()``——顺序敏感：controller 构造里就会用到 DATA_ROOT。
"""

from __future__ import annotations

import logging
import os
import sys
from logging.handlers import RotatingFileHandler

from PySide6.QtGui import QCursor
from PySide6.QtWidgets import QApplication, QMessageBox

from app import brand, paths, single_instance
from app.config import DRAG_THRESHOLD_PX, AppConfig
from app.win32 import window_ops

LOG_MAX_BYTES = 512 * 1024
LOG_BACKUP_COUNT = 2
# 版本号唯一来源 = app/__init__.py::__version__（经 brand 投影），不再另写字面量
APP_VERSION = brand.VERSION


def _init_logging(console: bool) -> None:
    level = logging.DEBUG if os.environ.get("DESKCOUNTCHEN_DEBUG") == "1" else logging.INFO
    fmt = "%(asctime)s %(levelname)s %(name)s: %(message)s"
    handlers: list[logging.Handler] = [
        RotatingFileHandler(
            paths.LOG_FILE,
            maxBytes=LOG_MAX_BYTES,
            backupCount=LOG_BACKUP_COUNT,
            encoding="utf-8",
        )
    ]
    if console:
        handlers.append(logging.StreamHandler())
    logging.basicConfig(level=level, format=fmt, handlers=handlers)
    logging.getLogger("PySide6").setLevel(logging.WARNING)


def parse_open_argument(argv: list[str]) -> str | None:
    """从命令行解析 ``--open <分类目录名>``（也支持 ``--open=<名字>``）。

    桌面快捷方式传的是 ``--open "常用"``，Windows 已去掉引号，这里拿到的
    就是裸的分类目录名（可能含空格）。
    """
    for index, item in enumerate(argv):
        if item == "--open":
            if index + 1 < len(argv):
                return argv[index + 1]
            return ""
        if item.startswith("--open="):
            return item.split("=", 1)[1]
    return None


def _install_crash_handlers() -> None:
    """pythonw 没有控制台：未捕获异常与硬崩溃都必须留下可查证据。"""
    import faulthandler

    try:
        crash_file = open(paths.LOGS_DIR / "crash.log", "a", encoding="utf-8")
        faulthandler.enable(file=crash_file, all_threads=True)
        _OPEN_FILES.append(crash_file)  # 保活：被 GC 回收会关闭文件描述符
    except OSError as exc:
        logging.getLogger("main").warning("faulthandler 初始化失败: %s", exc)

    def _hook(exc_type, exc, tb) -> None:
        logging.getLogger("uncaught").critical(
            "未捕获异常（进程即将退出）", exc_info=(exc_type, exc, tb)
        )

    sys.excepthook = _hook


_OPEN_FILES: list = []


def _apply_data_root() -> None:
    """读取 ``app.data_root`` 并注入 paths（失败一律回落默认 APP_DIR）。

    刻意放在日志初始化之后：日志必须写在 APP_DIR，不能被数据根带偏。
    任何异常都不外泄——配置损坏时照常启动（分类数据根退回默认目录）。
    """
    logger = logging.getLogger("main")
    try:
        cfg = AppConfig.load(paths.CONFIG_PATH)
        data_root = cfg.app.data_root
    except Exception as exc:  # noqa: BLE001 - pythonw 下异常外泄 = 静默崩溃
        logger.warning("读取数据根配置失败，使用默认数据根: %s", exc)
        return
    if data_root:
        paths.set_data_root(data_root)
    else:
        logger.info("未配置 data_root，使用默认数据根: %s", paths.DATA_ROOT)
    paths.ensure_base_dirs()


def main(argv: list[str]) -> int:
    # ``--version`` 必须在任何副作用之前：不建目录、不写日志、不碰互斥量，
    # 这样它在只读环境 / 数据盘未挂载时也能正常回答"你是谁"。
    if "--version" in argv:
        print(brand.version_line())
        return 0

    console = "--console" in argv
    # v1.2：``--console`` 改为"带控制台窗口真实运行"（QA 需要边跑边看日志），
    # 基础设施自检独立成 ``--selfcheck``（打印后立即退出，且在拿互斥量之前，
    # 这样已有实例在跑时也能自检）。
    selfcheck = "--selfcheck" in argv
    paths.ensure_base_dirs()
    _init_logging(console)
    _install_crash_handlers()
    logger = logging.getLogger("main")

    if selfcheck:
        print("基础设施就绪")
        return 0

    window_ops.set_app_user_model_id(brand.APP_USER_MODEL_ID)

    app = QApplication(argv)
    app.setApplicationName(brand.PRODUCT_NAME)
    app.setApplicationVersion(APP_VERSION)
    app.setQuitOnLastWindowClosed(False)  # 托盘常驻；窗口只隐藏不关闭
    # 面板条目拖拽阈值固定 4px：全局设置让 QListView 原生拖拽与需求一致
    QApplication.setStartDragDistance(DRAG_THRESHOLD_PX)

    # 数据根必须在构造 controller 之前注入（controller 构造里就会用 DATA_ROOT）
    _apply_data_root()
    logger.info("%s 启动：版本 %s；数据根 = %s；程序目录 = %s",
                brand.PRODUCT_NAME, APP_VERSION, paths.DATA_ROOT, paths.APP_DIR)

    open_dir = parse_open_argument(argv)
    # 第二实例的鼠标位置就是用户双击桌面图标的落点；必须在任何阻塞前取
    cursor = QCursor.pos()

    if not window_ops.acquire_single_instance_mutex():
        if open_dir is None:
            logger.info("已有 %s 实例在运行，本次不再启动", brand.PRODUCT_NAME)
            return 0
        logger.info("已有 %s 实例在运行，尝试唤醒（dir=%s）", brand.PRODUCT_NAME, open_dir)
        if single_instance.send_open_request(open_dir, cursor.x(), cursor.y()):
            return 0  # 静默退出：面板由已运行实例弹出
        # 唤醒失败（旧版本实例 / 端点不可用）→ 才降级为提示框
        QMessageBox.warning(
            None, brand.PRODUCT_NAME,
            f"{brand.PRODUCT_NAME} 已在运行，但无法唤醒已有实例。",
        )
        return 0

    if console:
        # 带控制台运行：日志同时输出到 stderr，便于在真实桌面上排查
        logger.info("%s %s 以控制台模式启动", brand.PRODUCT_NAME, APP_VERSION)

    from app.controller import AppController

    controller = AppController(app)
    server = single_instance.SingleInstanceServer()
    if not server.start():
        logger.warning("单实例唤醒服务启动失败：双击桌面图标将无法唤起本实例")
    server.open_requested.connect(controller.open_panel_for_dir)

    controller.start()
    if open_dir:
        # 本进程就是由 .lnk 拉起的第一个实例：直接打开对应分类
        controller.open_panel_for_dir(open_dir, cursor.x(), cursor.y())

    ret = app.exec()
    server.stop()
    controller.shutdown()
    return ret


if __name__ == "__main__":
    try:
        _exit_code = main(sys.argv)
    except Exception:  # noqa: BLE001 - pythonw 下必须留下堆栈，否则"静默崩溃"
        logging.getLogger("main").exception("主流程异常退出")
        raise
    sys.exit(_exit_code)
