"""AppController：总装配（唯一控制器，视图之间不互相引用）。

职责：
- 配置加载 / 分类登记（含未登记目录自动注册、空库自动建"常用"）；
- 桌面图标 = Windows 真实 .lnk 快捷方式（DesktopShortcutManager 维护），
  **不再有自绘悬浮窗**，因此也没有看门狗、置底、坐标持久化；
- 同时至多一个 PanelWindow：生命周期（开/关/切换）、目录实时刷新调度、
  拖拽排序回写（200ms 去抖）；
- 托盘（每个分类一个子菜单）、配置 400ms 去抖落盘（唯一写入口）；
- 单实例唤醒入口 ``open_panel_for_dir``（由 main.py 的 QLocalServer 转发）。

v1.2 架构变更（用户实测反馈："Win+D 图标消失没解决，那把图标直接放在桌面，
不要悬浮窗了"）：
- 桌面图标由 Windows 管理 → Win+D、位置、排序、双击启动全部是系统原生行为，
  原来"Win+D 把自绘窗口藏起来"的缺陷在此方案下自然消失；
- 分类的桌面视图 = ``<桌面>\\<label>.lnk`` → ``pythonw.exe main.py --open <dir>``；
- "隐藏分类"= 删除桌面 .lnk（分类与面板仍可从托盘访问）。

v1.4 架构变更（入口交给 SAO Utils 2 的 HUD 图标）：
- 数据根可配置（``app.data_root``，默认 ``%USERPROFILE%\\DesktopBox``），
  本模块所有路径一律走 ``paths.DATA_ROOT``（程序目录 ``paths.APP_DIR`` 不漂移）；
- 自动登记分类时跳过 ``app.category_exclude`` 里的目录名、点开头目录，
  以及"登记过但新数据根里不存在"的旧分类（标 visible=False，保留皮肤不删配置）；
- 快捷方式落点由 ``app.shortcut_mode`` 决定（默认 launcher = 私有启动器目录，
  绝不碰桌面），供 SAO 引用 → 单击 → 第二实例唤醒 → 弹面板。
"""

from __future__ import annotations

import logging
import shutil
from pathlib import Path

from PySide6.QtCore import QObject, QPoint, QRect, QTimer
from PySide6.QtGui import QCursor, QIcon
from PySide6.QtWidgets import QApplication, QInputDialog, QMessageBox, QSystemTrayIcon

from app import paths
from app.config import AppConfig, CategoryConfig
from app.desktop_shortcuts import DesktopShortcutManager
from app.folder_model import Entry, FolderModel
from app.icon_provider import IconCache
from app.panel_model import PanelModel
from app.win32 import window_ops
from app.windows import menus
from app.windows.panel_window import PanelWindow
from app.windows.settings_dialog import SettingsDialog

logger = logging.getLogger(__name__)

ORDER_SAVE_DEBOUNCE_MS = 200
SAVE_DEBOUNCE_MS = 400
TRAY_ICON_PX = 32
INVALID_FILENAME_CHARS = '\\/:*?"<>|'


class AppController(QObject):
    """控制器与装配点。"""

    def __init__(self, app: QApplication, parent: QObject | None = None) -> None:
        super().__init__(parent)
        self.app = app
        self.cfg = AppConfig.load(paths.CONFIG_PATH)
        # 图标缓存开关跟着配置走（去"快捷方式"小箭头；False = v1.3 老逻辑）
        self.icon_cache = IconCache(
            strip_shortcut_overlay=self.cfg.app.strip_shortcut_overlay
        )
        self.shortcuts = DesktopShortcutManager(
            icon_cache=self.icon_cache,
            ico_dir=paths.ICO_DIR,
            state_path=paths.LNK_STATE_PATH,
            project_root=paths.APP_ROOT,
            mode=self.cfg.app.shortcut_mode,
            launcher_dir=paths.LAUNCHERS_DIR,
        )
        self.folder_models: dict[str, FolderModel] = {}
        self.panel: PanelWindow | None = None
        self.active_cat_id: str | None = None
        self.tray: QSystemTrayIcon | None = None
        self._tray_menu = None            # QMenu 必须强引用，否则被 GC（托盘菜单消失）

        self._save_timer = QTimer(self)
        self._save_timer.setSingleShot(True)
        self._save_timer.setInterval(SAVE_DEBOUNCE_MS)
        self._save_timer.timeout.connect(self._do_save)

        self._order_timer = QTimer(self)
        self._order_timer.setSingleShot(True)
        self._order_timer.setInterval(ORDER_SAVE_DEBOUNCE_MS)
        self._order_timer.timeout.connect(self._do_save_order)
        self._pending_order_cat: str | None = None
        self._pending_order_names: list[str | None] = []

    # -- 启动 / 退出 ---------------------------------------------------------

    def start(self) -> None:
        """装配托盘与桌面快捷方式（视图按需创建，启动即完成）。"""
        paths.ensure_base_dirs()
        self._ensure_categories()
        self._setup_tray()
        self._sync_shortcuts()
        self._schedule_save()
        logger.info("DesktopBox 启动完成：%d 个分类", len(self.cfg.categories))

    def shutdown(self) -> None:
        """退出前清理（幂等）。桌面 .lnk 保留（下次启动继续用）。"""
        try:
            self.close_panel()
            for fm in self.folder_models.values():
                fm.stop_watch()
            self.folder_models.clear()
            if self.tray is not None:
                self.tray.hide()
            self.cfg.save()
            window_ops.release_single_instance_mutex()
            logger.info("DesktopBox 已退出")
        except Exception as exc:  # noqa: BLE001
            logger.warning("shutdown 异常: %s", exc)

    # -- 分类登记 -----------------------------------------------------------

    def _ensure_categories(self) -> None:
        """扫描数据根下未登记的子目录 → 自动登记；空库则建"常用"。

        v1.4 三条排除规则（顺序敏感）：
        1. ``app.category_exclude`` 里的名字（默认 ``快捷方式/桌面/z全部``，
           大小写不敏感）—— 6 个 SAO 目录里有 3 个不是分类；
        2. 以 "." 开头的隐藏目录（.logs 等程序目录不能变成分类）；
        3. 已登记但**在新数据根里不存在**的旧分类 → 只把 ``visible`` 置 False
           并记日志：不删配置、不动皮肤、不碰磁盘。这样换数据根回退时
           用户原来的皮肤/尺寸调不丢。
        """
        added = False
        try:
            existing = {
                p.name
                for p in paths.DATA_ROOT.iterdir()
                if p.is_dir() and not p.name.startswith(".")
            }
        except OSError as exc:
            logger.warning("扫描数据根目录失败 %s: %s", paths.DATA_ROOT, exc)
            existing = set()
        registered = {c.dir for c in self.cfg.categories}
        for dir_name in sorted(existing - registered):
            if self.cfg.app.is_dir_excluded(dir_name):
                logger.info("跳过目录（在 category_exclude 名单里）: %s", dir_name)
                continue
            cat = self.cfg.add_category(dir_name, dir_name)
            logger.info("自动登记分类: %s (%s)", dir_name, cat.id)
            added = True

        # 已登记但数据根本地不存在的分类：标隐藏（保留配置与皮肤）。
        # 关键防御：**根目录本身不可用**（E 盘没插 / 网络盘断线）时不做任何标记——
        # 否则一次临时掉盘就会把所有分类永久改成隐藏，插回磁盘还得逐个点"显示"。
        root_ready = False
        try:
            root_ready = paths.DATA_ROOT.is_dir()
        except OSError:
            root_ready = False
        if not root_ready:
            logger.warning("数据根当前不可用，本次不调整分类可见性: %s", paths.DATA_ROOT)
        else:
            for cat in self.cfg.categories:
                try:
                    exists = (paths.DATA_ROOT / cat.dir).is_dir()
                except OSError:
                    exists = False
                if not exists and cat.visible:
                    cat.visible = False
                    logger.warning(
                        "分类目录在数据根中不存在，已标记为隐藏（配置与皮肤保留）: "
                        "dir=%s root=%s", cat.dir, paths.DATA_ROOT,
                    )
                    added = True

        if not self.cfg.categories:
            paths.ensure_category_dir("常用")
            cat = self.cfg.add_category("常用", "常用")
            logger.info("首次启动创建默认分类: 常用 (%s)", cat.id)
            added = True
        if added:
            self._schedule_save()

    # -- 桌面快捷方式 ---------------------------------------------------------

    def _sync_shortcuts(self) -> None:
        """把启动器 .lnk 同步到配置状态（幂等；失败不影响程序其余功能）。"""
        try:
            self.shortcuts.sync(self.cfg.categories)
        except Exception as exc:  # noqa: BLE001 - COM/文件系统问题不得拖垮主流程
            logger.warning("同步启动器快捷方式失败: %s", exc)

    # -- 托盘 ---------------------------------------------------------------

    def _setup_tray(self) -> None:
        self.tray = QSystemTrayIcon(QIcon(self.icon_cache.default_icon(TRAY_ICON_PX)), self)
        self.tray.setToolTip("DesktopBox 桌面收纳")
        self.tray.setContextMenu(self._build_tray_menu())
        self.tray.show()

    def _build_tray_menu(self):
        """构建（或重建）托盘菜单；分类增删改后必须重建才能反映新状态。"""
        menu = menus.build_tray_menu(None, self._dispatch_tray, self.cfg.categories)
        self._tray_menu = menu
        return menu

    def _rebuild_tray_menu(self) -> None:
        if self.tray is not None:
            self._build_tray_menu()
            self.tray.setContextMenu(self._tray_menu)

    def _dispatch_tray(self, action: str, cat_id: str | None = None) -> None:
        """托盘菜单动作分发（分类级动作带 cat_id，全局动作不带）。"""
        if action == menus.TRAY_ACTION_NEW_CATEGORY:
            self._new_category()
        elif action == menus.TRAY_ACTION_OPEN_DATA_DIR:
            window_ops.shell_open(paths.DATA_ROOT)
        elif action == menus.TRAY_ACTION_OPEN_LAUNCHERS:
            # 启动器目录可能还没被 sync 创建过（新装 / 刚切模式），先确保存在
            paths.ensure_base_dirs()
            window_ops.shell_open(paths.LAUNCHERS_DIR)
        elif action == menus.TRAY_ACTION_EXIT:
            self.app.quit()
        elif cat_id:
            self._category_action(cat_id, action)

    def _new_category(self) -> None:
        name, ok = QInputDialog.getText(None, "新建分类", "分类名称（将创建同名文件夹）：")
        if not ok:
            return
        name = name.strip()
        if not name:
            return
        if any(ch in name for ch in INVALID_FILENAME_CHARS):
            QMessageBox.warning(None, "DesktopBox", "名称不能包含以下字符：" + INVALID_FILENAME_CHARS)
            return
        # dir 冲突不报错、不覆盖：追加 -2/-3（label 保持用户输入）
        candidate = name
        suffix = 2
        while self.cfg.find_by_dir(candidate) is not None or (paths.DATA_ROOT / candidate).exists():
            candidate = f"{name}-{suffix}"
            suffix += 1
        paths.ensure_category_dir(candidate)
        cat = self.cfg.add_category(candidate, name)
        logger.info("新建分类: label=%s dir=%s (%s)", name, candidate, cat.id)
        self._after_categories_changed()

    def _after_categories_changed(self) -> None:
        """分类集合/名称/可见性变化后的统一收尾：重建托盘 + 同步快捷方式 + 落盘。"""
        self._rebuild_tray_menu()
        self._sync_shortcuts()
        self._schedule_save()

    # -- 面板生命周期 ---------------------------------------------------------

    def open_panel(self, cat_id: str, anchor: QPoint | None = None) -> None:
        """为分类打开面板（内部先关旧面板）。

        Args:
            cat_id: 分类 id。
            anchor: 面板锚点（全局坐标）。桌面快捷方式双击时是鼠标落点；
                托盘打开时为 None → 用当前鼠标位置。
        """
        cat = self.cfg.find(cat_id)
        if cat is None:
            logger.warning("打开面板失败：分类不存在 %s", cat_id)
            return
        self.close_panel()

        fm = self._folder_model(cat)
        entries = fm.reload()

        panel_model = PanelModel(strip_extensions=self.cfg.app.strip_extensions)
        # 按顺序构建（v1.3 密集语义；fm.slots 已由 FolderModel 归一化，无空位）
        panel_model.build_slots(fm.slots, entries, self.icon_cache, self.cfg.app.panel_item_px)

        pw = PanelWindow(
            cat,
            self.cfg.app.panel_item_px,
            self.icon_cache,
            edit_press_ms=self.cfg.app.edit_press_ms,
        )
        pw.set_model(panel_model)
        pw.closed.connect(self._on_panel_closed)
        pw.open_folder_requested.connect(self._on_open_folder)
        pw.resized.connect(self._on_panel_resized)
        pw.grid.order_changed.connect(self._on_order_changed)
        pw.grid.launch_requested.connect(self._launch_entry)

        anchor_rect = self._anchor_rect(anchor)
        if cat.position_mode == "center":
            pw.place_center(anchor_rect)
        else:
            pw.place_near(anchor_rect)

        pw.show()
        pw.activateWindow()  # 面板需要焦点以支持 Esc / 失焦关闭

        fm.start_watch()
        self.panel = pw
        self.active_cat_id = cat_id
        logger.info("面板已打开: %s（%d 条目）", cat.dir, len(entries))

    def open_panel_for_dir(self, dir_name: str, x: int = 0, y: int = 0) -> None:
        """外部唤醒入口：按分类**目录名**打开面板，锚点为传来的鼠标落点。

        由 main.py 的 SingleInstanceServer（第二实例双击桌面 .lnk）调用，
        也用于"由 .lnk 首次拉起主实例"的场景。
        """
        cat = self.cfg.find_by_dir(dir_name)
        if cat is None:
            logger.warning("收到唤醒请求但分类未登记: dir=%s", dir_name)
            return
        if not cat.visible:
            # 桌面 .lnk 已删时不该有请求；手动启动的兜底：照样能开面板
            logger.info("分类 %s 处于隐藏状态，仍按请求打开面板", cat.dir)
        self.open_panel(cat.id, QPoint(int(x), int(y)))

    @staticmethod
    def _anchor_rect(anchor: QPoint | None) -> QRect:
        """把锚点转成 1×1 的矩形（PanelWindow.place_near 需要矩形）。"""
        point = anchor if anchor is not None else QCursor.pos()
        return QRect(point.x(), point.y(), 1, 1)

    def close_panel(self) -> None:
        """关闭面板并停止目录监听（幂等）。"""
        pw, cat_id = self.panel, self.active_cat_id
        self.panel = None
        self.active_cat_id = None
        if pw is None:
            return
        try:
            pw.closed.disconnect(self._on_panel_closed)
        except (RuntimeError, TypeError):
            pass
        pw.close()
        pw.deleteLater()
        if cat_id:
            fm = self.folder_models.get(cat_id)
            if fm is not None:
                fm.stop_watch()

    def _on_panel_closed(self, cat_id: str) -> None:
        """面板自行关闭（失焦 / Esc）后的清理。

        这里记一条 info 日志：面板"开→自关"在外部（跨进程唤醒连发、失焦竞态）
        只能靠日志观测，没有这条就无法区分"打开了但立刻自关"和"一直开着"。
        仅可观测性，不改变任何行为。
        """
        logger.info("面板已关闭（失焦/Esc）: %s", cat_id)
        if self.panel is not None and self.active_cat_id == cat_id:
            self.panel = None
            self.active_cat_id = None
            fm = self.folder_models.get(cat_id)
            if fm is not None:
                fm.stop_watch()
        pw = self.sender()
        if pw is not None:
            pw.deleteLater()

    def _folder_model(self, cat: CategoryConfig) -> FolderModel:
        fm = self.folder_models.get(cat.id)
        if fm is None:
            fm = FolderModel(cat.dir_path(paths.DATA_ROOT), poll_ms=self.cfg.app.watch_poll_ms)
            fm.changed.connect(lambda cat_id=cat.id: self._on_folder_changed(cat_id))
            self.folder_models[cat.id] = fm
        return fm

    # -- 目录实时刷新 ---------------------------------------------------------

    def refresh_category(self, cat_id: str) -> None:
        """重新加载分类数据；面板开着则重建（保留选中）。"""
        cat = self.cfg.find(cat_id)
        if cat is None:
            return
        fm = self._folder_model(cat)
        fm.reload()
        self.icon_cache.invalidate_dir(cat.dir_path(paths.DATA_ROOT))
        if self.panel is not None and self.active_cat_id == cat_id:
            selected = self.panel.grid.selected_names()
            self.panel.grid.reload_entries(fm.entries, selected, slots=fm.slots)

    def _on_folder_changed(self, cat_id: str) -> None:
        """目录内容变化：面板开着 → 重建模型；未开 → 缓存即可（下次打开用）。"""
        fm = self.folder_models.get(cat_id)
        if fm is None:
            return
        entries = fm.reload()
        if self.panel is not None and self.active_cat_id == cat_id:
            selected = self.panel.grid.selected_names()
            self.panel.grid.reload_entries(entries, selected, slots=fm.slots)
            logger.info("面板实时刷新: %s（%d 条目）", cat_id, len(entries))

    # -- 分类级动作（托盘转发） -----------------------------------------------

    def _category_action(self, cat_id: str, action: str) -> None:
        cat = self.cfg.find(cat_id)
        if cat is None:
            return
        if action == menus.CAT_ACTION_OPEN_PANEL:
            self.open_panel(cat_id)
        elif action == menus.CAT_ACTION_OPEN_FOLDER:
            window_ops.shell_open(cat.dir_path(paths.DATA_ROOT))
        elif action == menus.CAT_ACTION_RENAME:
            self._rename_category(cat)
        elif action == menus.CAT_ACTION_SETTINGS:
            self.apply_settings(cat_id)
        elif action == menus.CAT_ACTION_HIDE:
            self._hide_category(cat)
        elif action == menus.CAT_ACTION_SHOW:
            self._show_category(cat)

    def _rename_category(self, cat: CategoryConfig) -> None:
        text, ok = QInputDialog.getText(None, "重命名", "新的显示名称：", text=cat.label)
        if not ok:
            return
        new_label = text.strip()[:20]
        if not new_label or new_label == cat.label:
            return
        cat.label = new_label
        cat.sanitize()
        logger.info("分类重命名: %s → %s", cat.dir, cat.label)
        self._after_categories_changed()
        if self.panel is not None and self.active_cat_id == cat.id:
            self.close_panel()

    def _hide_category(self, cat: CategoryConfig) -> None:
        """隐藏分类 = 删除启动器 .lnk（分类与面板仍可从托盘访问）。"""
        cat.visible = False
        logger.info("隐藏分类（删除启动器快捷方式）: %s", cat.dir)
        self._after_categories_changed()
        if self.panel is not None and self.active_cat_id == cat.id:
            self.close_panel()

    def _show_category(self, cat: CategoryConfig) -> None:
        """显示分类 = 重新创建启动器 .lnk。"""
        cat.visible = True
        logger.info("显示分类（重建启动器快捷方式）: %s", cat.dir)
        self._after_categories_changed()

    # -- 设置对话框 -----------------------------------------------------------

    def apply_settings(self, cat_id: str) -> None:
        """打开单分类皮肤设置；OK 后复制图标图片、重建 ICO 与桌面快捷方式。"""
        cat = self.cfg.find(cat_id)
        if cat is None:
            return
        dlg = SettingsDialog(cat)
        dlg.open_folder_requested.connect(lambda cid: self._on_open_folder(cid))
        code = dlg.exec_()
        if code != SettingsDialog.DialogCode.Accepted:
            return

        if dlg.image_source:
            try:
                src = Path(dlg.image_source)
                dest = paths.ICONS_DIR / f"{cat.id}{src.suffix.lower()}"
                shutil.copyfile(src, dest)
                cat.image = str(dest)
                self.icon_cache.invalidate_dir(dest.parent)
            except OSError as exc:
                logger.warning("复制分类图标图片失败: %s", exc)
        elif dlg.image_cleared:
            cat.image = ""

        cat.sanitize()
        # 皮肤变化 → ICO 重建 + .lnk 重建（Windows 图标缓存需要文件重建才刷新）
        self._after_categories_changed()
        if self.panel is not None and self.active_cat_id == cat_id:
            self.close_panel()

    # -- 面板内动作 -----------------------------------------------------------

    def _launch_entry(self, entry: Entry) -> None:
        """启动条目：交给系统 Shell；失效快捷方式 → Windows 原生错误框。
        不预检、不提示、不灰显、不隐藏。"""
        window_ops.shell_open(entry.path)

    def _on_open_folder(self, cat_id: str) -> None:
        cat = self.cfg.find(cat_id)
        if cat is not None:
            window_ops.shell_open(cat.dir_path(paths.DATA_ROOT))

    def _on_panel_resized(self, cat_id: str, w: int, h: int) -> None:
        cat = self.cfg.find(cat_id)
        if cat is None:
            return
        cat.panel_w, cat.panel_h = w, h
        self._schedule_save()

    def _on_order_changed(self) -> None:
        """面板内拖拽排序完成 → 200ms 去抖写回 .order.json（v1.3 密集名单）。"""
        if self.panel is None or self.panel.grid.panel_model is None:
            return
        self._pending_order_cat = self.active_cat_id
        self._pending_order_names = self.panel.grid.panel_model.serialize_slots()
        self._order_timer.start()

    def _do_save_order(self) -> None:
        cat_id = self._pending_order_cat
        if not cat_id:
            return
        fm = self.folder_models.get(cat_id)
        if fm is not None:
            fm.save_slots(self._pending_order_names)
            logger.info("顺序已写回: %s", cat_id)
        self._pending_order_cat = None
        self._pending_order_names = []

    # -- 配置落盘 -----------------------------------------------------------

    def _schedule_save(self) -> None:
        self._save_timer.start()

    def _do_save(self) -> None:
        self.cfg.save()
        logger.debug("配置已保存")
