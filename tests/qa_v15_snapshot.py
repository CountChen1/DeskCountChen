"""【v1.5 验收·E 项】真实数据快照 / 对拍工具（不是 unittest，防被 discover 收走）。

用途：在任何会拉起**真实进程**（``--console`` / 第二实例唤醒）的验收步骤
**之前**抓一份真实环境指纹，之后用同一函数重抓并逐字段对比，证明"一个字节
都没动"。与 ``tests/sandbox.py`` 的分工：

- ``sandbox`` 负责"跑测试时不碰真实路径"（进程内 paths 重定向）；
- 本模块负责"真实进程/真实磁盘的跑前跑后对拍"（只读，不重定向 paths）。

## 采集项（与主理人验收清单 E 一一对应）

| 键 | 含义 |
|---|---|
| ``config`` | ``%USERPROFILE%\\DesktopBox\\.config.json`` 的 sha256 / size |
| ``sao`` | ``E:\\快捷方式SAO`` 的文件数 / 总字节 / 清单摘要 |
| ``desktop`` | 真实桌面条目集合（16 项、其中 12 个 .lnk） |
| ``launchers`` | ``.launchers`` 下 6 个 .lnk 的 mtime（零重建证据） |
| ``icons`` | ``.icons/ico`` 下 6 个 .ico 的 mtime（零重建证据） |
| ``processes`` | 残留的 DesktopBox 进程（``python*w.exe main.py``） |
| ``crash`` | ``crash.log`` 是否存在 |

清单摘要（``listing_digest``）不 hash 文件内容（4GB 太重），而是对
"相对路径 + 字节数 + mtime_ns"排序后拼接再 sha256 —— 足以发现增删改，
且成本只有一次元数据遍历。

用法：

```bash
.venv\\Scripts\\python.exe tests\\qa_v15_snapshot.py save   # 抓基线 → %TEMP%\\qa_v15\\baseline.json
.venv\\Scripts\\python.exe tests\\qa_v15_snapshot.py diff   # 再抓一次并与基线逐项对比
```
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
USERPROFILE = Path(os.environ.get("USERPROFILE", str(Path.home())))
APP_DIR = USERPROFILE / "DesktopBox"
CONFIG_PATH = APP_DIR / ".config.json"
SAO_ROOT = Path(r"E:\快捷方式SAO")
DESKTOP = USERPROFILE / "Desktop"
LAUNCHERS_DIR = APP_DIR / ".launchers"
ICO_DIR = APP_DIR / ".icons" / "ico"
OUT_DIR = Path(os.environ.get("TEMP", ".")) / "qa_v15"
BASELINE = OUT_DIR / "baseline.json"

__all__ = [
    "BASELINE",
    "collect",
    "compare",
    "listing_digest",
    "main",
]


def _sha256_file(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return ""


def _walk_stats(root: Path) -> dict:
    """递归统计文件数 / 总字节 / 清单摘要（只读元数据，不读内容）。"""
    files = 0
    total = 0
    lines: list[str] = []
    if not root.is_dir():
        return {"exists": False, "files": 0, "bytes": 0, "digest": ""}
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames.sort()
        for name in sorted(filenames):
            f = Path(dirpath) / name
            try:
                st = f.stat()
            except OSError:
                continue
            files += 1
            total += st.st_size
            rel = str(f.relative_to(root)).replace("\\", "/")
            lines.append(f"{rel}\t{st.st_size}\t{st.st_mtime_ns}")
    digest = hashlib.sha256("\n".join(lines).encode("utf-8")).hexdigest()
    return {"exists": True, "files": files, "bytes": total, "digest": digest}


def listing_digest(root: Path) -> str:
    """目录清单摘要（相对路径 + 字节 + mtime_ns）。"""
    return _walk_stats(root)["digest"]


def _mtime_map(root: Path, pattern: str) -> dict:
    """``{文件名: mtime_ns}``；目录不存在时返回空 dict。"""
    out: dict[str, int] = {}
    if not root.is_dir():
        return out
    for p in sorted(root.glob(pattern)):
        try:
            out[p.name] = p.stat().st_mtime_ns
        except OSError:
            pass
    return out


def _desktop_entries() -> dict:
    names: list[str] = []
    if DESKTOP.is_dir():
        names = sorted(p.name for p in DESKTOP.iterdir())
    return {
        "count": len(names),
        "lnk_count": sum(1 for n in names if n.lower().endswith(".lnk")),
        "names_digest": hashlib.sha256("\n".join(names).encode("utf-8")).hexdigest(),
    }


def _stray_processes() -> list[str]:
    """列出残留的 DesktopBox 主进程（命令行里带 main.py 的 python*）。"""
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command",
             "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
             "Select-Object ProcessId,CommandLine | ConvertTo-Json -Compress"],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    if not out:
        return []
    try:
        data = json.loads(out)
    except json.JSONDecodeError:
        return []
    if isinstance(data, dict):
        data = [data]
    found = []
    for item in data:
        cmd = str(item.get("CommandLine") or "")
        if "main.py" in cmd and "desktopbox" in cmd.replace("\\", "/").lower():
            found.append(f"pid={item.get('ProcessId')} {cmd}")
    return found


def collect() -> dict:
    """抓取一次完整真实状态快照（只读）。"""
    sao = _walk_stats(SAO_ROOT)
    return {
        "config": {
            "path": str(CONFIG_PATH),
            "exists": CONFIG_PATH.is_file(),
            "sha256": _sha256_file(CONFIG_PATH),
            "size": CONFIG_PATH.stat().st_size if CONFIG_PATH.is_file() else 0,
        },
        "sao": {
            "exists": sao["exists"],
            "files": sao["files"],
            "bytes": sao["bytes"],
            "digest": sao["digest"],
        },
        "desktop": _desktop_entries(),
        "launchers": _mtime_map(LAUNCHERS_DIR, "*.lnk"),
        "icons": _mtime_map(ICO_DIR, "*.ico"),
        "processes": _stray_processes(),
        "crash_log": (APP_DIR / "crash.log").is_file(),
    }


def compare(before: dict, after: dict) -> list[tuple[str, bool, str]]:
    """逐字段对比两份快照，返回 ``[(名称, 是否一致, 说明)]``。"""
    results: list[tuple[str, bool, str]] = []

    def add(name: str, ok: bool, detail: str) -> None:
        results.append((name, bool(ok), detail))

    b, a = before["config"], after["config"]
    add("真实 .config.json sha256 未变（零污染）",
        b["sha256"] == a["sha256"],
        f"{b['sha256'][:12]}… -> {a['sha256'][:12]}…")

    b, a = before["sao"], after["sao"]
    add("E:\\快捷方式SAO 文件数未变",
        b["files"] == a["files"], f"{b['files']} -> {a['files']} 个文件")
    add("E:\\快捷方式SAO 总字节未变",
        b["bytes"] == a["bytes"], f"{b['bytes']} -> {a['bytes']} 字节")
    add("E:\\快捷方式SAO 清单摘要未变（无增删改）",
        b["digest"] == a["digest"],
        f"{b['digest'][:12]}… -> {a['digest'][:12]}…")

    b, a = before["desktop"], after["desktop"]
    add("桌面条目数未变",
        b["count"] == a["count"], f"{b['count']} -> {a['count']} 项")
    add("桌面 .lnk 数未变",
        b["lnk_count"] == a["lnk_count"], f"{b['lnk_count']} -> {a['lnk_count']} 个 .lnk")
    add("桌面条目集合未变（无新增/消失图标）",
        b["names_digest"] == a["names_digest"],
        f"{b['names_digest'][:12]}… -> {a['names_digest'][:12]}…")

    b, a = before["launchers"], after["launchers"]
    add(".launchers\\*.lnk 数量未变", len(b) == len(a), f"{len(b)} -> {len(a)} 个")
    add(".launchers\\*.lnk mtime 全部未变（零重建）",
        b == a,
        "全部一致" if b == a else f"变化: {[k for k in set(b) | set(a) if b.get(k) != a.get(k)]}")

    b, a = before["icons"], after["icons"]
    add(".icons\\ico\\*.ico 数量未变", len(b) == len(a), f"{len(b)} -> {len(a)} 个")
    add(".icons\\ico\\*.ico mtime 全部未变（零重建）",
        b == a,
        "全部一致" if b == a else f"变化: {[k for k in set(b) | set(a) if b.get(k) != a.get(k)]}")

    add("无残留 DesktopBox 进程", not after["processes"],
        "无" if not after["processes"] else "; ".join(after["processes"]))
    add("crash.log 不存在",
        after["crash_log"] is False,
        str(APP_DIR / "crash.log") if after["crash_log"] else "不存在")
    return results


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    mode = argv[0] if argv else "show"
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    snap = collect()
    if mode == "save":
        BASELINE.write_text(json.dumps(snap, ensure_ascii=False, indent=2), encoding="utf-8")
        print(f"基线已写入 {BASELINE}")
        print(json.dumps(snap, ensure_ascii=False, indent=2))
        return 0
    if mode == "diff":
        if not BASELINE.is_file():
            print(f"缺少基线 {BASELINE}，请先执行 save")
            return 2
        before = json.loads(BASELINE.read_text(encoding="utf-8"))
        results = compare(before, snap)
        failed = [n for n, ok, _ in results if not ok]
        for name, ok, detail in results:
            print(f"[{'PASS' if ok else 'FAIL'}] {name}  ({detail})")
        print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
        return 0 if not failed else 1
    print(json.dumps(snap, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
