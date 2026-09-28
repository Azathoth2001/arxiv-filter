@echo off
rem ---------------------------------------------------------------------------
rem Source-mode launcher (Windows equivalent of `python -m arxiv_filter`).
rem
rem Interpreter lookup order:
rem   1. project .venv
rem   2. the active conda environment (CONDA_PREFIX)
rem   3. conda env "arxiv-filter" located via CONDA_ROOT / CONDA_EXE / `where conda`
rem   4. python.exe on PATH (the Windows Store stub is rejected)
rem
rem NOTE: this file is deliberately ASCII-only. cmd.exe parses .bat files using the
rem system ANSI codepage (GBK on a Chinese Windows), so UTF-8 Chinese comments are
rem garbled into stray commands -- that produced a screenful of
rem "'...' is not recognized as an internal or external command" on every launch.
rem Keep every line here ASCII. Same reason: no full-width punctuation.
rem ---------------------------------------------------------------------------
setlocal
cd /d "%~dp0"

set "PY="
if exist "%~dp0.venv\Scripts\python.exe" set "PY=%~dp0.venv\Scripts\python.exe"
if not defined PY if exist "%~dp0venv\Scripts\python.exe" set "PY=%~dp0venv\Scripts\python.exe"

if not defined PY if defined CONDA_PREFIX if exist "%CONDA_PREFIX%\python.exe" set "PY=%CONDA_PREFIX%\python.exe"
if not defined PY if defined CONDA_ROOT if exist "%CONDA_ROOT%\envs\arxiv-filter\python.exe" set "PY=%CONDA_ROOT%\envs\arxiv-filter\python.exe"

rem Locate conda via `where` (CONDA_EXE is not always a real path -- it can be a
rem shell alias, in which case expanding it inside a for-loop breaks parsing).
if not defined PY (
  for /f "delims=" %%C in ('where conda 2^>nul') do (
    if not defined PY if exist "%%~dpC..\envs\arxiv-filter\python.exe" set "PY=%%~dpC..\envs\arxiv-filter\python.exe"
  )
)

if not defined PY (
  for %%P in (python.exe) do if not defined PY set "PY=%%~$PATH:P"
)
rem The Windows app-alias python.exe is a stub that opens the Store, not an interpreter.
for %%P in ("%PY%") do if /i "%%~dpP"=="%LOCALAPPDATA%\Microsoft\WindowsApps\" set "PY="

if not defined PY (
  echo [ERROR] No usable Python interpreter found.
  echo Pick one of:
  rem Parentheses inside a parenthesised block must be escaped, or they close the
  rem block early and the rest of the line is executed as a command.
  echo   1^) conda create -n arxiv-filter -c conda-forge python=3.12 pip -y
  echo   2^) py -3.12 -m venv .venv
  pause
  exit /b 1
)

echo Using interpreter: %PY%
"%PY%" -m arxiv_filter %*
set "RC=%ERRORLEVEL%"
if not "%RC%"=="0" (
  echo.
  echo [HINT] Exit code %RC%. Log: %%LOCALAPPDATA%%\ArxivFilter\logs\app.log
  pause
)
exit /b %RC%
