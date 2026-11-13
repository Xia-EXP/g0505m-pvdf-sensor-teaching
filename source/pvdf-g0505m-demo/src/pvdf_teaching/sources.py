"""提供 Pico 串口、合成波形和 CSV 回放三种可切换的数据源。"""

from __future__ import annotations

import csv
import math
import queue
import random
import threading
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from typing import Protocol

from .models import SampleBatch, SourceStatus
from .protocol import (
    FrameParser,
    MessageType,
    decode_ack,
    decode_error,
    decode_hello,
    decode_sample_batch,
    encode_frame,
)

BatchCallback = Callable[[SampleBatch], None]
StatusCallback = Callable[[SourceStatus], None]
ManagedBatchCallback = Callable[[int, SampleBatch], None]
ManagedStatusCallback = Callable[[int, SourceStatus], None]


class ProtocolHandshakeError(RuntimeError):
    """表示串口已打开但设备参数、命令确认或固件状态不满足协议v1。"""


class StoppableSource(Protocol):
    """约束所有数据源必须提供启动和可等待的停止操作。"""

    def start(self) -> None: ...

    def stop(self) -> None: ...


def available_serial_ports() -> list[tuple[str, str]]:
    """列出当前串口；pyserial 缺失时返回空列表而不阻止模拟模式启动。"""

    try:
        from serial.tools import list_ports
    except ImportError:
        return []
    return [(port.device, port.description or port.device) for port in list_ports.comports()]


class SerialPicoSource:
    """在后台自动发现和重连 Pico，并把有效 SAMPLE_BATCH 帧交给控制器。"""

    def __init__(
        self,
        on_batch: BatchCallback,
        on_status: StatusCallback,
        port: str | None = None,
        baudrate: int = 115200,
        reconnect_seconds: float = 1.0,
        handshake_timeout_seconds: float = 2.0,
    ) -> None:
        """配置串口偏好；波特率仅满足 CDC API，真实采样率由帧内周期决定。"""

        self.on_batch = on_batch
        self.on_status = on_status
        self.port = port
        self.baudrate = baudrate
        self.reconnect_seconds = reconnect_seconds
        self.handshake_timeout_seconds = handshake_timeout_seconds
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._serial = None
        self._command_sequence = 0

    def start(self) -> None:
        """启动守护线程；如果设备暂时不存在，线程会持续低频重试。"""

        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="PicoSerialSource", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """通知固件停止推流、关闭串口并等待后台线程退出。"""

        self._stop_event.set()
        serial_port = self._serial
        if serial_port is not None:
            try:
                serial_port.write(encode_frame(MessageType.STOP, self._next_command_sequence()))
            except Exception:
                pass
            try:
                serial_port.close()
            except Exception:
                pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def _next_command_sequence(self) -> int:
        """生成桌面发往固件的独立控制命令序号。"""

        self._command_sequence = (self._command_sequence + 1) & 0xFFFFFFFF
        return self._command_sequence

    def _choose_port(self) -> str | None:
        """优先使用用户指定端口，否则按 Pico VID 和描述自动发现。"""

        if self.port:
            return self.port
        try:
            from serial.tools import list_ports
        except ImportError:
            return None
        ports = list(list_ports.comports())
        for candidate in ports:
            description = f"{candidate.description} {candidate.manufacturer}".lower()
            if candidate.vid == 0x2E8A or "pico" in description or "rp2" in description:
                return candidate.device
        return None

    def _run(self) -> None:
        """执行连接、协议解析、丢帧统计和断线后的自动重连循环。"""

        try:
            import serial
        except ImportError:
            self.on_status(SourceStatus("Pico USB", False, "未安装 pyserial，请先安装 requirements.txt"))
            return

        dropped_batches = 0
        missing_reported = False
        while not self._stop_event.is_set():
            selected_port = self._choose_port()
            if not selected_port:
                if not missing_reported:
                    self.on_status(SourceStatus("Pico USB", False, "等待 Pico 串口出现，将自动重连"))
                    missing_reported = True
                self._stop_event.wait(self.reconnect_seconds)
                continue
            missing_reported = False
            parser = FrameParser()
            previous_sequence: int | None = None
            try:
                serial_port = serial.Serial(selected_port, self.baudrate, timeout=0.10, write_timeout=0.5)
                self._serial = serial_port
                serial_port.reset_input_buffer()
                hello_sequence = self._next_command_sequence()
                start_sequence = self._next_command_sequence()
                serial_port.write(encode_frame(MessageType.HELLO, hello_sequence))
                serial_port.write(encode_frame(MessageType.START, start_sequence))
                handshake_started = time.monotonic()
                hello_valid = False
                start_acknowledged = False
                handshake_complete = False
                self.on_status(
                    SourceStatus("Pico USB", False, f"已打开 {selected_port}，正在核对设备信息与START确认", dropped_batches)
                )
                while not self._stop_event.is_set() and serial_port.is_open:
                    if not handshake_complete and time.monotonic() - handshake_started > self.handshake_timeout_seconds:
                        missing = []
                        if not hello_valid:
                            missing.append("有效HELLO")
                        if not start_acknowledged:
                            missing.append("START ACK")
                        raise ProtocolHandshakeError(f"握手超时，未收到{'和'.join(missing)}")
                    data = serial_port.read(4096)
                    if not data:
                        continue
                    for frame in parser.feed(data):
                        # 固件的序号由所有上行消息共享，因此必须先统计HELLO/ACK等控制帧再筛采样帧。
                        if previous_sequence is not None:
                            expected = (previous_sequence + 1) & 0xFFFFFFFF
                            if frame.sequence != expected:
                                gap = (frame.sequence - expected) & 0xFFFFFFFF
                                # 巨大的反向差值通常意味着设备重启，不把它误报为数十亿个丢帧。
                                if gap < 1_000_000:
                                    dropped_batches += gap
                                    self.on_status(
                                        SourceStatus(
                                            "Pico USB",
                                            True,
                                            f"检测到上行序号缺口，本次缺失 {gap} 帧",
                                            dropped_batches,
                                        )
                                    )
                        previous_sequence = frame.sequence
                        if frame.message_type == MessageType.HELLO:
                            try:
                                hello = decode_hello(frame)
                                if (
                                    hello.sample_rate_hz != 2000
                                    or hello.batch_samples != 20
                                    or hello.adc_bits != 12
                                    or hello.adc_max != 4095
                                    or hello.reference_mv != 3300
                                    or hello.adc_channel != 0
                                    or hello.capabilities & 0x07 != 0x07
                                ):
                                    raise ProtocolHandshakeError(
                                        "HELLO参数不兼容：需要2kS/s、20点、12位ADC、3.3V、ADC0及能力位0..2"
                                    )
                                hello_valid = True
                                version = ".".join(str(value) for value in hello.firmware_version)
                                if start_acknowledged:
                                    handshake_complete = True
                                    message = f"已开始采样 {selected_port} · 2000S/s · 20点/批"
                                else:
                                    message = (
                                        f"设备信息通过：固件{version} · {hello.sample_rate_hz}S/s · "
                                        f"每批{hello.batch_samples}点；等待START ACK"
                                    )
                                self.on_status(SourceStatus("Pico USB", handshake_complete, message, dropped_batches))
                            except ValueError as error:
                                self.on_status(SourceStatus("Pico USB", True, f"HELLO无效：{error}", dropped_batches))
                            continue
                        if frame.message_type == MessageType.ACK:
                            try:
                                ack = decode_ack(frame)
                                if (
                                    ack.acknowledged_type == MessageType.START
                                    and ack.acknowledged_sequence == start_sequence
                                ):
                                    start_acknowledged = True
                            except ValueError as error:
                                self.on_status(
                                    SourceStatus("Pico USB", handshake_complete, f"ACK无效：{error}", dropped_batches)
                                )
                            if hello_valid and start_acknowledged and not handshake_complete:
                                handshake_complete = True
                                self.on_status(
                                    SourceStatus(
                                        "Pico USB",
                                        True,
                                        f"已开始采样 {selected_port} · 2000S/s · 20点/批",
                                        dropped_batches,
                                    )
                                )
                            continue
                        if frame.message_type == MessageType.ERROR:
                            try:
                                device_error = decode_error(frame)
                                detail = device_error.text or f"错误码 {device_error.code}"
                            except ValueError as error:
                                detail = f"无法解析ERROR：{error}"
                                device_error = None
                            if device_error is not None and (
                                device_error.code == 5
                                or device_error.related_sequence in {hello_sequence, start_sequence}
                            ):
                                raise ProtocolHandshakeError(f"固件拒绝采样：{detail}")
                            self.on_status(SourceStatus("Pico USB", handshake_complete, f"固件报告：{detail}", dropped_batches))
                            continue
                        if frame.message_type != MessageType.SAMPLE_BATCH:
                            continue
                        if not handshake_complete:
                            # START ACK之前的样本不能证明命令已被确认，忽略可避免显示伪连接状态。
                            continue
                        try:
                            batch = decode_sample_batch(frame)
                        except ValueError as error:
                            self.on_status(SourceStatus("Pico USB", True, f"忽略无效采样帧：{error}", dropped_batches))
                            continue
                        if batch.buffer_overrun:
                            self.on_status(
                                SourceStatus("Pico USB", True, "固件采样缓冲溢出，请检查USB链路", dropped_batches)
                            )
                        self.on_batch(batch)
            except (OSError, serial.SerialException, ProtocolHandshakeError) as error:
                if not self._stop_event.is_set():
                    self.on_status(
                        SourceStatus("Pico USB", False, f"连接中断：{error}；正在自动重连", dropped_batches)
                    )
            finally:
                serial_port = self._serial
                self._serial = None
                if serial_port is not None:
                    try:
                        serial_port.close()
                    except Exception:
                        pass
            self._stop_event.wait(self.reconnect_seconds)
        self.on_status(SourceStatus("Pico USB", False, "已停止", dropped_batches))


class SimulationSource:
    """生成带噪声、冲击和衰减振铃的 2kS/s PVDF 教学波形。"""

    LEVEL_AMPLITUDES = {"light": 130.0, "medium": 360.0, "heavy": 720.0}

    def __init__(
        self,
        on_batch: BatchCallback,
        on_status: StatusCallback,
        auto_touch: bool = True,
        seed: int = 20260922,
    ) -> None:
        """配置可复现的噪声及每三秒一次的自动触摸，方便无硬件演示。"""

        self.on_batch = on_batch
        self.on_status = on_status
        self.auto_touch = auto_touch
        self._random = random.Random(seed)
        self._trigger_queue: queue.SimpleQueue[float] = queue.SimpleQueue()
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._sample_index = 0
        self._active_impacts: list[tuple[int, float]] = []

    def start(self) -> None:
        """启动按真实10ms批次节奏运行的模拟线程。"""

        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="PVDFSimulationSource", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """停止模拟线程并等待其退出。"""

        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def trigger(self, level: str = "medium") -> None:
        """排入一次轻、中或重触，供界面按钮和课堂讲解即时触发。"""

        if level not in self.LEVEL_AMPLITUDES:
            raise ValueError("模拟等级只能是 light、medium 或 heavy")
        self._trigger_queue.put(self.LEVEL_AMPLITUDES[level])

    def _sample_value(self, absolute_index: int) -> int:
        """叠加活动冲击的阻尼振铃和电子噪声，并限制到12位ADC范围。"""

        value = 2482.0 + self._random.gauss(0.0, 2.2)
        remaining: list[tuple[int, float]] = []
        for start_index, amplitude in self._active_impacts:
            elapsed = (absolute_index - start_index) / 2000.0
            if 0.0 <= elapsed <= 0.60:
                # 快速正向冲击叠加低频振铃，模拟薄膜弯曲后逐渐回到电荷平衡。
                impulse = 0.72 * amplitude * math.exp(-elapsed / 0.010)
                ringing = amplitude * math.exp(-elapsed / 0.075) * math.sin(2.0 * math.pi * 21.0 * elapsed)
                value += impulse + ringing
                remaining.append((start_index, amplitude))
        self._active_impacts = remaining
        return max(0, min(4095, round(value)))

    def _run(self) -> None:
        """按墙钟补偿调度批次，避免 sleep 累积误差改变回放速度。"""

        sequence = 0
        next_deadline = time.perf_counter()
        next_auto_touch = next_deadline + 3.0
        self.on_status(SourceStatus("模拟数据", True, "无需硬件：正在生成2kS/s合成PVDF波形"))
        while not self._stop_event.is_set():
            now = time.perf_counter()
            if self.auto_touch and now >= next_auto_touch:
                levels = ("light", "medium", "heavy")
                self.trigger(levels[(sequence // 300) % len(levels)])
                next_auto_touch = now + 3.0
            while True:
                try:
                    amplitude = self._trigger_queue.get_nowait()
                except queue.Empty:
                    break
                self._active_impacts.append((self._sample_index, amplitude))

            samples = tuple(self._sample_value(self._sample_index + offset) for offset in range(20))
            flags = (0x01 if min(samples) <= 931 else 0) | (0x02 if max(samples) >= 3165 else 0)
            batch = SampleBatch(
                sequence=sequence,
                device_timestamp_us=self._sample_index * 500,
                sample_period_us=500,
                samples=samples,
                flags=flags,
            )
            self.on_batch(batch)
            sequence = (sequence + 1) & 0xFFFFFFFF
            self._sample_index += len(samples)
            next_deadline += 0.010
            delay = next_deadline - time.perf_counter()
            if delay > 0:
                self._stop_event.wait(delay)
            elif delay < -0.100:
                # 调试器暂停后直接重置节拍，不补发大量过期批次堵塞界面。
                next_deadline = time.perf_counter()
        self.on_status(SourceStatus("模拟数据", False, "已停止"))


class CsvReplaySource:
    """按记录时的采样周期重放本软件生成的逐点 CSV 文件。"""

    def __init__(
        self,
        path: str | Path,
        on_batch: BatchCallback,
        on_status: StatusCallback,
        speed: float = 1.0,
    ) -> None:
        """保存回放文件和速度；速度大于1可用于快速复习课堂数据。"""

        self.path = Path(path)
        self.on_batch = on_batch
        self.on_status = on_status
        self.speed = speed
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        """启动后台回放并立即报告文件错误。"""

        if self.speed <= 0:
            raise ValueError("回放速度必须大于零")
        if not self.path.is_file():
            raise FileNotFoundError(self.path)
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="PVDFCsvReplay", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """中止回放并等待文件线程退出。"""

        self._stop_event.set()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    @staticmethod
    def _group_rows(rows: Iterable[dict[str, str]]) -> Iterable[list[dict[str, str]]]:
        """按批次序号合并连续CSV行，不把整个大文件一次性载入内存。"""

        current_sequence: str | None = None
        group: list[dict[str, str]] = []
        for row in rows:
            sequence = row.get("sequence", "")
            if group and sequence != current_sequence:
                yield group
                group = []
            current_sequence = sequence
            group.append(row)
        if group:
            yield group

    def _run(self) -> None:
        """逐批读取CSV并复原 SampleBatch，结束时保持可再次选择其他数据源。"""

        self.on_status(SourceStatus("课堂回放", True, f"正在回放 {self.path.name}"))
        try:
            with self.path.open("r", encoding="utf-8-sig", newline="") as handle:
                reader = csv.DictReader(handle)
                required = {"sequence", "device_timestamp_us", "sample_period_us", "adc_raw"}
                if not required.issubset(set(reader.fieldnames or [])):
                    raise ValueError(f"CSV缺少字段：{', '.join(sorted(required))}")
                for rows in self._group_rows(reader):
                    if self._stop_event.is_set():
                        break
                    first = rows[0]
                    samples = tuple(int(row["adc_raw"]) for row in rows)
                    flags = int(first.get("flags", "0") or 0)
                    batch = SampleBatch(
                        sequence=int(first["sequence"]),
                        device_timestamp_us=int(first["device_timestamp_us"]),
                        sample_period_us=int(first["sample_period_us"]),
                        samples=samples,
                        flags=flags,
                    )
                    self.on_batch(batch)
                    duration = batch.sample_period_us * len(batch.samples) / 1_000_000.0 / self.speed
                    self._stop_event.wait(duration)
        except (OSError, ValueError, KeyError) as error:
            self.on_status(SourceStatus("课堂回放", False, f"回放失败：{error}"))
            return
        self.on_status(SourceStatus("课堂回放", False, "回放结束"))


class SourceManager:
    """确保任意时刻只有一个采集源运行，并为界面提供统一切换入口。"""

    def __init__(self, on_batch: ManagedBatchCallback, on_status: ManagedStatusCallback) -> None:
        """保存带代际编号的跨线程回调和当前数据源。"""

        self.on_batch = on_batch
        self.on_status = on_status
        self.current: StoppableSource | None = None
        self._lock = threading.RLock()
        self._generation = 0

    @property
    def generation(self) -> int:
        """返回当前数据源代际，供Qt主线程丢弃已经排队的旧源批次。"""

        with self._lock:
            return self._generation

    def stop(self) -> None:
        """原子移除并停止当前数据源。"""

        with self._lock:
            source = self.current
            self.current = None
            # 即使当前为空也递增，确保停止动作能使所有已排队回调立即失效。
            self._generation += 1
        if source is not None:
            source.stop()

    def start_serial(self, port: str | None = None) -> None:
        """切换到可自动重连的 Pico USB 数据源。"""

        self.stop()
        with self._lock:
            generation = self._generation
        source = SerialPicoSource(
            lambda batch: self.on_batch(generation, batch),
            lambda status: self.on_status(generation, status),
            port=port,
        )
        with self._lock:
            self.current = source
        source.start()

    def start_simulation(self, auto_touch: bool = True) -> None:
        """切换到无需硬件的合成数据源。"""

        self.stop()
        with self._lock:
            generation = self._generation
        source = SimulationSource(
            lambda batch: self.on_batch(generation, batch),
            lambda status: self.on_status(generation, status),
            auto_touch=auto_touch,
        )
        with self._lock:
            self.current = source
        source.start()

    def start_replay(self, path: str | Path, speed: float = 1.0) -> None:
        """切换到CSV课堂回放源。"""

        self.stop()
        with self._lock:
            generation = self._generation
        source = CsvReplaySource(
            path,
            lambda batch: self.on_batch(generation, batch),
            lambda status: self.on_status(generation, status),
            speed=speed,
        )
        with self._lock:
            self.current = source
        source.start()

    def trigger_simulation(self, level: str) -> bool:
        """当当前源是模拟器时触发动作，否则返回False供界面提示。"""

        with self._lock:
            source = self.current
        if not isinstance(source, SimulationSource):
            return False
        source.trigger(level)
        return True
