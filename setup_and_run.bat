@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Signal Audio Visualizer - Setup and Run
echo ============================================
echo.

REM --- move into the folder this script lives in ---
set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

REM --- make sure the visualizer script is actually here before doing anything else ---
if not exist "audio_visualizer.py" (
    echo [ERROR] audio_visualizer.py was not found in this folder:
    echo   %SCRIPT_DIR%
    echo Place this setup script in the same folder as audio_visualizer.py and run it again.
    echo.
    pause
    exit /b 1
)

REM --- locate a working python interpreter ---
REM some installs (e.g. scoop) put a working "python" on PATH but no
REM registry entries for the "py" launcher, so we verify each candidate
REM actually runs rather than just checking if the command exists.

set PY_LAUNCHER=

python --version >nul 2>nul
if not errorlevel 1 (
    set PY_LAUNCHER=python
)

if "!PY_LAUNCHER!"=="" (
    py --version >nul 2>nul
    if not errorlevel 1 (
        set PY_LAUNCHER=py
    )
)

if "!PY_LAUNCHER!"=="" (
    py -3 --version >nul 2>nul
    if not errorlevel 1 (
        set PY_LAUNCHER=py -3
    )
)

if "!PY_LAUNCHER!"=="" (
    echo [ERROR] No working Python installation was found.
    echo "python" and "py" were both tried and neither runs on this system.
    echo Install Python from https://www.python.org/downloads/
    echo IMPORTANT: check "Add python.exe to PATH" during install, then run this script again.
    echo.
    pause
    exit /b 1
)

echo Using Python launcher: !PY_LAUNCHER!
!PY_LAUNCHER! --version
echo.

REM --- create a dedicated virtual environment so this never fights with other projects ---
if not exist "venv" (
    echo Creating a virtual environment in .\venv ...
    !PY_LAUNCHER! -m venv venv
    if errorlevel 1 (
        echo [ERROR] Failed to create the virtual environment.
        pause
        exit /b 1
    )
) else (
    echo Virtual environment already exists, reusing it.
)
echo.

call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate the virtual environment.
    pause
    exit /b 1
)

echo Upgrading pip...
python -m pip install --upgrade pip >nul 2>nul
echo.

echo Installing pinned dependencies from requirements.txt...
if exist "requirements.txt" (
    pip install --no-cache-dir -r requirements.txt
    if errorlevel 1 (
        echo requirements.txt install failed - trying pygame ^(plain^) as a fallback for pygame-ce...
        pip install --no-cache-dir numpy pyaudiowpatch pygame
        if errorlevel 1 (
            echo [ERROR] Could not install a working set of dependencies for this Python version.
            echo Try installing a different Python version from python.org ^(3.11-3.13 recommended^)
            echo and run this script again.
            pause
            exit /b 1
        )
    )
) else (
    echo requirements.txt not found next to this script - installing latest versions instead.
    pip install --no-cache-dir numpy pyaudiowpatch pygame-ce
    if errorlevel 1 (
        echo pygame-ce failed to install, falling back to plain pygame...
        pip install --no-cache-dir pygame
        if errorlevel 1 (
            echo [ERROR] Could not install a working pygame build for this Python version.
            pause
            exit /b 1
        )
    )
)
echo.

echo Verifying that everything imports correctly...
python -c "import numpy, pygame, pyaudiowpatch; print('All dependencies OK:'); print('  numpy  ', numpy.__version__); print('  pygame ', pygame.version.ver); print('  pyaudiowpatch OK')"
if errorlevel 1 (
    echo [ERROR] One or more libraries failed to import. See the error above.
    pause
    exit /b 1
)
echo.

echo ============================================
echo   Setup complete. Launching the visualizer...
echo ============================================
echo.
python audio_visualizer.py

echo.
echo The visualizer window was closed.
pause
