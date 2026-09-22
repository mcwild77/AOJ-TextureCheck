@echo off
rem Builds dist\TextureChecker.exe. Run on Windows with Python 3.10+ installed.
python -m pip install -r requirements-build.txt || exit /b 1
python -m PyInstaller --onefile --windowed --name TextureChecker run.py || exit /b 1
echo.
echo Built dist\TextureChecker.exe
