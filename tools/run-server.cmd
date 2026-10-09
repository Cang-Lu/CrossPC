@echo off
rem CrossPC server launcher (Windows, the machine with the keyboard and mouse): double-click it
rem Python 3 is required. Running tools\install_windows.ps1 once first is safer.
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
echo ================================================================
echo  CrossPC server is starting...
echo  Panic release (take back keyboard and mouse): Ctrl+Alt+F12
echo  Closing this window = stop the server (control returns to this machine at once)
echo ================================================================
echo.
python -m crosspc server %*
echo.
echo server has exited.
pause
