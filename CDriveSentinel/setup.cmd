@echo off
rem Double-click launcher: elevates and runs install.ps1.
rem Right-click -> "Run as administrator" is equivalent.
setlocal
set "PS1=%~dp0install.ps1"
if not exist "%PS1%" (
    echo install.ps1 not found next to this file.
    pause
    exit /b 1
)
powershell -NoProfile -ExecutionPolicy Bypass -Command ^
  "Start-Process -FilePath 'powershell.exe' -ArgumentList '-NoProfile','-ExecutionPolicy','Bypass','-NoExit','-File','\"%PS1%\"' -Verb RunAs"
endlocal
