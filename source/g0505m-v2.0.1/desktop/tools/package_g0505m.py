"""生成独立源码 ZIP 与便携交付 ZIP，避免混入旧教具二进制和本机环境。"""

import hashlib
import shutil
import time
import zipfile
from pathlib import Path


def release_version() -> str:
    """读取 grip_teaching.__version__ 的文本值；不导入包，避免拉起 Qt 或依赖本机安装。"""
    init = Path(__file__).resolve().parents[1] / "src" / "grip_teaching" / "__init__.py"
    for line in init.read_text(encoding="utf-8").splitlines():
        if line.startswith("__version__"):
            return line.split("=", 1)[1].strip().strip('"')
    raise ValueError("grip_teaching/__init__.py 缺少 __version__")


def add_tree(archive: zipfile.ZipFile, directory: Path, prefix: str) -> None:
    """按结构化 ZIP 路径添加文件，跳过解释器缓存和测试中间产物。"""
    for path in sorted(directory.rglob("*")):
        if path.is_file() and not any(part in ("__pycache__", ".pytest_cache", ".ruff_cache") for part in path.parts):
            archive.write(path, f"{prefix}/{path.relative_to(directory).as_posix()}")


def main() -> None:
    """只更新本次 G0505M 命名的交付物，原 PVDF 的 release 保持完整。"""
    desktop = Path(__file__).resolve().parents[1]
    workspace = desktop.parent
    docs = workspace / "docs" / "g0505m"
    release = workspace / "release"
    release.mkdir(exist_ok=True)
    # 作用：2.0 起交付打印版 PDF 说明书和桌角卡（由 docs/g0505m/print/build_print.py 生成），替代 1.0 的 docx。
    guide = docs / "G0505M手掌受力教具使用说明书-v2.0.pdf"
    card = docs / "G0505M桌角卡-v2.0.pdf"
    if not guide.exists() or not card.exists():
        raise FileNotFoundError("请先运行 docs/g0505m/print/build_print.py 生成并目视核对说明书和桌角卡")
    source_zip = release / f"G0505M教具源码-v{release_version()}.zip"
    with zipfile.ZipFile(source_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        add_tree(archive, desktop / "src" / "grip_teaching", "desktop/src/grip_teaching")
        for path in desktop.joinpath("tests").glob("test_grip_*.py"):
            archive.write(path, f"desktop/tests/{path.name}")
        archive.write(desktop / "tests" / "conftest.py", "desktop/tests/conftest.py")
        for name in ("run_g0505m.py", "g0505m_teaching.spec", "g0505m_pyproject.toml", "requirements.txt",
                     "requirements-dev.txt", "build_g0505m.ps1", "Start_G0505M.cmd", "启动G0505M教具.cmd"):
            archive.write(desktop / name, f"desktop/{name}")
        archive.write(desktop / "g0505m_pyproject.toml", "desktop/pyproject.toml")
        for name in ("verify_g0505m_ui.py", "verify_g0505m_portable.py", "verify_g0505m_source.py", "package_g0505m.py",
                     "check_g0505m_hardware.py", "soak_g0505m.py", "audit_g0505m_soak.py",
                     "finalize_g0505m_evidence.py"):
            archive.write(desktop / "tools" / name, f"desktop/tools/{name}")
        archive.write(workspace / "AGENTS.md", "AGENTS.md")
        archive.write(docs / "README.md", "README.md")
        archive.write(docs / "README.md", "docs/g0505m/README.md")
        archive.write(guide, f"docs/g0505m/{guide.name}")
        archive.write(card, f"docs/g0505m/{card.name}")
        for path in docs.joinpath("print").glob("*.*"):
            archive.write(path, f"docs/g0505m/print/{path.name}")
        for path in docs.joinpath("assets", "manual").glob("*.png"):
            archive.write(path, f"docs/g0505m/assets/manual/{path.name}")
        for name in ("hardware_latest.json", "soak_latest.json", "device_parameters_verified.json",
                     "software_validation.json", "验证结果.md"):
            if docs.joinpath(name).exists():
                archive.write(docs / name, f"docs/g0505m/{name}")
        for path in docs.joinpath("assets").glob("*.png"):
            if path.name.startswith("ui-") or path.name in ("manufacturer-numbering.png", "manufacturer-usb-ttl.png"):
                archive.write(path, f"docs/g0505m/assets/{path.name}")
    portable = desktop / "dist" / "G0505M手掌受力教具"
    if not portable.joinpath("G0505MTeaching.exe").exists():
        raise FileNotFoundError("缺少 PyInstaller 便携 EXE")
    # 作用：dist 中可能残留 1.0 的 docx 说明，内容已过时，交付前移除，避免教师看到旧界面。
    portable.joinpath("G0505M手掌受力教具图文操作说明.docx").unlink(missing_ok=True)
    shutil.copy2(guide, portable / guide.name)
    shutil.copy2(card, portable / card.name)
    shutil.copy2(docs / "README.md", portable / "使用说明.md")
    shutil.copy2(source_zip, portable / source_zip.name)
    for name in ("Start_G0505M.cmd", "启动G0505M教具.cmd"):
        shutil.copy2(desktop / name, portable / name)
    for name in ("hardware_latest.json", "soak_latest.json", "device_parameters_verified.json",
                 "software_validation.json", "验证结果.md"):
        if docs.joinpath(name).exists():
            shutil.copy2(docs / name, portable / name)
    illustrations = portable / "接线与编号图"
    illustrations.mkdir(exist_ok=True)
    for name in ("manufacturer-numbering.png", "manufacturer-usb-ttl.png"):
        shutil.copy2(docs / "assets" / name, illustrations / name)
    delivery = release / "G0505M手掌受力教具"
    # 作用：每次发布使用完整新目录，避免已移除的DLL残留；旧目录移到工作区tmp保留，而非删除用户文件。
    temporary = workspace / "tmp"
    temporary.mkdir(exist_ok=True)
    staging = temporary / f"g0505m-delivery-{time.time_ns()}"
    backup = temporary / f"g0505m-previous-{time.time_ns()}"
    if delivery.resolve().parent != release.resolve() or staging.resolve().parent != temporary.resolve():
        raise ValueError("发布目录必须位于本工作区的release和tmp")
    shutil.copytree(portable, staging)
    if delivery.exists():
        delivery.rename(backup)
    staging.rename(delivery)
    portable_zip = release / f"G0505M手掌受力教具-Windows-x64-v{release_version()}.zip"
    with zipfile.ZipFile(portable_zip, "w", zipfile.ZIP_DEFLATED) as archive:
        add_tree(archive, delivery, "G0505M手掌受力教具")
    hashes = []
    for path in (portable_zip, source_zip, delivery / "G0505MTeaching.exe", guide, card):
        with path.open("rb") as stream:
            digest = hashlib.file_digest(stream, "sha256").hexdigest()
        hashes.append(f"{digest}  {path.relative_to(workspace).as_posix()}")
    release.joinpath("G0505M-SHA256SUMS.txt").write_text("\n".join(hashes) + "\n", encoding="utf-8")
    print(f"便携包：{portable_zip} ({portable_zip.stat().st_size / 1024 / 1024:.1f} MB)")
    print(f"源码包：{source_zip}")


if __name__ == "__main__":
    main()
