@echo off
setlocal
chcp 65001 >nul
cd /d "%~dp0"
"%~dp0python\python.exe" "%~dp0register_test.py" %*
exit /b %errorlevel%
