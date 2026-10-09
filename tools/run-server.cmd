@echo off
rem CrossPC server(Windows, 接键鼠的那台)启动器: 双击即可
rem 需要 Python 3。先跑一次 tools\install_windows.ps1 更稳妥。
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
echo ================================================================
echo  CrossPC server 正在启动...
echo  紧急收回键鼠: Ctrl+Alt+F12
echo  关掉这个窗口 = 停止 server(会立刻把控制权还给本机)
echo ================================================================
echo.
python -m crosspc server %*
echo.
echo server 已退出。
pause
