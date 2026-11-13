@echo off
rem Launch from this folder so copied paths and spaces are supported.
if not exist "%~dp0G0505MTeaching.exe" (
    echo G0505MTeaching.exe is missing. Extract the complete portable ZIP first.
    pause
    exit /b 1
)
if not exist "%~dp0_internal\python312.dll" (
    echo Runtime is missing. Keep _internal beside the EXE.
    pause
    exit /b 1
)
start "" "%~dp0G0505MTeaching.exe"
