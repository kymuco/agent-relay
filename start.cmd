@echo off
setlocal
cd /d "%~dp0"
where py >nul 2>&1
if not errorlevel 1 (
    for %%V in (3.14 3.13 3.12 3.11 3.10) do (
        py -%%V -c "import sys; sys.exit(0)" >nul 2>&1
        if not errorlevel 1 (
            py -%%V "%~dp0bootstrap.py" %*
            goto :finished
        )
    )
)
python "%~dp0bootstrap.py" %*
:finished
set "status=%ERRORLEVEL%"
if not "%status%"=="0" (
    echo.
    echo AgentRelay setup or launch stopped. Check the message above.
    pause
)
exit /b %status%
