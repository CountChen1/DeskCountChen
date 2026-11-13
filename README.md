# Desk CountChen

> 把桌面上散乱的快捷方式按分类收进面板，点一下入口图标就弹出圆角面板，再点一下启动程序。

**Desk CountChen** 是一个 Windows 桌面图标收纳工具。它不替代桌面，也不接管你的图标，
而是给你的快捷方式加一层「分类面板」：面板常驻后台，由桌面图标 / 托盘 / SAO Utils 2 的 HUD
图标唤起，点击图标即可启动对应程序。

- 作者：**CountChen**
- 版本：**2.0.0**（原名 DesktopBox，2.0 起更名为 Desk CountChen）
- 平台：Windows 10 / 11（x64）
- 许可：**Apache-2.0**

> 2.0 是一次破坏性更名：程序名、exe 名、数据目录名全部变更，配置与分类数据**可直接沿用**
> （详见 [从 1.x 升级](#从-1x-升级)）。

---

## 功能

| 能力 | 说明 |
|---|---|
| 分类面板 | 每个分类一个网格面板，图标数量自适应，超出自动滚动 |
| 两种材质 | 毛玻璃（截屏降采样模糊）/ 纯色填充，可调圆角、尺寸、透明度 |
| 交互反馈 | 悬停放大 + 名字高亮、打开时缩放缓入、点击不残留选中态 |
| 拖拽排序 | 面板内拖动图标调整顺序，200ms 去抖落盘 |
| 托盘常驻 | 每个分类一个子菜单，可新建分类、打开数据目录、查看关于 |
| 单实例唤醒 | 再次点击入口不重开进程，而是把「打开哪个分类 + 鼠标落点」发给已运行实例 |
| 图标自动提取 | 自动解析 `.lnk` 目标取真实图标（不带快捷方式小箭头），也可自定义图片 |
| 全局设置 | 把某一分类的皮肤参数一键应用到全部分类 |

## 系统要求

- Windows 10 1809 及以上 / Windows 11
- 无需安装 Python，下载 Release 里的压缩包解压即用
- 可选：[SAO Utils 2](https://www.gpsoft.com.cn/)（用于把分类入口放到桌面 HUD）

## 安装

1. 到 [Releases](https://github.com/CountChen/DeskCountChen/releases) 下载最新的
   `DeskCountChen-<版本>-win64.zip`；
2. 解压到任意目录，例如 `E:\desktopbox\DeskCountChen\`；
3. 双击 `DeskCountChen.exe` 启动，首次运行会在**上级目录**自动建立 `data\` 存放配置与分类；
4. 右键托盘图标 → 新建分类 → 把快捷方式拖进对应分类文件夹即可。

### 目录布局

```
<你选的部署根>\
├── DeskCountChen\          # 程序本体（exe + _internal\，可整体替换升级）
│   ├── DeskCountChen.exe
│   └── _internal\
└── data\                   # 用户数据（配置 / 图标 / 启动器 / 日志 / 分类文件夹）
    ├── .config.json
    ├── .icons\
    ├── .launchers\         # 供 SAO 等外部程序引用的 .lnk
    ├── .logs\
    └── 常用\                # 分类目录（每个分类一个文件夹）
```

数据根由 `exe 所在目录的上一级 \ data` 决定；需要放到别处时设置环境变量
`DESKCOUNTCHEN_HOME` 指向目标目录即可。

### 与 SAO Utils 2 配合

托盘菜单 →「打开启动器目录」，把里面的 `启动-<分类>.lnk` 拖到 SAO 的 HUD 上，
之后点 HUD 图标就等于点这个分类的入口。

## 命令行

```bat
DeskCountChen.exe                 :: 正常启动（托盘常驻）
DeskCountChen.exe --open "常用"    :: 直接打开指定分类的面板
DeskCountChen.exe --console       :: 带控制台运行，日志同时输出到 stdout
DeskCountChen.exe --selfcheck     :: 只做基础设施自检，打印「基础设施就绪」后退出
DeskCountChen.exe --version       :: 打印产品名 + 版本 + 构建指纹后退出
```

## 从源码构建

```bat
python -m venv .venv
.venv\Scripts\python.exe -m pip install -r requirements.txt
.venv\Scripts\python.exe tools\build_dist.py
```

产物在 `dist\DeskCountChen\`。打包脚本会自动：写入构建指纹（commit + 时间）、
生成 PE 版本资源（`CompanyName=CountChen` / `ProductName=Desk CountChen` /
`LegalCopyright`）、用 `assets/app.ico` 作为 exe 图标，最后跑一次 `--selfcheck` 冒烟。

源码直接运行（不打包）：`run.bat`（无控制台）或 `run.bat --console`（带日志）。

## 如何验证是否为官方版本

本项目的构建带指纹，任何人（包括你）都能快速判断手上这份是不是原作者发布的：

1. **看文件属性**：右键 `DeskCountChen.exe` → 属性 → 详细信息，`公司` 应为 `CountChen`，
   `产品名称` 应为 `Desk CountChen`，`版权` 含 `Copyright (c) 2026 CountChen`；
2. **看构建指纹**：命令行运行 `DeskCountChen.exe --version`，或托盘菜单 →「关于」，
   输出形如 `Desk CountChen v2.0.0 (build a1b2c3d, 2026-11-13 09:30)`；
   把其中的 commit 与本仓库的 [Releases](https://github.com/CountChen/DeskCountChen/releases)
   标签对照——改过代码重新编译的版本，commit 必然对不上；
3. **看包完整性**：只从本仓库 Releases 下载，不要用来路不明的「绿色版」「破解版」。

## 从 1.x 升级

1. 关闭正在运行的旧版（托盘 → 退出）；
2. 解压新版到新目录（例如 `E:\desktopbox\DeskCountChen\`）；
3. 原来的 `data\` 目录**不用动**——2.0 的数据结构与 1.x 完全兼容；
4. 若之前把 SAO 的 HUD 图标指向旧 exe，用托盘菜单 →「打开启动器目录」重新拖一次即可。

## 许可证

源码以 [Apache License 2.0](LICENSE) 发布；分发时的归属与第三方组件声明见 [NOTICE](NOTICE)。

「Desk CountChen」「CountChen」及 `assets/app.ico` 图标用于标识本项目。你可以自由 fork 与修改，
但请保留版权声明，且不要把修改版当作原版发布。
