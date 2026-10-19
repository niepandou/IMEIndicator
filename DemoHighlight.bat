@echo off
rem IME Indicator - preview the indicator states
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if not defined PYC goto :nopy

"%PYC%" "%~dp0ime_indicator.py" --demo
echo.
echo Done.
pause
exit /b 0

:nopy
echo.
echo  [!] No usable Python found (needs 3.8+ with tcl/tk).
echo      Install from https://www.python.org/downloads/ and try again.
echo      If it is already installed, re-run the installer and tick
echo      "Add python.exe to PATH".
echo.
pause
exit /b 1
