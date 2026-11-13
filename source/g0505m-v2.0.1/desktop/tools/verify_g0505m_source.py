"""从独立源码ZIP验证运行，不读取本机项目的editable安装或源码缓存。"""

import json
import os
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path


def release_version() -> str:
    """读取 grip_teaching.__version__ 的文本值；不导入包，避免拉起 Qt 或依赖本机安装。"""
    init = Path(__file__).resolve().parents[1] / "src" / "grip_teaching" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError("grip_teaching/__init__.py 缺少 __version__")


def main() -> None:
    """第三方依赖复用构建环境；-I -S禁止项目pth和环境路径参与源码导入。"""
    workspace = Path(__file__).resolve().parents[2]
    target = workspace / "tmp" / f"源码校验 中文 空格 {time.time_ns()}"
    target.mkdir(parents=True)
    with zipfile.ZipFile(workspace / "release" / f"G0505M教具源码-v{release_version()}.zip") as archive:
        assert archive.testzip() is None
        assert not any("pvdf_teaching" in name for name in archive.namelist())
        archive.extractall(target)
    desktop = target / "desktop"
    dependencies = Path(sys.executable).resolve().parents[1] / "Lib" / "site-packages"
    assert dependencies.is_dir()
    environment = os.environ.copy()
    for key in ("PYTHONPATH", "PYTHONHOME", "QT_PLUGIN_PATH", "QT_QPA_PLATFORM_PLUGIN_PATH"):
        environment.pop(key, None)
    environment["QT_QPA_PLATFORM"] = "offscreen"
    environment["PATH"] = os.pathsep.join((environment.get("SystemRoot", "C:/Windows") + "/System32",
                                           environment.get("SystemRoot", "C:/Windows")))
    setup = """
import sys
from pathlib import Path
source = Path(sys.argv[1])
sys.path[:0] = [str(source / 'src'), sys.argv[2]]
import grip_teaching
assert Path(grip_teaching.__file__).resolve().is_relative_to(source.resolve())
"""  # 作用：子解释器只能导入解压目录的新教具，开发环境的项目.pth不被执行。
    tests_script = setup + """
import pytest
raise SystemExit(pytest.main(['tests', '-q', '--junitxml', 'source-tests.xml']))
"""
    smoke_script = setup + """
from grip_teaching.app import main
sys.argv = ['run_g0505m.py', '--smoke-demo', '--smoke-report', 'source-smoke.json']
raise SystemExit(main())
"""
    for script in (tests_script, smoke_script):
        result = subprocess.run([sys.executable, "-I", "-S", "-c", script, str(desktop), str(dependencies)],
                                cwd=desktop, env=environment, capture_output=True, text=True, timeout=30)
        if result.returncode:
            raise RuntimeError(result.stdout + result.stderr)
    suites = ET.parse(desktop / "source-tests.xml").getroot().findall("testsuite")
    smoke = json.loads(desktop.joinpath("source-smoke.json").read_text(encoding="utf-8"))
    assert smoke["mode"] == "demo" and smoke["valid_frames"] > 10 and smoke["visible"]
    report = {"extracted_to": str(target), "tests": sum(int(suite.get("tests", 0)) for suite in suites),
              "isolated_python": True, "project_pth_disabled": True, "source_import": "extracted_zip_only",
              "dependencies": "build_environment_third_party_only", "smoke": smoke}
    workspace.joinpath("tmp", "g0505m-source-check.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(report, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
