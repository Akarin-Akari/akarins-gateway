@echo off
REM Ensure running under cmd.exe (PowerShell invocation breaks batch parsing)
if not defined CMDCMDLINE (
    cmd /c "%~f0" %*
    exit /b
)
chcp 65001 >nul 2>&1

REM ====================================================
REM  Akarin Cluster Service Launcher
REM  akarins-gateway (7861) + gcli2api backend (7862)
REM  Author: UFO Meow (FuFu)
REM  Version: 2.0 - Matched to working reference
REM ====================================================

REM Check if already running inside Windows Terminal
if defined WT_SESSION goto :main_logic

REM Not in Windows Terminal, relaunch with wt
where wt >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Windows Terminal not found!
    echo [INFO] Please install from Microsoft Store
    pause
    exit /b 1
)

REM Relaunch this script inside Windows Terminal
wt -w AkarinCluster new-tab --title Launcher -d "F:\antigravity2api\akarins-gateway" cmd /k "chcp 65001 >nul & call \"%~f0\""
exit /b 0

:main_logic
setlocal enabledelayedexpansion
title Akarin Cluster Service - All Services Launcher

REM Hardcode the base directory to avoid path issues
set "BASE=F:\antigravity2api"
cd /d "%BASE%\akarins-gateway"

REM Log file setup
set "LOG_DIR=%BASE%\akarins-gateway\logs"
if not exist "%LOG_DIR%" mkdir "%LOG_DIR%"
set "LOG_FILE=%LOG_DIR%\launcher_%date:~0,4%%date:~5,2%%date:~8,2%_%time:~0,2%%time:~3,2%%time:~6,2%.log"
set "LOG_FILE=%LOG_FILE: =0%"

call :log "==================================================="
call :log "  Akarin Cluster Service Launcher Started"
call :log "==================================================="

echo ====================================================
echo   Akarin Cluster Service Launcher
echo   akarins-gateway (7861) + gcli2api backend (7862)
echo   Parallel Start Mode
echo ====================================================
echo.

REM Load environment variables
call :log "Loading environment variables..."
if exist "%BASE%\gcli2api\.env.launcher" (
    for /f "usebackq tokens=1,* delims==" %%a in ("%BASE%\gcli2api\.env.launcher") do (
        set "line=%%a"
        if not "!line:~0,1!"=="#" (
            if not "%%a"=="" set "%%a=%%b"
        )
    )
    call :log "Loaded .env.launcher"
) else (
    call :log "No .env.launcher found, using defaults"
    set USE_NEW_GATEWAY=true
    set HISTORY_CACHE_BACKEND=lru
    set HISTORY_CACHE_MAX_SIZE=1000
    set HISTORY_CACHE_STRATEGY=smart
    set HISTORY_CACHE_RECENT_COUNT=10
    set SCID_ENABLED=true
    set LOG_LEVEL=DEBUG
)

echo [INFO] USE_NEW_GATEWAY=%USE_NEW_GATEWAY%
echo [INFO] LOG_LEVEL=%LOG_LEVEL%
echo.

REM ====================================================
REM  Step 1: Stop all services
REM ====================================================
call :log "Stopping all services..."
echo [INFO] Stopping all services...

REM Smart stop with timeout fallback
python "%BASE%\gcli2api\stop_services.py" bun.exe ngrok.exe openlist.exe zerogravity.exe zeroclaw.exe

REM Kill processes on port 7861 (akarins-gateway) and 7862 (gcli2api backend)
call :log "Killing processes on port 7861 and 7862..."
python "%BASE%\gcli2api\kill_port.py" 7861
python "%BASE%\gcli2api\kill_port.py" 7862

REM Clean SQLite lock files
del /F /Q "%BASE%\gcli2api\data\*.db-wal" 2>nul
del /F /Q "%BASE%\gcli2api\data\*.db-shm" 2>nul

REM Wait for taskkill to complete
timeout /t 1 /nobreak >nul

call :log "All services stopped"
echo [INFO] All services stopped
echo.

REM ====================================================
REM  Step 2: Start services in parallel
REM ====================================================
call :log "Starting services..."
echo [INFO] Starting services...
echo.

REM Service 1: ZeroGravity MITM Proxy (port 8880+)
if not exist "%BASE%\zerogravity-src\target\release\zerogravity.exe" (
    echo [1/7] ZeroGravity - SKIPPED
    call :log "ZeroGravity - SKIPPED"
) else (
    echo [1/7] Starting ZeroGravity on port 8880...
    call :log "Starting ZeroGravity"
    wt -w AkarinCluster new-tab --title "ZeroGravity" -d "%BASE%\gcli2api\scripts" cmd /k "call start-zerogravity.cmd"
)

REM Service 2: Copilot API
if not exist "%USERPROFILE%\.local\share\copilot-api\github_token" (
    echo [2/7] Copilot API - SKIPPED
    call :log "Copilot API - SKIPPED"
) else (
    echo [2/7] Starting Copilot API on port 8141...
    call :log "Starting Copilot API"
    wt -w AkarinCluster new-tab --title "Copilot-API" -d "%BASE%\copilot-api" cmd /k "chcp 65001 >nul & title Copilot API & %USERPROFILE%\.bun\bin\bun.exe run ./src/main.ts start --port 8141 --verbose"
)

REM Service 3: ngrok Tunnel (tunneling akarins-gateway on 7861)
if not exist "%BASE%\ngrok_temp\ngrok.exe" (
    echo [3/7] ngrok - SKIPPED
    call :log "ngrok - SKIPPED"
) else (
    echo [3/7] Starting ngrok Tunnel on port 7861...
    call :log "Starting ngrok"
    wt -w AkarinCluster new-tab --title "ngrok" -d "%BASE%\ngrok_temp" cmd /k "chcp 65001 >nul & title ngrok Tunnel & ngrok.exe http 7861"
)

REM Service 4: OpenList
if not exist "F:\Program Files\openlist\openlist.exe" (
    echo [4/7] OpenList - SKIPPED
    call :log "OpenList - SKIPPED"
) else (
    echo [4/7] Starting OpenList on port 5244...
    call :log "Starting OpenList"
    wt -w AkarinCluster new-tab --title "OpenList" -d "F:\Program Files\openlist" cmd /k "chcp 65001 >nul & title OpenList & openlist.exe server"
)

REM Service 5: ZeroClaw Agent Runtime (port 42617)
if not exist "F:\zeroclaw\bin\zeroclaw.exe" (
    echo [5/7] ZeroClaw - SKIPPED
    call :log "ZeroClaw - SKIPPED"
) else (
    echo [5/7] Starting ZeroClaw daemon on port 42617...
    call :log "Starting ZeroClaw"
    wt -w AkarinCluster new-tab --title "ZeroClaw" -d "F:\zeroclaw" cmd /k "chcp 65001 >nul & title ZeroClaw Agent Runtime & call zeroclaw.cmd"
)

REM Service 6: gcli2api as Antigravity Backend (port 7862)
if not exist "%BASE%\gcli2api\web.py" (
    echo [6/7] gcli2api - SKIPPED
    echo [WARN] gcli2api not found, Antigravity backend will not be available.
    call :log "gcli2api - SKIPPED"
) else (
    echo [6/7] Starting gcli2api as Antigravity Backend on port 7862...
    call :log "Starting gcli2api backend on port 7862"
    wt -w AkarinCluster new-tab --title "gcli2api-7862" -d "%BASE%\akarins-gateway\scripts" cmd /k "call start-gcli2api-backend.cmd"
)

echo.

REM ====================================================
REM  Step 3: Show endpoints
REM ====================================================
echo ========================================
echo   Akarin Cluster Endpoints:
echo ========================================
echo.
echo   akarins-gateway (7861) - MAIN GATEWAY:
echo     http://127.0.0.1:7861/v1/chat/completions
echo     http://127.0.0.1:7861/v1/messages
echo     http://127.0.0.1:7861/v1/models
echo.
echo   gcli2api backend (7862) - ANTIGRAVITY:
echo     http://127.0.0.1:7862/antigravity/v1/messages
echo     http://127.0.0.1:7862  ^(Web Panel^)
echo.
echo   ZeroGravity:
echo     http://127.0.0.1:8880/v1 ^(auto port fallback 8880-8889^)
echo.
echo   copilot-api:
echo     http://127.0.0.1:8141/v1
echo.
echo   ngrok:
echo     Check ngrok tab for HTTPS URL
echo.
echo   OpenList:
echo     http://127.0.0.1:5244
echo.
echo   ZeroClaw:
echo     http://127.0.0.1:42617 ^(daemon gateway^)
echo.
echo ========================================
echo   Switch tabs with Ctrl+Tab
echo ========================================
echo.

REM ====================================================
REM  Step 4: Start akarins-gateway (blocking, must be last)
REM ====================================================
echo [7/7] Starting akarins-gateway on port 7861...
call :log "Starting akarins-gateway"

cd /d "%BASE%\akarins-gateway"

REM Set akarins-gateway environment variables
set PORT=7861
set ANTIGRAVITY_ENABLED=true
set ANTIGRAVITY_BASE_URL=http://127.0.0.1:7862/antigravity/v1

if not exist ".venv\Scripts\activate.bat" (
    echo [WARN] Virtual environment not found!
    echo [INFO] Please run: uv venv and uv sync
    call :log "ERROR: Virtual environment not found"
    pause
    exit /b 1
)

echo [INFO] Activating virtual environment...
call .venv\Scripts\activate.bat

echo [INFO] Checking akarins_gateway module...
if not exist "akarins_gateway\__main__.py" (
    echo [ERROR] akarins_gateway module not found!
    call :log "ERROR: akarins_gateway module not found"
    pause
    exit /b 1
)

call :log "Starting akarins-gateway server..."
echo [INFO] Starting akarins-gateway...
echo.
python -m akarins_gateway

echo.
call :log "Server stopped"
echo [INFO] Server stopped.
pause
exit /b 0

REM ====================================================
REM  Log function
REM ====================================================
:log
echo [%date% %time%] %~1 >> "%LOG_FILE%"
goto :eof
