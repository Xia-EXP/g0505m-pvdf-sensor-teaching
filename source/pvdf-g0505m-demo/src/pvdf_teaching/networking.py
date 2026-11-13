"""实现机器人 TCP NDJSON 客户端、本机模拟接收端和 ACK 延迟测量。"""

from __future__ import annotations

import json
import queue
import socket
import threading
import time
from collections import deque
from datetime import datetime, timezone
from typing import Any, Callable

from .models import ProcessedState, TouchEvent

JsonObject = dict[str, Any]
NetworkStatusCallback = Callable[[bool, str], None]
AckCallback = Callable[[JsonObject, float | None], None]
MessageCallback = Callable[[JsonObject], None]


def iso_now() -> str:
    """生成机器人协议使用的本地时区 ISO 8601 毫秒时间。"""

    return datetime.now(timezone.utc).astimezone().isoformat(timespec="milliseconds")


class RobotMessageFactory:
    """为20Hz状态和即时事件分配独立消息ID并构造稳定的公开JSON字段。"""

    def __init__(self) -> None:
        """从零建立线程安全的消息计数器。"""

        self._message_id = 0
        self._lock = threading.Lock()

    def _next_message_id(self) -> int:
        """生成不会因采样批次重复而冲突的消息编号。"""

        with self._lock:
            self._message_id += 1
            return self._message_id

    def telemetry(self, state: ProcessedState) -> JsonObject:
        """把最新处理状态转换为公开的 telemetry 数据包。"""

        return {
            "schema": "pvdf.touch.v1",
            "version": 1,
            "type": "telemetry",
            "message_id": self._next_message_id(),
            "sequence": state.sequence,
            "time": state.host_time_iso,
            "device_time_us": state.device_timestamp_us,
            "adc_raw": state.adc_raw,
            "voltage_v": round(state.voltage_v, 6),
            "baseline_v": round(state.baseline_v, 6),
            "delta_voltage_v": round(state.delta_voltage_v, 6),
            "envelope_v": round(state.envelope_v, 6),
            "touch_intensity_pct": round(state.intensity_pct, 2),
            "touch_state": "dynamic_change" if state.dynamic_touch else "idle",
            "level": state.level,
            "saturated": state.saturated,
        }

    def event(self, event: TouchEvent, state: ProcessedState) -> JsonObject:
        """把事件与同批最新状态合并，保证事件也含公共电压和触摸字段。"""

        return {
            "schema": "pvdf.touch.v1",
            "version": 1,
            "type": f"event_{event.phase}",
            "message_id": self._next_message_id(),
            "sequence": event.sequence,
            "time": event.host_time_iso,
            "device_time_us": state.device_timestamp_us,
            "adc_raw": state.adc_raw,
            "voltage_v": round(event.voltage_v, 6),
            "baseline_v": round(state.baseline_v, 6),
            "delta_voltage_v": round(event.delta_voltage_v, 6),
            "envelope_v": round(state.envelope_v, 6),
            "touch_intensity_pct": round(event.intensity_pct, 2),
            "touch_state": "dynamic_change" if event.phase == "start" else "idle",
            "level": event.level,
            "saturated": state.saturated,
            "event_id": event.event_id,
            "peak_delta_voltage_v": round(event.peak_delta_voltage_v, 6),
            "peak_intensity_pct": round(event.peak_intensity_pct, 2),
            "duration_ms": round(event.duration_ms, 2),
        }


class RobotSimulatorServer:
    """在本机真实 TCP 端口接收 NDJSON，并对每条合法消息回复 ACK。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18080,
        on_message: MessageCallback | None = None,
        on_status: NetworkStatusCallback | None = None,
    ) -> None:
        """保存监听地址和用于界面展示的线程安全回调。"""

        self.host = host
        self.port = port
        self.on_message = on_message or (lambda _message: None)
        self.on_status = on_status or (lambda _connected, _message: None)
        self._stop_event = threading.Event()
        self._ready_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._server_socket: socket.socket | None = None
        self._client_socket: socket.socket | None = None
        self.bound_port = port
        self._seen_message_ids: set[int] = set()
        self._seen_order: deque[int] = deque()

    def start(self, wait_ready_seconds: float = 1.0) -> None:
        """启动监听线程，并短暂等待端口绑定结果以便立即报告占用错误。"""

        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._ready_event.clear()
        self._seen_message_ids.clear()
        self._seen_order.clear()
        self._thread = threading.Thread(target=self._run, name="RobotSimulatorServer", daemon=True)
        self._thread.start()
        self._ready_event.wait(wait_ready_seconds)

    def stop(self) -> None:
        """关闭监听和已连接客户端，使阻塞中的 socket 立刻退出。"""

        self._stop_event.set()
        for connection in (self._client_socket, self._server_socket):
            if connection is not None:
                try:
                    connection.shutdown(socket.SHUT_RDWR)
                except OSError:
                    pass
                try:
                    connection.close()
                except OSError:
                    pass
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    @staticmethod
    def _ack_for(message: JsonObject, duplicate: bool = False) -> JsonObject:
        """根据协议标识构造成功或拒绝 ACK，并回显消息与采样序号。"""

        accepted = message.get("schema") == "pvdf.touch.v1" and isinstance(message.get("message_id"), int)
        return {
            "schema": "pvdf.ack.v1",
            "version": 1,
            "type": "ack",
            "ack_message_id": message.get("message_id"),
            "ack_sequence": message.get("sequence"),
            "accepted": accepted,
            "robot_time": iso_now(),
            "action": "data_received" if accepted else "rejected",
            "detail": (
                "重复message_id已去重，不重复执行动作"
                if accepted and duplicate
                else "模拟机器人已接收动态触摸数据"
                if accepted
                else "消息schema或message_id无效"
            ),
        }

    def _is_duplicate(self, message: JsonObject) -> bool:
        """在固定4096条窗口内按message_id去重，防止网络重试重复执行机器人动作。"""

        message_id = message.get("message_id")
        if not isinstance(message_id, int):
            return False
        if message_id in self._seen_message_ids:
            return True
        self._seen_message_ids.add(message_id)
        self._seen_order.append(message_id)
        if len(self._seen_order) > 4096:
            expired = self._seen_order.popleft()
            self._seen_message_ids.discard(expired)
        return False

    def _serve_client(self, client: socket.socket) -> None:
        """解析一个客户端的换行分帧数据，并限制单条消息大小防止内存失控。"""

        client.settimeout(0.20)
        buffer = bytearray()
        while not self._stop_event.is_set():
            try:
                data = client.recv(8192)
            except socket.timeout:
                continue
            if not data:
                break
            buffer.extend(data)
            if len(buffer) > 1_048_576:
                raise ValueError("单条NDJSON消息超过1MB限制")
            while b"\n" in buffer:
                raw_line, _, remainder = buffer.partition(b"\n")
                buffer = bytearray(remainder)
                if not raw_line.strip():
                    continue
                try:
                    message = json.loads(raw_line.decode("utf-8"))
                    if not isinstance(message, dict):
                        raise ValueError("JSON顶层必须是对象")
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as error:
                    error_ack = {
                        "schema": "pvdf.ack.v1",
                        "version": 1,
                        "type": "ack",
                        "ack_message_id": None,
                        "ack_sequence": None,
                        "accepted": False,
                        "robot_time": iso_now(),
                        "action": "rejected",
                        "detail": f"无法解析消息：{error}",
                    }
                    client.sendall((json.dumps(error_ack, ensure_ascii=False) + "\n").encode("utf-8"))
                    continue
                duplicate = self._is_duplicate(message)
                if not duplicate:
                    self.on_message(message)
                ack = self._ack_for(message, duplicate=duplicate)
                client.sendall((json.dumps(ack, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8"))

    def _run(self) -> None:
        """持续接受客户端；课堂电脑断开后仍可等待下一次自动重连。"""

        try:
            server = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
            self._server_socket = server
            server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            server.bind((self.host, self.port))
            self.bound_port = server.getsockname()[1]
            server.listen(2)
            server.settimeout(0.20)
            self._ready_event.set()
            self.on_status(True, f"模拟机器人监听 {self.host}:{self.bound_port}")
            while not self._stop_event.is_set():
                try:
                    client, address = server.accept()
                except socket.timeout:
                    continue
                self._client_socket = client
                self.on_status(True, f"模拟机器人已连接客户端 {address[0]}:{address[1]}")
                try:
                    self._serve_client(client)
                except (OSError, ValueError) as error:
                    if not self._stop_event.is_set():
                        self.on_status(False, f"模拟机器人客户端中断：{error}")
                finally:
                    try:
                        client.close()
                    except OSError:
                        pass
                    self._client_socket = None
        except OSError as error:
            self._ready_event.set()
            if not self._stop_event.is_set():
                self.on_status(False, f"模拟机器人无法监听 {self.host}:{self.port}：{error}")
        finally:
            server = self._server_socket
            self._server_socket = None
            if server is not None:
                try:
                    server.close()
                except OSError:
                    pass
            self.on_status(False, "模拟机器人已停止")


class RobotClient:
    """异步发送最新 telemetry 和不可丢弃的事件，并根据 ACK 计算往返延迟。"""

    def __init__(
        self,
        host: str = "127.0.0.1",
        port: int = 18080,
        on_status: NetworkStatusCallback | None = None,
        on_ack: AckCallback | None = None,
        on_sent: MessageCallback | None = None,
    ) -> None:
        """建立发送队列；高频telemetry只保留最新一条以防网络阻塞堆积。"""

        self.host = host
        self.port = port
        self.on_status = on_status or (lambda _connected, _message: None)
        self.on_ack = on_ack or (lambda _ack, _latency: None)
        self.on_sent = on_sent or (lambda _message: None)
        self._event_queue: queue.Queue[JsonObject] = queue.Queue()
        self._latest_telemetry: JsonObject | None = None
        self._telemetry_lock = threading.Lock()
        self._config_lock = threading.RLock()
        self._timing_lock = threading.Lock()
        self._queued_at: dict[int, tuple[float, str]] = {}
        self._stop_event = threading.Event()
        self._reconfigure_event = threading.Event()
        self._connected_event = threading.Event()
        self._thread: threading.Thread | None = None
        self._socket: socket.socket | None = None

    def start(self) -> None:
        """启动连接线程，目标暂时不可达时会每半秒自动重试。"""

        if self._thread and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(target=self._run, name="RobotTcpClient", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        """关闭连接并等待发送线程退出。"""

        self._stop_event.set()
        self._close_socket()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2.0)

    def configure(self, host: str, port: int) -> None:
        """验证并应用新目标，主动断开旧连接以触发无缝重连。"""

        if not host.strip():
            raise ValueError("机器人IP或主机名不能为空")
        if not 1 <= port <= 65535:
            raise ValueError("机器人端口必须在1..65535之间")
        with self._config_lock:
            self.host = host.strip()
            self.port = port
        self._reconfigure_event.set()
        self._close_socket()

    def send(self, payload: JsonObject) -> None:
        """排队发送；断线时丢弃事件，避免重连后把过期触摸当作新机器人指令。"""

        if payload.get("type") == "telemetry":
            # 必须先登记计时再发布给网络线程，避免极快ACK先到、随后留下悬空记录。
            self._remember_enqueue_time(payload)
            with self._telemetry_lock:
                replaced = self._latest_telemetry
                self._latest_telemetry = payload
            if replaced is not None:
                self._forget_timing(replaced)
        else:
            if self._connected_event.is_set():
                self._remember_enqueue_time(payload)
                self._event_queue.put(payload)
            else:
                self.on_status(False, f"未连接机器人，已安全丢弃过期事件 {payload.get('type')}")

    def _next_payload(self) -> JsonObject | None:
        """事件优先于周期状态，空闲时取走最新 telemetry。"""

        try:
            return self._event_queue.get_nowait()
        except queue.Empty:
            with self._telemetry_lock:
                payload = self._latest_telemetry
                self._latest_telemetry = None
                return payload

    def _put_back(self, payload: JsonObject) -> None:
        """发送失败后不重放旧事件；telemetry由下一周期的新状态自然覆盖。"""

        if payload.get("type") != "telemetry":
            self.on_status(False, f"连接中断，已安全丢弃未确认事件 {payload.get('type')}")
        self._forget_timing(payload)

    def _remember_enqueue_time(self, payload: JsonObject) -> None:
        """记录控制器识别并入队的时刻，使事件延迟包含本机排队而不只计算socket时间。"""

        message_id = payload.get("message_id")
        if not isinstance(message_id, int):
            return
        with self._timing_lock:
            self._queued_at[message_id] = (time.perf_counter(), str(payload.get("type", "unknown")))

    def _forget_timing(self, payload: JsonObject) -> None:
        """消息被覆盖或安全丢弃时同步清除延迟记录。"""

        message_id = payload.get("message_id")
        if isinstance(message_id, int):
            with self._timing_lock:
                self._queued_at.pop(message_id, None)

    def _clear_timings(self) -> None:
        """连接代际变化时清除不可能再收到ACK的旧记录。"""

        with self._timing_lock:
            self._queued_at.clear()

    def _discard_pending_events(self) -> None:
        """连接代际变化时清空旧事件，防止实体机器人收到迟到动作。"""

        while True:
            try:
                payload = self._event_queue.get_nowait()
                self._forget_timing(payload)
            except queue.Empty:
                return

    def _close_socket(self) -> None:
        """线程安全地关闭当前socket，使配置和退出立即生效。"""

        with self._config_lock:
            connection = self._socket
            self._socket = None
        self._connected_event.clear()
        if connection is not None:
            try:
                connection.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                connection.close()
            except OSError:
                pass

    def _connect(self) -> socket.socket:
        """读取一致的目标快照并建立带短超时的TCP连接。"""

        with self._config_lock:
            host, port = self.host, self.port
        connection = socket.create_connection((host, port), timeout=0.5)
        connection.settimeout(0.05)
        with self._config_lock:
            self._socket = connection
        self._connected_event.set()
        self.on_status(True, f"已连接机器人 {host}:{port}")
        return connection

    def _consume_ack_lines(self, buffer: bytearray) -> bytearray:
        """解析ACK并计算从事件识别入队到收到ACK的端到端毫秒数。"""

        while b"\n" in buffer:
            raw_line, _, remainder = buffer.partition(b"\n")
            buffer = bytearray(remainder)
            if not raw_line.strip():
                continue
            try:
                ack = json.loads(raw_line.decode("utf-8"))
                if not isinstance(ack, dict):
                    continue
            except (UnicodeDecodeError, json.JSONDecodeError):
                continue
            message_id = ack.get("ack_message_id")
            with self._timing_lock:
                queued_record = self._queued_at.pop(message_id, None) if isinstance(message_id, int) else None
            latency_ms = (time.perf_counter() - queued_record[0]) * 1000.0 if queued_record is not None else None
            annotated_ack = dict(ack)
            if queued_record is not None:
                # 下划线字段仅供本机界面分类延迟，不属于公开TCP ACK协议。
                annotated_ack["_local_sent_type"] = queued_record[1]
            self.on_ack(annotated_ack, latency_ms)
        return buffer

    def _expire_unacknowledged_messages(self) -> None:
        """清理30秒前仍未ACK的发送记录，保证长时间演示不会无限占用内存。"""

        with self._timing_lock:
            if len(self._queued_at) < 1024:
                return
            cutoff = time.perf_counter() - 30.0
            expired = [message_id for message_id, (queued_at, _kind) in self._queued_at.items() if queued_at < cutoff]
            for message_id in expired:
                self._queued_at.pop(message_id, None)

    def _run(self) -> None:
        """维护连接、发送队列和ACK接收缓冲，直到应用退出。"""

        connection: socket.socket | None = None
        receive_buffer = bytearray()
        last_error = ""
        while not self._stop_event.is_set():
            if self._reconfigure_event.is_set():
                self._reconfigure_event.clear()
                connection = None
                receive_buffer.clear()
                self._discard_pending_events()
                self._clear_timings()
            if connection is None:
                try:
                    connection = self._connect()
                    receive_buffer.clear()
                    last_error = ""
                except OSError as error:
                    message = f"等待机器人 {self.host}:{self.port}：{error}"
                    if message != last_error:
                        self.on_status(False, message)
                        last_error = message
                    self._stop_event.wait(0.5)
                    continue
            payload = self._next_payload()
            self._expire_unacknowledged_messages()
            try:
                if payload is not None:
                    encoded = (json.dumps(payload, ensure_ascii=False, separators=(",", ":")) + "\n").encode("utf-8")
                    connection.sendall(encoded)
                    message_id = payload.get("message_id")
                    if isinstance(message_id, int):
                        # 兼容内部或测试直接注入队列的消息；正常路径保留更早的识别入队时间。
                        with self._timing_lock:
                            self._queued_at.setdefault(
                                message_id,
                                (time.perf_counter(), str(payload.get("type", "unknown"))),
                            )
                    self.on_sent(payload)
                try:
                    data = connection.recv(8192)
                    if not data:
                        raise ConnectionError("机器人关闭了连接")
                    receive_buffer.extend(data)
                    receive_buffer = self._consume_ack_lines(receive_buffer)
                except socket.timeout:
                    pass
                if payload is None:
                    self._stop_event.wait(0.010)
            except OSError as error:
                if payload is not None:
                    self._put_back(payload)
                self.on_status(False, f"机器人连接中断：{error}；正在重连")
                self._close_socket()
                connection = None
                receive_buffer.clear()
                self._discard_pending_events()
                self._clear_timings()
        self._close_socket()
        self.on_status(False, "机器人连接已停止")
