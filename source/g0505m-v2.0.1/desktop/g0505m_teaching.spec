# -*- mode: python ; coding: utf-8 -*-
"""独立 G0505M Windows 便携构建，打包 Qt 与串口运行时。"""

from pathlib import Path

analysis = Analysis(
    ["run_g0505m.py"], pathex=["src"], binaries=[],
    # 作用：强脑 Revo2 模型（URDF/STL/许可证）按包内相对路径打入 _internal，三维手运行时从这里读取。
    datas=[("src/grip_teaching/assets/revo2", "grip_teaching/assets/revo2")],
    hiddenimports=["OpenGL.platform.win32", "OpenGL.arrays.numpymodule"],
    hookspath=[], hooksconfig={}, runtime_hooks=[], excludes=[], noarchive=False,
)  # 作用：只追踪新程序入口，不把 PVDF 的机器人控制路径混入新教具。
# 作用：Qt 6.11 调用 Windows 系统 ICU 的无版本符号；PATH 中 Poppler 的同名库会覆盖系统库并导致启动失败。
analysis.binaries = [
    entry for entry in analysis.binaries
    if Path(entry[0]).name.lower() != "icuuc.dll"
    and not (Path(entry[0]).name.lower().startswith("icudt") and entry[0].lower().endswith(".dll"))
]
archive = PYZ(analysis.pure)  # 作用：封装纯 Python 模块供便携解释器加载。
executable = EXE(
    archive, analysis.scripts, [], exclude_binaries=True, name="G0505MTeaching",
    debug=False, bootloader_ignore_signals=False, strip=False, upx=False,
    console=False, disable_windowed_traceback=False, argv_emulation=False,
)  # 作用：使用无控制台窗口入口，整目录复制后即可双击。
distribution = COLLECT(
    executable, analysis.binaries, analysis.datas, strip=False, upx=False,
    name="G0505M手掌受力教具",
)  # 作用：Qt DLL 必须随 _internal 一起分发，不能只复制 EXE。
