@echo off
rem Build the PyVNC Viewer for Windows. Run from anywhere: scripts\build_client_windows.bat [--installer]
rem   Output: dist\PyVNC Viewer\PyVNC Viewer.exe  (and dist\PyVNCViewer-Setup.exe with --installer)
setlocal
cd /d "%~dp0\.."

if not exist .venv (
    py -3 -m venv .venv || python -m venv .venv || goto :error
)
call .venv\Scripts\activate.bat || goto :error
python -m pip install --upgrade pip
python -m pip install -r requirements-client.txt pyinstaller || goto :error

if not exist assets\PyVNC.ico python scripts\make_icons.py || goto :error

pyinstaller --noconfirm --clean --log-level WARN ^
    --onedir --windowed ^
    --name "PyVNC Viewer" ^
    --icon "%CD%\assets\PyVNC.ico" ^
    --add-data "%CD%\assets\PyVNC.ico;assets" ^
    --add-data "%CD%\assets\PyVNC.png;assets" ^
    --specpath build ^
    --paths "%CD%" ^
    --hidden-import keyring.backends.Windows ^
    --collect-submodules client ^
    --collect-submodules common ^
    "%CD%\run_client.py" || goto :error

echo.
echo Built "dist\PyVNC Viewer\PyVNC Viewer.exe"

if /i not "%~1"=="--installer" exit /b 0

rem Kept outside parenthesised blocks: the ")" in "Program Files (x86)" would end the block.
set "ISCC=%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" set "ISCC=%LOCALAPPDATA%\Programs\Inno Setup 6\ISCC.exe"
if not exist "%ISCC%" goto :no_inno
"%ISCC%" scripts\installer.iss || goto :error
echo Built dist\PyVNCViewer-Setup.exe
exit /b 0

:no_inno
echo Inno Setup 6 not found. Install it from https://jrsoftware.org/isinfo.php and re-run.
exit /b 1

:error
echo Build failed.
exit /b 1
