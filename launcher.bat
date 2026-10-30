@echo off
REM ==========================================================
REM  lite-chat - 一键启动脚本 (Windows)
REM  双击本文件：启动服务，浏览器会自动打开
REM ==========================================================
cd /d "%~dp0"

REM 检查 Python 是否可用
where python >nul 2>nul
if errorlevel 1 (
    echo [错误] 未检测到 Python，请先安装 Python 3 并勾选“Add to PATH”。
    pause
    exit /b 1
)

echo 正在启动 lite-chat，稍候浏览器将自动打开...
echo 访问地址: http://127.0.0.1:8000
echo 按 Ctrl+C 或关闭此窗口即可停止服务
echo --------------------------------------------------
python app.py
