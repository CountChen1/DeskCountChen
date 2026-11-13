@echo off
rem ============================================================
rem  Desk CountChen 发布脚本：打标签 + 推送 GitHub + 生成 Release 压缩包
rem  双击运行即可；本文件用 GBK 编码保存，保证中文在 cmd 里不乱码。
rem ============================================================
setlocal enabledelayedexpansion
cd /d "%~dp0"

echo.
echo == Desk CountChen 发布脚本 ==

rem -- 0. 基础检查 -------------------------------------------------
if not exist ".venv\Scripts\python.exe" (
    echo [X] 找不到 .venv\Scripts\python.exe，请先按 README 创建虚拟环境
    goto :fail
)

rem -- 1. 版本号从代码里读（唯一事实来源 = app/__init__.py）--------
rem 用 findstr 而不是 python -c：python -c 的引号会被 cmd 吃掉（实测）。
set "VER="
for /f "tokens=2 delims== " %%V in ('findstr /b "__version__" app\__init__.py') do set "VER=%%~V"
if "!VER!"=="" (
    echo [X] 读不到版本号，检查 app\__init__.py
    goto :fail
)
echo 版本号：v!VER!

rem -- 2. 工作区必须干净（发布 = 某个确定的提交）-------------------
set "DIRTY="
for /f "delims=" %%i in ('git status --porcelain') do set "DIRTY=1"
if defined DIRTY (
    echo [X] 有未提交的改动，先 git add / git commit 再发布：
    git status --short
    goto :fail
)

rem -- 3. 打标签（已存在则跳过，幂等）------------------------------
git rev-parse -q --verify "refs/tags/v!VER!" >nul 2>&1
if errorlevel 1 (
    git tag -a "v!VER!" -m "Desk CountChen v!VER!"
    echo 已打标签：v!VER!
) else (
    echo 标签 v!VER! 已存在，跳过
)

rem -- 4. 远程仓库（没有就问一次）----------------------------------
git remote get-url origin >nul 2>&1
if not errorlevel 1 goto :have_remote
echo.
echo 尚未配置远程仓库。请先在 github.com 新建【空仓库】
echo （名字建议 DeskCountChen，不要勾选 README / license / gitignore），
echo 然后把仓库地址粘贴到下面。
echo.
set "URL="
set /p "URL=仓库地址（如 https://github.com/CountChen/DeskCountChen.git）："
if "!URL!"=="" (
    echo [X] 未输入地址，取消发布
    goto :fail
)
git remote add origin "!URL!"
echo 已添加远程 origin = !URL!
:have_remote
for /f "delims=" %%u in ('git remote get-url origin') do echo 远程仓库：%%u

rem -- 5. 推送（首次会弹出 GitHub 登录窗口，登录一次即可）----------
echo.
echo 正在推送 master ...
git push -u origin master
if errorlevel 1 goto :push_fail
echo 正在推送标签 ...
git push origin --tags
if errorlevel 1 goto :push_fail

rem -- 6. 生成 Release 压缩包（dist 存在才做）----------------------
if not exist "dist\DeskCountChen\DeskCountChen.exe" (
    echo.
    echo [提示] 没有 dist\DeskCountChen 产物，跳过压缩包。
    echo        先运行打包：.venv\Scripts\python.exe tools\build_dist.py
    goto :done
)
echo.
echo 正在生成 Release 压缩包 ...
powershell -NoProfile -ExecutionPolicy Bypass -Command "Compress-Archive -Path 'dist\DeskCountChen\*' -DestinationPath 'dist\DeskCountChen-!VER!-win64.zip' -Force"
if errorlevel 1 (
    echo [X] 压缩失败，可手动压缩 dist\DeskCountChen 整个目录
    goto :fail
)
echo 已生成：dist\DeskCountChen-!VER!-win64.zip
echo （发布时把它上传到 GitHub Release 的 v!VER! 标签下）

:done
echo.
echo == 完成：代码、标签已推送，Release 包已就绪 ==
echo 下一步：到 GitHub 仓库页面 - Releases - Draft a new release，
echo         选择标签 v!VER!，上传 dist\DeskCountChen-!VER!-win64.zip
echo.
pause
exit /b 0

:push_fail
echo.
echo [X] 推送失败。常见原因：
echo     1. 首次推送会弹 GitHub 登录窗口，别关它，登录后再看结果
echo     2. 仓库地址写错（git remote set-url origin 新地址 可改）
echo     3. 网络不通 / 需要代理
:fail
echo.
echo == 发布失败，按上面提示处理后重新双击本脚本（所有步骤幂等）==
echo.
pause
exit /b 1
