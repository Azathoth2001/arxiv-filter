@echo off
rem 源码模式启动（方案 §8.1 的等价 Windows 入口）。
rem 解释器优先级：项目 .venv → miniforge 的 arxiv-filter 环境 → PATH 上的 python。
rem 绝不修改 PowerShell 执行策略；所有调用都走绝对路径。
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "D:\miniforge3\envs\arxiv-filter\python.exe" set "PY=D:\miniforge3\envs\arxiv-filter\python.exe"
if not defined PY (
  for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
)

if not defined PY (
  echo [错误] 找不到可用的 Python 解释器。
  echo [Error] No usable Python interpreter found.
  echo 请任选其一：
  echo   1) D:\miniforge3\Scripts\conda.exe create -n arxiv-filter -c conda-forge python=3.12 pip -y
  echo   2) py -3.12 -m venv .venv
  pause
  exit /b 1
)

echo 使用解释器：%PY%
"%PY%" -m arxiv_filter %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [提示] 程序退出码 %RC%。日志位于 %%LOCALAPPDATA%%\ArxivFilter\logs\app.log
  pause
)
exit /b %RC%
