@echo off
title video-extract
cd /d "%~dp0"

rem 本地配置（放 API Key，已在 .gitignore 忽略）
if exist "llm.env.bat" call "llm.env.bat"

set "PY=%VIDEOEXTRACT_PY%"
if not defined PY set "PY=python"

echo.
echo   video-extract  正在启动，稍后自动打开浏览器 ...
echo   界面地址 http://127.0.0.1:8000    按 Ctrl+C 停止
echo.

"%PY%" -X utf8 server.py --open %*

echo.
echo   服务已停止。
pause
