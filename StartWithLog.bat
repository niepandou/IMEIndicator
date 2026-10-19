@echo off
rem IME Indicator - start with event logging (writes events.log)
cd /d "%~dp0"
call "%~dp0_find_python.bat"
if not defined PYW goto :nopy

echo Logging to events.log. When done, quit from the tray icon.
start "" "%PYW%" "%~dp0ime_indicator.py" --log
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
