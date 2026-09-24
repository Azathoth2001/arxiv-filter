@echo off
rem 发布包双击入口（方案 §8.4）。%~dp0 保证路径含空格/中文时仍从脚本所在目录启动。
setlocal
cd /d "%~dp0"

if exist "%~dp0arxiv-filter.exe" (
  start "" "%~dp0arxiv-filter.exe"
  exit /b 0
)

echo [错误] 未找到 arxiv-filter.exe。
echo [Error] arxiv-filter.exe was not found.
echo 请重新解压完整发布包，不要只复制 start.bat。
echo Re-extract the full release archive; do not copy start.bat alone.
pause
exit /b 1
