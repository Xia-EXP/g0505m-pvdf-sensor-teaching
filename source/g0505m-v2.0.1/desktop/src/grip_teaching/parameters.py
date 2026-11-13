"""识别新固件只读 SET=? 文本字段，未知格式保留原文而不猜配置。"""

import re


def parse_query(data: bytes) -> dict:
    """字段必须出现明确名称；互相矛盾的型号返回不会被登记为已确认。"""
    result = {}
    models = set(re.findall(rb"(?:^|[\r\n])type:\s*G0505M_(2|5|10)kg\s*(?=[\r\n]|$)", data))
    if len(models) == 1:
        result["range_kg"] = int(models.pop())
    patterns = {
        "interval_ms": rb"Set time:\s*(\d+) ms",
        "global_zero_g": rb"(?:^|[\r\n])Global zero value:\s*(\d+) g",
        "zero_deadband_g": rb"(?:^|[\r\n])Zero deadband:\s*(\d+) g",
        "software_version": rb"(?:^|[\r\n])software version:([^\r\n]+)",
    }  # 作用：内部零点和死区是厂家输出链的一部分，不能误称桌面获取了未处理的物理原始量。
    for key, pattern in patterns.items():
        matches = set(re.findall(pattern, data))
        if len(matches) == 1:
            value = matches.pop().strip()
            result[key] = value.decode("ascii", errors="replace") if key == "software_version" else int(value)
    left, right = b"Left Hand!" in data, b"Right Hand!" in data
    if left != right:
        result["hand"] = "L" if left else "R"
    return result
