"""图标提取：QFileIconProvider 封装 + (路径, mtime, 尺寸) 三元组缓存 + 内置默认图标。

关键事实（实测）：
- QFileIconProvider 在 PySide6.QtWidgets（Qt6 从 QtGui 迁入）；
- QFileInfo 在 PySide6.QtCore；
- 对不存在/失效路径 icon(QFileInfo) 仍返回非空通用图标 → 失效快捷方式
  照常显示，不灰显、不隐藏、不缓存失效结果、不提示；
- 绝不做存在性检测。

v1.4 新增（去掉 .lnk 左下角的"快捷方式"小箭头）：
- 直接对 ``.lnk`` 取图标，Shell 会叠加系统的小箭头叠加图标（这是资源管理器
  行为，Qt 只是照搬）；
- 用户要求面板里"看起来就是那个程序本身"，所以改为**解析 .lnk 拿到真实目标**，
  再对目标取图标：① 有自定义图标位置（GetIconLocation）且文件存在 → 对图标文件取图；
  ② 否则目标存在 → 对目标取图（目标是目录也走同一条路）；③ 全失败 → 回退到
  对 .lnk 本身取图（允许带箭头：宁可带箭头也不能空图/灰显）。
- 开关 ``strip_shortcut_overlay=False`` 时完全走 v1.3 老逻辑（可一键回退）。
- 解析失败**不缓存**失败结果：下次刷新还会再试一次（COM 单次解析很快）。
v1.4.1（面板显示全部文件类型后的取图约定）：
- 任意文件（.txt/.pdf/.docx/…）走 ``QFileIconProvider`` 拿系统关联图标；
- ``.url``（Internet 快捷方式，纯文本 INI）先解析 ``[InternetShortcut]`` 段的
  ``IconFile=`` / ``IconIndex=``，拿到 .ico 就用它（这才是"这个网址的脸"），
  解析失败/取不到再退回 ``QFileIconProvider``；
- 任何一环失败都回退**内置默认图标**，绝不出现空白格（延续 v1.3 "不灰显、
  不预检、不提示"的约定）。
"""

from __future__ import annotations

import logging
import os
from collections import OrderedDict
from pathlib import Path

from PySide6.QtCore import QFileInfo, QSize, Qt
from PySide6.QtGui import QColor, QLinearGradient, QPainter, QPainterPath, QPen, QPixmap
from PySide6.QtWidgets import QFileIconProvider

from app.win32 import shell_resolve

logger = logging.getLogger(__name__)

CACHE_MAX = 512
LNK_SUFFIX = ".lnk"
URL_SUFFIX = ".url"
LNK_MAX_DEPTH = 2      # .lnk → .lnk 递归上限（防循环链，正常快捷方式深度 ≤1）
INI_TEXT_MAX = 64 * 1024   # .url 文本读取上限（防病态大文件）
# IconFile 指向这些后缀时，按"图片本身"加载（QIcon），而不是当文件取系统关联图
IMAGE_ICON_SUFFIXES = {".ico", ".png", ".bmp", ".jpg", ".jpeg", ".gif"}


class IconCache:
    """QFileIconProvider 封装 + FIFO 缓存 + 代码绘制的默认分类图标。

    必须在 GUI 线程使用（QPixmap 非线程安全）。
    """

    def __init__(self, strip_shortcut_overlay: bool = True) -> None:
        """
        Args:
            strip_shortcut_overlay: True = 解析 .lnk 取真实目标图标（无小箭头）；
                False = v1.3 老逻辑（直接对 .lnk 取图，带系统箭头）。
        """
        self._provider = QFileIconProvider()  # 构造昂贵，复用单例
        self._cache: OrderedDict[tuple, QPixmap] = OrderedDict()
        self._default_cache: dict[int, QPixmap] = {}
        self.strip_shortcut_overlay = bool(strip_shortcut_overlay)

    # -- 缓存辅助 ----------------------------------------------------------

    @staticmethod
    def _mtime_ns(path: Path) -> int:
        try:
            return os.stat(path).st_mtime_ns
        except OSError:
            return 0

    def _cache_put(self, key: tuple, pm: QPixmap) -> QPixmap:
        self._cache[key] = pm
        self._cache.move_to_end(key)
        while len(self._cache) > CACHE_MAX:
            self._cache.popitem(last=False)
        return pm

    def _cache_get(self, key: tuple) -> QPixmap | None:
        cached = self._cache.get(key)
        if cached is not None:
            self._cache.move_to_end(key)
        return cached

    # -- 条目取图 ----------------------------------------------------------

    def pixmap(self, path: Path, px: int) -> QPixmap:
        """任意路径（.exe/.lnk/.url/任意文件/目录/失效路径）取 px×px 图标。"""
        px = int(px)
        key = (os.path.normcase(str(path)), self._mtime_ns(path), px)
        cached = self._cache_get(key)
        if cached is not None:
            return cached

        # ① 去箭头分支（.lnk + 开关开）：解析失败不缓存，下次仍会重试
        if self.strip_shortcut_overlay and str(path).casefold().endswith(LNK_SUFFIX):
            pm = self._lnk_target_pixmap(Path(path), px, 0)
            if pm is not None and not pm.isNull():
                return self._cache_put(key, pm)
            return self._provider_pixmap(path, px)   # 回退：允许带箭头，但不写主键缓存

        # ② .url：先用 INI 里声明的 IconFile（系统关联图往往只是空白的浏览器图标）
        if str(path).casefold().endswith(URL_SUFFIX):
            pm = self._url_icon_pixmap(Path(path), px)
            if pm is not None and not pm.isNull():
                return self._cache_put(key, pm)
            return self._provider_pixmap(path, px)

        return self._cache_put(key, self._provider_pixmap(path, px))

    def _provider_pixmap(self, path: Path, px: int) -> QPixmap:
        """QFileIconProvider 直取（v1.3 原逻辑；失败回退内置默认图标）。"""
        try:
            icon = self._provider.icon(QFileInfo(str(path)))
            pm = icon.pixmap(QSize(px, px)) if icon is not None else QPixmap()
        except Exception as exc:  # noqa: BLE001 - 单条取图失败不拖垮面板
            logger.warning("取图失败 %s: %s", path, exc)
            pm = QPixmap()
        if pm.isNull():
            pm = self.default_icon(px)
        return pm

    def _icon_from(self, path: Path, px: int) -> QPixmap | None:
        """对"解析后的真实路径"取图标；缓存键含该路径 + 其 mtime。

        Returns:
            QPixmap 或 None（None = 取不到，调用方继续走下一级回退）。
        """
        key = ("resolved", os.path.normcase(str(path)), self._mtime_ns(path), int(px))
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            icon = self._provider.icon(QFileInfo(str(path)))
            pm = icon.pixmap(QSize(int(px), int(px))) if icon is not None else QPixmap()
        except Exception as exc:  # noqa: BLE001
            logger.warning("解析后取图失败 %s: %s", path, exc)
            return None
        if pm.isNull():
            return None
        return self._cache_put(key, pm)

    def _lnk_target_pixmap(self, lnk: Path, px: int, depth: int) -> QPixmap | None:
        """解析 .lnk → 对目标/自定义图标取图（无箭头）。失败返回 None。

        Args:
            lnk: .lnk 路径。
            px: 目标像素。
            depth: 递归深度（.lnk → .lnk 链的保护，上限 LNK_MAX_DEPTH）。
        """
        if depth > LNK_MAX_DEPTH:
            return None
        try:
            info = shell_resolve.read_link(lnk)
        except Exception as exc:  # noqa: BLE001 - 解析层已兜住，这里再兜一层
            logger.warning("解析快捷方式失败 %s: %s", lnk, exc)
            return None
        if not info.ok:
            return None

        # ① 自定义图标位置优先（用户显式指定的图标才是"这个快捷方式的脸"）
        if info.icon_location:
            try:
                icon_path = Path(info.icon_location)
                if icon_path.is_file():
                    pm = self._icon_from(icon_path, px)
                    if pm is not None:
                        return pm
            except (OSError, ValueError) as exc:
                logger.info("图标位置不可用 %s: %s", info.icon_location, exc)

        # ② 目标本身（.exe / 目录 / 其他 .lnk）
        if not info.target:
            return None
        try:
            target = Path(info.target)
        except (TypeError, ValueError):
            return None
        try:
            if not target.exists():
                return None
        except OSError:
            return None
        if str(target).casefold().endswith(LNK_SUFFIX):
            return self._lnk_target_pixmap(target, px, depth + 1)
        return self._icon_from(target, px)

    @staticmethod
    def _read_url_ini(url_path: Path) -> dict[str, str]:
        """读 ``.url``（Internet 快捷方式）的 INI 键值，只取 ``[InternetShortcut]`` 段。

        ``.url`` 是纯文本，**不需要 COM**。编码可能是 UTF-8（可能带 BOM）或 ANSI(GBK)，
        依次尝试；任何异常返回空 dict（调用方回退系统图标）。

        Args:
            url_path: .url 文件路径。

        Returns:
            小写键 → 值。读不到返回 ``{}``。
        """
        try:
            raw = url_path.read_bytes()[:INI_TEXT_MAX]
        except OSError:
            return {}
        if not raw:
            return {}
        text = ""
        for encoding in ("utf-8-sig", "gbk", "latin-1"):
            try:
                text = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
        result: dict[str, str] = {}
        in_section = False
        for line in text.splitlines():
            stripped = line.strip()
            if not stripped or stripped.startswith((";", "#")):
                continue
            if stripped.startswith("[") and stripped.endswith("]"):
                in_section = stripped[1:-1].strip().casefold() == "internetshortcut"
                continue
            if not in_section or "=" not in stripped:
                continue
            key, _, value = stripped.partition("=")
            result[key.strip().casefold()] = value.strip()
        return result

    def _image_pixmap(self, path: Path, px: int) -> QPixmap | None:
        """把图片文件（.ico/.png/.bmp/.jpg）当图标加载（不是当"文件"取系统关联图）。

        为什么不用 QFileIconProvider：对 ``.ico`` 它返回的是"图片文件"的通用图标，
        而不是 ico 本身画的东西 —— 用户显式指定的图标必须按原图渲染。

        Returns:
            QPixmap 或 None（加载失败）。
        """
        key = ("img", os.path.normcase(str(path)), self._mtime_ns(path), int(px))
        cached = self._cache_get(key)
        if cached is not None:
            return cached
        try:
            from PySide6.QtGui import QIcon

            pm = QIcon(str(path)).pixmap(QSize(int(px), int(px)))
        except Exception as exc:  # noqa: BLE001 - 坏图不拖垮面板
            logger.info("加载图片图标失败 %s: %s", path, exc)
            return None
        if pm.isNull():
            return None
        return self._cache_put(key, pm)

    def _url_icon_pixmap(self, url_path: Path, px: int) -> QPixmap | None:
        """``.url`` 的图标：优先用 INI 里声明的 ``IconFile=``。

        为什么值得单独处理：``.url`` 走系统关联图标时通常只拿到浏览器的通用图标，
        而用户在资源管理器里给网址设过图标时，INI 里会写 ``IconFile=``——那才是
        "这个网址的脸"。

        已知限制：``IconIndex=`` 被忽略（要精确取 exe/dll 里的第 N 个图标需要
        ``ExtractIconExW``，收益低、风险高）；若 IconFile 指向 exe/dll，则拿到的是
        该文件的第 0 个图标（通常就是它的主图标）。

        Returns:
            QPixmap 或 None（None = 取不到，调用方回退系统图标）。
        """
        ini = self._read_url_ini(url_path)
        icon_file = ini.get("iconfile", "").strip().strip('"')
        if not icon_file:
            return None
        try:
            candidate = Path(os.path.expandvars(icon_file))
        except (TypeError, ValueError) as exc:
            logger.info("解析 .url 的 IconFile 失败 %s: %s", icon_file, exc)
            return None
        # 绝对路径最常见；少数工具写相对路径（相对 .url 所在目录）
        candidates = [candidate]
        if not candidate.is_absolute():
            candidates.append(url_path.parent / candidate)
        for cand in candidates:
            try:
                if not cand.is_file():
                    continue
            except OSError:
                continue
            if cand.suffix.lower() in IMAGE_ICON_SUFFIXES:
                pm = self._image_pixmap(cand, px)
            else:
                pm = self._icon_from(cand, px)   # exe/dll 等：取文件自身图标
            if pm is not None and not pm.isNull():
                return pm
        return None

    def invalidate_dir(self, dir_path: Path) -> None:
        """清掉某目录前缀下的全部缓存条目（mtime 相同的兜底）。"""
        prefix = os.path.normcase(str(dir_path))

        def _hit(key: tuple) -> bool:
            # 键形态： (path, mtime, px) / ("resolved", path, mtime, px)
            #          / ("catimg", path, mtime, px)
            for part in key[:2]:
                if isinstance(part, str) and part.startswith(prefix):
                    return True
            return False

        for key in [k for k in self._cache if isinstance(k, tuple) and _hit(k)]:
            self._cache.pop(key, None)

    # -- 分类图标 ----------------------------------------------------------

    def category_pixmap(self, cat, px: int) -> QPixmap:
        """分类桌面图标：自定义图片优先，缺失/为空回退内置默认。

        自定义图片文件缺失时静默回退（不灰显、不提示）。
        """
        image = getattr(cat, "image", "") or ""
        if image:
            px = int(px)
            key = ("catimg", os.path.normcase(image), self._mtime_ns(Path(image)), px)
            cached = self._cache.get(key)
            if cached is not None:
                self._cache.move_to_end(key)
                return cached
            try:
                from PySide6.QtGui import QIcon

                pm = QIcon(image).pixmap(QSize(px, px))
                if not pm.isNull():
                    return self._cache_put(key, pm)
            except Exception as exc:  # noqa: BLE001
                logger.warning("加载分类自定义图标失败 %s: %s", image, exc)
        return self.default_icon(px)

    # -- 内置默认分类图标 ----------------------------------------------------

    def default_icon(self, px: int) -> QPixmap:
        """QPainter 代码绘制的收纳箱图标（无外部资源依赖）。"""
        px = max(8, int(px))
        cached = self._default_cache.get(px)
        if cached is not None:
            return cached
        pm = QPixmap(px, px)
        pm.fill(Qt.GlobalColor.transparent)
        p = QPainter(pm)
        p.setRenderHint(QPainter.RenderHint.Antialiasing, True)
        s = float(px)

        # 箱体：带渐变的圆角矩形
        body = QPainterPath()
        body.addRoundedRect(s * 0.14, s * 0.34, s * 0.72, s * 0.48, s * 0.10, s * 0.10)
        grad = QLinearGradient(0.0, s * 0.34, 0.0, s * 0.82)
        grad.setColorAt(0.0, QColor("#6FA8DC"))
        grad.setColorAt(1.0, QColor("#3D78B5"))
        p.setPen(QPen(QColor("#2A5687"), max(1.0, s * 0.035)))
        p.setBrush(grad)
        p.drawPath(body)

        # 箱盖
        lid = QPainterPath()
        lid.addRoundedRect(s * 0.10, s * 0.18, s * 0.80, s * 0.20, s * 0.08, s * 0.08)
        p.setBrush(QColor("#4F86C0"))
        p.drawPath(lid)

        # 盖上提手
        handle = QPainterPath()
        handle.addRoundedRect(s * 0.38, s * 0.08, s * 0.24, s * 0.14, s * 0.06, s * 0.06)
        p.setBrush(QColor("#3D78B5"))
        p.drawPath(handle)

        # 高光线条
        p.setPen(QPen(QColor(255, 255, 255, 110), max(1.0, s * 0.03)))
        p.drawLine(int(s * 0.24), int(s * 0.52), int(s * 0.76), int(s * 0.52))
        p.end()
        self._default_cache[px] = pm
        return pm
