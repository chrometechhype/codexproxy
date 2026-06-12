@echo off
setlocal EnableDelayedExpansion

set "SCRIPT_DIR=%~dp0"
cd /d "%SCRIPT_DIR%"

if not "%PORT%"=="" (
    echo Using PORT=!PORT!
) else (
    set "PORT=9090"
    echo PORT not set, defaulting to !PORT!.
)

echo Starting CodexProxy on http://127.0.0.1:!PORT! (admin at /admin) ...
echo Press Ctrl+C to stop.

uv run cdx-server

echo.
echo CodexProxy exited with code %errorlevel%.
pause >nul

endlocal
