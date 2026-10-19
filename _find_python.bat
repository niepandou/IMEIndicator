@echo off
rem Detect a usable Python. Sets PYW (windowless) and PYC (console).
rem Called by StartIMEIndicator.bat / StartWithLog.bat / DemoHighlight.bat.
rem
rem Why not just "py -3w": on some machines the py launcher cannot resolve the
rem installed Python (it prints "No suitable Python runtime found" and exits),
rem which looks exactly like "the program flashes and quits". So we actually
rem run a probe command to confirm each candidate before accepting it.
setlocal enabledelayedexpansion
set "PYW="
set "PYC="
set "PROBE=import sys,tkinter,ctypes,json;assert sys.version_info[0]==3 and sys.version_info[1]>=8"

rem 1) pythonw / python from PATH (skip the Microsoft Store alias stubs)
for /f "delims=" %%P in ('where pythonw 2^>nul') do (
    if not defined PYW (
        echo %%P | findstr /i /c:"WindowsApps" >nul
        if errorlevel 1 (
            "%%P" -c "%PROBE%" >nul 2>nul
            if not errorlevel 1 set "PYW=%%P"
        )
    )
)
for /f "delims=" %%P in ('where python 2^>nul') do (
    if not defined PYC (
        echo %%P | findstr /i /c:"WindowsApps" >nul
        if errorlevel 1 (
            "%%P" -c "%PROBE%" >nul 2>nul
            if not errorlevel 1 set "PYC=%%P"
        )
    )
)

rem 2) fallback: py launcher - ask it for the real exe path
if not defined PYW (
    where py >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%P in ('py -3w -c "import sys;print(sys.executable)" 2^>nul') do (
            if not defined PYW if exist "%%P" set "PYW=%%P"
        )
    )
)
if not defined PYC (
    where py >nul 2>nul
    if not errorlevel 1 (
        for /f "delims=" %%P in ('py -3 -c "import sys;print(sys.executable)" 2^>nul') do (
            if not defined PYC if exist "%%P" set "PYC=%%P"
        )
    )
)

rem 3) last resort: the default folder of the python.org installer
if not defined PYW (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined PYW if exist "%%~D\pythonw.exe" (
            "%%~D\pythonw.exe" -c "%PROBE%" >nul 2>nul
            if not errorlevel 1 set "PYW=%%~D\pythonw.exe"
        )
    )
)
if not defined PYC (
    for /d %%D in ("%LOCALAPPDATA%\Programs\Python\Python3*") do (
        if not defined PYC if exist "%%~D\python.exe" (
            "%%~D\python.exe" -c "%PROBE%" >nul 2>nul
            if not errorlevel 1 set "PYC=%%~D\python.exe"
        )
    )
)

endlocal & set "PYW=%PYW%" & set "PYC=%PYC%"
