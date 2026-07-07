@echo off
setlocal enabledelayedexpansion

echo ============================================
echo   Signal Audio Visualizer - Build .exe
echo ============================================
echo.
echo This creates a standalone SignalVisualizer.exe that runs without
echo Python, pip, or a virtual environment installed on the target PC.
echo.

set SCRIPT_DIR=%~dp0
cd /d "%SCRIPT_DIR%"

if not exist "audio_visualizer.py" (
    echo [ERROR] audio_visualizer.py was not found in this folder.
    pause
    exit /b 1
)

if not exist "venv" (
    echo [ERROR] No virtual environment found. Run setup_and_run.bat at least
    echo once first, so dependencies are installed, then run this script.
    pause
    exit /b 1
)

call venv\Scripts\activate.bat
if errorlevel 1 (
    echo [ERROR] Failed to activate the virtual environment.
    pause
    exit /b 1
)

echo Installing PyInstaller...
pip install --no-cache-dir pyinstaller
if errorlevel 1 (
    echo [ERROR] Failed to install PyInstaller.
    pause
    exit /b 1
)
echo.

echo Cleaning up any previous build...
if exist "build" rmdir /s /q "build"
if exist "dist" rmdir /s /q "dist"
if exist "SignalVisualizer.spec" del /q "SignalVisualizer.spec"
echo.

echo Building SignalVisualizer.exe (this can take a minute or two)...
echo.
pyinstaller --noconfirm --onefile --windowed ^
    --name "SignalVisualizer" ^
    --collect-all pyaudiowpatch ^
    --collect-all pygame ^
    --collect-all OpenGL ^
    audio_visualizer.py

if errorlevel 1 (
    echo.
    echo [ERROR] The build failed. Scroll up for the PyInstaller error output.
    pause
    exit /b 1
)

echo.
echo ============================================
echo   Build complete!
echo ============================================
echo.
echo Your standalone app is at:
echo   %SCRIPT_DIR%dist\SignalVisualizer.exe
echo.
echo You can copy just that one .exe file anywhere and run it directly -
echo no Python installation needed on the target machine.
echo.
echo Note: it will still create a "logs" folder and "visualizer_config.json"
echo next to wherever you run the .exe from, same as the Python version.
echo.
pause
