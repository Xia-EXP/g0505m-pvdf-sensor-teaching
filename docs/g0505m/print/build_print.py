"""用本机 Edge 无头模式把说明书和桌角卡 HTML 转为 A4 PDF；不依赖 Word 或额外排版库。"""

import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUTPUT = HERE.parent  # 作用：PDF 与其他 G0505M 文档放在同一目录，打包脚本从这里取。
TARGETS = {
    "manual.html": "G0505M手掌受力教具使用说明书-v2.0.pdf",
    "desk_card.html": "G0505M桌角卡-v2.0.pdf",
}
EDGE_CANDIDATES = (
    Path("C:/Program Files (x86)/Microsoft/Edge/Application/msedge.exe"),
    Path("C:/Program Files/Microsoft/Edge/Application/msedge.exe"),
)


def find_edge() -> Path:
    """Windows 10/11 自带 Edge；找不到时明确报错，不生成残缺 PDF。"""
    for path in EDGE_CANDIDATES:
        if path.exists():
            return path
    raise FileNotFoundError("未找到 Microsoft Edge，无法把 HTML 转为 PDF")


def main() -> None:
    """逐个转换；使用临时用户目录，避免干扰教师正在使用的浏览器配置。"""
    edge = find_edge()
    with tempfile.TemporaryDirectory() as profile:
        for source, target in TARGETS.items():
            pdf = OUTPUT / target
            pdf.unlink(missing_ok=True)
            # 作用：桌角卡靠脚本复制 8 份卡片，需留出脚本执行时间再打印。
            subprocess.run([str(edge), "--headless=new", "--disable-gpu", f"--user-data-dir={profile}",
                            "--no-pdf-header-footer", "--virtual-time-budget=3000",
                            f"--print-to-pdf={pdf}", (HERE / source).as_uri()],
                           check=True, timeout=120, capture_output=True)
            if not pdf.exists() or pdf.stat().st_size < 10_000:
                sys.exit(f"生成失败：{pdf}")
            print(f"{pdf.name}  {pdf.stat().st_size / 1024:.0f} KB")


if __name__ == "__main__":
    main()
