"""用假串口复验 8N1、只读查询及拔插重连；不向真实硬件发测试指令。"""

import time

import serial

from grip_teaching.sources import SerialReader


def test_passive_read_query_and_reconnect(monkeypatch) -> None:
    """默认只收数据，显式查询只发 SET=?，拔出异常后打开同一端口。"""
    parameters = []
    statuses = []
    received = []
    writes = []
    devices = []

    class FakeDevice:
        """有限读取后模拟拔出；第二次连接保持超时直到停止。"""

        def __init__(self, *args, **kwargs):
            """记录实际串口配置，防止实现悄悄改变供电相关控制线。"""
            parameters.append((args, kwargs))
            self.index = len(devices)
            self.count = 0
            self.closed = False
            devices.append(self)

        def __enter__(self):
            """模拟串口上下文，关闭事件用于检查资源释放。"""
            return self

        def __exit__(self, *args):
            """所有正常退出和异常退出都必须释放连接。"""
            self.closed = True

        @property
        def in_waiting(self):
            """让读取长度保持合法，反映串口暂时没有后续字节。"""
            return 0

        def read(self, size):
            """先返回少量拆包字节，查询完成后再触发一次拔出。"""
            self.count += 1
            time.sleep(.003)
            if self.index == 0 and self.count == 1:
                return b"\x40\x31"
            if self.index == 0 and writes:
                raise serial.SerialException("测试拔出")
            return b""

        def write(self, data):
            """记录唯一允许的只读查询字节。"""
            writes.append(data)

    monkeypatch.setattr(serial, "Serial", FakeDevice)
    reader = SerialReader("COM_FAKE", lambda data, timestamp: received.append((data, timestamp)),
                          lambda opened, message: statuses.append(opened))
    reader.start()
    deadline = time.monotonic() + 2
    try:
        while not received and time.monotonic() < deadline:
            time.sleep(.005)
        assert received and not writes
        reader.query()
        while len(devices) < 2 and time.monotonic() < deadline:
            time.sleep(.01)
        assert len(devices) == 2
    finally:
        reader.stop()
    assert writes == [b"SET=?\r\n"]
    assert statuses[:3] == [True, False, True]
    assert all(device.closed for device in devices)
    assert not reader.thread.is_alive()
    arguments, settings = parameters[0]
    assert arguments == ("COM_FAKE", 115200)
    assert settings["bytesize"] == 8 and settings["stopbits"] == 1 and settings["parity"] == "N"
