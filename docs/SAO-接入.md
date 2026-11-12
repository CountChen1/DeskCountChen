# 接入 SAO Utils 2（v1.4 桌面入口方案）

v1.4 起，桌面入口**不再由 DesktopBox 写 .lnk**，而是：

```
SAO Utils 2 的 HUD 图标（用户单击）
        ↓  执行一个 .lnk
%USERPROFILE%\DesktopBox\.launchers\启动-<分类>.lnk
        ↓  pythonw.exe main.py --open "<分类目录名>"
第二个 DesktopBox 进程 → 单实例唤醒 → 第一个实例弹出该分类的面板
```

这样做的原因：

- SAO 的 HUD 图标是**独立于桌面窗口**的浮层，按 Win+D 不会被藏起来，
  用户点它随时可用；
- DesktopBox 只负责"被叫起来弹面板"，入口完全交给 SAO 管理（拖动位置、
  换图标、分组都在 SAO 里做）；
- 分类增删只影响 `.launchers` 里的文件，**桌面保持干净**。

---

## 一、每个 HUD 图标怎么配（一次性，约 1 分钟/个）

1. 右键 SAO Utils 2 里要用的那个 HUD 图标；
2. **Change Action**（更改动作）→ 动作类型选 **Open File / 打开文件**
   （不同皮肤汉化文案可能不同，认准"打开文件/运行程序"那一项）；
3. 路径填 / 浏览到：

   ```
   %USERPROFILE%\DesktopBox\.launchers\启动-<分类名>.lnk
   ```

   实际完整路径示例（本机）：`C:\Users\Z\DesktopBox\.launchers\启动-游戏加速.lnk`
4. 图标建议直接用 SAO 自己的图标设置（DesktopBox 生成的是面板启动图标，
   HUD 上好不好看见仁见智）；
5. 保存。**单击该 HUD 图标 → 面板弹出**。

> 托盘菜单里新增了 **「打开启动器目录（供 SAO 引用）」**，点它直接打开
> `.launchers` 目录，复制/拖拽文件名更方便。

### 面板出现在哪

- 面板出现在**鼠标当前位置**（SAO 单击时鼠标就在图标上），即"贴合鼠标落点"；
- 分类设置里把"弹出位置"改成 **屏幕居中**，则忽略鼠标位置、在该屏居中。

### 为什么单击就能用

`.lnk` 的目标是 `pythonw.exe main.py --open "<分类>"`。点它 = 起第二个进程；
第二个进程发现单实例互斥量已被占用，就把"分类名 + 鼠标落点"通过
QLocalSocket 发给第一个实例并**静默退出**，第一个实例弹面板。
全程不弹"已在运行"对话框。

---

## 二、Win+D 之后还在吗

**在。** HUD 图标是 SAO 的浮层，不属于桌面窗口，Win+D 只影响桌面上的图标和窗口。
反过来：正因为这一点，DesktopBox 也不再需要 v1.2 时代那套"桌面置底 + 看门狗
拉回"的自绘窗口逻辑。

---

## 三、分类增删之后要做什么

DesktopBox 的 `.launchers` 目录是**启动时幂等同步**的：

| 动作 | DesktopBox 这边 | 你要做的 |
|---|---|---|
| 新建分类 | 下次启动（或托盘新建后立刻）生成 `启动-<新分类>.lnk` | 在 SAO 里新建一个 HUD 图标，指向新文件 |
| 重命名分类 | 旧 `启动-旧名.lnk` 删除，生成 `启动-新名.lnk` | 把 SAO 里该项的指向改成新文件名 |
| 隐藏分类 | 该 `.lnk` 被删除（分类仍可从托盘打开） | 在 SAO 里删掉对应 HUD 图标（否则点了没反应） |
| 删除分类 | 该 `.lnk` 被删除 | 同上 |

> 想让 SAO 的指向**永不失效**：分类名保持稳定，只改显示文字（label）而不改
> 目录名是行不通的——启动器文件名跟着 label 走（与 SAO 里看到的名称一致更直观）。
> 如果频繁改名，可以每次改完后点一次托盘 →「打开启动器目录」，看目录里
> 现在叫什么，再去 SAO 里改指向。

改完后想立刻生效：托盘右键 →「退出」，再双击 `run.bat` 启动（同步在启动时做）。

---

## 四、数据根目录（分类数据放哪）

v1.4 支持把分类数据根搬出 `%USERPROFILE%`：

```jsonc
// %USERPROFILE%\DesktopBox\.config.json
{
  "app": {
    "data_root": "E:\\快捷方式SAO",              // 分类数据根；"" = 默认 DesktopBox 目录
    "category_exclude": ["快捷方式", "桌面", "z全部"],  // 自动登记时要跳过的目录名
    "shortcut_mode": "launcher",                 // launcher = 私有启动器（默认）| desktop = 写真实桌面
    "strip_extensions": true,                    // 面板内名称去扩展名（任意文件，v1.4.1 起）
    "strip_shortcut_overlay": true                // 解析 .lnk 取目标图标（去掉"快捷方式"小箭头）
  }
}
```

要点：

- **程序自己的东西永远在 `%USERPROFILE%\DesktopBox`**：`.config.json`、`.icons`、
  `.ico`、`.logs`、`.launchers`。数据根换到别的盘（甚至外接盘临时未挂载）时，
  配置和日志依然在原处，程序照常启动，只是分类目录不可用。
- 数据根下**不是所有目录都是分类**：`category_exclude` 里的名字、以 `.` 开头的
  目录都不会被登记。
- 已经登记过、但在新数据根里找不到的分类 → 自动标为"隐藏"（**不删配置、
  不动皮肤**），挪回目录或用托盘「显示」即可恢复。

---

## 五、备份与恢复

改动前的完整备份在：

```
E:\快捷方式SAO_备份_20261112-1900\        （约 3.9 GB，1136 个文件）
```

恢复步骤（摘自该目录的 `恢复说明.md`，以那份为准）：

1. 托盘右键 →「退出」，退出 DesktopBox；
2. 把当前的 `E:\快捷方式SAO\` 改名为 `E:\快捷方式SAO_新`（不要直接删，留证据）；
3. 把 `E:\快捷方式SAO_备份_20261112-1900\` **整体改名**为 `E:\快捷方式SAO\`；
4. 想连 DesktopBox 旧设置一起回退：把
   `_DesktopBox_原始状态\.config.json` 复制回 `C:\Users\Z\DesktopBox\.config.json`；
5. 想恢复旧的桌面图标：把 `_DesktopBox_原始状态\桌面快捷方式_已移出\常用.lnk`
   复制回 `C:\Users\Z\Desktop\`。

> DesktopBox 在分类目录里只新增一个隐藏文件 `.order.json`（记录图标顺序），
> 不会修改 / 移动 / 重命名任何原有文件。想恢复"按名称排序"，删掉它即可。

配置单文件备份：迁移脚本每次执行都会把当前配置另存为
`%USERPROFILE%\DesktopBox\.config.json.bak-<YYYYmmdd-HHMMSS>`。

---

## 六、本方案会动哪些文件（可审计清单）

| 路径 | 行为 |
|---|---|
| `%USERPROFILE%\DesktopBox\.config.json` | 读写（迁移脚本 + 程序保存） |
| `%USERPROFILE%\DesktopBox\.launchers\启动-*.lnk` | 新建 / 重建 / 删除（都是自己生成的） |
| `%USERPROFILE%\DesktopBox\.icons\` | ICO 缓存与同步指纹 |
| `%USERPROFILE%\DesktopBox\.logs\` | 日志（含 crash.log） |
| `E:\快捷方式SAO\<分类>\` | **只读目录名**用于登记；写入仅限 `.order.json`（拖拽排序时） |
| `E:\快捷方式SAO\<分类>\.order.json` | 拖拽排序记录（原子写、隐藏属性） |
| 桌面上指向本项目的旧 `.lnk` | 迁移时清理，**删除前必须验证 target 是 python(w).exe 且参数含 `--open`** |
| 桌面上其它任何文件 | **不动** |

迁移脚本 `tools/migrate_to_sao.py` 只写 `%USERPROFILE%\DesktopBox` 下的东西，
不会往 `E:\快捷方式SAO` 写任何文件（连 `.order.json` 都不写）。
