"""端到端回归（不是 unittest，防被 discover 收走）。

用例 A：真实分类目录 + 真实链路（PanelGrid._apply_slot_move → controller 200ms
        去抖 → FolderModel.save_slots），验证 v1.3 的"密集插入"语义（完全对齐
        手机桌面文件夹，空位体系已废除）：
        ①向前拖到占用图标 = 插入让位，原位置由后面图标自动补齐
        ②向后拖同理 ③历史脏数据（空位/幽灵）加载即自愈、落盘后文件为密集形态
        ④批量前移/后移落点正确 ⑤拖到网格外 = 追加末尾 ⑥不连续多选整块搬移。
用例 B：真实 .config.json 加载路径（AppConfig.load），混合新旧配置：分类自带的
        radius=0 / opacity=0 必须赢，缺字段的分类才拿迁移值；并用真实面板渲染验证
        "opacity=0 只剩内容"、"未用 setWindowOpacity"。
用例 C：桌面快捷方式（v1.2 新增）——启动后每个可见分类恰有一个 .lnk + .ico，
        隐藏分类后 .lnk 消失。为了不动用户真实桌面，把 manager 的 desktop_dir
        指向临时目录。

安全措施（2026-11-12 事故后重做，**不再依赖"备份 + 还原"**）：
    本脚本从一开始就把 ``app.paths`` 整个命名空间重定向到临时沙箱目录
    （见 ``tests/sandbox.py``），因此配置、分类目录、``.ico``、``.lnk`` 全部写在
    沙箱里，真实 ``%USERPROFILE%\\DeskCountChen`` 只被**读取一次**做 sha256 指纹。
    结束时用该指纹断言"真实配置一个字节都没动"。
    这样 Ctrl-C、超时被杀、未捕获异常都不会留下污染 —— 因为压根没写过真实路径。
    旧实现是"跑前备份到 %TEMP% + finally 无条件写回"，那不是修复：finally 不保证
    执行，备份时机也可能抓到错的字节态（真实覆盖过用户的 6 个 SAO 分类）。

用法：.venv\\Scripts\\python.exe tests\\regression_e2e.py
"""

from __future__ import annotations

import json
import os
import shutil
import sys
import tempfile
import time
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))
os.environ.pop("QT_QPA_PLATFORM", None)

from PySide6.QtCore import Qt  # noqa: E402
from PySide6.QtGui import QImage  # noqa: E402
from PySide6.QtWidgets import QApplication  # noqa: E402

from app import paths  # noqa: E402
from tests import sandbox  # noqa: E402  # 必须在任何 paths.* 使用之前完成重定向
from app.controller import AppController  # noqa: E402
from app.panel_model import HOLE_ROLE, NAME_ROLE  # noqa: E402
from app.win32 import window_ops  # noqa: E402

RESULTS: list[tuple[str, bool, str]] = []
CAT_DIR = "QA_验证分类"
CAT_FILL = "QA_迁移填充分类"
FIXTURE_NAMES = ("a.lnk", "b.exe", "c.lnk", "d.exe", "e.lnk")

# 沙箱根（由 main() 在 QApplication 之前赋值）；此处仅为类型占位。
SANDBOX_ROOT: Path | None = None


def check(name: str, ok: bool, detail: str = "") -> None:
    RESULTS.append((name, bool(ok), detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  ({detail})" if detail else ""))


def pump(app, seconds: float) -> None:
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        app.processEvents()
        time.sleep(0.02)


def read_order_file(dir_path: Path) -> dict:
    order_path = dir_path / ".order.json"
    window_ops.clear_file_hidden(order_path)  # 隐藏属性会让 open('w') 被拒，读之前先清
    return json.loads(order_path.read_text(encoding="utf-8"))


def read_order(dir_path: Path) -> list[str]:
    return read_order_file(dir_path)["order"]


def _ico_entry_count(ico: Path) -> int:
    """读 ICO 头里的图像条目数（非法/缺失返回 0）。"""
    try:
        data = Path(ico).read_bytes()
    except OSError:
        return 0
    if len(data) < 6 or data[:4] != b"\x00\x00\x01\x00":
        return 0
    return int.from_bytes(data[4:6], "little")


def read_slots(dir_path: Path) -> list[str | None]:
    return read_order_file(dir_path)["slots"]


def render_alpha(widget) -> QImage:
    img = QImage(widget.size(), QImage.Format.Format_ARGB32_Premultiplied)
    img.fill(Qt.GlobalColor.transparent)
    widget.render(img)
    return img


def cleanup_dirs() -> None:
    for name in (CAT_DIR, CAT_FILL):
        shutil.rmtree(paths.ROOT / name, ignore_errors=True)


def prepare_fixtures(fake_desktop: Path) -> Path:
    """造分类目录 + 带 ghost 的 .order.json + 混合新旧配置。返回用例分类目录。"""
    cleanup_dirs()
    paths.ensure_base_dirs()
    fake_desktop.mkdir(parents=True, exist_ok=True)
    case_dir = paths.ROOT / CAT_DIR
    case_dir.mkdir(parents=True, exist_ok=True)
    for name in FIXTURE_NAMES:
        (case_dir / name).write_bytes(b"")
    write_slots(case_dir, ["a.lnk", "b.exe", "c.lnk", "d.exe", "e.lnk", "ghost.lnk"])
    (paths.ROOT / CAT_FILL).mkdir(parents=True, exist_ok=True)

    mixed = {
        "version": 1,
        "app": {
            "radius": 24, "opacity": 50, "bg_color": "#123456", "position_mode": "center",
            "drag_threshold_px": 4, "edit_press_ms": 400,
        },
        "categories": [
            {"id": "cat_qa_a", "dir": CAT_DIR, "label": "QA验证", "radius": 0, "opacity": 0,
             "panel_w": 360, "panel_h": 260},
            {"id": "cat_qa_b", "dir": CAT_FILL, "label": "QA填充"},
        ],
    }
    # 注意：这里的 CONFIG_PATH 已被 sandbox_paths() 重定向到临时沙箱，
    # 写的是夹具配置而不是用户配置（main() 里先快照再沙箱，顺序不能颠倒）。
    paths.CONFIG_PATH.write_text(json.dumps(mixed, ensure_ascii=False, indent=2), encoding="utf-8")
    return case_dir


def write_slots(case_dir: Path, slots: list[str | None]) -> None:
    """把槽位记录直接写进 .order.json（模拟"用户上次拖拽后的状态"）。"""
    order_path = case_dir / ".order.json"
    window_ops.clear_file_hidden(order_path)
    order_path.write_text(
        json.dumps(
            {
                "version": 1,
                "order": [s for s in slots if s],
                "slots": slots,
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )


def visible_names(grid) -> list[str]:
    """面板上真正显示的条目名（排除空位占位行）。"""
    m = grid.panel_model.model
    return [
        str(m.item(r).data(NAME_ROLE))
        for r in range(m.rowCount())
        if not m.item(r).data(HOLE_ROLE)
    ]


class ScenarioRunner:
    """把"改 .order.json → 重开面板 → 拖拽 → 读回"这一段重复流程收拢。"""

    def __init__(self, app, controller, case_dir: Path) -> None:
        self.app = app
        self.controller = controller
        self.case_dir = case_dir

    def reopen(self, slots: list[str | None]):
        """重写 .order.json 后重开面板，返回面板里的 grid。

        注意（真实 windows 插件下的已知竞态，与数据根/沙箱无关）：
        ``PanelWindow`` 收到 ``WindowDeactivate`` 后 120ms 会自关（失焦关闭是产品
        需求）。自动化里"关掉旧面板 → 立刻开新面板"时，旧窗口的 ``deleteLater``
        被处理的那一帧会让 Windows 短暂切换活动窗口，新面板可能因此被判定为
        失活而自关。这里先用更长的 settle 等旧窗口真正销毁，失败再重试（最多 3 次），
        重试次数会打印出来，便于发现退化。
        """
        write_slots(self.case_dir, slots)
        self.controller.close_panel()
        pump(self.app, 0.5)          # 等旧面板 deleteLater 真正销毁、激活状态稳定
        panel = None
        for attempt in range(1, 4):
            self.controller.open_panel("cat_qa_a")
            # 轮询等待（≤2s）：不能"打开后固定等 0.8s 再判"，那会把"面板被抢焦点
            # 自关"的瞬间误判成打不开。
            deadline = time.monotonic() + 2.0
            while time.monotonic() < deadline:
                pump(self.app, 0.05)
                if self.controller.panel is not None:
                    break
            panel = self.controller.panel
            if panel is not None:
                if attempt > 1:
                    print(f"    （面板第 {attempt} 次尝试才保持打开：失激活竞态，非用例失败）")
                break
            pump(self.app, 0.3)
        if panel is None:
            raise RuntimeError("面板未能打开（重试 3 次、每次轮询 2s 仍被失焦自关）")
        return panel.grid

    def drag(self, grid, rows: list[int], target: int) -> None:
        """走真实拖拽落地链路，再等 controller 200ms 去抖写盘。"""
        grid._apply_slot_move(rows, target)
        pump(self.app, 0.6)


ALL5 = ["a.lnk", "b.exe", "c.lnk", "d.exe", "e.lnk"]


def run_slot_cases(app, controller, case_dir: Path) -> None:
    """用例 A：密集插入语义 + 历史脏数据自愈 + 批量搬移 + 不连续多选。"""
    run = ScenarioRunner(app, controller, case_dir)

    print("\n--- A1：向前拖到占用图标 = 插入让位，原位置由后方补齐 ---")
    grid = run.reopen(list(ALL5))
    print(f"    初始可见 = {visible_names(grid)}")
    run.drag(grid, [1], 0)                      # b.exe 拖到 a.lnk 的位置
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots} 可见 = {visible_names(grid)}")
    check("向前拖：被拖条目落到松手的位置", slots[0] == "b.exe", str(slots))
    check("向前拖：a.lnk 让位后移一格，没有换到别处", slots[1] == "a.lnk", str(slots))
    check("向前拖：布局密集（无任何空位）", None not in slots, str(slots))
    check("向前拖：其余条目顺序不变",
          slots == ["b.exe", "a.lnk", "c.lnk", "d.exe", "e.lnk"], str(slots))

    print("\n--- A2：向后拖到占用图标（同理插入，后方图标补齐原位置） ---")
    grid = run.reopen(list(ALL5))
    run.drag(grid, [0], 4)                      # a.lnk 拖到 e.lnk 的位置
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots} 可见 = {visible_names(grid)}")
    check("向后拖：a.lnk 插到 e.lnk 的位置", slots[3] == "a.lnk", str(slots))
    check("向后拖：b/c/d 依次前移一格补齐（密集语义）",
          slots[:3] == ["b.exe", "c.lnk", "d.exe"], str(slots))
    check("向后拖：未拖动条目相对次序不变",
          slots == ["b.exe", "c.lnk", "d.exe", "a.lnk", "e.lnk"], str(slots))

    print("\n--- A3：历史脏数据（空位+幽灵）加载即自愈，落盘后为密集形态 ---")
    dirty = ["a.lnk", None, None, "ghost.lnk", "b.exe", None, "c.lnk", "d.exe", "e.lnk"]
    grid = run.reopen(dirty)
    print(f"    脏数据加载后可见 = {visible_names(grid)}")
    check("脏数据：空位与幽灵不再显示，真实条目全部保留",
          visible_names(grid) == list(ALL5), str(visible_names(grid)))
    run.drag(grid, [2], 0)                      # c.lnk 拖到最前
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots}")
    check("脏数据：拖拽落盘后文件为密集形态（无 None/幽灵）",
          slots == ["c.lnk", "a.lnk", "b.exe", "d.exe", "e.lnk"], str(slots))
    check("脏数据：面板可见顺序与磁盘一致",
          visible_names(grid) == slots, str(visible_names(grid)))

    print("\n--- A4：批量前移（整块插到落点之前，相对次序保持） ---")
    grid = run.reopen(list(ALL5))
    run.drag(grid, [3, 4], 0)                   # [d.exe, e.lnk] 拖到最前
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots}")
    check("批量前移：整块落到最前且内部次序保持",
          slots == ["d.exe", "e.lnk", "a.lnk", "b.exe", "c.lnk"], str(slots))

    print("\n--- A5：批量后移（落点就是松手的那一行，不存在行号偏移） ---")
    grid = run.reopen(list(ALL5))
    run.drag(grid, [0, 1], 3)                   # [a,b] 拖到 d.exe 上
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots}")
    check("批量后移：块紧贴在落点图标之前",
          slots == ["c.lnk", "a.lnk", "b.exe", "d.exe", "e.lnk"], str(slots))

    print("\n--- A6：拖到网格外 = 追加到末尾 ---")
    grid = run.reopen(list(ALL5))
    run.drag(grid, [0], -1)
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots}")
    check("网格外落点：被拖条目追加到末尾",
          slots == ["b.exe", "c.lnk", "d.exe", "e.lnk", "a.lnk"], str(slots))

    print("\n--- A7：不连续多选整块搬移（v1.2 缺陷 2 的 e2e 回归） ---")
    grid = run.reopen(list(ALL5))
    run.drag(grid, [0, 2], -1)                  # [a.lnk, c.lnk] 拖到网格外
    slots = read_slots(case_dir)
    print(f"    拖拽后 slots = {slots} 可见 = {visible_names(grid)}")
    check("不连续多选：整块追加到末尾、块内次序保持",
          slots == ["b.exe", "d.exe", "e.lnk", "a.lnk", "c.lnk"], str(slots))
    check("不连续多选：面板可见顺序与磁盘一致",
          visible_names(grid) == slots, str(visible_names(grid)))


def run_config_and_panel_cases(app, controller) -> None:
    """用例 B：旧配置迁移不得覆盖分类自带皮肤 + 真实面板渲染。"""
    print("\n--- B：旧配置迁移不得覆盖分类自带皮肤 ---")
    cat_a = controller.cfg.find_by_dir(CAT_DIR)
    cat_b = controller.cfg.find_by_dir(CAT_FILL)
    check("两个分类都被加载", cat_a is not None and cat_b is not None)
    if cat_a is not None:
        print(f"    {CAT_DIR}: radius={cat_a.radius} opacity={cat_a.opacity} "
              f"bg={cat_a.bg_color} mode={cat_a.position_mode}")
        check("分类自带的 radius=0 未被旧全局 24 覆盖", cat_a.radius == 0, str(cat_a.radius))
        check("分类自带的 opacity=0 未被旧全局 50 覆盖", cat_a.opacity == 0, str(cat_a.opacity))
        check("分类未写的 bg_color 拿到了迁移值", cat_a.bg_color == "#123456", cat_a.bg_color)
        check("分类未写的 position_mode 拿到了迁移值", cat_a.position_mode == "center",
              cat_a.position_mode)
    if cat_b is not None:
        check("缺失字段的分类被正确填充迁移值",
              (cat_b.radius, cat_b.opacity, cat_b.bg_color, cat_b.position_mode)
              == (24, 50, "#123456", "center"),
              f"{cat_b.radius}/{cat_b.opacity}/{cat_b.bg_color}/{cat_b.position_mode}")

    controller.open_panel("cat_qa_a")
    pump(app, 0.8)
    pw = controller.panel
    check("opacity=0 的分类面板已打开", pw is not None)
    if pw is None:
        return
    img = render_alpha(pw)
    pad_alpha = img.pixelColor(img.width() // 2, 4).alpha()
    print(f"    渲染 alpha: 顶部内边距={pad_alpha} windowOpacity={pw.windowOpacity()} "
          f"radius={pw.cat.radius}")
    check("opacity=0 时背景完全透明（只剩内容）", pad_alpha == 0, str(pad_alpha))
    check("未用 setWindowOpacity（窗口仍不透明）",
          abs(pw.windowOpacity() - 1.0) < 1e-6, str(pw.windowOpacity()))
    check("面板圆角取分类自带的 0", pw.cat.radius == 0, str(pw.cat.radius))
    check("面板尺寸取分类配置 360x260", (pw.width(), pw.height()) == (360, 260),
          f"{pw.width()}x{pw.height()}")
    controller.close_panel()
    pump(app, 0.3)


def run_shortcut_cases(controller, fake_desktop: Path) -> None:
    """用例 C：桌面 .lnk / .ico 同步（真实 COM 写盘，落在临时桌面目录）。"""
    print("\n--- C：桌面快捷方式同步（临时桌面目录，不动真实桌面） ---")
    lnks = sorted(p.name for p in fake_desktop.iterdir() if p.suffix.lower() == ".lnk")
    print(f"    当前分类 = {[(c.label, c.dir, c.visible) for c in controller.cfg.categories]}")
    print(f"    桌面 .lnk = {lnks}")
    # 夹具分类之外的目录（例如真实数据目录里已存在的"常用"）会被自动登记，
    # 因此这里按"包含关系"断言，只关心夹具里的两个分类。
    check("每个可见分类一个 .lnk",
          {"QA验证.lnk", "QA填充.lnk"}.issubset(set(lnks)), str(lnks))

    # 注意：必须断言 manager 实际使用的 ico_dir（已被重定向到临时目录）。
    # 之前这里写死 paths.ICO_DIR，能通过只是因为上一次跑测试在真实目录里留下了
    # 残渣 —— 属于"假通过"，一旦清理缓存就暴露。
    ico_dir = controller.shortcuts.ico_dir
    ico_a = ico_dir / "cat_qa_a.ico"
    check("分类 ICO 已生成", ico_a.exists() and ico_a.stat().st_size > 0, str(ico_a))
    check("ICO 文件结构合法（Magic=0/1 且至少一个图像条目）",
          ico_a.exists() and _ico_entry_count(ico_a) >= 1,
          str(_ico_entry_count(ico_a)) if ico_a.exists() else "缺失")
    check("ICO 是多尺寸打包（小图标不靠缩放）",
          ico_a.exists() and _ico_entry_count(ico_a) >= 3,
          str(_ico_entry_count(ico_a)) if ico_a.exists() else "缺失")
    lnk_a = fake_desktop / "QA验证.lnk"
    if lnk_a.exists():
        data = lnk_a.read_bytes()
        check(".lnk 里带着 --open 分类目录名",
              '--open "QA_验证分类"'.encode("utf-16-le") in data)
        check(".lnk 目标里带 main.py",
              "main.py".encode("utf-16-le") in data, f"{len(data)} bytes")

    cat_b = controller.cfg.find_by_dir(CAT_FILL)
    if cat_b is not None:
        cat_b.visible = False
        controller._after_categories_changed()      # 等同托盘"隐藏"动作
        pump(QApplication.instance(), 0.3)
        lnks = sorted(p.name for p in fake_desktop.iterdir() if p.suffix.lower() == ".lnk")
        print(f"    隐藏一个分类后桌面 .lnk = {lnks}")
        check("隐藏分类后桌面 .lnk 被删除", "QA填充.lnk" not in lnks, str(lnks))
        check("隐藏后其他分类的 .lnk 不受影响", "QA验证.lnk" in lnks, str(lnks))
        check("隐藏后分类仍在配置里（可从托盘访问）",
              controller.cfg.find_by_dir(CAT_FILL) is not None)
        cat_b.visible = True
        controller._after_categories_changed()
        pump(QApplication.instance(), 0.3)
        lnks = sorted(p.name for p in fake_desktop.iterdir() if p.suffix.lower() == ".lnk")
        check("重新显示后 .lnk 被重建", "QA填充.lnk" in lnks, str(lnks))


def main() -> int:
    global SANDBOX_ROOT

    # ① 第一件事：抓真实配置指纹（此时 paths 还指向真实目录，只读不写）
    real = sandbox.snapshot_real()
    print(f"真实配置基线：{real.describe()}")

    # ② 整体重定向到临时沙箱 —— 必须早于任何 paths.* 使用
    SANDBOX_ROOT = sandbox.sandbox_paths(prefix="dbx_e2e_sandbox_")
    paths.ensure_base_dirs()
    print(f"沙箱数据根：{paths.DATA_ROOT}（真实目录不再被写入）")

    app = QApplication(sys.argv)
    app.setQuitOnLastWindowClosed(False)
    QApplication.setStartDragDistance(4)

    tmp_root = Path(tempfile.mkdtemp(prefix="dbx_e2e_desktop_"))
    fake_desktop = tmp_root / "Desktop"
    case_dir = prepare_fixtures(fake_desktop)
    try:
        controller = AppController(app)          # 构造里就是 AppConfig.load(paths.CONFIG_PATH)
        controller.shortcuts.desktop_dir = fake_desktop   # 关键：不动真实桌面
        # 同步状态也放临时目录：沙箱里可能跑出多个分类记录，会干扰"隐藏/删除"断言
        controller.shortcuts.state_path = tmp_root / "lnk_state.json"
        controller.shortcuts.ico_dir = tmp_root / "ico"
        # v1.4：默认落点已改为私有启动器目录；用例 C 验的是"桌面落点"这条
        # 仍然保留的可回退链路，所以这里显式切回 desktop 模式。
        controller.shortcuts.mode = "desktop"
        controller.start()
        pump(app, 1.2)

        run_config_and_panel_cases(app, controller)
        run_slot_cases(app, controller, case_dir)
        run_shortcut_cases(controller, fake_desktop)

        controller.shutdown()
    finally:
        app.quit()
        cleanup_dirs()
        shutil.rmtree(tmp_root, ignore_errors=True)
        # ③ 拆沙箱：先删夹具目录，再整棵删掉沙箱根
        check("沙箱内临时分类目录已清理",
              not (paths.ROOT / CAT_DIR).exists() and not (paths.ROOT / CAT_FILL).exists())
        if SANDBOX_ROOT is not None:
            shutil.rmtree(SANDBOX_ROOT, ignore_errors=True)
            check("沙箱目录已删除", not SANDBOX_ROOT.exists(), str(SANDBOX_ROOT))
        # ④ 零污染护栏：真实配置 sha256 必须与快照一致（不依赖任何"还原"动作）
        check("真实用户配置零改动（sha256 一致）", *sandbox.guard_real_untouched(real))

    failed = [n for n, ok, _ in RESULTS if not ok]
    print(f"\n共 {len(RESULTS)} 项，通过 {len(RESULTS) - len(failed)} 项")
    if failed:
        print("失败项：" + ", ".join(failed))
        return 1
    print("端到端回归全部通过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
