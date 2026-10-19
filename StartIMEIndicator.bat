@echo off
rem IME Indicator - normal start (no console window, lives in the tray)
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if not defined PYW goto :nopy

start "" "%PYW%" "%~dp0ime_indicator.py"
echo Started. Look for the tray icon; the indicator pops up when you switch.
ping -n 3 127.0.0.1 >nul
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
