@echo off
rem Builds dist\TextureChecker.exe. Run on Windows with Python 3.10+ installed.
rem Builds inside a private venv (build\venv) so only requirements.txt gets bundled,
rem not whatever else is installed in the main Python (scipy, OpenCV, pandas...).
setlocal
cd /d "%~dp0"

if not exist build\venv\Scripts\python.exe (
    echo Creating build environment in build\venv ...
    python -m venv build\venv || goto :fail
)
set PY=build\venv\Scripts\python.exe

echo Installing build requirements ...
%PY% -m pip install --quiet --disable-pip-version-check -r requirements-build.txt || goto :fail

echo Building TextureChecker.exe ...
%PY% -m PyInstaller --noconfirm --log-level WARN --onefile --windowed --name TextureChecker run.py || goto :fail

echo.
echo Built dist\TextureChecker.exe
pause
exit /b 0

:fail
echo.
echo BUILD FAILED - see the messages above.
pause
exit /b 1
