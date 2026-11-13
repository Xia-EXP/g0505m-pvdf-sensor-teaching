"""保存原始厂家值、每帧零点、接收字节及会话信息，保证回放可追溯。"""

import csv
import json
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import BinaryIO, TextIO

from .models import GripFrame
from .protocol import FrameParser, ParserStats

FIELDS = ["received_ns", "hand"] + [f"raw_{i:02d}_g" for i in range(1, 26)] + [
    f"zero_{i:02d}_g" for i in range(1, 26)
]  # 每行保存零点快照，记录途中归零后也可以还原净值。


@dataclass(frozen=True)
class ReplayRow:
    """回放单元：不可变原始帧与当时的零点，共享同一电脑时间轴。"""

    frame: GripFrame
    baseline: tuple[int, ...]


class SessionRecorder:
    """CSV、有效帧和全部串口字节一起保存；落盘失败时让界面明确停止记录。"""

    def __init__(self, folder: Path, metadata: dict) -> None:
        """只创建新目录，避免覆盖已有课堂实验。"""
        folder.mkdir(parents=True, exist_ok=False)
        self.folder = folder
        self.metadata = {"format_version": 1, "timestamp": "host_receive_unix_ns", **metadata}
        self.metadata["events"] = []
        self.count = 0
        self._streams: list[TextIO | BinaryIO] = []
        try:
            self.csv_file = (folder / "channels.csv").open("w", encoding="utf-8-sig", newline="")
            self._streams.append(self.csv_file)
            self.raw_file = (folder / "valid_frames.bin").open("wb")
            self._streams.append(self.raw_file)
            self.rx_file = (folder / "received_bytes.bin").open("wb")
            self._streams.append(self.rx_file)
            self.writer = csv.writer(self.csv_file)
            self.writer.writerow(FIELDS)
            self._write_metadata()
        except Exception:
            for stream in self._streams:
                stream.close()
            raise

    def _write_metadata(self) -> None:
        """先写临时文件再替换，断电时尽量保留上次完整参数。"""
        temporary = self.folder / "session.tmp"
        temporary.write_text(json.dumps(self.metadata, ensure_ascii=False, indent=2), encoding="utf-8")
        temporary.replace(self.folder / "session.json")

    def receive_bytes(self, data: bytes) -> None:
        """记录噪声与坏帧；离线排查不会仅看到已筛选的好帧。"""
        self.rx_file.write(data)

    def write(self, frame: GripFrame, baseline: tuple[int, ...]) -> None:
        """写入空间 25 路和实际有效帧，定期刷新避免全靠正常退出。"""
        self.writer.writerow([frame.received_ns, frame.hand, *frame.values, *baseline])
        self.raw_file.write(frame.raw)
        self.count += 1
        if self.count % 50 == 0:
            for stream in self._streams:
                stream.flush()

    def event(self, received_ns: int, kind: str, **values) -> None:
        """持久化归零、量程确认和色标变化，注明发生的电脑时间。"""
        self.metadata["events"].append({"received_ns": received_ns, "kind": kind, **values})
        self._write_metadata()

    def close(self, stats: ParserStats) -> None:
        """先关闭全部流，再写最终计数；即使写参数失败也释放文件句柄。"""
        for stream in self._streams:
            stream.close()
        self.metadata.update({"recorded_frames": self.count, "parser_at_stop": asdict(stats)})
        self._write_metadata()


def load_replay(path: Path) -> list[ReplayRow]:
    """严格读取本程序 CSV；格式/时间错误会拒绝，避免看似正常的错位回放。"""
    rows = []
    previous_ns = -1
    with path.open(encoding="utf-8-sig", newline="") as stream:
        reader = csv.DictReader(stream)
        if reader.fieldnames != FIELDS:
            raise ValueError("CSV 字段不符合 G0505M 25 通道记录格式")
        for number, row in enumerate(reader, 2):
            try:
                from .protocol import encode_frame

                received_ns = int(row["received_ns"])
                values = tuple(int(row[key]) for key in FIELDS[2:27])
                baseline = tuple(int(row[key]) for key in FIELDS[27:])
                if received_ns < previous_ns or any(not 0 <= item <= 65535 for item in baseline):
                    raise ValueError("时间倒退或零点越界")
                frame = GripFrame(row["hand"], values, received_ns, encode_frame(row["hand"], values))
                rows.append(ReplayRow(frame, baseline))
                previous_ns = received_ns
                if len(rows) > 1_000_000:
                    raise ValueError("单次回放限制为 100 万帧，请拆分实验记录")
            except (ValueError, TypeError) as exc:
                raise ValueError(f"第 {number} 行记录无效：{exc}") from exc
    if not rows:
        raise ValueError("记录中没有有效数据帧")
    return rows


def load_metadata(path: Path) -> dict:
    """可单独回放 CSV，但缺少会话 JSON 时来源和量程必须保持未确认。"""
    metadata_path = path.with_name("session.json")
    if not metadata_path.exists():
        return {}
    metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
    if not isinstance(metadata, dict) or metadata.get("format_version") != 1:
        raise ValueError("会话参数格式不受支持")
    if metadata.get("source") not in (None, "serial", "demo"):
        raise ValueError("会话来源字段无效")
    if metadata.get("range_kg_user_confirmed") not in (None, 2, 5, 10):
        raise ValueError("会话量程字段无效")
    events = metadata.get("events", [])
    if not isinstance(events, list) or len(events) > 100_000:
        raise ValueError("会话事件数量或类型无效")
    for event in events:
        if not isinstance(event, dict) or type(event.get("received_ns")) is not int or event["received_ns"] < 0:
            raise ValueError("会话事件接收时间无效")
    tared = metadata.get("tared_at_start", {})
    if not isinstance(tared, dict) or any(key not in ("L", "R") or type(value) is not bool for key, value in tared.items()):
        raise ValueError("会话归零标志无效")
    return metadata


def inspect_raw(path: Path) -> ParserStats:
    """分块核对原始字节文件，适合复核长时间记录的实际帧数和错误数。"""
    parser = FrameParser()
    with path.open("rb") as stream:
        while chunk := stream.read(65536):
            parser.feed(chunk)
    return parser.stats
