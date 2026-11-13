"""用实际新固件文本格式验证型号识别，避免盲目硬编码为某个量程。"""

from grip_teaching.parameters import parse_query


def test_actual_firmware_response() -> None:
    """允许周期二进制帧与参数文本同到，内部零点与死区也保留。"""
    data = b"\x40\x32\x00\x72Set time:50 ms\r\n\r\nsoftware version:V.2026-8-7M-r5\r\n\r\ntype:G0505M_5kg\r\n"
    data += b"Zero deadband:3 g\r\nGlobal zero value:20 g\r\nRight Hand!\r\n"
    assert parse_query(data) == {"range_kg": 5, "interval_ms": 50, "software_version": "V.2026-8-7M-r5",
                                 "zero_deadband_g": 3, "global_zero_g": 20, "hand": "R"}


def test_unknown_or_conflicting_query_not_confirmed() -> None:
    """错误类型和互相矛盾的返回不得把未知量程改成确认状态。"""
    assert parse_query(b"type:another_5kg\r\n") == {}
    assert "range_kg" not in parse_query(b"type:G0505M_2kg\r\ntype:G0505M_10kg\r\n")
