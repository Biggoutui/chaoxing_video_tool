@echo off
call "%~dp0run.cmd" --login %*
set "TOOL_EXIT=%errorlevel%"
pause
exit /b %TOOL_EXIT%
