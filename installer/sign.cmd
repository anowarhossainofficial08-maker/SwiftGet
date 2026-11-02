@echo off
REM Signs one file. Used by the GitHub build (and by Inno Setup for the installer/uninstaller).
REM Needs the environment variables SIGNTOOL, SIGN_PFX and SIGN_PASSWORD.
"%SIGNTOOL%" sign /f "%SIGN_PFX%" /p "%SIGN_PASSWORD%" /fd SHA256 /tr http://timestamp.digicert.com /td SHA256 /d SwiftGet %1
if not errorlevel 1 exit /b 0
REM timestamp server unreachable: sign without a timestamp
"%SIGNTOOL%" sign /f "%SIGN_PFX%" /p "%SIGN_PASSWORD%" /fd SHA256 /d SwiftGet %1
