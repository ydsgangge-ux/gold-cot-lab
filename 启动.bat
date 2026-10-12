@echo off
rem Switch the console to UTF-8 so the Chinese progress lines printed by
rem scripts\launch.py are readable on any Windows, including English installs.
chcp 65001 >nul 2>nul
setlocal
title Gold COT Lab
cd /d "%~dp0"

set "PY="
where python >nul 2>nul && set "PY=python"
if not defined PY (
  where py >nul 2>nul && set "PY=py"
)

if not defined PY goto nopython

%PY% -c "import sys; sys.exit(0 if sys.version_info >= (3, 10) else 1)" >nul 2>nul
if errorlevel 1 goto oldpython

%PY% "scripts\launch.py"
set "CODE=%ERRORLEVEL%"

if not "%CODE%"=="0" (
  echo.
  echo   The launcher exited with code %CODE%. Please read the message above.
  echo.
  pause
)

endlocal & exit /b %CODE%

:nopython
echo.
echo   ==============================================================
echo    Python was not found on this computer.
echo   ==============================================================
echo.
echo    This tool runs on Python. Please install it once:
echo.
echo        https://www.python.org/downloads/
echo.
echo    During the install, tick "Add python.exe to PATH".
echo    Then double-click this file again.
echo.
echo   ==============================================================
echo.
pause
endlocal
exit /b 1

:oldpython
echo.
echo   ==============================================================
echo    Your Python is too old. Version 3.10 or newer is required.
echo   ==============================================================
echo.
echo    Please install a newer version:
echo        https://www.python.org/downloads/
echo.
echo   ==============================================================
echo.
pause
endlocal
exit /b 1