@echo off
REM Build SwiftGet and its installer on your own Windows PC.
REM Needs: Python 3.10+ (python.org) and, for the installer, Inno Setup 6 (jrsoftware.org).
cd /d "%~dp0"
python -m pip install --upgrade pyinstaller -r requirements.txt || goto :error
pyinstaller --noconfirm --onedir --windowed --name SwiftGet --icon assets\swiftget.ico --add-data "assets\swiftget.ico;assets" --collect-data sv_ttk --hidden-import pystray._win32 run.py || goto :error
set ISCC="%ProgramFiles(x86)%\Inno Setup 6\ISCC.exe"
if exist %ISCC% (
  %ISCC% installer\swiftget.iss || goto :error
  echo.
  echo Done!  Installer: %~dp0dist-installer\SwiftGet-Setup.exe
) else (
  echo.
  echo App built in %~dp0dist\SwiftGet\  ^(install Inno Setup 6 to also build SwiftGet-Setup.exe^)
)
pause
exit /b 0
:error
echo Build failed.
pause
exit /b 1
