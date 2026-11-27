@echo off
setlocal EnableExtensions
chcp 65001 >nul 2>&1

set "PROTO_DIR=%~dp0"
set "VENV_PY=%PROTO_DIR%.venv\Scripts\python.exe"

if not exist "%VENV_PY%" (
    echo.
    echo [ERROR] Python virtual environment not found at:
    echo         "%VENV_PY%"
    echo.
    echo Create it once from this folder with:
    echo         python -m venv .venv
    echo.
    pause
    exit /b 1
)

pushd "%PROTO_DIR%" >nul

:MENU
cls
echo ========================================================================
echo   Circadian Desk -- Week-1 Local Desktop Beta (Pure Track A v6.1)
echo   Zero Admin Rights ^| Zero Registry Edits ^| Zero Cloud ^| Zero Recording
echo ========================================================================
echo.
echo   [1] 3-Second Monitor Safety Check
echo       Preview 45%% @ 3000K for 3s, restore original display, print PASS/FAIL
echo.
echo   [2] 30-Second First-Run Setup
echo       Detect display hardware, check Windows Night Light, save schedule/lights
echo.
echo   [3] Start Open-Loop Desktop Beta (Tray + Auto-Pause + Braun UI + 7-Day Log)
echo       System Tray icon, Photoshop/Games Auto-Pause, http://127.0.0.1:8765, CSV log
echo.
echo   [4] Run Offline Demo Simulation
echo       Zero-hardware day-to-evening showcase in terminal (display untouched)
echo.
echo   [0] Exit
echo.
set "CHOICE="
set /p "CHOICE=Select an option [1-4, 0]: "

if "%CHOICE%"=="1" goto RUN_SAFETY
if "%CHOICE%"=="2" goto RUN_SETUP
if "%CHOICE%"=="3" goto RUN_BETA
if "%CHOICE%"=="4" goto RUN_DEMO
if "%CHOICE%"=="0" goto END

echo.
echo Invalid choice. Please enter 1, 2, 3, 4, or 0.
timeout /t 2 >nul
goto MENU

:RUN_SAFETY
echo.
"%VENV_PY%" -m orchestrator.daemon --monitor --monitor-safety-check --safety-hold-s 3.0
echo.
pause
goto MENU

:RUN_SETUP
echo.
"%VENV_PY%" -m orchestrator.daemon --setup
echo.
pause
goto MENU

:RUN_BETA
echo.
echo Starting Open-Loop Desktop Beta + System Tray + Games/Photoshop Auto-Pause + Braun UI...
echo Telemetry log: "%USERPROFILE%\.circadian_orchestrator\desk_log.csv"
echo Right-click the System Tray icon or press Ctrl+C anytime to restore display cleanly.
echo.
"%VENV_PY%" -m orchestrator.daemon --config --open-loop --monitor --web-ui --tray --auto-pause --open-browser --log-csv "%USERPROFILE%\.circadian_orchestrator\desk_log.csv"
echo.
pause
goto MENU

:RUN_DEMO
echo.
"%VENV_PY%" -m orchestrator.daemon --simulate day_to_evening_demo --light mock --ticks 8 --interval 0.5
echo.
pause
goto MENU

:END
popd >nul
endlocal
exit /b 0
