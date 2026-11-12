@echo off
rem DesktopBox 启动脚本
rem 默认：使用 pythonw.exe 无控制台窗口启动
rem 调试：run.bat --console   使用 python.exe，可看到日志输出
setlocal
set "ROOT=%~dp0"

if "%~1"=="--console" (
    set "PYEXE=%ROOT%.venv\Scripts\python.exe"
    "%PYEXE%" "%ROOT%main.py" --console
    endlocal
    goto :eof
)

set "PYW=%ROOT%.venv\Scripts\pythonw.exe"
if not exist "%PYW%" set "PYW=%ROOT%.venv\Scripts\python.exe"
start "" "%PYW%" "%ROOT%main.py"
endlocal
