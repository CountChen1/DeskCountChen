"""验证"测试中途被中断也不会污染真实配置"（2026-11-12 事故的验收项之一）。

三种中断强度：
1. CTRL_BREAK（等价 Ctrl-C，Python 会抛 KeyboardInterrupt，finally 仍会执行）；
2. taskkill /F（强杀：**finally 完全不执行**，最严苛）；
3. 对比：中断前后真实 .config.json 的 sha256 必须完全一致。

用法：.venv\\Scripts\\python.exe tests\\verify_no_pollution.py
"""

from __future__ import annotations

import hashlib
import os
import signal
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

PYEXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
E2E = PROJECT_ROOT / "tests" / "regression_e2e.py"
CONFIG = Path(os.environ.get("USERPROFILE", str(Path.home()))) / "DeskCountChen" / ".config.json"


def sha256_of(path: Path) -> str:
    try:
        return hashlib.sha256(path.read_bytes()).hexdigest()
    except OSError:
        return "<缺失>"


def _decode(raw: bytes) -> str:
    """子进程输出可能是 UTF-8 也可能是 GBK（取决于 PYTHONIOENCODING 是否生效）。"""
    for encoding in ("utf-8", "gbk"):
        try:
            return raw.decode(encoding)
        except UnicodeDecodeError:
            continue
    return raw.decode("utf-8", errors="replace")


def run_case(name: str, interrupt_after: float, hard_kill: bool) -> tuple[bool, str]:
    """起 e2e → 在 interrupt_after 秒后中断 → 返回 (配置未变, 说明)。"""
    before = sha256_of(CONFIG)
    out = Path(tempfile.gettempdir()) / f"dbx_interrupt_{'kill' if hard_kill else 'ctrl'}.log"
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    # 强制子进程用 UTF-8 输出：Windows 控制台重定向默认走 GBK，读到一半就解码炸了
    env["PYTHONIOENCODING"] = "utf-8"
    # 关键：输出重定向到文件时 Python 默认块缓冲，中断/强杀会丢掉未刷盘的日志，
    # 那样就无法判断"中断时是否已进入沙箱"，所以强制行缓冲。
    env["PYTHONUNBUFFERED"] = "1"
    log = out.open("wb")             # 二进制句柄：避免父进程文本层参与子进程写字节
    try:
        proc = subprocess.Popen(
            [str(PYEXE), str(E2E)],
            cwd=str(PROJECT_ROOT),
            stdout=log,
            stderr=subprocess.STDOUT,
            env=env,
            creationflags=subprocess.CREATE_NEW_PROCESS_GROUP,
        )
        time.sleep(interrupt_after)
        mid = sha256_of(CONFIG)          # 中断瞬间（测试正在跑）
        if hard_kill:
            # capture_output 必须用字节：taskkill 的中文提示是 GBK，按 UTF-8 解码会炸
            subprocess.run(["taskkill", "/F", "/PID", str(proc.pid)],
                           capture_output=True)
        else:
            # 新进程组里 CTRL_C 被禁用，用 CTRL_BREAK（Python 同样转成 KeyboardInterrupt）
            os.kill(proc.pid, signal.CTRL_BREAK_EVENT)
        try:
            rc = proc.wait(timeout=30)
        except subprocess.TimeoutExpired:
            proc.kill()
            rc = proc.wait(timeout=10)
    finally:
        log.close()
    after = sha256_of(CONFIG)
    text = _decode(out.read_bytes())
    started = "真实配置基线" in text and "沙箱数据根" in text
    ok = (before == mid == after)
    detail = (f"before={before[:12]} 中断瞬间={mid[:12]} after={after[:12]} "
              f"rc={rc} 中断前已进入沙箱={started}")
    print(f"[{'PASS' if ok else 'FAIL'}] {name}: {detail}")
    return ok, detail


def main() -> int:
    print(f"真实配置: {CONFIG}\nsha256(开始)={sha256_of(CONFIG)}")
    results = []
    # 3.5s 时 e2e 正在跑用例 B/A（沙箱已建立、面板已开），此时中断
    results.append(run_case("Ctrl-C（CTRL_BREAK）中断 e2e", 3.5, hard_kill=False))
    # 强杀：finally 完全不执行
    results.append(run_case("taskkill /F 强杀 e2e（finally 不执行）", 3.5, hard_kill=True))
    print(f"sha256(结束)={sha256_of(CONFIG)}")

    failed = [d for ok, d in results if not ok]
    print(f"\n共 {len(results)} 项，通过 {len(results) - len(failed)} 项")
    if failed:
        return 1
    print("中断场景下真实配置零改动（不再依赖 finally 还原）")
    return 0


if __name__ == "__main__":
    sys.exit(main())
