"""DesktopBox 测试包（标准库 unittest，无第三方依赖）。

约定：
- 全部测试只用 tempfile 临时目录，绝不写 %USERPROFILE%\\DesktopBox；
- 需要 Qt 的测试统一走 offscreen 平台，可离线运行。
"""
