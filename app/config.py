"""配置数据层：Settings / CategoryConfig / AppConfig。

设计要点（按主理人修正：外观参数按分类独立）：
- 每个分类各自包含：icon_px、image、label、panel_w/h、radius、bg_color、
  opacity、position_mode；
- 全局 Settings 只保留行为参数（阈值/轮询间隔等）；
- 配置损坏 → 使用内存默认配置，不覆盖用户文件；
- 所有 JSON 写入均为原子写。

v1.2 语义重整（桌面图标改为 Windows 真实快捷方式）：
- 删除 ``cat.x`` / ``cat.y``：桌面图标位置由 Windows 自己管理，不再持久化坐标；
- 删除 ``app.watchdog_ms``：Win+D 看门狗随悬浮窗体系一起移除；
- ``cat.visible`` 语义变为"是否在桌面上保留 .lnk"（False = 从桌面移除，
  分类仍可从托盘访问）；
- 旧配置文件里的 ``x`` / ``y`` / ``watchdog_ms`` 会被静默忽略（不报错、不迁移），
  保存时自然不会写回。

v1.4 新增（入口改为 SAO Utils 2 的 HUD 图标）：
- ``app.data_root``：分类数据根（空 = 默认 ``%USERPROFILE%\\DeskCountChen``）；
- ``app.category_exclude``：自动登记分类时要跳过的目录名（大小写不敏感；
  6 个 SAO 目录里 ``快捷方式`` / ``桌面`` / ``z全部`` 不是分类）；
- ``app.shortcut_mode``：``"launcher"``（写私有启动器 .lnk 给 SAO 引用，
  绝不碰桌面）| ``"desktop"``（v1.3 行为，写真实桌面）；
- ``app.strip_extensions``：面板内显示名去掉 .lnk/.exe 扩展名；
- ``app.strip_shortcut_overlay``：解析 .lnk 取目标图标，去掉"快捷方式"小箭头。
"""

from __future__ import annotations

import json
import logging
import os
import re
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path

logger = logging.getLogger(__name__)

# 与需求一致的全局常量（不散落魔法数）
DRAG_THRESHOLD_PX = 4        # 手势判定阈值固定 4px
MIN_PANEL_W = 240
MIN_PANEL_H = 160
ICON_PX_MIN, ICON_PX_MAX = 48, 128
RADIUS_MIN, RADIUS_MAX = 0, 24
OPACITY_MIN, OPACITY_MAX = 0, 100
LABEL_MAX_CHARS = 20

_COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")
_DEFAULT_BG_COLOR = "#F5F6F8"
_POSITION_MODES = ("anchor", "center")

# 面板材质（v1.5）：blur = 截屏 + 降采样模糊的毛玻璃；solid = 纯色填充（v1.4 行为）
MATERIAL_BLUR = "blur"
MATERIAL_SOLID = "solid"
MATERIALS = (MATERIAL_BLUR, MATERIAL_SOLID)

# 快捷方式落点模式（v1.4）：launcher = 私有启动器目录（SAO 引用），desktop = 真实桌面
SHORTCUT_MODE_LAUNCHER = "launcher"
SHORTCUT_MODE_DESKTOP = "desktop"
SHORTCUT_MODES = (SHORTCUT_MODE_LAUNCHER, SHORTCUT_MODE_DESKTOP)

# 自动登记分类时默认跳过的目录名（数据根下这些目录不是分类）
DEFAULT_CATEGORY_EXCLUDE = ("快捷方式", "桌面", "z全部")

_TRUE_TEXT = ("1", "true", "yes", "on", "y")
_FALSE_TEXT = ("0", "false", "no", "off", "n")


def iso_utc_now() -> str:
    """ISO 8601 UTC 时间戳（秒精度）。"""
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def atomic_write_json(path: Path, data: dict) -> None:
    """原子写 JSON：tmp → flush+fsync → os.replace。失败清理 tmp。"""
    tmp = path.with_name(f"{path.name}.tmp.{os.getpid()}")
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)
    except OSError as exc:
        logger.error("原子写 JSON 失败 %s: %s", path, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass


def clamp_int(value, lo: int, hi: int, default: int) -> int:
    """int 化并 clamp；非法值取 default。"""
    try:
        v = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(hi, v))


def _valid_color(value, default: str = _DEFAULT_BG_COLOR) -> str:
    s = str(value).strip() if value else ""
    return s if _COLOR_RE.match(s) else default


def normalize_bool(value, default: bool) -> bool:
    """宽松布尔解析：真布尔直接用，字符串/数字按常见写法识别，其余取 default。

    配置是用户可直接编辑的 JSON，出现 ``"false"``（带引号）很常见；
    严格 ``bool("false") == True`` 会让开关静默失效，故显式解析。
    """
    if isinstance(value, bool):
        return value
    if isinstance(value, (int, float)):
        return bool(value)
    if isinstance(value, str):
        text = value.strip().casefold()
        if text in _TRUE_TEXT:
            return True
        if text in _FALSE_TEXT:
            return False
    return default


def normalize_shortcut_mode(value) -> str:
    """快捷方式落点模式：只接受 launcher / desktop，非法值回落 launcher。"""
    text = str(value or "").strip().casefold()
    return text if text in SHORTCUT_MODES else SHORTCUT_MODE_LAUNCHER


def normalize_material(value) -> str:
    """面板材质：只接受 blur / solid，非法值回落 blur（v1.5 默认毛玻璃）。"""
    text = str(value or "").strip().casefold()
    return text if text in MATERIALS else MATERIAL_BLUR


def normalize_data_root(value) -> str:
    """数据根：去首尾空白；非法/空 → ""（= 默认 APP_DIR）。"""
    if not isinstance(value, str):
        return ""
    return value.strip()


def normalize_category_exclude(value) -> list[str]:
    """跳过目录名列表：非列表视为未配置（返回默认值）。

    - 元素转 str 并去空白，空串丢弃；
    - 大小写不敏感去重（Windows 路径语义），保留首次出现的写法与顺序。
    """
    if not isinstance(value, (list, tuple)):
        return list(DEFAULT_CATEGORY_EXCLUDE)
    out: list[str] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, str):
            continue
        name = item.strip()
        if not name:
            continue
        key = name.casefold()
        if key in seen:
            continue
        seen.add(key)
        out.append(name)
    return out


@dataclass
class Settings:
    """全局行为参数（不含外观；外观按分类独立）。"""

    icon_px_default: int = 64        # 新建分类的默认图标尺寸
    panel_item_px: int = 48          # 面板内条目图标像素（常量语义）
    drag_threshold_px: int = DRAG_THRESHOLD_PX
    edit_press_ms: int = 400         # 面板条目长按进入编辑态
    watch_poll_ms: int = 1000        # 目录轮询签名比对

    # -- v1.4 新增 --------------------------------------------------------
    data_root: str = ""              # 分类数据根；""= 默认 %USERPROFILE%\DeskCountChen
    category_exclude: list[str] = field(
        default_factory=lambda: list(DEFAULT_CATEGORY_EXCLUDE)
    )
    shortcut_mode: str = SHORTCUT_MODE_LAUNCHER   # launcher | desktop
    strip_extensions: bool = True    # 面板内名称去扩展名
    strip_shortcut_overlay: bool = True  # 解析 .lnk 取目标图标（去掉小箭头）

    def to_dict(self) -> dict:
        return {
            "icon_px_default": self.icon_px_default,
            "panel_item_px": self.panel_item_px,
            "drag_threshold_px": self.drag_threshold_px,
            "edit_press_ms": self.edit_press_ms,
            "watch_poll_ms": self.watch_poll_ms,
            "data_root": self.data_root,
            # 必须 copy：直接返回内部 list 会让 to_dict 的调用方改到运行时状态
            "category_exclude": list(self.category_exclude),
            "shortcut_mode": self.shortcut_mode,
            "strip_extensions": self.strip_extensions,
            "strip_shortcut_overlay": self.strip_shortcut_overlay,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "Settings":
        d = d if isinstance(d, dict) else {}
        return cls(
            icon_px_default=clamp_int(d.get("icon_px_default"), ICON_PX_MIN, ICON_PX_MAX, 64),
            panel_item_px=clamp_int(d.get("panel_item_px"), 32, 96, 48),
            drag_threshold_px=clamp_int(d.get("drag_threshold_px"), 2, 16, DRAG_THRESHOLD_PX),
            edit_press_ms=clamp_int(d.get("edit_press_ms"), 200, 2000, 400),
            watch_poll_ms=clamp_int(d.get("watch_poll_ms"), 500, 10000, 1000),
            data_root=normalize_data_root(d.get("data_root")),
            category_exclude=normalize_category_exclude(d.get("category_exclude")),
            shortcut_mode=normalize_shortcut_mode(d.get("shortcut_mode")),
            strip_extensions=normalize_bool(d.get("strip_extensions"), True),
            strip_shortcut_overlay=normalize_bool(d.get("strip_shortcut_overlay"), True),
        )

    def is_dir_excluded(self, dir_name: str) -> bool:
        """该目录名是否在"不登记为分类"的名单里（大小写不敏感）。"""
        key = str(dir_name or "").strip().casefold()
        if not key:
            return True
        return key in {name.casefold() for name in self.category_exclude}


@dataclass
class CategoryConfig:
    """单个分类的全部配置（含独立外观）。"""

    id: str = ""
    dir: str = ""                    # 数据目录名，创建后不可改
    label: str = ""                  # 图标下方显示文字（可改，≤20 字符）
    image: str = ""                  # 自定义图标图片绝对路径；""=内置默认
    icon_px: int = 64                # 48–128，每分类独立（ICO 源图尺寸）
    visible: bool = True             # 是否在桌面上保留 .lnk（False = 从桌面移除）
    panel_w: int = 420               # 面板宽高记忆（≥240×160）
    panel_h: int = 300
    radius: int = 12                 # 面板圆角 0–24
    bg_color: str = _DEFAULT_BG_COLOR
    opacity: int = 88                # 面板透明度 0–100（0=只剩内容）
    position_mode: str = "anchor"    # anchor=贴合鼠标落点 / center=屏幕居中
    material: str = MATERIAL_BLUR    # v1.5：blur=毛玻璃(截屏模糊) / solid=纯色

    def dir_path(self, root: Path) -> Path:
        """本分类的数据目录（不创建）。"""
        return Path(root) / self.dir

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "dir": self.dir,
            "label": self.label,
            "image": self.image,
            "icon_px": self.icon_px,
            "visible": self.visible,
            "panel_w": self.panel_w,
            "panel_h": self.panel_h,
            "radius": self.radius,
            "bg_color": self.bg_color,
            "opacity": self.opacity,
            "position_mode": self.position_mode,
            "material": self.material,
        }

    @classmethod
    def from_dict(cls, d: dict | None) -> "CategoryConfig":
        d = d if isinstance(d, dict) else {}
        mode = str(d.get("position_mode", "anchor"))
        if mode not in _POSITION_MODES:
            mode = "anchor"
        return cls(
            id=str(d.get("id") or f"cat_{uuid.uuid4().hex[:8]}"),
            dir=str(d.get("dir") or ""),
            label=str(d.get("label") or "")[:LABEL_MAX_CHARS],
            image=str(d.get("image") or ""),
            icon_px=clamp_int(d.get("icon_px"), ICON_PX_MIN, ICON_PX_MAX, 64),
            visible=bool(d.get("visible", True)),
            panel_w=clamp_int(d.get("panel_w"), MIN_PANEL_W, 4000, 420),
            panel_h=clamp_int(d.get("panel_h"), MIN_PANEL_H, 3000, 300),
            radius=clamp_int(d.get("radius"), RADIUS_MIN, RADIUS_MAX, 12),
            bg_color=_valid_color(d.get("bg_color")),
            opacity=clamp_int(d.get("opacity"), OPACITY_MIN, OPACITY_MAX, 88),
            position_mode=mode,
            # v1.5：旧配置（v1.4）没有该字段 → 默认毛玻璃，向后兼容
            material=normalize_material(d.get("material")),
        )

    def sanitize(self) -> None:
        """就地 clamp 全部数值字段（外部改动后调用，保证约束）。"""
        self.icon_px = clamp_int(self.icon_px, ICON_PX_MIN, ICON_PX_MAX, 64)
        self.panel_w = clamp_int(self.panel_w, MIN_PANEL_W, 4000, 420)
        self.panel_h = clamp_int(self.panel_h, MIN_PANEL_H, 3000, 300)
        self.radius = clamp_int(self.radius, RADIUS_MIN, RADIUS_MAX, 12)
        self.opacity = clamp_int(self.opacity, OPACITY_MIN, OPACITY_MAX, 88)
        self.label = str(self.label)[:LABEL_MAX_CHARS] or "分类"
        self.bg_color = _valid_color(self.bg_color)
        if self.position_mode not in _POSITION_MODES:
            self.position_mode = "anchor"
        self.material = normalize_material(self.material)


@dataclass
class AppConfig:
    """全局配置（根对象）。"""

    version: int = 1
    app: Settings = field(default_factory=Settings)
    categories: list[CategoryConfig] = field(default_factory=list)
    config_path: Path | None = None

    # -- 持久化 ------------------------------------------------------------

    def to_dict(self) -> dict:
        return {
            "version": self.version,
            "app": self.app.to_dict(),
            "categories": [c.to_dict() for c in self.categories],
        }

    @classmethod
    def from_dict(cls, d: dict, config_path: Path | None = None) -> "AppConfig":
        d = d if isinstance(d, dict) else {}
        # 兼容迁移：旧版配置把外观字段放在 app 段（全局皮肤）。
        # 读取后下发给所有分类作为默认值，然后从 app 段丢弃（Settings 不收）。
        legacy_skin: dict = {}
        app_raw = d.get("app")
        if isinstance(app_raw, dict):
            for key in ("radius", "bg_color", "opacity", "position_mode"):
                if key in app_raw:
                    legacy_skin[key] = app_raw[key]
        cats_raw = d.get("categories")
        cats: list[CategoryConfig] = []
        pairs: list[tuple[dict, CategoryConfig]] = []  # (原始 dict, 解析后的配置)
        if isinstance(cats_raw, list):
            seen_ids: set[str] = set()
            for item in cats_raw:
                try:
                    cat = CategoryConfig.from_dict(item)
                except Exception as exc:  # noqa: BLE001 - 单条损坏不拖垮全局
                    logger.warning("分类配置损坏，已跳过: %s (%s)", item, exc)
                    continue
                if not cat.dir:
                    logger.warning("分类配置缺少 dir，已跳过: %s", cat.id)
                    continue
                if cat.id in seen_ids:  # 防御：id 重复时重新生成
                    cat.id = f"cat_{uuid.uuid4().hex[:8]}"
                seen_ids.add(cat.id)
                cats.append(cat)
                pairs.append((item if isinstance(item, dict) else {}, cat))
        if legacy_skin:
            # 迁移原则：只填充"该字段在分类 dict 中缺失"的分类；
            # 分类里已显式写过的皮肤值（用户调过的）绝不被旧全局值覆盖。
            logger.info("迁移旧版全局外观字段到缺失该字段的分类: %s", list(legacy_skin))
            for raw, cat in pairs:
                if "radius" in legacy_skin and "radius" not in raw:
                    cat.radius = clamp_int(legacy_skin["radius"], RADIUS_MIN, RADIUS_MAX, 12)
                if "opacity" in legacy_skin and "opacity" not in raw:
                    cat.opacity = clamp_int(legacy_skin["opacity"], OPACITY_MIN, OPACITY_MAX, 88)
                if "bg_color" in legacy_skin and "bg_color" not in raw:
                    cat.bg_color = _valid_color(legacy_skin["bg_color"])
                if ("position_mode" in legacy_skin and "position_mode" not in raw
                        and legacy_skin["position_mode"] in _POSITION_MODES):
                    cat.position_mode = str(legacy_skin["position_mode"])
        return cls(
            version=clamp_int(d.get("version"), 1, 1, 1),
            app=Settings.from_dict(app_raw),  # 未知键（含旧外观字段）自然被丢弃
            categories=cats,
            config_path=config_path,
        )

    @classmethod
    def load(cls, path: Path) -> "AppConfig":
        """加载配置；文件缺失/损坏时返回默认配置且不覆盖原文件。"""
        try:
            text = Path(path).read_text(encoding="utf-8")
            data = json.loads(text)
        except FileNotFoundError:
            logger.info("配置文件不存在，使用默认配置: %s", path)
            return cls(config_path=path)
        except (OSError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            logger.warning("配置文件损坏，使用默认配置(不覆盖原文件) %s: %s", path, exc)
            return cls(config_path=path)
        cfg = cls.from_dict(data, config_path=path)
        logger.info("配置已加载: %s（%d 个分类）", path, len(cfg.categories))
        return cfg

    def save(self) -> None:
        """原子写配置（配置写入的唯一入口）。"""
        if self.config_path is None:
            return
        atomic_write_json(Path(self.config_path), self.to_dict())

    # -- 分类管理 ----------------------------------------------------------

    def find(self, cat_id: str) -> CategoryConfig | None:
        """按 id 查找分类。"""
        for c in self.categories:
            if c.id == cat_id:
                return c
        return None

    def find_by_dir(self, dir_name: str) -> CategoryConfig | None:
        """按数据目录名查找分类。"""
        for c in self.categories:
            if c.dir == dir_name:
                return c
        return None

    def add_category(self, dir_name: str, label: str, icon_px: int | None = None) -> CategoryConfig:
        """新增分类（桌面位置由 Windows 管理，不再分配错开坐标）。"""
        cat = CategoryConfig(
            id=f"cat_{uuid.uuid4().hex[:8]}",
            dir=dir_name,
            label=str(label)[:LABEL_MAX_CHARS] or dir_name,
            icon_px=icon_px if icon_px is not None else self.app.icon_px_default,
        )
        cat.sanitize()
        self.categories.append(cat)
        return cat

    def remove_category(self, cat_id: str) -> None:
        """移除分类登记（不删除磁盘目录）。"""
        self.categories = [c for c in self.categories if c.id != cat_id]
