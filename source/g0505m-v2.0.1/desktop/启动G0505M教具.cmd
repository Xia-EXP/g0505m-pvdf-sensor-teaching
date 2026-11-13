@echo off
chcp 65001 >nul
rem 作用：路径使用脚本所在目录，解压到中文或含空格目录后也能启动。
if not exist "%~dp0G0505MTeaching.exe" (
    echo 未找到程序，请先完整解压便携 ZIP。
    pause
    exit /b 1
)
if not exist "%~dp0_internal\python312.dll" (
    echo 运行库缺失，请保持 _internal 文件夹与 EXE 在同一目录。
    pause
    exit /b 1
)
start "" "%~dp0G0505MTeaching.exe"
