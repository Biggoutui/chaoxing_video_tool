@echo off
call "%~dp0run.cmd" --inspect %*
set "TOOL_EXIT=%errorlevel%"
pause
exit /b %TOOL_EXIT%
