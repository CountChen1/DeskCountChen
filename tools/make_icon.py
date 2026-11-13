"""生成产品图标 ``assets/app.ico``（多尺寸，PNG 压缩的 ICO）。

为什么需要它：exe / 快捷方式 / GitHub 仓库都需要一个固定图标。项目本来就
有一套「无外部资源依赖」的 QPainter 默认图标（``IconCache.default_icon``），
这里直接复用它渲染成 ICO，保证 **托盘图标与 exe 图标是同一张脸**，不引入第二份
美术资源。

用法（venv 内，无需真实显示器）::

    .venv/Scripts/python.exe tools/make_icon.py            # 写 assets/app.ico
    .venv/Scripts/python.exe tools/make_icon.py --check    # 只校验能否生成

ICO 结构：ICONDIR(6B) + n×ICONDIRENTRY(16B) + n 段 PNG 数据。
Vista 起 ICO 允许内嵌 PNG，因此不用手写 BMP/AND 掩码。
"""

from __future__ import annotations

import argparse
import os
import struct
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

ICON_PATH = PROJECT_ROOT / "assets" / "app.ico"
SIZES = (16, 24, 32, 48, 64, 128, 256)


def _render_pngs(sizes: tuple[int, ...]) -> list[tuple[int, bytes]]:
    """用项目的 QPainter 图标渲染各尺寸 PNG。

    注意：**不要**设 ``QT_QPA_PLATFORM=offscreen``——本机 PySide6 的 offscreen
    插件在 QApplication 构造阶段直接段错误（实测 EXIT=139）。默认 windows 平台
    可正常工作，且全程不 show() 任何窗口，不会闪窗。
    """
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtWidgets import QApplication

    app = QApplication.instance() or QApplication([])

    from app.icon_provider import IconCache

    cache = IconCache()

    out: list[tuple[int, bytes]] = []
    for px in sizes:
        pm = cache.default_icon(px)
        if pm.isNull():
            raise SystemExit(f"渲染失败：尺寸 {px} 的图标为空")
        # QBuffer 不持有 QByteArray，必须显式留引用；写成 QBuffer(QByteArray())
        # 会在第二次编码时段错误（临时对象已被回收，QBuffer 指向野内存）。
        store = QByteArray()
        buf = QBuffer(store)
        buf.open(QIODevice.OpenModeFlag.WriteOnly)
        if not pm.toImage().save(buf, "PNG"):
            raise SystemExit(f"渲染失败：尺寸 {px} 无法编码为 PNG")
        out.append((px, bytes(store)))
        buf.close()
    assert app is not None  # 保持 QApplication 活到渲染结束
    return out


def build_ico(pngs: list[tuple[int, bytes]]) -> bytes:
    """把若干 PNG 打包成单文件 ICO。"""
    count = len(pngs)
    header = struct.pack("<HHH", 0, 1, count)  # reserved, type=icon, count
    entries = bytearray()
    payload = bytearray()
    offset = 6 + 16 * count
    for px, data in pngs:
        dim = 0 if px >= 256 else px  # ICO 里 0 表示 256
        entries += struct.pack(
            "<BBBBHHII", dim, dim, 0, 0, 1, 32, len(data), offset + len(payload)
        )
        payload += data
    return bytes(header + entries + payload)


def make_icon(target: Path = ICON_PATH) -> Path:
    """生成 ICO 并写盘，返回路径。"""
    pngs = _render_pngs(SIZES)
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_ico(pngs))
    return target


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description="生成 assets/app.ico")
    ap.add_argument("--check", action="store_true", help="只试渲染，不写文件")
    ap.add_argument("--out", default=str(ICON_PATH), help="输出路径")
    args = ap.parse_args(argv)

    if args.check:
        pngs = _render_pngs(SIZES)
        print(f"渲染正常：{len(pngs)} 个尺寸，最大 {len(pngs[-1][1])} 字节")
        return 0

    path = make_icon(Path(args.out))
    print(f"已生成 {path}（{path.stat().st_size} 字节）")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
