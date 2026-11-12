"""托盘菜单构建（动作以 dispatch 回调交给 controller）。

v1.2 变更：
- 桌面图标已改为 Windows 真实 .lnk 快捷方式，**右键菜单入口随之取消**
  （右键桌面快捷方式由系统负责：打开/属性/删除，我们不再接管）；
- 因此所有分类级操作（打开面板 / 设置 / 打开此文件夹 / 隐藏或显示）都搬到
  **托盘菜单里每个分类的子菜单**，保证功能不丢；
- 托盘菜单结构：``<分类子菜单...> ｜ 新建分类 ｜ 打开数据目录 ｜ 退出``。

v1.4 变更（入口改为 SAO Utils 2 的 HUD 图标）：
- 桌面不再有我们的图标，新增「打开启动器目录（供 SAO 引用）」：
  SAO 的 "Change Action" 要从这里挑 ``启动-<分类>.lnk``。
"""

from __future__ import annotations

from typing import Callable, Iterable

from PySide6.QtWidgets import QMenu

# 分类级动作键（托盘里每个分类的子菜单）
CAT_ACTION_OPEN_PANEL = "open_panel"
CAT_ACTION_OPEN_FOLDER = "open_folder"
CAT_ACTION_RENAME = "rename"
CAT_ACTION_SETTINGS = "settings"
CAT_ACTION_HIDE = "hide"      # 隐藏 = 删除快捷方式
CAT_ACTION_SHOW = "show"      # 显示 = 重新创建快捷方式

# 托盘菜单全局动作键
TRAY_ACTION_NEW_CATEGORY = "new_category"
TRAY_ACTION_OPEN_DATA_DIR = "open_data_dir"
TRAY_ACTION_OPEN_LAUNCHERS = "open_launchers"
TRAY_ACTION_EXIT = "exit"

__all__ = [
    "CAT_ACTION_OPEN_PANEL",
    "CAT_ACTION_OPEN_FOLDER",
    "CAT_ACTION_RENAME",
    "CAT_ACTION_SETTINGS",
    "CAT_ACTION_HIDE",
    "CAT_ACTION_SHOW",
    "TRAY_ACTION_NEW_CATEGORY",
    "TRAY_ACTION_OPEN_DATA_DIR",
    "TRAY_ACTION_OPEN_LAUNCHERS",
    "TRAY_ACTION_EXIT",
    "build_category_menu",
    "build_tray_menu",
]

Dispatch = Callable[[str, "str | None"], None]


def build_category_menu(parent, dispatch: Dispatch, cat_id: str,
                        label: str, visible: bool) -> QMenu:
    """单个分类的子菜单（打开面板 / 设置 / 打开此文件夹 / 隐藏或显示 / 重命名）。"""
    menu = QMenu(label, parent)
    menu.addAction("打开面板", lambda: dispatch(CAT_ACTION_OPEN_PANEL, cat_id))
    menu.addAction("设置…", lambda: dispatch(CAT_ACTION_SETTINGS, cat_id))
    menu.addAction("打开此文件夹", lambda: dispatch(CAT_ACTION_OPEN_FOLDER, cat_id))
    menu.addSeparator()
    if visible:
        menu.addAction("隐藏（删除启动器快捷方式）", lambda: dispatch(CAT_ACTION_HIDE, cat_id))
    else:
        menu.addAction("显示（重建启动器快捷方式）", lambda: dispatch(CAT_ACTION_SHOW, cat_id))
    menu.addAction("重命名…", lambda: dispatch(CAT_ACTION_RENAME, cat_id))
    return menu


def build_tray_menu(parent, dispatch: Dispatch,
                    categories: Iterable = ()) -> QMenu:
    """托盘菜单：每个分类一个子菜单 + 全局项（新建分类/打开数据目录/退出）。

    Args:
        categories: 可迭代的分类配置对象（需要 .id / .label / .visible）。
    """
    menu = QMenu(parent)
    cats = list(categories)
    for cat in cats:
        label = str(getattr(cat, "label", "") or getattr(cat, "dir", "") or "分类")
        if not getattr(cat, "visible", True):
            label = f"{label}（已隐藏）"
        menu.addMenu(build_category_menu(
            menu, dispatch, str(getattr(cat, "id", "")), label,
            bool(getattr(cat, "visible", True)),
        ))
    if cats:
        menu.addSeparator()
    menu.addAction("新建分类", lambda: dispatch(TRAY_ACTION_NEW_CATEGORY, None))
    menu.addAction("打开数据目录", lambda: dispatch(TRAY_ACTION_OPEN_DATA_DIR, None))
    menu.addAction("打开启动器目录（供 SAO 引用）",
                   lambda: dispatch(TRAY_ACTION_OPEN_LAUNCHERS, None))
    menu.addSeparator()
    menu.addAction("退出", lambda: dispatch(TRAY_ACTION_EXIT, None))
    return menu
