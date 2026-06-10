@echo off
title Building Debeed.exe...
color 0A
cls

:: Always run from THIS file's folder (fixes "gui.py does not exist"
:: when launched as admin or from a different directory)
cd /d "%~dp0"

echo.
echo  ====================================================
echo    Debeed Builder - Turning Python Script into EXE
echo  ====================================================
echo.
echo  Working folder: %CD%
echo.

:: =====================================================
:: STEP 1: Check Python is available
:: =====================================================
echo [1/5] Checking Python...
python --version 2>nul
if %errorlevel% neq 0 (
    color 0C
    echo.
    echo  ERROR: Python not found on this computer.
    echo  Install from: https://python.org/downloads
    echo  Make sure to tick "Add Python to PATH" during install!
    echo.
    pause
    exit /b 1
)
echo  Python found!

:: =====================================================
:: STEP 2: Install PyInstaller
:: =====================================================
echo.
echo [2/5] Installing PyInstaller...
python -m pip install pyinstaller --quiet --upgrade
if %errorlevel% neq 0 (
    color 0C
    echo.
    echo  ERROR: Failed to install PyInstaller.
    echo  Fix: Right-click build.bat and choose "Run as administrator"
    echo.
    pause
    exit /b 1
)
echo  PyInstaller ready!

:: =====================================================
:: STEP 3: Install/check project dependencies
:: =====================================================
echo.
echo [3/5] Checking project dependencies...
python -m pip install playwright --quiet
if %errorlevel% neq 0 (
    echo  WARNING: Could not verify playwright. Continuing anyway...
)
echo  Dependencies checked!

:: =====================================================
:: STEP 4: Build the .exe
:: =====================================================
echo.
echo [4/5] Building Debeed.exe (the GUI app)...
echo       This will take 3-7 minutes. Do NOT close this window!
echo.

:: Check if launch_chrome.bat exists before adding it
set ADD_DATA_FLAG=
if exist "launch_chrome.bat" (
    set ADD_DATA_FLAG=--add-data "launch_chrome.bat;."
)

python -m PyInstaller ^
    --onefile ^
    --windowed ^
    --name "Debeed" ^
    --collect-all playwright ^
    --hidden-import tkinter ^
    --hidden-import tkinter.ttk ^
    --hidden-import tkinter.messagebox ^
    --hidden-import tkinter.filedialog ^
    --hidden-import tkinter.scrolledtext ^
    --hidden-import playwright ^
    --hidden-import playwright.sync_api ^
    --hidden-import playwright._impl._driver ^
    --hidden-import playwright._impl._api_types ^
    --hidden-import csv ^
    --hidden-import queue ^
    --hidden-import threading ^
    --hidden-import io ^
    --hidden-import shutil ^
    --hidden-import os ^
    --hidden-import sys ^
    --hidden-import subprocess ^
    --hidden-import time ^
    %ADD_DATA_FLAG% ^
    gui.py

if %errorlevel% neq 0 (
    color 0C
    echo.
    echo  ====================================================
    echo    BUILD FAILED - Read the errors above
    echo  ====================================================
    echo.
    echo  Most common fixes:
    echo.
    echo  1. Test your app runs normally first:
    echo        python gui.py
    echo.
    echo  2. If you see "ModuleNotFoundError: No module named X":
    echo        Add this line inside build.bat:
    echo        --hidden-import X
    echo.
    echo  3. If build keeps failing, try --onedir instead of --onefile
    echo     (creates a folder instead of 1 file, but more reliable)
    echo     Change line in this file:   --onefile
    echo     to:                         --onedir
    echo.
    echo  4. Try running this file as Administrator
    echo.
    pause
    exit /b 1
)

:: =====================================================
:: STEP 5: Clean up temporary build files
:: =====================================================
echo.
echo [5/5] Cleaning up temp files...
if exist "build" rmdir /s /q "build"
if exist "Debeed.spec" del /q "Debeed.spec"

echo.
color 0A
echo  ====================================================
echo    SUCCESS! Your .exe is ready.
echo  ====================================================
echo.
echo    File location: dist\Debeed.exe
echo.
echo    This opens as the Debeed window (your GUI).
echo    NO black console appears - all output shows inside
echo    the app's Activity Log. First launch takes 5-10
echo    seconds to unpack - this is normal.
echo.
echo    Share ONLY the Debeed.exe file with users.
echo    They do NOT need Python installed.
echo.
echo    IMPORTANT: Users must have Google Chrome installed.
echo    (Most people already do!)
echo.
pause
