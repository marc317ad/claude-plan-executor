@echo off
rem Windows counterpart of run_mcp_server.sh: resolve an interpreter and
rem exec it with the forwarded args (the plan-ops MCP server script).
rem Resolution order mirrors the .sh launcher:
rem   1. IMPLEMENT_PLAN_PYTHON (if it exists)
rem   2. <repo-root>\venv\Scripts\python.exe
rem   3. <repo-root>\.venv\Scripts\python.exe
rem   4. python on PATH
setlocal

set "PROJECT_ROOT=%~dp0..\..\.."

if defined IMPLEMENT_PLAN_PYTHON (
    if exist "%IMPLEMENT_PLAN_PYTHON%" (
        "%IMPLEMENT_PLAN_PYTHON%" %*
        exit /b %errorlevel%
    )
)

if exist "%PROJECT_ROOT%\venv\Scripts\python.exe" (
    "%PROJECT_ROOT%\venv\Scripts\python.exe" %*
    exit /b %errorlevel%
)

if exist "%PROJECT_ROOT%\.venv\Scripts\python.exe" (
    "%PROJECT_ROOT%\.venv\Scripts\python.exe" %*
    exit /b %errorlevel%
)

where python >nul 2>nul
if %errorlevel%==0 (
    python %*
    exit /b %errorlevel%
)

echo plan-ops MCP launcher could not find an executable interpreter; tried IMPLEMENT_PLAN_PYTHON, venv\Scripts, .venv\Scripts, and python on PATH 1>&2
exit /b 127
