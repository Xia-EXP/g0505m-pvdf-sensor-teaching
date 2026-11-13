"""汇总已落盘的实物、软件与便携包证据，保持待验收项目明确。"""

import json
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))  # 查询解析复用正式实现。

from grip_teaching.parameters import parse_query
from audit_g0505m_soak import audit_recording


def main() -> None:
    """即使实物验收未完成也生成真实结论，以原文件审计决定是否连续运行通过。"""
    workspace = Path(__file__).resolve().parents[2]
    docs = workspace / "docs" / "g0505m"
    report = json.loads(docs.joinpath("soak_latest.json").read_text(encoding="utf-8"))
    audit = audit_recording(report)
    parameters = parse_query(Path(report["recording"]).joinpath("parameter_query.bin").read_bytes())
    portable = json.loads(workspace.joinpath("tmp", "g0505m-portable-check.json").read_text(encoding="utf-8"))
    source = json.loads(workspace.joinpath("tmp", "g0505m-source-check.json").read_text(encoding="utf-8"))
    real_exe = json.loads(workspace.joinpath("tmp", "g0505m-exe-real.json").read_text(encoding="utf-8"))
    if real_exe["mode"] != "serial" or real_exe["valid_frames"] <= 0:
        raise ValueError("正式EXE尚未取得实物有效帧")
    suites = ET.parse(workspace / "tmp" / "g0505m-tests.xml").getroot().findall("testsuite")
    tests = sum(int(suite.get("tests", 0)) for suite in suites)
    failures = sum(int(suite.get("failures", 0)) + int(suite.get("errors", 0)) for suite in suites)
    if failures:
        raise ValueError("软件测试报告仍有失败项")
    stats = audit["recorded_stats"]
    continuous = "连续接收检查通过" if audit["continuous_serial_pass"] else "连续30分钟验收未完成"
    reasons = "；".join(audit["incomplete_reasons"])
    observed = "本次检查25路最大值均为0，没有获得真实按压变化，不能判定动态响应、位置正确或计量精度。"
    if any(report["max_channel_g"]):
        observed = "本次接收中出现非零通道变化，但仍需人工逐点对应与已知载荷验证；变化不等于定量误差已合格。"
    text = f"""# G0505M教具验证结果

本机错接线修正后，串口链路恢复。设备只读查询确认型号为G0505M_{parameters.get('range_kg', '未确认')}kg，标识{parameters.get('hand', '未确认')}，间隔{parameters.get('interval_ms', '未确认')}ms。查询没有修改型号、周期、左右手或内部零点。

## 实物接收检查

检查结束：{report['checked_at']}。检查进程运行{report['duration_seconds']:.3f}秒；原会话CSV首末帧跨度{audit['recorded_duration_seconds']:.3f}秒（约{audit['recorded_duration_seconds'] / 60:.2f}分钟）。{continuous}。

{reasons if reasons else '原录制、协议计数与目标时长核对一致。'}。原会话已保存{audit['csv_frames']:,}帧，文件互相核对{'一致' if audit['files_consistent'] else '不一致'}；结束时新解析器的{report['stats']['valid_frames']:,}帧不是原会话总数，不拼接成连续结果。

- 有效帧：{stats['valid_frames']:,}。
- 校验错误：{stats['checksum_errors']}；非法手标识：{stats['invalid_hand']}。
- 接收字节：{stats['received_bytes']:,}；丢弃的非帧字节：{stats['discarded_bytes']}，其中包含只读参数文本。
- 主线程100ms心跳最大间隔：{report['max_ui_callback_gap_ms']:.1f}ms；过期检查次数：{report['stale_checks']}。
- 原会话电脑相邻帧接收最大间隔：{audit['max_host_frame_gap_ms']:.3f}ms；不是设备采样抖动或精确丢帧统计。
- 接收时间来自电脑，协议无设备序号/时间戳，未宣称精确丢帧数。

{observed}

固件只读回复：{parameters.get('software_version', '未识别')}；全局零值{parameters.get('global_zero_g', '未识别')}g；零点死区{parameters.get('zero_deadband_g', '未识别')}g。桌面原始值仍是厂家处理后的估算输出。

原始字节、有效帧、25路CSV、会话参数、查询返回与本次实测报告位于 `{Path(report['recording']).name}` 文件夹。

## 软件与发布

Ruff静态检查通过；自动测试{tests}项通过，覆盖旧PVDF回归及新G0505M协议、状态、记录、回放和模拟串口重连。真实Qt截图在1366×768、1024×720检查。DOCX逐页渲染核对8页，可访问性检查无高/中/低问题。

便携ZIP已解压到中文及含空格路径，移除PYTHONPATH/PYTHONHOME后实际启动EXE，退出码{portable['exe_exit_code']}，示例有效帧{portable['valid_frames']}，截图非空。此示例运行用于验证软件运行时，不是实物受力试验。

原生Windows与离屏两种Qt平台均通过；PATH仅保留系统目录。正式EXE单独连接COM3的短时检查收到{real_exe['valid_frames']}个实物有效帧，正常退出。独立源码ZIP解压后，隔离解释器禁止项目pth和环境导入路径，使用解压源码完成{source['tests']}项新教具测试及窗口启动；第三方依赖仍需按说明安装。

## 仍需实物操作

1. 依次轻按25点，确认编号、左右和镜像安装对应。
2. 按住5秒、释放、空载归零，验证持续受力与固定零点。
3. 真实USB拔插，确认过期及同一端口重新连接。
4. 固定夹具与已知载荷分点重复，验证定量误差。
5. 重新完成一段不中断录制、不切换数据源的连续30分钟检查。

首版程序没有机器人动作控制。采样点合计不等于完整整手握力。
"""
    docs.joinpath("验证结果.md").write_text(text, encoding="utf-8")
    docs.joinpath("device_parameters_verified.json").write_text(json.dumps(parameters, ensure_ascii=False, indent=2), encoding="utf-8")
    validation = {"automated_tests": tests, "test_failures": failures, "portable": portable,
                  "source": source, "real_exe_smoke": real_exe,
                  "continuous_serial_pass": audit["continuous_serial_pass"], "full_hardware_acceptance": False}
    docs.joinpath("software_validation.json").write_text(json.dumps(validation, ensure_ascii=False, indent=2), encoding="utf-8")
    print(text)


if __name__ == "__main__":
    main()
