"""从发布ZIP解压到中文含空格目录并实际运行EXE，验证自包含运行时。"""

import json
import hashlib
import os
import subprocess
import time
import zipfile
from pathlib import Path

from PySide6.QtGui import QImage


def release_version() -> str:
    """读取 grip_teaching.__version__ 的文本值；不导入包，避免拉起 Qt 或依赖本机安装。"""
    init = Path(__file__).resolve().parents[1] / "src" / "grip_teaching" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError("grip_teaching/__init__.py 缺少 __version__")


def main() -> None:
    """用明确示例模式启动拷贝后的EXE，不抢占正在实测的COM3。"""
    workspace = Path(__file__).resolve().parents[2]
    archive_path = workspace / "release" / f"G0505M手掌受力教具-Windows-x64-v{release_version()}.zip"
    destination = workspace / "tmp" / f"便携校验 中文 空格 {time.time_ns()}"
    destination.mkdir(parents=True)
    with zipfile.ZipFile(archive_path) as archive:
        assert archive.testzip() is None
        assert all(".." not in Path(name).parts for name in archive.namelist())
        archive.extractall(destination)
        entries = len(archive.namelist())
    portable = destination / "G0505M手掌受力教具"
    assert portable.joinpath("_internal", "python312.dll").exists()
    assert portable.joinpath("启动G0505M教具.cmd").exists()
    assert portable.joinpath("G0505M手掌受力教具使用说明书-v2.0.pdf").exists()
    assert portable.joinpath("G0505M桌角卡-v2.0.pdf").exists()
    assert not portable.joinpath("G0505M手掌受力教具图文操作说明.docx").exists()
    # 作用：系统ICU由受支持的Windows提供；不能把其他工具的同名版本化ICU打进来覆盖它。
    assert not portable.joinpath("_internal", "icuuc.dll").exists()
    environment = os.environ.copy()
    environment.pop("PYTHONPATH", None)
    environment.pop("PYTHONHOME", None)
    for key in ("QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH", "QML2_IMPORT_PATH"):
        environment.pop(key, None)
    system_root = Path(environment.get("SystemRoot", "C:/Windows"))
    environment["PATH"] = os.pathsep.join(str(path) for path in (system_root / "System32", system_root))
    platforms = {}
    for platform in ("offscreen", "windows"):
        report_path = destination / f"smoke-{platform}.json"
        screenshot = destination / f"smoke-{platform}.png"
        environment["QT_QPA_PLATFORM"] = platform
        result = subprocess.run([str(portable / "G0505MTeaching.exe"), "--smoke-demo", "--smoke-ms", "1800",
                                 "--smoke-report", str(report_path), "--smoke-image", str(screenshot)],
                                cwd=destination, env=environment, capture_output=True, text=True, timeout=30)
        assert result.returncode == 0, result.stderr
        report = json.loads(report_path.read_text(encoding="utf-8"))
        assert report["visible"] and report["mode"] == "demo" and report["valid_frames"] > 10
        image = QImage(str(screenshot))
        assert not image.isNull() and image.width() >= 1024
        colors = {image.pixelColor(x, y).name() for x in range(0, image.width(), 10) for y in range(0, image.height(), 10)}
        assert len(colors) > 30
        platforms[platform] = {"exe_exit_code": result.returncode, "valid_frames": report["valid_frames"],
                               "canvas_colors": len(colors)}
    with portable.joinpath("G0505MTeaching.exe").open("rb") as stream:
        exe_digest = hashlib.file_digest(stream, "sha256").hexdigest()
    report.update({"zip_entries": entries, "extracted_to": str(destination), "canvas_colors": len(colors),
                   "exe_exit_code": result.returncode, "source_environment_removed": True,
                   "system_only_path": True, "platforms": platforms, "exe_sha256": exe_digest})
    workspace.joinpath("tmp", "g0505m-portable-check.json").write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
