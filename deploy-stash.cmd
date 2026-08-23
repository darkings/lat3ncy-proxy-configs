@echo off
setlocal

rem 仅调用本机 PowerShell；认证由 SSH 配置/代理负责，本文件不包含任何凭据。
powershell.exe -NoLogo -NoProfile -ExecutionPolicy Bypass -File "%~dp0deploy-stash.ps1" %*
set "exit_code=%ERRORLEVEL%"

if not "%exit_code%"=="0" pause
exit /b %exit_code%
