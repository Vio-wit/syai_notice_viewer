@echo off
chcp 65001 >nul
set "ROOT=%~dp0"
if not exist "%ROOT%.venv\Scripts\python.exe" (
  echo 请先执行 README.md 中的安装命令。
  exit /b 1
)
"%ROOT%.venv\Scripts\python.exe" "%ROOT%notice_viewer.py" %*
