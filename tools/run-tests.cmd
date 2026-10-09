@echo off
rem CrossPC one-click acceptance tests (Windows).
rem Double-click this file. It opens a normal console, so input injection and
rem global hooks are NOT restricted (unlike an AI assistant session).
rem All output is also written to logs\*.log
chcp 65001 >nul
setlocal
cd /d "%~dp0.."
set PYTHONIOENCODING=utf-8
set PYTHONUTF8=1

where python >nul 2>nul
if errorlevel 1 (
  echo.
  echo [!] Python not found on PATH.
  echo     Install it with:  winget install -e --id Python.Python.3.12
  echo     ^(Microsoft Store placeholder does not work either^)
  echo.
  pause
  exit /b 1
)

python tools\run-tests.py
echo.
pause
