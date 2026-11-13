"""把逐点原始数据保存为 CSV，并把触摸事件保存为 JSONL。"""

from __future__ import annotations

import csv
import json
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

from .models import ProcessedBatch

CSV_FIELDS = (
    "recorded_time",
    "device_timestamp_us",
    "sample_period_us",
    "sequence",
    "sample_index",
    "flags",
    "adc_raw",
    "voltage_v",
    "baseline_v",
    "delta_voltage_v",
    "envelope_v",
    "intensity_pct",
    "dynamic_touch",
)


class DataRecorder:
    """管理一次课堂会话的 CSV、JSONL 文件及定期落盘。"""

    def __init__(self) -> None:
        """建立空记录器，只有用户点击开始记录后才创建文件。"""

        self._lock = threading.RLock()
        self._csv_handle = None
        self._event_handle = None
        self._csv_writer: csv.DictWriter | None = None
        self._session_directory: Path | None = None
        self._last_flush_monotonic = 0.0

    @property
    def active(self) -> bool:
        """返回当前是否有一个可写记录会话。"""

        with self._lock:
            return self._csv_handle is not None

    @property
    def session_directory(self) -> Path | None:
        """返回本次会话目录，便于界面展示导出位置。"""

        with self._lock:
            return self._session_directory

    def start(self, parent_directory: str | Path) -> Path:
        """在用户选择的目录下新建带时间戳的独立会话目录。"""

        import time

        with self._lock:
            if self._csv_handle is not None:
                raise RuntimeError("记录已经开始")
            parent = Path(parent_directory).expanduser().resolve()
            parent.mkdir(parents=True, exist_ok=True)
            suffix = 0
            base_name = datetime.now().strftime("PVDF课堂记录_%Y%m%d_%H%M%S")
            session = parent / base_name
            while session.exists():
                suffix += 1
                session = parent / f"{base_name}_{suffix}"
            session.mkdir()

            csv_path = session / "raw_samples.csv"
            event_path = session / "touch_events.jsonl"
            self._csv_handle = csv_path.open("w", encoding="utf-8-sig", newline="", buffering=262_144)
            self._event_handle = event_path.open("w", encoding="utf-8", newline="\n", buffering=65_536)
            self._csv_writer = csv.DictWriter(self._csv_handle, fieldnames=CSV_FIELDS)
            self._csv_writer.writeheader()
            self._session_directory = session
            self._last_flush_monotonic = time.monotonic()
            return session

    def record_batch(self, batch: ProcessedBatch) -> None:
        """把处理结果逐样本写入 CSV，保留以后复现算法所需的原始 ADC。"""

        import time

        with self._lock:
            if self._csv_writer is None or self._csv_handle is None:
                return
            for index, adc_raw in enumerate(batch.raw.samples):
                device_timestamp_us = batch.raw.device_timestamp_us + index * batch.raw.sample_period_us
                self._csv_writer.writerow(
                    {
                        "recorded_time": batch.latest.host_time_iso,
                        "device_timestamp_us": device_timestamp_us,
                        "sample_period_us": batch.raw.sample_period_us,
                        "sequence": batch.raw.sequence,
                        "sample_index": index,
                        "flags": batch.raw.flags,
                        "adc_raw": adc_raw,
                        "voltage_v": f"{float(batch.voltages_v[index]):.7f}",
                        "baseline_v": f"{batch.latest.baseline_v:.7f}",
                        "delta_voltage_v": f"{float(batch.deltas_v[index]):.7f}",
                        "envelope_v": f"{float(batch.envelopes_v[index]):.7f}",
                        "intensity_pct": f"{float(batch.intensities_pct[index]):.3f}",
                        "dynamic_touch": int(batch.latest.dynamic_touch),
                    }
                )
            now = time.monotonic()
            if now - self._last_flush_monotonic >= 1.0:
                self._csv_handle.flush()
                if self._event_handle is not None:
                    self._event_handle.flush()
                self._last_flush_monotonic = now

    def record_event(self, payload: dict[str, Any]) -> None:
        """把发送给机器人的事件原样写成一行 JSON，便于逐条课堂讲解。"""

        with self._lock:
            if self._event_handle is None:
                return
            self._event_handle.write(json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n")

    def stop(self) -> Path | None:
        """刷新并关闭会话文件，返回目录供界面向用户确认。"""

        with self._lock:
            session = self._session_directory
            for handle in (self._csv_handle, self._event_handle):
                if handle is not None:
                    handle.flush()
                    handle.close()
            self._csv_handle = None
            self._event_handle = None
            self._csv_writer = None
            self._session_directory = None
            return session

