"""拟人手几何与示意姿态：网格有效、受力只让对应手指弯曲、左右镜像一致、25 点贴在手上。"""

import numpy as np
import pytest

from grip_teaching import human_hand_model as human
from grip_teaching.models import CHANNEL_COUNT


def frame(**points):
    """构造 25 路帧；c1..c25 为 1 起的通道编号。"""
    values = [0] * CHANNEL_COUNT
    for key, value in points.items():
        values[int(key[1:]) - 1] = value
    return tuple(values)


def world(model, pose, segment, point):
    """段局部点 → 手腕坐标。"""
    return (model.segment_transforms(pose)[segment] @ np.append(point, 1.0))[:3]


@pytest.mark.parametrize("side", ["L", "R"])
def test_meshes_are_finite_with_unit_normals(side):
    """所有零件顶点有限、法线为单位长度，避免渲染出黑面或破面。"""
    model = human.load_human_hand(side)
    assert {part.segment for part in model.parts} >= {"palm", "thumb_distal", "pinky_distal"}
    for part in model.parts:
        assert np.isfinite(part.vertices).all()
        assert np.allclose(np.linalg.norm(part.normals, axis=2), 1.0, atol=1e-3)


def test_pressure_curls_only_loaded_finger_toward_palm():
    """只压中指：中指尖向掌侧(+x)、向下(−z)移动，食指保持放松姿态。"""
    model = human.load_human_hand("R")
    rest = human.pose_from_values(frame())
    pressed = human.pose_from_values(frame(c3=3000))
    tip = np.array((0.0, 0.0, 0.02))
    before, after = world(model, rest, "middle_distal", tip), world(model, pressed, "middle_distal", tip)
    assert after[0] > before[0] + 0.02 and after[2] < before[2] - 0.04
    assert np.allclose(world(model, rest, "index_distal", tip), world(model, pressed, "index_distal", tip))
    assert human.pose_from_values(None) == rest


def test_thumb_and_index_load_closes_pinch():
    """拇指+食指受力（捏取）：拇指转向手指一侧（y 减小），两指尖距离明显缩短。"""
    model = human.load_human_hand("R")
    tip = np.array((0.0, 0.0, 0.02))

    def tips(values):
        pose = human.pose_from_values(values)
        return world(model, pose, "thumb_distal", tip), world(model, pose, "index_distal", tip)

    thumb_rest, index_rest = tips(frame())
    thumb_pinch, index_pinch = tips(frame(c1=800, c2=800))
    assert thumb_pinch[1] < thumb_rest[1] - 0.02
    assert np.linalg.norm(thumb_pinch - index_pinch) < np.linalg.norm(thumb_rest - index_rest) - 0.02


def test_left_hand_mirrors_right():
    """左手每个测点都是右手关于 y 的镜像，保证左右手显示一致。"""
    pose = human.pose_from_values(frame(c2=900, c21=400))
    right, left = human.load_human_hand("R"), human.load_human_hand("L")
    for (seg_r, off_r), (seg_l, off_l) in zip(human.sensor_anchors("R"), human.sensor_anchors("L")):
        assert seg_r == seg_l
        point_r, point_l = world(right, pose, seg_r, off_r), world(left, pose, seg_l, off_l)
        assert np.allclose(point_l, point_r * np.array((1, -1, 1)), atol=1e-9)


def test_anchors_sit_on_palm_side_in_vendor_order():
    """25 个锚点都在掌侧；每列从指尖到根部高度递减，拇指在 +y 一侧（右手）。"""
    model = human.load_human_hand("R")
    pose = human.pose_from_values(frame())
    points = np.array([world(model, pose, segment, offset) for segment, offset in human.sensor_anchors("R")])
    assert (points[:, 0] > 0).all()
    for column in range(1, 5):
        heights = [points[row * 5 + column][2] for row in range(4)]
        assert heights == sorted(heights, reverse=True)
    assert points[0][1] > points[1:5, 1].max()
