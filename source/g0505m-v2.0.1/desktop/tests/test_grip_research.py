"""2.0 教学研究功能的纯逻辑测试：事件分析、引导实验状态机、标定拟合与 Revo2 运动学。"""

import math

import numpy as np
import pytest

from grip_teaching import analysis, calibration, experiments, revo2_model
from grip_teaching.calibration import Calibration, Sample


def press_curve(rate=20, rest=1.0, rise=0.3, hold=2.0, level=500.0, drift=0.0, residual=0.0):
    """合成一次按压：静止→上升→带漂移的保持→下降→残留；返回 (t, y)。"""
    t = np.arange(0, rest + rise + hold + rise + 3.0, 1 / rate)
    y = np.zeros_like(t)
    a, b, c, d = rest, rest + rise, rest + rise + hold, rest + rise + hold + rise
    up = (t >= a) & (t < b)
    y[up] = level * (t[up] - a) / rise
    keep = (t >= b) & (t < c)
    y[keep] = level * (1 + drift * (t[keep] - b))
    down = (t >= c) & (t < d)
    end_level = level * (1 + drift * hold)
    y[down] = end_level * (1 - (t[down] - c) / rise)
    y[t >= d] = residual
    return t, y


def test_detect_single_event_metrics():
    """峰值、保持均值、漂移率和残留应与合成参数一致。"""
    t, y = press_curve(drift=0.02, residual=10.0)
    events = analysis.detect_events(t, y, 3)
    assert len(events) == 1
    event = events[0]
    assert event.channel == 3
    assert event.peak_g == pytest.approx(520, rel=0.02)
    assert event.hold_drift_pct_per_s == pytest.approx(2.0, abs=0.4)
    assert event.residual_g == pytest.approx(10.0)
    assert 2.0 < event.duration_s < 3.0


def test_hysteresis_band_prevents_chatter():
    """在开始阈值附近抖动但不低于释放阈值时，只算一次按压。"""
    t = np.arange(0, 3, 0.05)
    y = np.where((t > 0.5) & (t < 2.5), 41 + 15 * np.sin(t * 40), 0.0)
    assert len(analysis.detect_events(t, y, 1)) == 1


def test_short_spike_ignored():
    """短于最小时长的尖峰不算按压。"""
    t = np.arange(0, 2, 0.05)
    y = np.where((t > 1.0) & (t < 1.1), 300.0, 0.0)
    assert analysis.detect_events(t, y, 1) == []


def test_residual_invalid_when_next_press_follows_quickly():
    """下一次按压落在残留观察窗内时，残留不可评价（NaN），不能把新按压读成残留。"""
    t = np.arange(0, 6, 0.05)
    y = np.where(((t > 1) & (t < 2)) | ((t > 2.6) & (t < 3.6)), 400.0, 0.0)
    events = analysis.detect_events(t, y, 1)
    assert len(events) == 2
    assert math.isnan(events[0].residual_g)
    assert events[1].residual_g == pytest.approx(0.0)


def test_short_hold_drift_is_not_reported_as_zero():
    """保持段不足 0.5 s 无法评价漂移，应为 NaN 而不是 0。"""
    t = np.arange(0, 3, 0.05)
    y = np.where((t > 1) & (t < 1.3), 300.0, 0.0)
    assert math.isnan(analysis.detect_events(t, y, 1)[0].hold_drift_pct_per_s)


def test_repeat_stats_requires_two_samples():
    """单次数据不能评价重复性；CV 使用样本标准差。"""
    assert analysis.repeat_stats([100.0]) is None
    stats = analysis.repeat_stats([90.0, 100.0, 110.0])
    assert stats.mean == pytest.approx(100)
    assert stats.cv_pct == pytest.approx(10.0)


def test_zone_shares_ignore_negative_values():
    """负净值（零点漂移）不参与占比，全零时不除零。"""
    values = [0] * 25
    values[0], values[20], values[1] = 300, 100, -50
    shares = analysis.zone_shares(values)
    assert shares[0] == pytest.approx(0.75) and shares[5] == pytest.approx(0.25)
    assert analysis.zone_shares([0] * 25) == [0.0] * 6


def run(runner, frames, rate=20, start=0.0):
    """按给定帧率喂入帧序列，返回最后时间。"""
    t = start
    for values in frames:
        runner.feed(runner.hand, t, tuple(values))
        t += 1 / rate
    return t


def frame(**points):
    """生成 25 点帧，参数形如 c3=200。"""
    values = [0] * 25
    for key, value in points.items():
        values[int(key[1:]) - 1] = value
    return values


def test_point_check_detects_match_and_mismatch():
    """按对的点记为一致；按错时如实记录响应最大的点。"""
    runner = experiments.ExperimentRunner(experiments.point_check(), "R")
    t = run(runner, [frame()] * 40)
    assert runner.needs_tare and runner.index == 1
    runner.acknowledge_tare()
    t = run(runner, [frame(c1=300)] * 10 + [frame()] * 5, start=t)
    t = run(runner, [frame(c7=300, c2=100)] * 10 + [frame()] * 5, start=t)
    summary = runner.summary()
    assert summary["checked"] == 2 and summary["matched"] == 1
    assert summary["mismatches"][0]["target"] == 2 and summary["mismatches"][0]["responded"] == 7


def test_rest_requires_continuous_quiet():
    """松手判定被中途按压打断时要重新计时。"""
    runner = experiments.ExperimentRunner(experiments.point_check(), "L")
    t = run(runner, [frame()] * 20 + [frame(c5=100)] + [frame()] * 20)
    assert runner.index == 0
    run(runner, [frame()] * 15, start=t)
    assert runner.index == 1


def test_other_hand_frames_are_ignored():
    """实验只处理所选手。"""
    runner = experiments.ExperimentRunner(experiments.point_check(), "L")
    assert runner.feed("R", 0.0, tuple([0] * 25)) is False
    assert runner.frames == []


def test_light_heavy_summary_and_skip():
    """轻中重峰值单调且统计重复性；跳过的步骤不进入统计。"""
    runner = experiments.ExperimentRunner(experiments.light_heavy(channel=2, repeats=2), "L")
    t = run(runner, [frame()] * 40)
    runner.acknowledge_tare()
    for level in (100, 300, 600, 110, 320):
        t = run(runner, [frame(c2=level)] * 15 + [frame()] * 5, start=t)
    runner.skip(t)
    assert runner.finished
    summary = runner.summary()
    assert summary["monotonic"]
    assert summary["levels"]["轻"]["peaks"] == [100, 110]
    assert summary["levels"]["重"]["peaks"] == [600]


def test_hold_release_measures_drift_and_residual():
    """保持期间读数下降应得到负漂移；松手后的残留被记录。"""
    runner = experiments.ExperimentRunner(experiments.hold_release(seconds=4), "L")
    t = run(runner, [frame()] * 40)
    runner.acknowledge_tare()
    hold = [frame(c1=round(500 - 10 * i / 20), c2=round(400 - 8 * i / 20)) for i in range(100)]
    t = run(runner, hold, start=t)
    assert runner.step.kind == "release"
    run(runner, [frame(c1=12)] * 80, start=t)
    summary = runner.summary()
    assert runner.finished
    assert summary["sum_drift_pct_per_s"] < -1.0
    assert summary["residual_max_g"] == pytest.approx(12) and summary["residual_channel"] == 1


def test_objects_summary_groups_by_name():
    """同一物体多次抓握合并统计接触点数和主要用力区域。"""
    runner = experiments.ExperimentRunner(experiments.objects(("球",), repeats=2), "R")
    t = run(runner, [frame()] * 40)
    runner.acknowledge_tare()
    for _ in range(2):
        t = run(runner, [frame(c21=400, c22=300, c1=100)] * 20 + [frame()] * 5, start=t)
    table = runner.summary()["objects"]["球"]
    assert table["trials"] == 2 and table["contact_points"] == 3
    assert table["top_zone_index"] == 5


def test_check_levels_enforces_safety():
    """必须登记量程、包含 0 g 且不超过 80% 量程。"""
    with pytest.raises(ValueError, match="量程"):
        calibration.check_levels([0, 100, 200], None)
    with pytest.raises(ValueError, match="0 g"):
        calibration.check_levels([100, 200, 300], 5)
    with pytest.raises(ValueError, match="80%"):
        calibration.check_levels([0, 1000, 4500], 5)
    assert calibration.check_levels([500, 0, 200], 5) == [0, 200, 500]


def test_plan_steps_load_then_unload():
    """升序加载、降序卸载，顶点只出现一次。"""
    assert calibration.plan_steps([0, 100, 200]) == [(0, "up"), (100, "up"), (200, "up"), (100, "down"), (0, "down")]


def test_stable_mean_window_and_rejection():
    """只取放好后 3–5 s 的数据；波动大时拒绝。"""
    samples = [(i / 20, 1000.0 if i / 20 < 3 else 500.0) for i in range(120)]
    mean, _, message = calibration.stable_mean(samples, 0.0)
    assert mean == pytest.approx(500) and message == "已稳定"
    noisy = [(i / 20, 500.0 + (60 if i % 2 else -60)) for i in range(120)]
    assert calibration.stable_mean(noisy, 0.0)[0] is None
    assert calibration.stable_mean(samples[:10], 0.0)[0] is None


def test_fit_linear_with_hysteresis_and_apply(tmp_path):
    """已知线性关系应还原斜率截距；卸载偏高体现为迟滞；保存后读回一致且只修正已标定点。"""
    samples = []
    for load in (0, 200, 500, 1000):
        samples.append(Sample(load, "up", load / 1.25 + 20, 1.0))
    for load in (500, 200, 0):
        samples.append(Sample(load, "down", load / 1.25 + 28, 1.0))
    result = calibration.fit("R", 6, samples, 10.0, {"type": "G0505M_5kg"})
    assert result.slope == pytest.approx(1.25, rel=0.02)
    assert result.r2 > 0.999
    assert result.hysteresis_pct_fs == pytest.approx(1.0, abs=0.2)
    assert result.repeatability_pct_fs is None
    cal = Calibration("R", {6: result})
    path = tmp_path / "cal.json"
    cal.save(path)
    loaded = Calibration.load(path)
    values = tuple([100] * 25)
    applied = loaded.apply(values)
    assert applied[0] == 100 and applied[5] == round(result.apply(100))
    assert loaded.channels[6].samples[0] == samples[0]


def test_fit_rejects_flat_readings():
    """读数不随载荷变化说明压错位置，必须拒绝而不是给出无意义斜率。"""
    samples = [Sample(load, "up", 50.0, 1.0) for load in (0, 200, 500)]
    with pytest.raises(ValueError, match="没有变化"):
        calibration.fit("L", 1, samples, 10.0)


def test_calibration_load_rejects_foreign_file(tmp_path):
    """其他格式的 JSON 不能被当作标定文件。"""
    path = tmp_path / "x.json"
    path.write_text('{"format": "other"}', encoding="utf-8")
    with pytest.raises(ValueError):
        Calibration.load(path)


@pytest.mark.parametrize("side", ["L", "R"])
def test_revo2_model_loads_and_moves(side):
    """两只手都应有 6 个主动关节；弯曲食指会让指尖向掌面（+x）移动且关节限位生效。"""
    model = revo2_model.load_hand(side)
    assert len(model.active_joints) == 6
    name = model.joint("index_proximal_joint").name
    straight = model.link_transforms({})[f"{model.side}_index_distal_link"][:3, 3]
    bent = model.link_transforms({name: 1.0})[f"{model.side}_index_tip_link"][:3, 3]
    straight_tip = model.link_transforms({})[f"{model.side}_index_tip_link"][:3, 3]
    assert bent[0] > straight_tip[0] + 0.02
    over = model.link_transforms({name: 99.0})[f"{model.side}_index_distal_link"]
    limit = model.link_transforms({name: model.joint("index_proximal_joint").upper})[f"{model.side}_index_distal_link"]
    assert np.allclose(over, limit)
    assert straight[2] > 0.1


def test_revo2_meshes_are_valid_binary_stl():
    """随包 STL 全部能读出三角形，防止打包遗漏或文件损坏。"""
    for side in ("L", "R"):
        model = revo2_model.load_hand(side)
        for visual in model.visuals:
            triangles, normals = revo2_model.load_stl(visual.mesh)
            assert triangles.shape[1:] == (3, 3) and len(triangles) == len(normals) > 100


def test_pose_mapping_is_monotonic_and_bounded():
    """受力越大弯曲越多，超过上限饱和；无数据时保持张开。"""
    model = revo2_model.load_hand("R")
    joint = model.joint("middle_proximal_joint")
    angles = [revo2_model.pose_from_values(model, tuple(frame(c3=v)))[joint.name] for v in (0, 200, 600, 5000)]
    assert angles[0] == 0 and angles[0] < angles[1] < angles[2] <= angles[3] == pytest.approx(joint.upper)
    assert all(value == 0 for value in revo2_model.pose_from_values(model, None).values())


def test_sensor_anchors_cover_all_channels():
    """25 个锚点都挂在存在的连杆上；左右手掌部锚点关于 y 镜像。"""
    for side in ("L", "R"):
        model = revo2_model.load_hand(side)
        links = model.link_transforms({})
        anchors = revo2_model.sensor_anchors(model)
        assert len(anchors) == 25 and all(link in links for link, _ in anchors)
    right = revo2_model.sensor_anchors(revo2_model.load_hand("R"))[20][1]
    left = revo2_model.sensor_anchors(revo2_model.load_hand("L"))[20][1]
    assert right[1] == pytest.approx(-left[1]) and not math.isclose(right[1], 0)
