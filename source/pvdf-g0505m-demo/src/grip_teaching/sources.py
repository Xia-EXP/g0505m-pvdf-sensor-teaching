"""独立 USB-TTL 读取线程，只发送用户发起的只读参数查询。"""

import queue
import threading
import time
from collections.abc import Callable

import serial
from serial.tools import list_ports


def available_ports() -> list[tuple[str, str]]:
    """列举端口而不自动选择陌生硬件，避免向其他设备发送查询。"""
    return [(port.device, port.description or port.device) for port in list_ports.comports()]


class SerialReader:
    """固定端口自动重连；通过回调交给 Qt 主线程，工作线程不操作界面。"""

    def __init__(self, port: str, on_data: Callable, on_status: Callable) -> None:
        """保留串口选择；供电电压与 UART 逻辑电平必须在硬件侧确认。"""
        self.port = port
        self.on_data = on_data
        self.on_status = on_status
        self.stop_event = threading.Event()
        self.commands: queue.Queue[bytes] = queue.Queue()
        self.thread: threading.Thread | None = None

    def start(self) -> None:
        """创建一条后台读线程，让没有数据时 Qt 仍然可操作。"""
        self.thread = threading.Thread(target=self._run, name="G0505M-serial", daemon=True)
        self.thread.start()

    def stop(self) -> None:
        """有限等待读取超时，不发送 START/STOP 等厂家未定义指令。"""
        self.stop_event.set()
        if self.thread:
            self.thread.join(timeout=1.5)

    def query(self) -> None:
        """固定白名单只读 SET=?，不能通过界面发送 SETF/SETM 修改设备。"""
        self.commands.put(b"SET=?\r\n")

    def _run(self) -> None:
        """打开 115200 8N1，拔插后重新打开同一端口并丢弃旧半帧。"""
        while not self.stop_event.is_set():
            try:
                with serial.Serial(
                    self.port, 115200, bytesize=serial.EIGHTBITS,
                    parity=serial.PARITY_NONE, stopbits=serial.STOPBITS_ONE,
                    timeout=0.10, write_timeout=0.5,
                ) as device:
                    self.on_status(True, f"{self.port} 已打开 · 等待有效帧")
                    while not self.stop_event.is_set():
                        try:
                            command = self.commands.get_nowait()
                        except queue.Empty:
                            command = None
                        if command:
                            device.write(command)
                        data = device.read(max(1, min(device.in_waiting, 4096)))
                        if data:
                            self.on_data(data, time.time_ns())
            except (serial.SerialException, OSError) as exc:
                if not self.stop_event.is_set():
                    self.on_status(False, f"{self.port} 连接中断，将重试：{exc}")
                    self.stop_event.wait(1.0)
