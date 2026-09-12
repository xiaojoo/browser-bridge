@echo off
rem DeepSeek Browser Bridge - Windows 一键启动
cd /d "%~dp0"

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 未找到 .venv。请先完成初始化:
    echo   python -m venv .venv
    echo   .venv\Scripts\pip install -r requirements.txt
    echo   .venv\Scripts\python -m playwright install chromium
    pause
    exit /b 1
)

start "" "http://127.0.0.1:8765"
".venv\Scripts\python.exe" main.py
pause
