""".order.json 的加载 / 容错 / 原子保存 / 隐藏属性。

Schema v1:
    {"version": 1, "updated": "<ISO8601 UTC>", "order": ["名字", ...]}

容错约定：
- 文件缺失 / JSON 损坏 / 结构非法 → 视为无记录（[]），绝不删除用户文件；
- order 里允许包含磁盘上已不存在的名字（stale），保存时保留在尾部。

v1.3（密集语义）：``slots`` 字段兼容读取——历史文件里的 null（空位）与
幽灵名在加载时一律丢弃（densify），保存时只写密集名单（order 与 slots
内容相同）。空位体系已在 v1.3 整体废除（用户决策：完全对齐手机桌面）。
"""

from __future__ import annotations

import json
import logging
from pathlib import Path

from app.config import atomic_write_json, iso_utc_now
from app.slot_ops import densify
from app.win32 import window_ops

logger = logging.getLogger(__name__)

ORDER_FILE_NAME = ".order.json"


class OrderStore:
    """分类目录内隐藏顺序文件 .order.json 的读写封装。"""

    def __init__(self, dir_path: Path) -> None:
        self.dir_path = Path(dir_path)
        self.order_path = self.dir_path / ORDER_FILE_NAME

    def load(self) -> list[str]:
        """加载顺序记录；任何失败都返回 []（按名排序兜底）。"""
        try:
            text = self.order_path.read_text(encoding="utf-8")
            data = json.loads(text)
            names = data["order"]
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, KeyError, TypeError) as exc:
            logger.warning(".order.json 损坏，按名排序兜底 %s: %s", self.order_path, exc)
            return []
        if not isinstance(names, list):
            logger.warning(".order.json 的 order 字段非法: %s", self.order_path)
            return []
        # 只保留字符串；不在这里去重（_merge 有防御性去重）
        return [n for n in names if isinstance(n, str)]

    def load_slots(self) -> list[str]:
        """加载顺序记录（v1.3 密集语义）；任何失败都返回 []（按名排序兜底）。

        兼容读取历史 Schema v1 的 slots 字段：null（空位）与非字符串一律
        丢弃，幽灵名同样丢弃——空位体系已废除，加载即归一化为密集名单。
        旧文件只有 order 字段 → 直接用 order。
        """
        try:
            text = self.order_path.read_text(encoding="utf-8")
            data = json.loads(text)
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError, UnicodeDecodeError, TypeError) as exc:
            logger.warning(".order.json 损坏，按名排序兜底 %s: %s", self.order_path, exc)
            return []
        slots = data.get("slots") if isinstance(data, dict) else None
        if isinstance(slots, list):
            return densify(s if isinstance(s, str) else None for s in slots)
        order = data.get("order") if isinstance(data, dict) else None
        if isinstance(order, list):
            return [n for n in order if isinstance(n, str)]
        return []

    def save_slots(self, slots: list[str | None]) -> list[str]:
        """保存顺序记录（v1.3 密集语义），返回真正落盘的名单。

        - 空位（None）一律丢弃、重复名去重（防御）、只写密集名单；
        - order 字段与 slots 字段内容相同（旧版工具兼容读取 order）；
        - 幽灵名（磁盘上已不存在的记录）不再保留占格——空位体系废除。

        Returns:
            实际写入的密集名单（与文件内容一致）。调用方用它同步内存模型。
        """
        clean: list[str] = []
        seen: set[str] = set()
        for s in slots:
            if isinstance(s, str) and s.strip():
                k = s.casefold()
                if k in seen:
                    continue
                seen.add(k)
                clean.append(s)
        clean = densify(clean)
        try:
            # 与 save() 相同的防御：写前清隐藏属性，写完设回
            window_ops.clear_file_hidden(self.order_path)
            data = {
                "version": 1,
                "updated": iso_utc_now(),
                "order": list(clean),
                "slots": list(clean),
            }
            atomic_write_json(self.order_path, data)
            window_ops.set_file_hidden(self.order_path)
        except OSError as exc:
            logger.error("保存 .order.json 失败 %s: %s", self.order_path, exc)
        return list(clean)

    def save(self, visible_names: list[str], old_order: list[str]) -> None:
        """保存"当前可视顺序 + 残留的 stale 记录"。

        stale = 旧记录中、当前可视顺序里已不存在的名字（保持原相对顺序）。
        保证"记录不无限累积"且"新顺序立即生效"。
        """
        try:
            # 实测：open(path,'w') 覆写隐藏文件会 [Errno 13]（CRT 老规矩），
            # 而 atomic_write_json 走 os.replace() 对隐藏目标本可成功；
            # 此处仍先清属性属防御性写法——避免将来换成 open('w') 等写入
            # 方式时踩坑。写完再设回隐藏。
            window_ops.clear_file_hidden(self.order_path)
            visible_keys = {v.casefold() for v in visible_names}
            stale = [n for n in old_order if isinstance(n, str) and n.casefold() not in visible_keys]
            data = {
                "version": 1,
                "updated": iso_utc_now(),
                "order": list(visible_names) + stale,
            }
            atomic_write_json(self.order_path, data)
            window_ops.set_file_hidden(self.order_path)
        except OSError as exc:
            logger.error("保存 .order.json 失败 %s: %s", self.order_path, exc)
