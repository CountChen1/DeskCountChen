"""跨进程冒烟探针（不是 unittest，防被 discover 收走）。

v1.2 架构（桌面图标 = Windows 真实 .lnk 快捷方式）下的关键跨进程行为：

1. 第一实例：启动 7 秒后仍存活、stderr 无异常栈、app.log 出现"启动完成"；
2. 桌面快捷方式链路：用 ``main.py --open <分类目录名>`` 再起一个进程
   （= 用户双击桌面图标），它必须**静默退出（退出码 0、不弹框）**，
   并把"打开该分类面板"的请求转给第一实例（日志有证据）；
3. 结束进程后无 python.exe / pythonw.exe 残留。

注意：本探针会驱动真实产品行为，因此会在**真实桌面**上维护分类 .lnk
（这正是被测功能本身，不是测试副作用）。

用法：.venv\\Scripts\\python.exe tests\\probe_windows.py
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PYEXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
MAIN = PROJECT_ROOT / "main.py"
DATA_ROOT = Path(os.environ.get("USERPROFILE", "")) / "DeskCountChen"
LOG_FILE = DATA_ROOT / ".logs" / "app.log"
CONFIG = DATA_ROOT / ".config.json"

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def log_lines() -> list[str]:
    if not LOG_FILE.exists():
        return []
    return LOG_FILE.read_text(encoding="utf-8", errors="replace").splitlines()


def count_log(keyword: str) -> int:
    return sum(1 for line in log_lines() if keyword in line)


def first_category_dir() -> str:
    """取配置里第一个分类的目录名（用作 --open 的参数）。"""
    try:
        data = json.loads(CONFIG.read_text(encoding="utf-8"))
        for item in data.get("categories") or []:
            if isinstance(item, dict) and item.get("dir"):
                return str(item["dir"])
    except (OSError, ValueError):
        pass
    return "常用"


def launch(*args: str) -> tuple[subprocess.Popen, Path, Path]:
    out = Path(tempfile.gettempdir()) / "dbx_probe_out.log"
    err = Path(tempfile.gettempdir()) / "dbx_probe_err.log"
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)  # 必须用真实 windows 平台插件
    proc = subprocess.Popen(
        [str(PYEXE), str(MAIN), *args],
        cwd=str(PROJECT_ROOT),
        stdout=out.open("w", encoding="utf-8", errors="replace"),
        stderr=err.open("w", encoding="utf-8", errors="replace"),
        env=env,
    )
    return proc, out, err


def kill(proc: subprocess.Popen) -> int:
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait(timeout=10)
    return proc.returncode


def python_processes() -> list[str]:
    """列出仍带 main.py 命令行的 python 进程（排除本探针自身）。"""
    ps = subprocess.run(
        [
            "powershell", "-NoProfile", "-Command",
            "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" "
            "| ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }",
        ],
        capture_output=True, text=True, encoding="utf-8-sig", errors="replace",
    ).stdout
    me = os.getpid()
    leftovers = []
    for line in ps.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        pid_text, _, cmd = line.partition("\t")
        pid_num = int(pid_text) if pid_text.strip().isdigit() else -1
        if pid_num == me:
            continue
        if "main.py" in cmd.lower():
            leftovers.append(line)
    return leftovers


def main() -> int:
    print(f"解释器: {PYEXE}")
    print(f"入口: {MAIN}")
    print(f"日志: {LOG_FILE}")
    started_base = count_log("启动完成")
    wake_base = count_log("已请求已有实例打开分类")
    panel_base = count_log("面板已打开")
    print(f"基线: 启动完成 {started_base} 次，唤醒请求 {wake_base} 次，面板打开 {panel_base} 次")

    cat_dir = first_category_dir()
    print(f"唤醒目标分类目录: {cat_dir}")

    print("\n[1] 启动第一个实例…")
    proc, _, err_log = launch()
    print(f"    pid={proc.pid}，等待 7 秒")
    time.sleep(7)
    alive = proc.poll() is None
    err_text = err_log.read_text(encoding="utf-8", errors="replace")
    check("第一实例 7 秒后存活（python 启动无崩溃）", alive, f"exit={proc.poll()}")
    check("第一实例 stderr 无异常栈", not err_text.strip(), repr(err_text[:300]))
    started = count_log("启动完成")
    check("第一实例日志出现「启动完成」", started == started_base + 1,
          f"{started_base} → {started}")

    print("\n[2] 模拟双击桌面图标：main.py --open <分类目录名> …")
    proc2, _, err2 = launch("--open", cat_dir)
    try:
        code2 = proc2.wait(timeout=15)
    except subprocess.TimeoutExpired:
        code2 = None
    err2_text = err2.read_text(encoding="utf-8", errors="replace")
    print(f"    第二实例退出码={code2} stderr={repr(err2_text[:200])}")
    check("第二实例静默退出（退出码 0，不再弹「已在运行」框）", code2 == 0, str(code2))
    check("第二实例 stderr 无异常", not err2_text.strip(), repr(err2_text[:200]))

    time.sleep(2.0)
    wake = count_log("已请求已有实例打开分类")
    panel = count_log("面板已打开")
    print(f"    日志计数: 唤醒请求 {wake_base} → {wake}，面板打开 {panel_base} → {panel}")
    check("第二实例把唤醒请求发出去了", wake > wake_base, f"{wake_base} → {wake}")
    check("第一实例收到请求并打开了对应分类的面板", panel > panel_base, f"{panel_base} → {panel}")

    started_after = count_log("启动完成")
    check("第二实例没有变成第二个主实例", started_after == started, f"{started} → {started_after}")

    print("\n[3] 结束进程并检查残留…")
    rc = kill(proc)
    print(f"    第一实例退出码={rc}")
    time.sleep(1.0)
    left = python_processes()
    check("无 python.exe / pythonw.exe 残留", not left, "; ".join(left)[:300])

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("跨进程冒烟探针全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
