"""v1.4 SAO 接入状态自检（只读，绝不写任何文件）。

检查目标：确认「SAO 图标做入口 + DesktopBox 面板做内容」这套链路的关键不变量
全部成立。用户任何时候都能跑它确认状态，不需要懂代码。

用法：
    .venv\\Scripts\\python.exe tools\\verify_sao_state.py
    .venv\\Scripts\\python.exe tools\\verify_sao_state.py --json

判据（全部来自 .config.json 与磁盘事实，不猜）：
 1. app.data_root 指向 E:\\快捷方式SAO（用户指定的数据根）
 2. app.shortcut_mode == 'launcher'（不再往桌面写 .lnk）
 3. strip_extensions / strip_shortcut_overlay 均为 True（名称去扩展名、图标去箭头）
 4. category_exclude 恰好挡住 快捷方式 / 桌面 / z全部 三个容器目录
 5. 分类恰好 6 个，名称集合与 SAO 的 6 个 HUD 图标一一对应
 6. 每个分类：id 唯一、.icons/ico/<id>.ico 存在、**外观参数在合法区间内**、数据目录存在
 7. 每个分类：.launchers/启动-<分类>.lnk 存在，且反解析出的 --open 参数是那个分类
 8. lnk_state.json 里 6 条记录都标 where=launcher（没有残留的桌面落点记录）
 9. .launchers 与 .icons/ico 里没有多余文件（已注销分类的残留）
 10. 真实桌面没有本程序的 .lnk

注意第 6 条只校验**合法区间**，不比对具体数值。面板宽度、圆角、颜色、居中/贴合
都是用户随时会调的东西，工具锁死它们就会"用户一改就红"——那是工具的 bug。
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from app.win32.shell_resolve import read_link  # noqa: E402
# 外观参数的合法区间只从 app.config 取，避免工具里再抄一份（抄了就会漂移）。
# ``_POSITION_MODES`` 是模块私有元组，这里按"单一事实来源"直接引用并标注。
from app.config import (  # noqa: E402
    MATERIALS,
    MIN_PANEL_H,
    MIN_PANEL_W,
    OPACITY_MAX,
    OPACITY_MIN,
    RADIUS_MAX,
    RADIUS_MIN,
    _POSITION_MODES as POSITION_MODES,
)

HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
APP_DIR = HOME / "DesktopBox"
CONFIG_PATH = APP_DIR / ".config.json"
ICO_DIR = APP_DIR / ".icons" / "ico"
STATE_PATH = APP_DIR / ".icons" / "lnk_state.json"
LAUNCHERS_DIR = APP_DIR / ".launchers"
EXPECTED_ROOT = r"E:\快捷方式SAO"
EXPECTED_DIRS = ["游戏加速", "影音娱乐", "工作学习", "社交通讯", "系统硬件", "文档下载"]
EXPECTED_EXCLUDE = ["快捷方式", "桌面", "z全部"]

# 面板尺寸上限：app.config 的 clamp_int 里是字面量 4000 / 3000（不是具名常量），
# 因此这里只能跟随复制；改 app.config 的上限时请同步改这里（下面有断言兜底：
# 若面板尺寸超出这里，自检会直接红，不会静默放过）。
MAX_PANEL_W = 4000
MAX_PANEL_H = 3000

# ``#RRGGBB``：与 app/config.py 的 `_COLOR_RE` 等价
COLOR_RE = re.compile(r"^#[0-9A-Fa-f]{6}$")

RESULTS: list[dict] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append({"name": name, "ok": bool(ok), "detail": detail})
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  → {detail}" if detail else ""))


def skin_problems(cat: dict) -> dict:
    """校验 7 个外观字段是否落在**合法区间**内（不比对具体数值）。

    为什么不比对具体值：这些参数是用户随时会调的（有人把面板拖宽、有人改成居中
    显示），本工具是给用户自检用的，锁死数值就会让用户一改就红——那是工具的 bug，
    不是配置的 bug。这里只回答"这个配置程序能不能正常吃下去"。

    Args:
        cat: ``.config.json`` 里单个分类的 dict。

    Returns:
        ``{字段: (实际值, 期望)}``；空 dict 表示 7 个字段全部合法。
    """
    bad: dict = {}

    def _int_in(key: str, lo: int, hi: int) -> None:
        if key not in cat:      # 缺失字段由 app.config 的默认值补齐，合法
            return
        value = cat.get(key)
        if not isinstance(value, int) or isinstance(value, bool) or not lo <= value <= hi:
            bad[key] = (value, f"{lo}..{hi}")

    _int_in("panel_w", MIN_PANEL_W, MAX_PANEL_W)
    _int_in("panel_h", MIN_PANEL_H, MAX_PANEL_H)
    _int_in("radius", RADIUS_MIN, RADIUS_MAX)
    _int_in("opacity", OPACITY_MIN, OPACITY_MAX)

    if "bg_color" in cat:
        bg = cat.get("bg_color")
        if not (isinstance(bg, str) and COLOR_RE.match(bg)):
            bad["bg_color"] = (bg, "#RRGGBB")
    if "position_mode" in cat and cat.get("position_mode") not in POSITION_MODES:
        bad["position_mode"] = (cat.get("position_mode"), "/".join(POSITION_MODES))
    # v1.5 新增字段：v1.4 的配置里根本没有它（实测用户当前配置就是如此），
    # 缺失时 normalize_material() 会补 "blur"，因此"缺失"是合法的。
    if "material" in cat and cat.get("material") not in MATERIALS:
        bad["material"] = (cat.get("material"), "/".join(MATERIALS))
    return bad


def real_desktop() -> Path:
    """真实桌面（支持 OneDrive 重定向）；拿不到就退回 ~/Desktop。"""
    try:
        import ctypes
        from ctypes import wintypes

        class GUID(ctypes.Structure):
            _fields_ = [("Data1", wintypes.DWORD), ("Data2", wintypes.WORD),
                        ("Data3", wintypes.WORD), ("Data4", ctypes.c_ubyte * 8)]

        fid = GUID(0xB4BFCC3A, 0xDB2C, 0x424C,
                   (ctypes.c_ubyte * 8)(0xB0, 0x29, 0x7F, 0xE9, 0x9A, 0x87, 0xC6, 0x41))
        ptr = ctypes.c_wchar_p()
        if ctypes.windll.shell32.SHGetKnownFolderPath(ctypes.byref(fid), 0, None,
                                                      ctypes.byref(ptr)) == 0:
            path = Path(ptr.value)
            ctypes.windll.ole32.CoTaskMemFree(ptr)
            return path
    except Exception:
        pass
    return HOME / "Desktop"


def main() -> int:
    ap = argparse.ArgumentParser(description="DesktopBox v1.4 SAO 接入状态自检（只读）")
    ap.add_argument("--json", action="store_true", help="以 JSON 输出结果")
    args = ap.parse_args()

    if not CONFIG_PATH.exists():
        check("配置文件存在", False, str(CONFIG_PATH))
        return _finish(args)
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    app_cfg = cfg.get("app", {})
    cats = cfg.get("categories", [])

    # 1-4 全局开关
    check("数据根 = E:\\快捷方式SAO", app_cfg.get("data_root") == EXPECTED_ROOT,
          repr(app_cfg.get("data_root")))
    check("落点模式 = launcher（不写桌面 .lnk）",
          app_cfg.get("shortcut_mode") == "launcher", repr(app_cfg.get("shortcut_mode")))
    check("名称去扩展名已开启", app_cfg.get("strip_extensions") is True,
          str(app_cfg.get("strip_extensions")))
    check("图标去箭头已开启", app_cfg.get("strip_shortcut_overlay") is True,
          str(app_cfg.get("strip_shortcut_overlay")))
    check("排除容器目录 = 快捷方式/桌面/z全部",
          app_cfg.get("category_exclude") == EXPECTED_EXCLUDE,
          str(app_cfg.get("category_exclude")))

    # 5 分类集合
    dirs = [c.get("dir", "") for c in cats]
    check("分类数 = 6", len(cats) == 6, f"实际 {len(cats)}")
    check("分类名与 6 个 HUD 图标一一对应", sorted(dirs) == sorted(EXPECTED_DIRS),
          ", ".join(dirs))
    check("分类 id 唯一", len({c.get("id") for c in cats}) == len(cats))

    # 6-7 逐分类
    for c in cats:
        name, cid = c.get("dir", "?"), c.get("id", "")
        ico = ICO_DIR / f"{cid}.ico"
        check(f"{name} · 分类 ICO 存在", ico.exists(), ico.name)
        skin_bad = skin_problems(c)
        check(f"{name} · 外观参数在合法范围内", not skin_bad,
              str(skin_bad) if skin_bad else
              f"w{c.get('panel_w')} h{c.get('panel_h')} r{c.get('radius')} "
              f"op{c.get('opacity')} {c.get('bg_color')} "
              f"{c.get('position_mode')}/{c.get('material')}")
        check(f"{name} · 数据目录存在",
              (Path(EXPECTED_ROOT) / name).is_dir(), str(Path(EXPECTED_ROOT) / name))

        lnk = LAUNCHERS_DIR / f"启动-{name}.lnk"
        exists = lnk.exists()
        check(f"{name} · 启动器 .lnk 存在", exists, lnk.name)
        if exists:
            lt = read_link(lnk)
            want_arg = f'--open "{name}"'
            check(f"{name} · 启动器指向本分类",
                  want_arg in (lt.arguments or ""),
                  (lt.arguments or "").split("main.py")[-1].strip())

    # 8 落点状态无桌面残留
    if STATE_PATH.exists():
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        entries = state.get("categories", state) if isinstance(state, dict) else {}
        if isinstance(entries, dict):
            wheres = sorted({(v or {}).get("where", "") for v in entries.values()})
            check("落点状态全部为 launcher（无桌面残留）", wheres == ["launcher"], str(wheres))
            unknown = [k for k in entries if k not in {c.get("id") for c in cats}]
            check("落点状态无已注销分类的记录", not unknown, str(unknown))
    else:
        check("落点状态文件存在", False, str(STATE_PATH))

    # 9 目录无多余文件（多余 = 已注销分类的残留）
    want_lnks = {f"启动-{n}.lnk" for n in EXPECTED_DIRS}
    got_lnks = {p.name for p in LAUNCHERS_DIR.glob("*.lnk")} if LAUNCHERS_DIR.is_dir() else set()
    check(".launchers 无多余 .lnk", got_lnks <= want_lnks, str(sorted(got_lnks - want_lnks)))

    want_icos = {f"{c.get('id')}.ico" for c in cats if c.get("id")}
    got_icos = {p.name for p in ICO_DIR.glob("*.ico")} if ICO_DIR.is_dir() else set()
    check(".icons/ico 无多余 .ico", got_icos <= want_icos, str(sorted(got_icos - want_icos)))

    # 10 桌面干净
    desk = real_desktop()
    stale = []
    if desk.is_dir():
        for p in desk.glob("*.lnk"):
            try:
                lt = read_link(p)
            except Exception:
                continue
            blob = f"{lt.target} {lt.arguments}".lower()
            if "desktopbox" in blob or "--open" in blob:
                stale.append(p.name)
    check("桌面无本程序 .lnk", not stale, str(stale))

    return _finish(args)


def _finish(args) -> int:
    failed = [r for r in RESULTS if not r["ok"]]
    if args.json:
        print(json.dumps({"total": len(RESULTS), "failed": len(failed),
                          "results": RESULTS}, ensure_ascii=False, indent=2))
    else:
        print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
        if failed:
            print("失败项：" + "; ".join(r["name"] for r in failed))
        else:
            print("SAO 接入状态全部正常")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
