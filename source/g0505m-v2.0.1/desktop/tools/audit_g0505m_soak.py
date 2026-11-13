"""核对真实录制文件，防止用检查进程的运行时长冒充连续采集验收。"""

import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # 作用：使用正式协议核对实际接收字节。

from grip_teaching.protocol import FrameParser


def audit_recording(report: dict) -> dict:
    """以CSV、原始帧和会话结束计数交叉核对；无序号协议仅统计电脑实际收到的数据。"""
    folder = Path(report["recording"])
    metadata = json.loads(folder.joinpath("session.json").read_text(encoding="utf-8"))
    count = nonzero = 0
    first_ns = previous_ns = last_ns = None
    max_gap_ns = 0
    with folder.joinpath("channels.csv").open(encoding="utf-8-sig", newline="") as stream:
        for row in csv.DictReader(stream):
            received_ns = int(row["received_ns"])
            if previous_ns is not None:
                if received_ns < previous_ns:
                    raise ValueError("录制电脑时间倒退，不能认定连续接收")
                max_gap_ns = max(max_gap_ns, received_ns - previous_ns)
            first_ns = received_ns if first_ns is None else first_ns
            previous_ns = last_ns = received_ns
            count += 1
            nonzero += any(int(row[f"raw_{index:02d}_g"]) for index in range(1, 26))
    parser = FrameParser()
    parser.feed(folder.joinpath("received_bytes.bin").read_bytes(), 0)
    raw = folder.joinpath("valid_frames.bin").read_bytes()
    span = (last_ns - first_ns) / 1e9 if count else 0.0
    start_stats = metadata.get("parser_at_start", {})
    stop_stats = metadata.get("parser_at_stop", {})
    recorded_stats = {key: value - start_stats.get(key, 0) for key, value in stop_stats.items()}
    consistent = (count == metadata.get("recorded_frames") == parser.stats.valid_frames
                  and len(raw) == count * 53 and recorded_stats.get("valid_frames") == count)
    # 作用：旧检查程序只报告最后一个解析器；结束计数少于已保存计数时已发生重置，不能把两段合成一次连续验收。
    final_stats = report.get("stats", {})
    reset = final_stats.get("valid_frames", 0) < stop_stats.get("valid_frames", 0)
    source_changes = report.get("source_changes", [])
    reasons = []
    if not consistent:
        reasons.append("CSV、有效原始帧与会话计数不一致")
    if span < max(0.0, report["target_seconds"] - 2.0):
        reasons.append("原始录制时长不足目标，录制提前停止")
    if reset or source_changes:
        reasons.append("检查后段解析计数重置或数据源改变，原因未核实")
    if report.get("recording_interrupted"):
        reasons.append("检查期间原录制会话停止或被替换")
    if report.get("stale_checks") or max_gap_ns > 1_000_000_000:
        reasons.append("检查期间出现数据过期")
    if report.get("errors") or parser.stats.checksum_errors or parser.stats.invalid_hand:
        reasons.append("存在连接、记录或协议错误")
    if not report.get("completed_duration"):
        reasons.append("检查进程未完成目标时长")
    return {"csv_frames": count, "recorded_duration_seconds": round(span, 6),
            "max_host_frame_gap_ms": round(max_gap_ns / 1e6, 3), "nonzero_frames": nonzero,
            "files_consistent": consistent, "recorded_stats": recorded_stats,
            "final_parser_reset_detected": reset, "continuous_serial_pass": not reasons,
            "incomplete_reasons": reasons}


def main() -> None:
    """保留原检查数据，补充审计字段并同步到原始会话，便于复核结论。"""
    workspace = Path(__file__).resolve().parents[2]
    target = workspace / "docs" / "g0505m" / "soak_latest.json"
    report = json.loads(target.read_text(encoding="utf-8"))
    report["recording_audit"] = audit_recording(report)
    report["continuous_serial_pass"] = report["recording_audit"]["continuous_serial_pass"]
    report["stats_scope"] = "final_parser_only"
    text = json.dumps(report, ensure_ascii=False, indent=2)
    target.write_text(text, encoding="utf-8")
    Path(report["recording"]).joinpath("soak_report.json").write_text(text, encoding="utf-8")
    print(json.dumps(report["recording_audit"], ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
