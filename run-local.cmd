@echo off
setlocal
chcp 65001 >nul
pushd "%~dp0"
set "PYTHONUTF8=1"
set "RESULT=1"
where python >nul 2>&1
if not errorlevel 1 goto python
where py >nul 2>&1
if not errorlevel 1 goto py
echo [ERROR] Install Python 3.10 or newer and add it to PATH.
goto finished

:python
python -B "%~dp0offline_demo.py" %*
set "RESULT=%ERRORLEVEL%"
goto finished

:py
py -3 -B "%~dp0offline_demo.py" %*
set "RESULT=%ERRORLEVEL%"

:finished
echo.
echo [OFFLINE] Exit code: %RESULT%
if "%~1"=="" pause
popd
exit /b %RESULT%
