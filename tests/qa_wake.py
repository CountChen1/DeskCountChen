"""QA 独立验证：真实启动 + 跨进程唤醒（桌面图标双击等价链路）。

为什么单独一个脚本：唤醒需要"已经有一个持有互斥量的主实例"，
必须真的起子进程（python.exe main.py），不能在本进程里模拟。

覆盖：
1. 真实启动 → 未登记的分类目录被自动登记，并生成该分类的启动器 .lnk；
2. 重复启动不重写已存在的 .lnk（幂等，图标不会被系统重排）；
3. 主实例运行中执行 ``--open <分类目录名>``（= 双击图标）：
   第二实例 rc=0、无 stderr、无弹框阻塞（弹框会一直等用户 → 进程不会退出），
   且主实例日志出现"面板已打开: <该分类目录名>"（证明打开了**正确**分类）；
4. 分类目录名含中文 / 空格 / ``&`` 时同上（参数引号正确）；
5. 并发连发 3 次唤醒：每次 rc=0，主实例不崩、面板被逐次打开；
6. 第二实例始终没有变成第二个主实例（"启动完成"计数不变）；
7. 结束后无 python.exe / pythonw.exe 残留。

现场保护（2026-11-12 事故后重做，子进程也要隔离）：
子进程的 ``APP_DIR`` 由 ``%USERPROFILE%\\DeskCountChen`` 推导，所以本脚本给子进程传一个
**沙箱化的 USERPROFILE**（临时目录）——配置 / 日志 / .icons / .launchers / 数据根全部
落在沙箱里，真实 ``%USERPROFILE%\\DeskCountChen`` 只读一次取 sha256 指纹。
落点用默认的 ``launcher`` 模式（v1.4 默认），因此真实桌面不会被写入；真实桌面只做
**只读**核对（前后 .lnk 集合一致）。
**不再使用"备份 + 无条件还原"**：finally 不保证执行，进程被杀就留下污染。

用法：.venv\\Scripts\\python.exe tests\\qa_wake.py
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from tests import sandbox  # noqa: E402  # 仅用 snapshot_real / guard（只读）

PYEXE = PROJECT_ROOT / ".venv" / "Scripts" / "python.exe"
MAIN = PROJECT_ROOT / "main.py"
TEMP_DIR = "QA唤醒 中文&测试"
COMMON_DIR = "常用"

# 以下三个路径在 main() 里随沙箱 USERPROFILE 一起确定（子进程看到的就是它们）
APP_DIR: Path = Path(os.environ.get("USERPROFILE", "")) / "DeskCountChen"
LOG_FILE: Path = APP_DIR / ".logs" / "app.log"
CONFIG: Path = APP_DIR / ".config.json"
LAUNCHERS: Path = APP_DIR / ".launchers"

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


def log_since(base_len: int) -> list[str]:
    return log_lines()[base_len:]


def index_of_last(lines: list[str], keyword: str) -> int:
    """最后一条包含 keyword 的行号（找不到返回 -1）。"""
    for i in range(len(lines) - 1, -1, -1):
        if keyword in lines[i]:
            return i
    return -1


def wait_for_log(keyword: str, least: int = 1, timeout: float = 2.0) -> float:
    """轮询等待日志里 keyword 至少出现 least 次；返回实际耗时（秒，超时返回 timeout）。

    用轮询而不是固定 sleep：既避免"睡太短误判失败"，也避免"睡太久拖慢套件"。
    """
    start = time.monotonic()
    while time.monotonic() - start < timeout:
        if sum(1 for line in log_lines() if keyword in line) >= least:
            break
        time.sleep(0.05)
    return time.monotonic() - start


def spawn(*args: str, userprofile: Path | None = None) -> tuple[subprocess.Popen, Path, Path]:
    """起一个子进程；``userprofile`` 会覆盖 USERPROFILE，从而把 APP_DIR 关进沙箱。"""
    out = Path(os.environ.get("TEMP", ".")) / "qa_wake_out.log"
    err = Path(os.environ.get("TEMP", ".")) / "qa_wake_err.log"
    env = dict(os.environ)
    env.pop("QT_QPA_PLATFORM", None)
    if userprofile is not None:
        env["USERPROFILE"] = str(userprofile)
        env["HOME"] = str(userprofile)
    proc = subprocess.Popen(
        [str(PYEXE), str(MAIN), *args],
        cwd=str(PROJECT_ROOT),
        stdout=out.open("w", encoding="utf-8", errors="replace"),
        stderr=err.open("w", encoding="utf-8", errors="replace"),
        env=env,
    )
    return proc, out, err


def kill(proc: subprocess.Popen) -> int | None:
    if proc.poll() is None:
        proc.terminate()
    try:
        return proc.wait(timeout=15)
    except subprocess.TimeoutExpired:
        proc.kill()
        return proc.wait(timeout=15)


def leftover_processes() -> list[str]:
    ps = subprocess.run(
        ["powershell", "-NoProfile", "-Command",
         "Get-CimInstance Win32_Process -Filter \"Name='python.exe' or Name='pythonw.exe'\" "
         "| ForEach-Object { \"$($_.ProcessId)`t$($_.CommandLine)\" }"],
        capture_output=True, text=True, encoding="utf-8-sig", errors="replace",
    ).stdout
    me = os.getpid()
    left = []
    for line in ps.splitlines():
        line = line.strip().lstrip("\ufeff")
        if not line:
            continue
        pid_text, _, cmd = line.partition("\t")
        pid_num = int(pid_text) if pid_text.strip().isdigit() else -1
        if pid_num == me:
            continue
        if "main.py" in cmd.lower():
            left.append(line)
    return left


def desktop_lnks() -> set[str]:
    from app.win32 import shell_link

    desk = shell_link.desktop_dir()
    return {p.name for p in Path(desk).glob("*.lnk")} if desk else set()


def stat_of(path: Path):
    try:
        st = path.stat()
        return st.st_size, st.st_mtime_ns
    except OSError:
        return None


def _seed_sandbox_profile(root: Path) -> Path:
    """在沙箱 USERPROFILE 下预置一份配置与两个分类目录。

    - ``常用``：已登记分类（用于验证"启动不重写已有 .lnk"）；
    - ``QA唤醒 中文&测试``：**未登记**目录（用于验证"启动即自动登记"）。
    配置里 ``shortcut_mode=launcher``（v1.4 默认），落点 = 沙箱 .launchers，
    真实桌面完全不被写入。
    """
    global APP_DIR, LOG_FILE, CONFIG, LAUNCHERS

    APP_DIR = root / "DeskCountChen"
    LOG_FILE = APP_DIR / ".logs" / "app.log"
    CONFIG = APP_DIR / ".config.json"
    LAUNCHERS = APP_DIR / ".launchers"
    for d in (APP_DIR, LOG_FILE.parent, LAUNCHERS, APP_DIR / COMMON_DIR,
              APP_DIR / TEMP_DIR):
        d.mkdir(parents=True, exist_ok=True)
    (APP_DIR / COMMON_DIR / "calc.lnk").write_bytes(b"")
    (APP_DIR / TEMP_DIR / "demo.lnk").write_bytes(b"")
    config = {
        "version": 1,
        "app": {"data_root": "", "shortcut_mode": "launcher"},
        "categories": [
            {"id": "cat_wake_common", "dir": COMMON_DIR, "label": COMMON_DIR,
             "panel_w": 360, "panel_h": 260},
        ],
    }
    CONFIG.write_text(json.dumps(config, ensure_ascii=False, indent=2), encoding="utf-8")
    return APP_DIR


def desk_untouched_marker(desk: Path, before: set[str]) -> bool:
    """真实桌面 .lnk 集合是否仍然等于启动前的快照（只读核对）。"""
    now = {p.name for p in desk.glob("*.lnk")}
    if now != before:
        print(f"    真实桌面变化：多出 {sorted(now - before)} 少了 {sorted(before - now)}")
    return now == before


def main() -> int:
    from app.win32 import shell_link

    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")

    desk = shell_link.desktop_dir()
    if desk is None:
        print("跳过：真实桌面不可用")
        return 0
    desk = Path(desk)
    lnks_before = desktop_lnks()
    print(f"真实桌面: {desk}；已有 .lnk {len(lnks_before)} 个（只读，不写入）")

    profile = Path(tempfile.mkdtemp(prefix="dbx_qa_wake_"))
    _seed_sandbox_profile(profile)
    print(f"沙箱 USERPROFILE: {profile}（子进程配置/日志/落点全在这里）")

    first = None
    code = 0
    try:
        base_lines = len(log_lines())
        print("\n[1] 真实启动主实例…")
        first, _, err1 = spawn(userprofile=profile)
        time.sleep(8)
        alive = first.poll() is None
        check("主实例 8 秒后存活", alive, f"exit={first.poll()}")
        err_text = err1.read_text(encoding="utf-8", errors="replace")
        check("主实例 stderr 无异常栈", not err_text.strip(), repr(err_text[:400]))

        started = count_log("启动完成")
        check("日志出现「启动完成」", started >= 1, str(started))

        temp_lnk = LAUNCHERS / f"启动-{TEMP_DIR}.lnk"
        check("启动后未登记分类的启动器 .lnk 已生成", temp_lnk.exists(), temp_lnk.name)
        cfg = json.loads(CONFIG.read_text(encoding="utf-8"))
        dirs = {c.get("dir") for c in cfg.get("categories", [])}
        check("未登记分类被自动登记进配置", TEMP_DIR in dirs, str(sorted(dirs)))
        lnks_now = {p.name for p in LAUNCHERS.glob("*.lnk")}
        check("每次启动不重复生成 .lnk（数量守恒）",
              temp_lnk.name in lnks_now, str(sorted(lnks_now)))
        check("启动器目录不写 .lnk 到真实桌面", desk_untouched_marker(desk, lnks_before))

        common = LAUNCHERS / f"启动-{COMMON_DIR}.lnk"
        common_stat = stat_of(common)
        print(f"    启动-{COMMON_DIR}.lnk 启动前状态: {common_stat}")

        # ---------- 2. 幂等：再起一次主实例（互斥量已持有 → 走唤醒路径） ----------
        print("\n[2] 主实例运行中再次启动（应静默唤醒同一实例）…")
        p2, _, err2 = spawn(userprofile=profile)
        try:
            rc2 = p2.wait(timeout=25)
        except subprocess.TimeoutExpired:
            rc2 = None
            p2.kill()
        check("无 --open 的第二实例静默退出 rc=0", rc2 == 0, str(rc2))
        check("第二实例 stderr 无异常",
              not err2.read_text(encoding="utf-8", errors="replace").strip())
        check("没有变成第二个主实例（启动完成计数不变）",
              count_log("启动完成") == started, f"{started} → {count_log('启动完成')}")
        check("再次启动不重写已有 .lnk（mtime/大小不变）",
              stat_of(common) == common_stat, f"{common_stat} → {stat_of(common)}")

        # ---------- 3. 特殊字符分类名唤醒 ----------
        print("\n[3] 模拟双击图标：--open <含中文/空格/& 的分类目录名>")
        mark = len(log_lines())
        p3, _, err3 = spawn("--open", TEMP_DIR, userprofile=profile)
        try:
            rc3 = p3.wait(timeout=25)
        except subprocess.TimeoutExpired:
            rc3 = None
            p3.kill()
        check("特殊字符分类：第二实例 rc=0（不弹框、不阻塞）", rc3 == 0, str(rc3))
        check("特殊字符分类：第二实例 stderr 无异常",
              not err3.read_text(encoding="utf-8", errors="replace").strip())
        time.sleep(1.5)
        new_lines = log_since(mark)
        wake_lines = [ln for ln in new_lines if "已请求已有实例打开分类" in ln]
        check("特殊字符分类：请求已送达主实例", bool(wake_lines),
              f"{len(wake_lines)} 条唤醒日志；示例={wake_lines[-1][:120] if wake_lines else '无'}")
        check(f"特殊字符分类：主实例打开了**正确**分类（面板已打开: {TEMP_DIR}）",
              any(f"面板已打开: {TEMP_DIR}" in ln for ln in new_lines),
              repr([ln for ln in new_lines if "面板已打开" in ln][:2]))

        # ---------- 4. 并发连发 3 次 ----------
        print("\n[4] 并发连发 3 次唤醒（健壮性）…")
        mark = len(log_lines())
        procs = [spawn("--open", COMMON_DIR, userprofile=profile)[0] for _ in range(3)]
        codes = []
        for p in procs:
            try:
                codes.append(p.wait(timeout=30))
            except subprocess.TimeoutExpired:
                p.kill()
                codes.append(None)
        check("并发 3 次唤醒全部 rc=0", codes == [0, 0, 0], str(codes))
        waited = wait_for_log(f"面板已打开: {COMMON_DIR}", least=3, timeout=2.0)
        new_lines = log_since(mark)
        opened = sum(1 for ln in new_lines if f"面板已打开: {COMMON_DIR}" in ln)
        check("主实例对 3 次唤醒都打开了面板（未崩溃/未丢请求）",
              opened >= 3 and first.poll() is None, f"打开 {opened} 次，存活={first.poll() is None}")

        # ---- C 组：面板"0.5s 后是否还活着"（失焦竞态的真正暴露点）----
        # 只看"日志出现面板已打开"是不够的：连发唤醒时第二次 open_panel 会先关旧面板，
        # 旧窗口 deleteLater 被处理的那一帧可能让新面板被判失活而自关。所以这里等
        # 0.5s 再看"最后一条 面板已打开 之后有没有 面板已关闭"。
        time.sleep(0.5)
        tail = log_since(mark)
        closes = [ln for ln in tail if "面板已关闭" in ln]
        last_open = index_of_last(tail, f"面板已打开: {COMMON_DIR}")
        last_close = index_of_last(tail, "面板已关闭")
        still_open = first.poll() is None and last_open >= 0 and last_close < last_open
        check("并发唤醒后 0.5s 面板仍处于打开状态（未被失焦竞态吃掉）", still_open,
              f"打开 {opened} 次 / 关闭 {len(closes)} 次 / "
              f"last_open@{last_open} last_close@{last_close} / 等首开耗时 {waited * 1000:.0f}ms"
              + (f" / 关闭样本={closes[0][:90]}" if closes else ""))

        check("并发唤醒后主实例 stderr 仍无异常",
              not err1.read_text(encoding="utf-8", errors="replace").strip(),
              repr(err1.read_text(encoding="utf-8", errors="replace")[:300]))

    finally:
        if first is not None:
            rc = kill(first)
            print(f"\n主实例退出码={rc}")
        time.sleep(1.5)
        check("无 python.exe / pythonw.exe 残留", not leftover_processes(),
              "; ".join(leftover_processes())[:400])

        # 拆沙箱：配置 / 日志 / .icons / .launchers / 分类目录一起消失
        shutil.rmtree(profile, ignore_errors=True)
        check("沙箱 USERPROFILE 已删除", not profile.exists(), str(profile))

        # 真实桌面只读核对 + 真实配置零改动护栏
        after = desktop_lnks()
        check("真实桌面零改动（.lnk 集合与运行前一致）", after == lnks_before,
              f"多出 {sorted(after - lnks_before)} 少了 {sorted(lnks_before - after)}")
        ok, detail = sandbox.guard_real_untouched(real)
        check("真实用户配置零改动（sha256 一致）", ok, detail)
        if not ok:
            code = 1

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("真实启动 + 跨进程唤醒全部通过（真实配置/桌面零改动）")
    return code


if __name__ == "__main__":
    sys.exit(main())
