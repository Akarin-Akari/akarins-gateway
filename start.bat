@echo off
chcp 65001 >nul 2>&1
title Akarin's Gateway

echo ============================================================
echo   Akarin's Gateway - Startup
echo ============================================================
echo.

:: Check Python
where python >nul 2>&1
if errorlevel 1 (
    echo [ERROR] Python not found in PATH!
    echo Please install Python 3.11+ and add it to PATH.
    pause
    exit /b 1
)

:: Show Python version
for /f "tokens=*" %%i in ('python --version 2^>^&1') do echo   Python: %%i

:: Check if venv exists and activate
if exist ".venv\Scripts\activate.bat" (
    echo   Activating virtual environment...
    call ".venv\Scripts\activate.bat"
) else if exist "venv\Scripts\activate.bat" (
    echo   Activating virtual environment...
    call "venv\Scripts\activate.bat"
) else (
    echo   [WARN] No virtual environment found. Using system Python.
    echo   Tip: python -m venv .venv ^&^& .venv\Scripts\activate ^&^& pip install -e .
)

:: Load .env if exists
if exist ".env" (
    echo   Loading .env configuration...
) else if exist ".env.example" (
    echo   [WARN] No .env found. Copy .env.example to .env for custom config.
)

echo.
echo   Starting gateway...
echo ============================================================
echo.

python -m akarins_gateway

if errorlevel 1 (
    echo.
    echo [ERROR] Gateway exited with error code %errorlevel%
    pause
)
