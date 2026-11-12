# DesktopBox 部署指南（v1.6）

> 分享给别人 / 搬盘，只看这一篇。核心思路：**程序与程序私有数据放一起，
> 整个目录拷走即分享**；分类数据根继续指向 `E:\快捷方式SAO`（SAO Utils 的
> HUD 目录，不随部署漂移）。

## 1. 部署布局

```
E:\desktopbox\
├── DesktopBox\              # 打包产物（tools/build_dist.py 生成，压缩即可分享）
│   ├── DesktopBox.exe
│   └── _internal\
└── data\                    # 程序私有目录 APP_DIR（frozen 态默认，自动创建）
    ├── .config.json         # 全局配置
    ├── .icons\              # 分类图标缓存（.ico / lnk_state.json）
    ├── .launchers\          # 启动器 .lnk（SAO HUD 引用）
    ├── .logs\               # app.log / crash.log
    └── 常用\                # 历史遗留目录（当前数据根不在这里，见 §3）
```

- 打包产物放到**任何盘的任意目录**都能跑，但 `APP_DIR` 固定取
  "exe 所在目录的上一级 `\data`"（见 §3）。上例中
  `E:\desktopbox\DesktopBox\DesktopBox.exe` → 数据在 `E:\desktopbox\data`。
- 分类数据**不在** `\data` 里：`.config.json` 的 `app.data_root` 指向
  `E:\快捷方式SAO`，6 个分类目录（游戏加速/影音娱乐/工作学习/社交通讯/
  系统硬件/文档下载）一直在那里，与 SAO HUD 共用。

## 2. 打包与分享

```
.venv\Scripts\python.exe tools\build_dist.py
```

脚本做四件事：缺 PyInstaller 则装入 venv → 清理旧 build/dist → PyInstaller
onedir + windowed（无控制台）打包 main.py，排除 tests/docs/tools →
自动跑 `dist\DesktopBox\DesktopBox.exe --selfcheck`（退出码非 0 即失败）。

**分享给别人**：把 `dist\DesktopBox\` 整个目录压缩发给对方即可——
对方解压到任意目录运行 `DesktopBox.exe`，首次启动自动建 `\data` 和默认配置。
对方是"从零开始"的全新用户；如果想连配置/图标一起给，把 `\data` 也一起打包
（但注意 `\data\.config.json` 里的 `data_root` 是你自己机器的路径，见 §3）。

## 3. 数据根解析规则（app/paths.py）

| 运行态 | APP_DIR（程序私有目录：配置/图标/日志/启动器） |
|---|---|
| 源码（v1.5 起不变） | `%USERPROFILE%\DesktopBox` |
| frozen（打包）默认 | `<exe 所在目录的上一级>\data` |
| frozen + 环境变量 | `DESKTOPBOX_HOME` 指向完整 APP_DIR，**优先于**默认值 |

- `DATA_ROOT`（分类数据）与 `APP_DIR` 是两回事：默认 `DATA_ROOT = APP_DIR`，
  但 `.config.json` 的 `app.data_root` 一旦设置就以它为准（`main.py`
  启动时注入，本机制 v1.4 起未变）。当前机器 `data_root = E:\快捷方式SAO`。
- frozen 态 exe 若被放进深层目录（如误拷进 `.venv\Scripts\`），只兜底
  `parent.parent`，**绝不向上无限爬**——宁可就近猜错也不把数据写进盘根；
  真要自定义位置请设 `DESKTOPBOX_HOME`。

## 4. 启动器链（SAO HUD → DesktopBox）

- 启动器 .lnk 由 `DesktopShortcutManager` 幂等管理，源码态目标 =
  `pythonw.exe main.py --open "<分类>"`；**frozen 态目标 = 产品 exe 自身 +
  `--open "<分类>"`**（无脚本参数），工作目录 = exe 所在目录。
- 当前部署的 .lnk 链：`E:\快捷方式SAO\启动-*.lnk`（6 个，SAO HUD 引用）
  ← 逐字节复制自 `E:\desktopbox\data\.launchers\启动-*.lnk`
  ← 由 `tools\rebuild_launchers_frozen.py` 用产品 exe 重建。
- 单击 HUD 图标 → 起第二实例 → 单实例唤醒 → 第一实例弹面板。

## 5. 本次迁移记录（2026-11-13，C 盘 → E 盘）

1. `tools\build_dist.py` 产出 `dist\DesktopBox\`，selfcheck 冒烟通过；
2. robocopy 产物 → `E:\desktopbox\DesktopBox\`；
3. 迁移前 diff 确认 `C:\Users\Z\DesktopBox` 与全量备份
   `E:\desktopbox-backup-20261113\DesktopBox` 一致，然后 robocopy
   **复制**（C 盘原样保留）→ `E:\desktopbox\data\`；
4. `tools\rebuild_launchers_frozen.py` 用产品 exe 重建 6 个启动器
   （写 `E:\desktopbox\data\.launchers`，等价 frozen 产品自己 sync 的结果），
   并覆盖发布到 `E:\快捷方式SAO\`（旧目标已失效；旧 .lnk 备份在
   `E:\desktopbox-backup-20261113\sao-lnk`）；
5. 自检：`DesktopBox.exe --selfcheck` 退出码 0 +
   `tools\verify_sao_state.py --app-dir E:\desktopbox\data` 43 项全绿。

**回滚**：恢复备份 `E:\desktopbox-backup-20261113\` 的 6 个 SAO 根 .lnk
（目标 = 旧 venv pythonw）+ 继续用源码态（run.bat）；C 盘数据全程未动。

## 6. 常见问题

- **换盘/改数据位置**：设环境变量 `DESKTOPBOX_HOME` 指向新的 APP_DIR；
  分类数据位置改 `.config.json` 的 `app.data_root`。
- **双开检测**：exe 版与源码版共用同一把单实例互斥量，同时只能跑一个。
- **自检**：`DesktopBox.exe --selfcheck`（不碰互斥量，随时可跑）；
  SAO 链路状态：`tools\verify_sao_state.py`（只读，43 项）。
