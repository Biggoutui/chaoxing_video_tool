@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
if exist ".venv\Scripts\python.exe" (
    set "TOOL_PYTHON=%~dp0.venv\Scripts\python.exe"
) else (
    where python >nul 2>&1
    if errorlevel 1 (
        echo 未找到 Python。请安装 Python 3.10 或更新版本，并按 README 创建 .venv。
        exit /b 1
    )
    set "TOOL_PYTHON=python"
)
"%TOOL_PYTHON%" -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>&1
if errorlevel 1 (
    echo 无法运行 Python 3.10 或更新版本。请检查 Python 安装及 .venv 环境。
    exit /b 1
)
"%TOOL_PYTHON%" -c "import playwright.sync_api" >nul 2>&1
if errorlevel 1 (
    echo 当前 Python 环境缺少 Playwright。请按 README 安装 requirements.txt。
    exit /b 1
)
"%TOOL_PYTHON%" video_tool.py %*
exit /b %errorlevel%
