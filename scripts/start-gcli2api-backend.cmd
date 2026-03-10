@echo off
chcp 65001 >nul
title gcli2api Backend (7862)

cd /d "F:\antigravity2api\gcli2api"

set PORT=7862
set GATEWAY_ENABLED=false
set USE_NEW_GATEWAY=true
set HISTORY_CACHE_BACKEND=lru
set HISTORY_CACHE_MAX_SIZE=1000
set HISTORY_CACHE_STRATEGY=smart
set HISTORY_CACHE_RECENT_COUNT=10
set SCID_ENABLED=true

if not exist ".venv\Scripts\activate.bat" (
    echo [ERROR] gcli2api virtual environment not found!
    echo [INFO] Please run: cd gcli2api ^& uv venv ^& uv sync
    pause
    exit /b 1
)

call .venv\Scripts\activate.bat
python web.py
