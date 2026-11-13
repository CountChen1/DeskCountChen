"""【v1.5 验收·F 项】真实端到端：起真进程 + 第二实例唤醒。

## 为什么用隔离的 ``USERPROFILE``

v1.5 给每个分类新增了 ``material`` 字段，**任何一次真实启动都会把配置重写成
带 material 的版本**（``AppConfig.load → save`` 实测 sha256 从 ``fc1ca55b…``
变成 ``61117c40…``）。若直接用真实 ``%USERPROFILE%\\DeskCountChen``，验收就必然
改写用户配置，违背「数据零污染」红线。

因此本脚本：

1. 只**读**真实配置，把它复制到临时 ``USERPROFILE\\DeskCountChen\\.config.json``
   （``app.data_root`` 仍指向真实 ``E:\\快捷方式SAO``）；
2. 用 ``USERPROFILE=<临时目录>`` 起真进程 → ``paths.APP_DIR`` 落在临时目录，
   ``.icons / .ico / .launchers / .logs`` 全部在临时目录重建；
3. 分类数据根仍是真实的 ``E:\\快捷方式SAO``，所以**分类数 / 条目数 / 名称**
   都是真实数据；且只读（打开面板不写 ``.order.json``，已核对
   ``FolderModel.reload`` 只读）。

## 判据（与验收清单 F 对应）

- 日志确认「数据根 = E:\\快捷方式SAO」与「6 个分类」；
- 启动无异常、无 crash.log；
- 第二实例 ``--open "游戏加速"`` → 第一实例日志出现「面板已打开: 游戏加速（27 条目）」；
- 结束后两个进程都退出，无残留。

用法：``.venv\\Scripts\\python.exe tests\\qa_v15_e2e.py``
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
PYTHON = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
REAL_HOME = Path(os.environ.get("USERPROFILE", str(Path.home())))
REAL_CONFIG = REAL_HOME / "DeskCountChen" / ".config.json"
OUT_DIR = Path(os.environ.get("TEMP", ".")) / "qa_v15"
WORK = OUT_DIR / "e2e_home"          # 隔离的 USERPROFILE
EXPECTED_ROOT = r"E:\快捷方式SAO"
WAKE_DIR = "游戏加速"
EXPECTED_ITEMS = 27
STARTUP_TIMEOUT_S = 40.0

RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def info(msg: str) -> None:
    print(f"[INFO] {msg}")


def prepare_home() -> Path:
    """建隔离 USERPROFILE，并把真实配置复制进去。"""
    app_dir = WORK / "DeskCountChen"
    if WORK.exists():
        shutil.rmtree(WORK, ignore_errors=True)
    (app_dir / ".logs").mkdir(parents=True, exist_ok=True)
    raw = REAL_CONFIG.read_bytes()
    (app_dir / ".config.json").write_bytes(raw)
    cfg = json.loads(raw.decode("utf-8"))
    info(f"隔离 USERPROFILE = {WORK}")
    info(f"复制真实配置 {len(raw)} 字节；data_root={cfg['app'].get('data_root')!r}；"
         f"{len(cfg.get('categories', []))} 个分类")
    return WORK


def kill_stale() -> list[str]:
    """杀掉本项目遗留的 pythonw 主进程（它们在跑 v1.4 旧代码并持有单实例互斥量）。"""
    killed: list[str] = []
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        "Where-Object { $_.CommandLine -like '*desktopbox*main.py*' } | "
        "ForEach-Object { $_.ProcessId }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        ).stdout
    except (OSError, subprocess.SubprocessError):
        return killed
    for line in out.split():
        pid = line.strip()
        if not pid.isdigit():
            continue
        subprocess.run(["taskkill", "/PID", pid, "/F"],
                       capture_output=True, text=True, timeout=30)
        killed.append(pid)
    if killed:
        info(f"清理遗留进程（工程师上一轮 e2e 残留、跑 v1.4 旧代码）: {killed}")
    return killed


def read_log(path: Path) -> list[str]:
    try:
        return path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return []


def wait_for(log: Path, needle: str, timeout: float) -> tuple[bool, list[str]]:
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        lines = read_log(log)
        if any(needle in line for line in lines):
            return True, lines
        time.sleep(0.4)
    return False, read_log(log)


def main() -> int:
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    if not REAL_CONFIG.is_file():
        print(f"真实配置不存在：{REAL_CONFIG}")
        return 2
    kill_stale()
    home = prepare_home()
    app_dir = home / "DeskCountChen"
    log_file = app_dir / ".logs" / "app.log"
    crash_file = app_dir / ".logs" / "crash.log"

    env = dict(os.environ)
    env["USERPROFILE"] = str(home)
    env["DESKCOUNTCHEN_DEBUG"] = "0"
    env.pop("QT_QPA_PLATFORM", None)

    stdout_path = OUT_DIR / "e2e_first_stdout.log"
    first = None
    second = None
    try:
        # ---- 第一实例（带控制台真实运行）----
        with open(stdout_path, "wb") as out:
            first = subprocess.Popen(
                [str(PYTHON), str(PROJECT_ROOT / "main.py"), "--console"],
                cwd=str(PROJECT_ROOT), env=env,
                stdout=out, stderr=subprocess.STDOUT,
            )
        info(f"第一实例 pid={first.pid}，等待启动（最多 {STARTUP_TIMEOUT_S:.0f}s）")
        ok_started, lines = wait_for(log_file, "DeskCountChen 启动完成", STARTUP_TIMEOUT_S)
        check("F1 第一实例在超时内完成启动（日志出现「启动完成」）", ok_started,
              f"{len(lines)} 行日志")
        text = "\n".join(lines)
        check(f"F2 日志确认数据根 = {EXPECTED_ROOT}",
              f"数据根 = {EXPECTED_ROOT}" in text,
              next((ln for ln in lines if "数据根 =" in ln), "未找到"))
        check("F3 日志确认 6 个分类",
              "启动完成：6 个分类" in text,
              next((ln for ln in lines if "启动完成" in ln), "未找到"))
        bad = [ln for ln in lines
               if (" ERROR " in ln or " CRITICAL " in ln
                   or "未捕获异常" in ln or "Traceback" in ln)]
        check("F4 启动期间无 ERROR/CRITICAL/未捕获异常", not bad,
              "; ".join(bad[:3]) if bad else f"{len(lines)} 行日志全为 INFO")
        check("F5 未发生硬崩溃（.logs\\crash.log 为空）",
              crash_file.is_file() and crash_file.stat().st_size == 0,
              f"{crash_file} size="
              f"{crash_file.stat().st_size if crash_file.is_file() else 'N/A'}"
              "（faulthandler 会无条件创建该文件，判据只能是「内容为空」）")
        check("F6 第一实例进程存活", first.poll() is None,
              f"returncode={first.poll()}")

        # ---- 第二实例：--open 唤醒 ----
        second = subprocess.Popen(
            [str(PYTHON), str(PROJECT_ROOT / "main.py"), "--open", WAKE_DIR],
            cwd=str(PROJECT_ROOT), env=env,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        )
        info(f"第二实例 pid={second.pid}（--open {WAKE_DIR}），等待唤醒")
        ok_wake, lines = wait_for(
            log_file, f"面板已打开: {WAKE_DIR}", 25.0)
        check(f"F7 第二实例唤醒成功（日志出现「面板已打开: {WAKE_DIR}」）", ok_wake,
              next((ln for ln in lines if "面板已打开" in ln), "未找到"))
        opened = [ln for ln in lines if f"面板已打开: {WAKE_DIR}" in ln]
        check(f"F8 面板条目数 = {EXPECTED_ITEMS}", bool(opened)
              and f"（{EXPECTED_ITEMS} 条目）" in opened[-1],
              opened[-1].split("INFO ")[-1] if opened else "无")
        check("F9 第二实例静默退出（不弹「已在运行」框）",
              _wait_exit(second, 15.0) is not None,
              f"returncode={second.poll()}")
        check("F10 第一实例仍存活（被唤醒后没退出）", first.poll() is None,
              f"returncode={first.poll()}")
        # 唤醒链路取证：单实例服务的两行日志
        tail = "\n".join(read_log(log_file)[-12:])
        info("日志尾部：\n" + "\n".join(f"    {ln}" for ln in read_log(log_file)[-8:]))
        check("F11 唤醒链路日志完整（已有实例 → 已请求 → 面板已打开）",
              "已有 DeskCountChen 实例在运行" in tail
              and "已请求已有实例打开分类" in tail,
              "三段日志齐备" if "已请求已有实例打开分类" in tail else "缺环节")
    finally:
        # ---- 收尾：杀干净 ----
        for name, proc in (("第一实例", first), ("第二实例", second)):
            if proc is not None and proc.poll() is None:
                proc.terminate()
                try:
                    proc.wait(8)
                except subprocess.TimeoutExpired:
                    proc.kill()
                info(f"{name} 已终止（returncode={proc.poll()}）")
        time.sleep(1.5)
        leftover = _leftover()
        check("F12 结束后无残留 DeskCountChen 进程", not leftover,
              "; ".join(leftover) if leftover else "无")

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项，失败 {len(failed)} 项")
    if failed:
        print("失败项：" + "; ".join(failed))
    print(f"隔离目录（未删除，便于取证）：{WORK}")
    return 0 if not failed else 1


def _wait_exit(proc, timeout: float):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if proc.poll() is not None:
            return proc.returncode
        time.sleep(0.2)
    return None


def _leftover() -> list[str]:
    script = (
        "Get-CimInstance Win32_Process -Filter \"Name like 'python%'\" | "
        "Where-Object { $_.CommandLine -like '*desktopbox*main.py*' } | "
        "ForEach-Object { \"$($_.ProcessId) $($_.CommandLine)\" }"
    )
    try:
        out = subprocess.run(
            ["powershell", "-NoProfile", "-NonInteractive", "-Command", script],
            capture_output=True, text=True, timeout=60,
        ).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return []
    return [ln for ln in out.splitlines() if ln.strip()]


if __name__ == "__main__":
    sys.exit(main())
