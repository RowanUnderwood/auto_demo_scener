@echo off
cd /d "%~dp0"
if not exist "static\three\three.module.js" (
    echo Downloading three.js vendor files...
    python download_vendor.py
    if errorlevel 1 (
        echo ERROR: Failed to download three.js. Check your internet connection.
        pause
        exit /b 1
    )
)
python app.py --launch
