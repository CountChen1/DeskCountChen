"""分类"启动器"快捷方式全生命周期管理（.ico 生成 + .lnk 落点同步）。

v1.4 变更（入口改为 SAO Utils 2 的 HUD 图标）：
- 用户实际点的是 SAO Utils 2 的 HUD 图标，DeskCountChen **不再往真实桌面写 .lnk**；
  改为在私有目录 ``%USERPROFILE%\\DeskCountChen\\.launchers`` 生成
  ``启动-<分类>.lnk``，供 SAO 的 "Change Action → 选择 .lnk" 引用。
  单击 = 起第二个实例 → 单实例唤醒 → 第一实例弹面板（Win+D 后依旧在）。
- ``mode`` 决定落点：
  - ``"launcher"``（默认）：写 ``launcher_dir``，**绝不触碰桌面目录**；
  - ``"desktop"``：v1.3 行为（写真实桌面），保留作为可回退模式。
- **迁移清理**：首次以 launcher 模式同步时，把历史上写在真实桌面的 .lnk 收掉。
  安全红线：删除前必须用 shell_resolve 解析出 target/arguments，确认它指向
  本项目的 python(w).exe 且参数含 ``--open``；解析失败或目标不符一律跳过。

设计（架构决策见 docs/design.md §10，v1.4 见 docs/SAO-接入.md，v1.6 见 docs/deploy.md）：
- 源码态快捷方式目标 = ``pythonw.exe <项目根>\\main.py --open <分类目录名>``，
  工作目录 = 项目根，图标 = 本模块生成的分类 .ico；
- frozen（PyInstaller 打包）态目标 = ``DeskCountChen.exe --open <分类目录名>``
  （``sys.executable`` 就是产品 exe，无脚本参数），工作目录 = exe 所在目录；
- .ico 缓存到 ``.icons/ico/<cat_id>.ico``，皮肤（image）或尺寸变化时重建；
- 幂等同步：缺则补建、目标/图标/名字变化则重建、分类隐藏则删除；
- **不重写没变化的 .lnk**（Windows 按路径缓存图标位置，无谓重写会引起重排），
  是否变化靠 ``.icons/lnk_state.json`` 的内容指纹判断（原子写）。
"""

from __future__ import annotations

import json
import logging
import os
import re
import struct
import sys
from pathlib import Path

from PySide6.QtCore import QBuffer, QIODevice, Qt
from PySide6.QtGui import QImage, QPixmap

from app import brand, paths
from app.config import (
    SHORTCUT_MODE_DESKTOP,
    SHORTCUT_MODE_LAUNCHER,
    atomic_write_json,
)
from app.win32 import shell_link, shell_resolve

logger = logging.getLogger(__name__)

LNK_SUFFIX = ".lnk"
INVALID_STEM_CHARS = '\\/:*?"<>|'      # 与"新建分类"的校验规则保持一致
STEM_MAX_CHARS = 60
ICO_PX = 256                          # ICO 源图边长：Windows 会按需缩放（32/48/96/256）
ICO_SIZES = (256, 48, 32, 16)         # 多尺寸 ICO 里打包的边长（含小尺寸，保证小图标清晰）
ICO_MAGIC = (0, 1)                    # reserved=0, type=1（图标）
ICO_HEADER_SIZE = 6
ICO_ENTRY_SIZE = 16
STATE_VERSION = 1

# 本程序自己生成的分类图标缓存文件名（cat_ + 8 位十六进制 + .ico）。
# 用于 _prune_orphan_icons 的纵深防御：只删符合本程序命名规范的文件。
# 注意：这是"额外保护"而不是主判据——主判据是"本次真正注销的分类 id"
# （removed_ids）；两者冲突时宁可不删（多留几 KB 缓存 << 误删用户文件）。
ICO_NAME_RE = re.compile(r"cat_[0-9a-f]{8}\.ico")

MODE_LAUNCHER = SHORTCUT_MODE_LAUNCHER
MODE_DESKTOP = SHORTCUT_MODE_DESKTOP
# 启动器 .lnk 的文件名前缀：SAO 里看到的都是"启动-xxx"，一眼能认出用途
LAUNCHER_PREFIX = "启动-"
# 历史记录缺 where 时按桌面模式理解（v1.3 只有桌面落点）
DEFAULT_RECORD_WHERE = MODE_DESKTOP

__all__ = [
    "DesktopShortcutManager",
    "LAUNCHER_PREFIX",
    "MODE_DESKTOP",
    "MODE_LAUNCHER",
    "launcher_file_name",
    "plan_lnk_names",
    "sanitize_stem",
    "validate_ico",
]


def sanitize_stem(label: str) -> str:
    """把分类名清洗成合法的 .lnk 主文件名（与新建分类的非法字符规则一致）。"""
    text = str(label or "")
    for ch in INVALID_STEM_CHARS:
        text = text.replace(ch, "_")
    # Windows 不允许文件名以点或空格结尾；控制字符一并清掉
    text = "".join(ch for ch in text if ord(ch) >= 32).strip().strip(".").strip()
    if not text:
        text = "分类"
    return text[:STEM_MAX_CHARS]


def launcher_file_name(base_name: str) -> str:
    """把 ``<分类>.lnk`` 变成启动器文件名 ``启动-<分类>.lnk``（幂等）。"""
    name = str(base_name or "")
    if name.casefold().startswith(LAUNCHER_PREFIX.casefold()):
        return name
    return f"{LAUNCHER_PREFIX}{name}"


def plan_lnk_names(categories) -> dict[str, str]:
    """为每个分类规划唯一的桌面 .lnk 文件名（含扩展名）。

    纯函数，便于单测。重名（不同分类同 label）时追加 `` (2)``、`` (3)``，
    大小写不敏感判重（Windows 语义）。
    """
    used: set[str] = set()
    plan: dict[str, str] = {}
    for cat in categories:
        base = sanitize_stem(getattr(cat, "label", "") or getattr(cat, "dir", ""))
        stem = base
        index = 2
        while f"{stem}{LNK_SUFFIX}".casefold() in used:
            stem = f"{base} ({index})"
            index += 1
        used.add(f"{stem}{LNK_SUFFIX}".casefold())
        plan[str(getattr(cat, "id", ""))] = f"{stem}{LNK_SUFFIX}"
    return plan


def validate_ico(path: Path) -> bool:
    """校验 .ico 结构（ICONDIR + 每个 ICONDIRENTRY 的偏移/长度都在文件内）。"""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return False
    if len(data) < ICO_HEADER_SIZE:
        return False
    reserved, kind, count = struct.unpack("<HHH", data[:ICO_HEADER_SIZE])
    if (reserved, kind) != ICO_MAGIC or count < 1:
        return False
    directory_end = ICO_HEADER_SIZE + count * ICO_ENTRY_SIZE
    if len(data) < directory_end:
        return False
    for i in range(count):
        off = ICO_HEADER_SIZE + i * ICO_ENTRY_SIZE
        entry = struct.unpack("<BBBBHHII", data[off:off + ICO_ENTRY_SIZE])
        size, offset = entry[6], entry[7]
        if size <= 0 or offset < directory_end or offset + size > len(data):
            return False
    return True


def _png_bytes(pm: QPixmap) -> bytes:
    """QPixmap → PNG 字节（自研 ICO 容器的负载）。"""
    if pm.isNull():
        return b""
    buf = QBuffer()
    buf.open(QIODevice.OpenModeFlag.WriteOnly)
    image: QImage = pm.toImage()
    if not image.save(buf, "PNG"):
        buf.close()
        return b""
    data = bytes(buf.data())
    buf.close()
    return data


def _write_ico_multi(dest: Path, source: QPixmap, sizes: tuple[int, ...]) -> bool:
    """自研多尺寸 ICO 容器（PNG 负载）——Qt 的 ICO 插件只能写单张图时的兜底。

    Vista+ 的 ICO 支持 PNG 压缩负载；每个目录项 bWidth/bHeight 为 0 表示 256。
    """
    payloads: list[tuple[int, bytes]] = []
    seen: set[int] = set()
    for size in sizes:
        if size in seen or size > 256:
            continue
        seen.add(size)
        scaled = source.scaled(
            size, size,
            Qt.AspectRatioMode.KeepAspectRatio,
            Qt.TransformationMode.SmoothTransformation,
        )
        data = _png_bytes(scaled)
        if data:
            payloads.append((size, data))
    if not payloads:
        return False
    header = struct.pack("<HHH", ICO_MAGIC[0], ICO_MAGIC[1], len(payloads))
    offset = ICO_HEADER_SIZE + ICO_ENTRY_SIZE * len(payloads)
    entries = b""
    body = b""
    for size, data in payloads:
        dim = 0 if size >= 256 else size
        entries += struct.pack("<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset)
        body += data
        offset += len(data)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.write_bytes(header + entries + body)
        os.replace(tmp, dest)
    except OSError as exc:
        logger.warning("写 ICO 失败（自研容器）%s: %s", dest, exc)
        try:
            tmp.unlink(missing_ok=True)
        except OSError:
            pass
        return False
    return validate_ico(dest)


def _write_ico(dest: Path, source: QPixmap) -> bool:
    """把分类图标写成 .ico，成功返回 True。

    **优先自研多尺寸容器**（16/32/48/256 + PNG 负载）：实测 Qt 的 ICO 插件走
    ``QPixmap.save(..., "ICO")`` 时写的是**单张未压缩 BMP 负载**（256×256 就有
    270KB，且资源管理器小图标视图只能缩放出模糊的 16px）。多尺寸容器体积小一个
    数量级、小尺寸也清晰，因此作为首选。
    Qt 插件只在多尺寸容器失败时兜底（保证极端环境仍有图标可用）。
    """
    dest = Path(dest)
    if _write_ico_multi(dest, source, ICO_SIZES):
        return True
    logger.warning("多尺寸 ICO 写出失败，退回 Qt 单图 ICO: %s", dest)
    tmp = dest.with_name(dest.name + ".tmp")
    try:
        tmp.unlink(missing_ok=True)
        if source.save(str(tmp), "ICO") and validate_ico(tmp):
            os.replace(tmp, dest)
            return True
    except Exception as exc:  # noqa: BLE001 - 降级路径必须能兜住
        logger.warning("Qt ICO 写出失败 %s: %s", dest, exc)
    try:
        tmp.unlink(missing_ok=True)
    except OSError:
        pass
    return False


def _default_python_exe() -> str:
    """pythonw.exe（无控制台窗口）；找不到时退回当前解释器。"""
    exe = Path(sys.executable)
    if exe.name.lower() == "python.exe":
        candidate = exe.with_name("pythonw.exe")
        if candidate.exists():
            return str(candidate)
    elif exe.name.lower() == "pythonw.exe":
        return str(exe)
    return str(exe)


class DesktopShortcutManager:
    """分类启动器快捷方式的创建 / 更新 / 删除 + 分类 ICO 生成。

    v1.4：落点由 ``mode`` 决定（launcher = 私有启动器目录，desktop = 真实桌面）。
    """

    def __init__(
        self,
        icon_cache,
        ico_dir: Path,
        state_path: Path,
        project_root: Path | None = None,
        desktop_dir: Path | None = None,
        python_exe: str | None = None,
        script_path: Path | None = None,
        mode: str = MODE_LAUNCHER,
        launcher_dir: Path | None = None,
        frozen_target: bool | None = None,
    ) -> None:
        """
        Args:
            icon_cache: IconCache（提供 category_pixmap / default_icon）。
            ico_dir: ICO 缓存目录（.icons/ico）。
            state_path: 同步状态文件（.icons/lnk_state.json）。
            project_root: 项目根目录（.lnk 的工作目录 = main.py 所在目录）；
                None = 自动取 app 包的上一级目录。
            desktop_dir: 桌面目录；None = 每次同步时用 SHGetKnownFolderPath 解析
                （单元测试注入临时目录，避免动真实桌面）。
            python_exe: .lnk 目标解释器；None = 自动取 venv 的 pythonw.exe。
            script_path: .lnk 目标脚本；None = project_root / "main.py"。
            mode: ``"launcher"``（默认，写 launcher_dir，绝不碰桌面）或
                ``"desktop"``（v1.3 行为）；非法值按 launcher 处理。
            launcher_dir: 启动器 .lnk 目录；None = ``paths.LAUNCHERS_DIR``。
            frozen_target: 显式指定 .lnk 目标语义（B4 迁移工具用）。None =
                按当前进程 ``sys.frozen`` 自动判断；True = 即使在源码态进程里
                也按 frozen 语义取默认值（目标 = exe + 仅 ``--open`` 参数）。
                迁移脚本从源码 venv 运行但要求生成 frozen 形态的 .lnk，
                按进程检测会得出错误结论（实测踩过：参数被塞进 main.py）。
        """
        self.icon_cache = icon_cache
        self.ico_dir = Path(ico_dir)
        self.state_path = Path(state_path)
        self._frozen = (getattr(sys, "frozen", False)
                        if frozen_target is None else bool(frozen_target))
        self.desktop_dir = Path(desktop_dir) if desktop_dir is not None else None
        if python_exe is not None:
            self.python_exe = str(python_exe)
        elif self._frozen:
            # frozen 态：.lnk 目标 = 产品 exe 自身（--open 参数见 _arguments）
            self.python_exe = str(Path(sys.executable).resolve())
        else:
            self.python_exe = _default_python_exe()
        if project_root is not None:
            self.project_root = Path(project_root)
        elif self._frozen:
            # frozen 态：工作目录 = 目标 exe 所在目录。从 python_exe 推导
            # （真实 frozen 运行时它就是 sys.executable；frozen_target=True
            # 的源码进程里 sys.executable 是解释器，不能拿来当产品目录）
            self.project_root = Path(self.python_exe).resolve().parent
        else:
            self.project_root = paths.APP_ROOT
        if script_path is not None:
            self.script_path: Path | None = Path(script_path)
        elif self._frozen:
            # frozen 态没有 main.py：目标直接是 exe，参数只有 --open
            self.script_path = None
        else:
            self.script_path = self.project_root / "main.py"
        self.launcher_dir = Path(launcher_dir) if launcher_dir is not None else paths.LAUNCHERS_DIR
        self.mode = MODE_DESKTOP if str(mode) == MODE_DESKTOP else MODE_LAUNCHER
        self._auto_desktop: Path | None = None
        self._state: dict[str, dict] = {}
        self._warned_no_desktop = False
        self._warned_forbidden: set[str] = set()   # v1.5：启动器目录踩线告警去重

    # -- 对外入口 -----------------------------------------------------------

    def sync(self, categories) -> None:
        """把快捷方式同步到配置状态（幂等，可在任何变更后调用）。"""
        # 迁移清理必须先做：它要把"历史上写到桌面"的 .lnk 收掉（只在 launcher 模式）
        self._cleanup_legacy_desktop_lnks()
        base_dir = self._target_dir()
        if base_dir is None:
            return
        plan = self._plan(categories)
        state = self._load_state()
        # 已管理基数：**必须在下面两个 pop 循环之前**取，且取自**磁盘上的 state**
        # （``_load_state()`` 的返回值），而不是 ``self._state`` —— 后者在进程刚启动
        # 时还是空字典（只有 sync 末尾才赋值），会让熔断判据算成 0 → 熔断失效。
        # 2026-11-12 16:58 的真实形态正是"重启后 cfg 只剩 1 个分类、state 里仍有 6 条"。
        managed_before = len(state)
        changed = False
        alive: set[str] = set()

        for cat in categories:
            cat_id = str(getattr(cat, "id", ""))
            alive.add(cat_id)
            name = plan.get(cat_id, "")
            record = dict(state.get(cat_id) or {})
            old_name = str(record.get("lnk") or "")
            old_where = str(record.get("where") or DEFAULT_RECORD_WHERE)

            if not getattr(cat, "visible", True):
                # 隐藏分类 = 删除 .lnk（分类与面板仍可从托盘访问）
                if old_where == self.mode:
                    self._delete_lnk(base_dir, old_name)
                else:
                    self._delete_own_lnk(self._dir_for_mode(old_where), old_name,
                                         reason="换落点清理")
                if cat_id in state:
                    state.pop(cat_id, None)
                    changed = True
                continue

            ico_path, ico_sig, ico_rebuilt = self.ensure_ico(cat, record)
            signature = self._signature(cat, name, ico_path)
            dest = base_dir / name
            if (record.get("sig") == signature
                    and record.get("lnk") == name
                    and record.get("where") == self.mode
                    and dest.exists()
                    and not ico_rebuilt):
                continue

            if old_name and (old_name != name or old_where != self.mode):
                # 重命名 / 换落点：旧文件先删
                if old_where == self.mode:
                    self._delete_lnk(base_dir, old_name)
                else:
                    self._delete_own_lnk(self._dir_for_mode(old_where), old_name,
                                         reason="换落点清理")
            if self._write_lnk(dest, cat, ico_path):
                state[cat_id] = {
                    "lnk": name,
                    "sig": signature,
                    "ico_sig": ico_sig,
                    "where": self.mode,
                }
                changed = True

        # 已从配置里移除的分类：把它的 .lnk 一并清掉。
        # 关键防御：**只能按"这个名字现在没人用了"删除**——分类被删后重建
        # （id 变了、label 相同）时，旧记录的 lnk 名和新分类完全一样，盲删会
        # 把刚建好的快捷方式删掉（实测踩过）。
        #
        # 注意：本循环里 .lnk 的删除**不做熔断**（启动器必须跟着配置走，否则 SAO
        # 会指向不存在的文件，比留几个死 .ico 糟得多）；熔断只作用于 .ico 清理。
        # 但 state 记录照常 pop —— 记录描述的是"我生成过什么"，配置里没有就该清。
        keep_names = {name.casefold() for name in plan.values()}
        removed_ids: set[str] = set()   # 本次**真正注销**的分类 id（prune 的唯一判据）
        for cat_id in [cid for cid in state if cid not in alive]:
            record = state.get(cat_id) or {}
            name = str(record.get("lnk") or "")
            where = str(record.get("where") or DEFAULT_RECORD_WHERE)
            if name and name.casefold() in keep_names:
                logger.info("跳过删除（同名快捷方式已归属其他分类）: %s", name)
            elif where == self.mode:
                self._delete_lnk(base_dir, name)
            else:
                self._delete_own_lnk(self._dir_for_mode(where), name, reason="注销清理")
            removed_ids.add(str(cat_id))
            state.pop(cat_id, None)
            changed = True

        self._prune_orphan_icons(alive, removed_ids, managed_before)
        if changed:
            self._save_state(state)
        self._state = state
        self._log_unmanaged_icons(alive)

    def launcher_paths(self, categories) -> dict[str, Path]:
        """``cat_id → 启动器 .lnk 路径``（供文档 / 调试；不创建文件、不读配置）。

        与 mode 无关：永远给出"如果交给 SAO 引用，应该指向哪个文件"。
        走 :func:`plan_lnk_names` 保证与 sync 完全同一套重名去重规则。
        """
        base = Path(self.launcher_dir)
        plan = plan_lnk_names(categories)
        return {cat_id: base / launcher_file_name(name) for cat_id, name in plan.items()}

    def _prune_orphan_icons(self, alive: set[str], removed_ids: set[str],
                            managed_before: int) -> None:
        """清理"本次真正注销的分类"的 .ico 缓存。

        **判据 = 注销动作的直接产物**（``removed_ids`` 由 ``sync()`` 在"配置里
        已不存在、但 lnk_state 里仍有记录"的那一批里收集），**不是**"当前配置里
        没有这个 id"。

        为什么不能用"配置里没有"当判据：配置被整体替换或缩小（迁移换数据根、
        手工编辑 ``.config.json`` 后重启、读到损坏/空配置）时，用户其余分类的
        ico 会全部变成"孤儿"而被删掉 —— 2026-11-12 16:58 的日志里就误删过 5 个
        （``cat_0885b2c4 / cat_992dc662 / cat_b675af73 / cat_qa_a / cat_qa_b``）。

        ``removed_ids`` 只解决了"从未由本程序管理过的文件被删"这一半问题。
        16:58 的真实形态是 **cfg 被夹具覆盖成 1 个分类、state 里仍有 6 条记录**
        —— 那种情况下 ``removed_ids`` 恰好是"其余 5 条"，与"用户主动删了 5 个
        分类"在信息上**完全同构**，判据无从区分。因此再加两道闸（见下）。

        三道闸（顺序不可调换）：
            闸 1（``alive`` 为空）：配置里一个分类都没有 → 几乎必然是配置不可读 /
                数据根未挂载（掉盘、读到空文件、首次运行读到坏 JSON）。此时
                ``removed_ids`` 恰好 == 全部 state 记录，绝不能当"全部注销"处理。
            闸 2（``removed_ids`` 为空）：无事可做，直接返回。
            闸 3（**比例熔断**）：一次同步里"注销"占比过半 → 判定配置可疑，跳过
                清理。代价是可能多留几个 KB 的 ico（可再生缓存）；收益是配置被
                覆盖时不会把用户其他分类的图标批量删掉。

        Args:
            alive: 本次配置里存在的分类 id 集合（闸 1 用）。
            removed_ids: 本次真正注销的分类 id 集合（来自 ``sync()`` 的注销循环）。
            managed_before: 本次同步**开始前**（两个 ``state.pop`` 之前）state 里的
                记录数；取 ``_load_state()`` 的返回值而不是 ``self._state``
                （后者在进程刚启动时还是空字典 → 熔断永远不触发）。

        刻意保留的残留（**已知限制，别去修**）：
            磁盘上"配置里没有、state 里也没有"的 .ico（例如上面那批）**不会被
            自动清理**。两种产生方式：
              ① 注销时 ico 被 explorer/SAO 短暂持有 → ``unlink`` 失败只记
                 warning，此后该 id 再不会进 ``removed_ids``；
              ② **先隐藏、后删除**：隐藏时 ③ 之外的路径已 ``state.pop`` 但 ico
                 保留（这是正确行为）；之后彻底删除时该 id 既不在 ``alive`` 也
                 不在 ``state``，而注销循环的遍历来源是 ``state`` → 永远看不到它。
                 旧判据（"cfg 里没有就删"）在这一步是能删的，**这是行为变化**。
            要"修"这两条只能回到"猜哪些该删"的老判据，一改就重演 16:58 的误删。
            宁可多留几 KB 缓存，也不误删用户文件；要收口请做**独立的维护动作**
            （tools/ 下显式扫描 + 人工确认），不要塞回 ``sync()`` 的隐式路径里。
            ``sync()`` 末尾的 ``_log_unmanaged_icons`` 会把这类残留数量记进 debug
            日志，保证它**可观测**而不是黑盒。

        纵深防御（代码审查追加，独立于"id 可信"这一假设）：
            ① 目标路径 resolve 之后必须是 ``ico_dir`` 的**直接子文件**
               （``Path.is_relative_to`` 是**词法**比较，必须先 resolve 才能挡住
               ``..`` 穿越：``base/".."/"cat_x.ico"`` 会被它误判为"在 ico_dir 内"）；
            ② 只删形如 ``cat_[0-9a-f]{8}.ico`` 的文件；
            ③ 其余一律跳过并记 debug 日志（不抛异常、不影响主流程）。
        """
        # 闸 1：配置里一个分类都没有 → 一律不动手（cfg=[] 时 removed_ids 是全部）
        if not alive:
            # state 也为空时本来就没东西可删，不必打扰用户（首次运行 / 全新安装）
            if managed_before > 0:
                logger.warning(
                    "配置里没有任何分类（拟注销 %d/%d 个同步记录），疑似配置不可读或"
                    "数据根未挂载，跳过图标缓存清理", len(removed_ids), managed_before)
            return
        # 闸 2：无事可做
        if not removed_ids:
            return
        # 闸 3：熔断 —— 一次同步里注销占比过高，判定配置可疑，跳过清理。
        # 阈值 = ceil(managed_before / 2)，下限 2（避免"2 个里注销 1 个"也算异常）。
        threshold = max(2, (managed_before + 1) // 2)
        if managed_before >= 2 and len(removed_ids) >= threshold:
            logger.warning(
                "疑似配置异常：本次将注销 %d/%d 个分类（阈值 %d），"
                "跳过图标缓存清理以保护用户图标", len(removed_ids), managed_before, threshold)
            return
        try:
            base = Path(self.ico_dir)
            base_resolved = base.resolve()
        except (OSError, RuntimeError, TypeError, ValueError):
            logger.debug("ico_dir 非法或无法解析，跳过缓存清理: %r", self.ico_dir)
            return
        for raw_id in sorted(str(cid) for cid in removed_ids):
            ico = base / f"{raw_id}.ico"
            # ① 目录穿越防线：必须 **resolve 之后** 再比较。
            #    坑：`Path.is_relative_to` 是纯**词法**比较，`base/".."/"cat_x.ico"`
            #    的 parts 以 base 开头 → 会返回 True，`..` 穿越就被放行了。
            #    因此这里先 resolve 消掉 `..`/符号链接，再要求"是 ico_dir 的直接子文件"。
            try:
                target_resolved = ico.resolve()
                inside = (target_resolved.parent == base_resolved
                          and target_resolved.is_relative_to(base_resolved))
            except (OSError, RuntimeError, ValueError):
                inside = False
            if not inside:
                logger.debug("跳过越界的图标缓存路径: %s", ico)
                continue
            # ② 命名规范防线
            if not ICO_NAME_RE.fullmatch(ico.name):
                logger.debug("跳过非本程序命名的图标缓存: %s", ico.name)
                continue
            try:
                if not ico.is_file():
                    continue
                ico.unlink()
                logger.info("清理已注销分类的图标缓存: %s", ico.name)
            except OSError as exc:  # noqa: BLE001 - 缓存清理失败不算错误
                logger.warning("清理图标缓存失败（%s）: %s", ico.name, exc)

    def _log_unmanaged_icons(self, alive: set[str]) -> None:
        """把"未受管图标缓存"的数量记进 debug 日志（**只观测，不删除**）。

        ``removed_ids`` 判据必然留下永久残留（见 ``_prune_orphan_icons`` 的
        "已知限制"）。没有这条日志时，用户问"为什么 ``.icons/ico`` 里有用不到的
        图标"只能靠猜；有了它，一次 ``--debug`` 就能给出确定答案。

        Args:
            alive: 本次配置里存在的分类 id 集合；其 ``<id>.ico`` 视为受管。

        用 debug 级别是刻意的：正常情况下该数量为 0，不该在用户日志里刷屏。
        """
        try:
            base = Path(self.ico_dir)
            managed = {f"{cid}.ico" for cid in alive}
            stray = [
                p.name for p in base.iterdir()
                if p.is_file() and ICO_NAME_RE.fullmatch(p.name) and p.name not in managed
            ]
        except (OSError, TypeError, ValueError) as exc:  # noqa: BLE001 - 纯观测，失败即静默
            logger.debug("统计未受管图标缓存失败: %s", exc)
            return
        logger.debug("未受管图标缓存 %d 个（可能是已注销残留，不自动删除）", len(stray))

    def ensure_ico(self, cat, record: dict | None = None) -> tuple[Path | None, str, bool]:
        """保证分类的 .ico 是最新的。

        Returns:
            (ico 路径或 None, 图标指纹, 是否本次重建)。重建时调用方必须刷新 .lnk，
            否则 Windows 会继续显示旧图标。
        """
        record = record or {}
        dest = self.ico_dir / f"{cat.id}.ico"
        sig = self._image_signature(cat)
        if dest.exists() and record.get("ico_sig") == sig and validate_ico(dest):
            return dest, sig, False

        pm = self.icon_cache.category_pixmap(cat, ICO_PX)
        if pm.isNull():
            pm = self.icon_cache.default_icon(ICO_PX)
        try:
            self.ico_dir.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建 ICO 目录失败 %s: %s", self.ico_dir, exc)
            return None, "", False
        if _write_ico(dest, pm):
            logger.info("分类图标 ICO 已生成: %s", dest)
            return dest, sig, True
        logger.warning("分类图标 ICO 生成失败（该分类将使用默认快捷方式图标）: %s", cat.id)
        return None, "", False

    def remove_all(self, categories) -> None:
        """删除所有分类的 .lnk（退出卸载类操作用；正常流程不需要）。"""
        base_dir = self._target_dir()
        if base_dir is None:
            return
        for name in self._plan(categories).values():
            self._delete_lnk(base_dir, name)

    # -- 内部实现 -----------------------------------------------------------

    def _plan(self, categories) -> dict[str, str]:
        """``cat_id → 实际落盘文件名``（launcher 模式带"启动-"前缀）。"""
        base = plan_lnk_names(categories)
        if self.mode != MODE_LAUNCHER:
            return base
        return {cat_id: launcher_file_name(name) for cat_id, name in base.items()}

    def _target_dir(self) -> Path | None:
        """当前模式对应的落点目录（launcher 模式顺手确保目录存在）。

        安全防线（v1.5 追加）：launcher 目录**绝不允许**落在用户数据根上。
        在返回目录、以及 ``mkdir`` 之前先校验，校验不过直接拒绝写入并只告警
        一次。理由见 :meth:`_forbidden_launcher_dir`。
        """
        if self.mode == MODE_LAUNCHER:
            blocked = self._forbidden_launcher_dir()
            if blocked:
                self._warn_forbidden_launcher_dir(blocked)
                return None
            try:
                self.launcher_dir.mkdir(parents=True, exist_ok=True)
            except OSError as exc:
                logger.warning("创建启动器目录失败 %s: %s", self.launcher_dir, exc)
                return None
            if not self.launcher_dir.is_dir():
                logger.warning("启动器目录不可用: %s", self.launcher_dir)
                return None
            return self.launcher_dir
        return self._resolve_desktop()

    def _dir_for_mode(self, mode: str) -> Path | None:
        """其它模式对应的落点目录（用于"换模式"时清理旧文件）。

        同样受 :meth:`_forbidden_launcher_dir` 约束：清理会**删文件**，一个错误的
        launcher_dir 在这里比在写入处更危险，因此这里也要挡（返回 None 时调用方
        的 ``_delete_own_lnk`` 会直接跳过）。
        """
        if str(mode) == MODE_LAUNCHER:
            blocked = self._forbidden_launcher_dir()
            if blocked:
                self._warn_forbidden_launcher_dir(blocked)
                return None
            return self.launcher_dir
        return self._resolve_desktop()

    @staticmethod
    def _resolve_safely(p: Path) -> Path:
        """``resolve()`` 的安全版：解析失败（不存在/无权限/环）退回绝对路径。"""
        try:
            return Path(p).resolve()
        except (OSError, RuntimeError):
            try:
                return Path(p).absolute()
            except (OSError, RuntimeError):
                return Path(p)

    def _forbidden_launcher_dir(self) -> str:
        """启动器目录是否踩线；返回拒绝原因，空串表示通过。

        为什么要有这道防线：启动器 .lnk 是"给 SAO HUD 图标引用的入口文件"，
        必须待在 ``%USERPROFILE%\\DeskCountChen\\.launchers``。一旦某个调用方把
        ``launcher_dir`` 传成用户**数据根**（例如 ``E:\\快捷方式SAO``），
        6 个 ``启动-*.lnk`` 就会直接落在用户自己的分类数据根上，混在用户目录里——
        这是"动了用户数据"，属于必须无条件拦住的事，跟调用方写没写错无关。

        拒绝条件（按顺序）：
        1. 命中白名单 ``paths.LAUNCHERS_DIR`` → 放行（正常情况下就是它）；
        2. 等于 ``paths.DATA_ROOT`` → 拒绝；
        3. 等于 ``paths.APP_DIR`` → 拒绝（会把 .lnk 撒到程序目录根）；
        4. 是 ``paths.DATA_ROOT`` 的直属子目录 → 拒绝（那是分类数据目录）。

        注意 ``DATA_ROOT`` 默认等于 ``APP_DIR``（旧配置），此时规则 2/3 同时命中，
        而规则 1 先放行，所以正常路径不会被误拦。
        """
        target = self._resolve_safely(self.launcher_dir)
        if target == self._resolve_safely(paths.LAUNCHERS_DIR):
            return ""
        data_root = self._resolve_safely(paths.DATA_ROOT)
        if target == data_root:
            return f"等于分类数据根 {paths.DATA_ROOT}"
        if target == self._resolve_safely(paths.APP_DIR):
            return f"等于程序目录 {paths.APP_DIR}"
        if target.parent == data_root:
            return f"是分类数据根 {paths.DATA_ROOT} 的直属子目录（分类数据目录）"
        return ""

    def _warn_forbidden_launcher_dir(self, reason: str) -> None:
        """拒绝写入的告警——**每个实例每种原因只报一次**，避免每 tick 刷屏。"""
        if reason in self._warned_forbidden:
            return
        self._warned_forbidden.add(reason)
        logger.warning(
            "拒绝把启动器写入 %s：%s。启动器目录必须是 %s，"
            "绝不会落在用户数据根上；本次同步已跳过启动器落点。",
            self.launcher_dir, reason, paths.LAUNCHERS_DIR,
        )

    def launcher_dir_blocked_reason(self) -> str:
        """对外只读查询：启动器目录被拦的原因（空串 = 正常）。

        供自检/诊断脚本使用，避免它们去碰私有实现。
        """
        return self._forbidden_launcher_dir()

    def _cleanup_legacy_desktop_lnks(self) -> None:
        """迁移清理：收掉历史上写在**真实桌面**的 .lnk（仅 launcher 模式）。

        安全红线（绝不能误删用户文件）：桌面同名文件必须先用 shell_resolve
        解析，确认 ``target`` 的 exe 名是 ``python.exe`` / ``pythonw.exe``
        **且** ``arguments`` 含 ``--open``（本项目的固定参数）才删除；
        解析失败、目标不符、文件不存在 → 一律跳过并记日志。

        清理后把 state 里这些记录的 ``where`` 归一为 ``launcher``：
        以后不再重复扫描桌面（桌面 .lnk 已不存在，重复扫描纯属浪费）。
        """
        if self.mode != MODE_LAUNCHER:
            return
        state = self._load_state()
        if not state:
            return
        desktop = self._resolve_desktop()
        changed = False
        for cat_id, record in list(state.items()):
            where = str(record.get("where") or "")
            if where == MODE_LAUNCHER:
                continue
            name = str(record.get("lnk") or "")
            if name and desktop is not None:
                self._delete_own_lnk(desktop, name, reason="迁移清理（桌面 → 启动器）")
            new_record = dict(record)
            new_record["where"] = MODE_LAUNCHER
            state[cat_id] = new_record
            changed = True
        if changed:
            self._save_state(state)
            logger.info("历史桌面快捷方式清理完成，state.where 已归一为 launcher")

    def _is_own_shortcut(self, path: Path) -> bool:
        """该 .lnk 是否由本项目创建（target=python(w)/产品 exe 且参数含 --open）。"""
        info = shell_resolve.read_link(path)
        if not info.ok:
            logger.info("跳过删除（无法解析快捷方式，按用户文件处理）: %s", path)
            return False
        exe_name = Path(info.target).name.casefold() if info.target else ""
        own_exes = {"python.exe", "pythonw.exe"}
        if getattr(sys, "frozen", False):
            # frozen 态我们自己的 .lnk 目标是产品 exe（如 DeskCountChen.exe）
            own_exes.add(Path(sys.executable).name.casefold())
        if exe_name not in own_exes:
            logger.info("跳过删除（目标不是本项目入口）: %s → %s", path, info.target)
            return False
        if "--open" not in info.arguments:
            logger.info("跳过删除（参数不含 --open）: %s → %s", path, info.arguments)
            return False
        return True

    def _delete_own_lnk(self, base_dir: Path | None, name: str, reason: str = "") -> bool:
        """删除"确认属于本项目"的 .lnk；不确定就一律不删。"""
        if base_dir is None or not name:
            return False
        path = Path(base_dir) / name
        try:
            if not path.is_file():
                return False
        except OSError:
            return False
        if not self._is_own_shortcut(path):
            return False
        try:
            path.unlink()
            logger.info("%s：已删除指向本项目的快捷方式 %s", reason or "清理", path)
            return True
        except OSError as exc:
            logger.warning("删除快捷方式失败 %s: %s", path, exc)
            return False

    def _resolve_desktop(self) -> Path | None:
        if self.desktop_dir is not None:
            return self.desktop_dir
        if self._auto_desktop is None:
            self._auto_desktop = shell_link.desktop_dir()
        if self._auto_desktop is None and not self._warned_no_desktop:
            logger.warning("真实桌面目录不可用，本次不创建桌面快捷方式")
            self._warned_no_desktop = True
        return self._auto_desktop

    def _image_signature(self, cat) -> str:
        """图标指纹：自定义图片路径 + mtime；无图时为"默认图标"标记。"""
        image = str(getattr(cat, "image", "") or "").strip()
        if image:
            path = Path(image)
            try:
                mtime = os.stat(path).st_mtime_ns
            except OSError:
                mtime = 0
            if mtime:
                return f"img:{os.path.normcase(str(path))}:{mtime}"
        return "default"

    def _signature(self, cat, name: str, ico_path: Path | None) -> str:
        """.lnk 内容指纹：目标/参数/工作目录/图标/名称/落点模式，变一项即重建。"""
        return "|".join((
            self.mode,
            str(self.python_exe),
            str(self.script_path),
            self._arguments(cat),
            str(self.project_root),
            str(ico_path or ""),
            name,
            str(getattr(cat, "label", "")),
        ))

    def _arguments(self, cat) -> str:
        """``"<项目根>\\main.py" --open "<分类目录名>"``（frozen 态无脚本段）。

        源码态：脚本必须出现在参数里（.lnk 的目标是解释器）；分类名可能含
        空格，始终加引号。frozen 态：目标就是产品 exe 自身，参数只有 ``--open``。
        """
        if self.script_path is None:
            return f'--open "{getattr(cat, "dir", "")}"'
        return f'"{self.script_path}" --open "{getattr(cat, "dir", "")}"'

    def _write_lnk(self, dest: Path, cat, ico_path: Path | None) -> bool:
        return shell_link.create_shortcut(
            dest=dest,
            target=str(self.python_exe),
            arguments=self._arguments(cat),
            working_dir=str(self.project_root),
            icon_path=str(ico_path) if ico_path else str(self.python_exe),
            description=f"{brand.PRODUCT_NAME} 分类：{getattr(cat, 'label', '')}",
        )

    def _delete_lnk(self, base_dir: Path, name: str) -> None:
        """删除**当前落点目录**里我们自己记录的 .lnk（名字来自 state，非用户输入）。"""
        if not name:
            return
        path = Path(base_dir) / name
        try:
            if path.exists():
                path.unlink()
                logger.info("快捷方式已删除: %s", path)
        except OSError as exc:
            logger.warning("删除快捷方式失败 %s: %s", path, exc)

    def _load_state(self) -> dict[str, dict]:
        """读取同步状态；损坏/缺失一律视为空（只影响是否重建，不影响正确性）。"""
        try:
            data = json.loads(self.state_path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except (OSError, ValueError) as exc:
            logger.warning("快捷方式状态文件损坏，按空状态处理 %s: %s", self.state_path, exc)
            return {}
        cats = data.get("categories") if isinstance(data, dict) else None
        if not isinstance(cats, dict):
            return {}
        return {str(k): v for k, v in cats.items() if isinstance(v, dict)}

    def _save_state(self, state: dict[str, dict]) -> None:
        try:
            self.state_path.parent.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            logger.warning("创建状态目录失败 %s: %s", self.state_path.parent, exc)
            return
        atomic_write_json(self.state_path, {"version": STATE_VERSION, "categories": state})
