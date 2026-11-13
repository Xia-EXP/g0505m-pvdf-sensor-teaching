"""强脑 Revo2 灵巧手模型：解析 URDF、读取 STL、计算正运动学，并把 25 点受力映射为示意姿态。

本模块不依赖 Qt/OpenGL，便于单元测试。模型来自 BrainCoTech/revo2_description（Apache-2.0），
只用于屏幕显示；软件不连接、不控制任何真实机械手。

物理边界：G0505M 是压力阵列，测不到手指关节角度。这里的弯曲角度由“该手指受力”推算，
仅用于让学生直观看到“哪根手指在用力”，不得当作实测运动学数据。
"""

from __future__ import annotations

import math
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path

import numpy as np

from .models import CHANNEL_COUNT

ASSET_ROOT = Path(__file__).with_name("assets") / "revo2"  # 随包分发的 URDF、STL 和许可证。
FINGERS = ("thumb", "index", "middle", "ring", "pinky")  # 与厂家编号列顺序一致：1 拇指 … 5 小指。
FULL_CURL_G = 1200.0  # 单指受力达到该值时画成“握紧”；只影响示意动作幅度，与量程无关。
CURL_DEADBAND_G = 30.0  # 低于该值视为未用力，避免零点噪声让模型抖动。
THUMB_OPPOSE_RAD = 0.95  # 拇指用力时外展到对掌位，使拇指朝向食指而不是平贴手掌。


@dataclass(frozen=True)
class Visual:
    """一个可见零件：所属连杆、STL 文件和相对连杆的固定变换。"""

    link: str
    mesh: Path
    origin: np.ndarray


@dataclass(frozen=True)
class Joint:
    """URDF 关节；mimic 关节的角度由主动关节按倍率推出，与实物连杆联动一致。"""

    name: str
    kind: str
    parent: str
    child: str
    origin: np.ndarray
    axis: np.ndarray
    lower: float = 0.0
    upper: float = 0.0
    mimic: str | None = None
    multiplier: float = 1.0
    offset: float = 0.0


@dataclass
class HandModel:
    """一只手的完整运动学树。"""

    side: str
    root: str
    joints: list[Joint]
    visuals: list[Visual]
    children: dict[str, list[Joint]] = field(default_factory=dict)

    def __post_init__(self) -> None:
        """预先建父子索引，正运动学每帧只做矩阵乘法。"""
        for joint in self.joints:
            self.children.setdefault(joint.parent, []).append(joint)

    @property
    def active_joints(self) -> list[Joint]:
        """可独立驱动的转动关节（每只手 6 个）。"""
        return [joint for joint in self.joints if joint.kind == "revolute" and not joint.mimic]

    def joint(self, suffix: str) -> Joint:
        """按去掉 left_/right_ 前缀后的名字查关节，左右手共用同一套映射代码。"""
        name = f"{self.side}_{suffix}"
        for joint in self.joints:
            if joint.name == name:
                return joint
        raise KeyError(name)

    def link_transforms(self, angles: dict[str, float]) -> dict[str, np.ndarray]:
        """给定主动关节角（弧度），返回每个连杆相对手掌底座的 4×4 变换。

        mimic 关节按 URDF 的倍率跟随；角度会被夹在关节限位内，防止画出实物做不到的姿态。
        """
        # 作用：先把主动关节夹在自身限位内，再推算 mimic；否则超限输入会让联动关节弯过实物连杆的范围。
        driven = {joint.name: min(max(angles.get(joint.name, 0.0), joint.lower), joint.upper)
                  for joint in self.active_joints}
        resolved: dict[str, float] = {}
        for joint in self.joints:
            if joint.kind != "revolute":
                continue
            value = driven.get(joint.mimic or joint.name, 0.0)
            if joint.mimic:
                value = value * joint.multiplier + joint.offset
            resolved[joint.name] = min(max(value, joint.lower), joint.upper)
        transforms = {self.root: np.eye(4)}
        stack = [self.root]
        while stack:
            parent = stack.pop()
            for joint in self.children.get(parent, []):
                local = joint.origin
                if joint.kind == "revolute":
                    local = local @ axis_angle(joint.axis, resolved[joint.name])
                transforms[joint.child] = transforms[parent] @ local
                stack.append(joint.child)
        return transforms


def rpy_matrix(roll: float, pitch: float, yaw: float) -> np.ndarray:
    """URDF 约定的固定轴 RPY：R = Rz(yaw)·Ry(pitch)·Rx(roll)。"""
    cr, sr, cp, sp, cy, sy = (math.cos(roll), math.sin(roll), math.cos(pitch),
                              math.sin(pitch), math.cos(yaw), math.sin(yaw))
    return np.array([
        [cy * cp, cy * sp * sr - sy * cr, cy * sp * cr + sy * sr],
        [sy * cp, sy * sp * sr + cy * cr, sy * sp * cr - cy * sr],
        [-sp, cp * sr, cp * cr],
    ])


def origin_matrix(element: ET.Element | None) -> np.ndarray:
    """把 <origin xyz rpy> 转成齐次矩阵；缺省时为单位变换。"""
    matrix = np.eye(4)
    if element is None:
        return matrix
    xyz = [float(value) for value in element.get("xyz", "0 0 0").split()]
    rpy = [float(value) for value in element.get("rpy", "0 0 0").split()]
    matrix[:3, :3] = rpy_matrix(*rpy)
    matrix[:3, 3] = xyz
    return matrix


def axis_angle(axis: np.ndarray, angle: float) -> np.ndarray:
    """罗德里格斯公式：绕单位轴旋转 angle 的齐次矩阵。"""
    x, y, z = axis
    c, s = math.cos(angle), math.sin(angle)
    t = 1 - c
    matrix = np.eye(4)
    matrix[:3, :3] = [
        [t * x * x + c, t * x * y - s * z, t * x * z + s * y],
        [t * x * y + s * z, t * y * y + c, t * y * z - s * x],
        [t * x * z - s * y, t * y * z + s * x, t * z * z + c],
    ]
    return matrix


def parse_urdf(path: Path, mesh_root: Path) -> HandModel:
    """只解析显示需要的 link/visual/joint；package:// 路径改指向随包资源目录。"""
    robot = ET.parse(path).getroot()
    visuals = []
    for link in robot.findall("link"):
        for visual in link.findall("visual"):
            mesh = visual.find("geometry/mesh")
            if mesh is None:
                continue
            # 作用：URDF 写的是 ROS 包路径，桌面程序没有 ROS，只取文件名在本地目录查找。
            filename = mesh.get("filename", "").split("/")[-1]
            visuals.append(Visual(link.get("name"), mesh_root / filename, origin_matrix(visual.find("origin"))))
    joints = []
    for element in robot.findall("joint"):
        limit = element.find("limit")
        mimic = element.find("mimic")
        axis = np.array([float(v) for v in (element.find("axis").get("xyz") if element.find("axis") is not None
                                            else "0 0 1").split()])
        norm = np.linalg.norm(axis)
        joints.append(Joint(
            name=element.get("name"), kind=element.get("type"),
            parent=element.find("parent").get("link"), child=element.find("child").get("link"),
            origin=origin_matrix(element.find("origin")), axis=axis / norm if norm else axis,
            lower=float(limit.get("lower", 0)) if limit is not None else 0.0,
            upper=float(limit.get("upper", 0)) if limit is not None else 0.0,
            mimic=mimic.get("joint") if mimic is not None else None,
            multiplier=float(mimic.get("multiplier", 1)) if mimic is not None else 1.0,
            offset=float(mimic.get("offset", 0)) if mimic is not None else 0.0,
        ))
    children = {joint.child for joint in joints}
    roots = [link.get("name") for link in robot.findall("link") if link.get("name") not in children]
    if len(roots) != 1:
        raise ValueError("URDF 必须只有一个根连杆")
    side = "left" if roots[0].startswith("left") else "right"
    return HandModel(side, roots[0], joints, visuals)


@lru_cache(maxsize=2)
def load_hand(side: str) -> HandModel:
    """按 L/R 读取对应 URDF；结果缓存，切换左右手不重复解析。"""
    name = {"L": "left", "R": "right"}[side]
    return parse_urdf(ASSET_ROOT / "urdf" / f"revo2_{name}_hand.urdf", ASSET_ROOT / "meshes" / f"revo2_{name}_hand")


@lru_cache(maxsize=64)
def load_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """读取二进制 STL，返回 (三角形顶点 N×3×3 米, 面法向 N×3)。

    厂家文件均为二进制格式；遇到 ASCII STL 或长度不符时明确报错，避免静默画出残缺模型。
    """
    data = path.read_bytes()
    if len(data) < 84:
        raise ValueError(f"STL 文件过短：{path.name}")
    count = int.from_bytes(data[80:84], "little")
    if len(data) != 84 + count * 50:
        raise ValueError(f"不是有效的二进制 STL：{path.name}")
    record = np.dtype([("normal", "<f4", 3), ("vertices", "<f4", (3, 3)), ("attr", "<u2")])
    table = np.frombuffer(data, dtype=record, count=count, offset=84)
    return table["vertices"].astype(np.float32), table["normal"].astype(np.float32)


def finger_loads(values: tuple[int, ...] | None) -> dict[str, float]:
    """每根手指取 4 个点中的最大正值；用最大值而非求和，避免把面积大误读成力大。"""
    if values is None or len(values) != CHANNEL_COUNT:
        return {finger: 0.0 for finger in FINGERS}
    return {finger: float(max(0, *(values[index + row * 5] for row in range(4))))
            for index, finger in enumerate(FINGERS)}


def curl_fraction(load_g: float) -> float:
    """受力→弯曲比例 0..1；平滑曲线让轻按也有可见反馈，重按逐渐饱和。"""
    if load_g <= CURL_DEADBAND_G:
        return 0.0
    x = min(1.0, (load_g - CURL_DEADBAND_G) / (FULL_CURL_G - CURL_DEADBAND_G))
    return x * x * (3 - 2 * x)


def pose_from_values(model: HandModel, values: tuple[int, ...] | None) -> dict[str, float]:
    """把 25 点受力换算为 6 个主动关节角（示意，不是实测关节角）。"""
    loads = finger_loads(values)
    angles = {}
    for finger in FINGERS[1:]:
        joint = model.joint(f"{finger}_proximal_joint")
        angles[joint.name] = curl_fraction(loads[finger]) * joint.upper
    thumb = curl_fraction(loads["thumb"])
    flex = model.joint("thumb_proximal_joint")
    angles[flex.name] = thumb * flex.upper
    abduct = model.joint("thumb_metacarpal_joint")
    # 作用：拇指只要用力就先转到对掌位，再随受力弯曲；更接近真实抓握时的拇指动作。
    angles[abduct.name] = min(1.0, thumb * 3) * THUMB_OPPOSE_RAD
    return angles


def sensor_anchors(model: HandModel) -> list[tuple[str, np.ndarray]]:
    """返回 25 个感应点在 3D 模型上的位置（所属连杆, 连杆坐标系下的点）。

    Revo2 每指只有近节、远节两段，G0505M 每指有 4 个点：指尖、上段画在远节，
    下段、根部画在近节；掌部 5 点沿掌面分布。位置是示意性的对应，不是传感器的物理坐标。
    """
    side = model.side
    anchors: list[tuple[str, np.ndarray]] = [("", np.zeros(3))] * CHANNEL_COUNT
    # 作用：四指连杆沿 z 延伸、绕 y 弯曲，指腹在 +x 侧（由 touch_link 网格位置核对）。
    # 近节长度取远节关节的 z 偏移，下段/根部按比例放，适配各指长度差异。
    thumb_sign = 1.0 if side == "right" else -1.0  # 左手拇指连杆沿 -y 延伸，是右手的镜像。
    for column, finger in enumerate(FINGERS):
        if finger == "thumb":
            # 作用：拇指连杆沿 ±y 延伸、绕 x 弯曲，指腹在 +z 侧。
            points = {0: ("distal", (0.0, 0.020, 0.0095)), 1: ("distal", (0.0, 0.007, 0.0085)),
                      2: ("proximal", (0.0, 0.040, 0.0160)), 3: ("proximal", (0.0, 0.018, 0.0160))}
            points = {row: (seg, (x, y * thumb_sign, z)) for row, (seg, (x, y, z)) in points.items()}
        else:
            length = float(model.joint(f"{finger}_distal_joint").origin[2, 3])
            points = {0: ("distal", (0.0170, 0.0, 0.028)), 1: ("distal", (0.0100, 0.0, 0.009)),
                      2: ("proximal", (0.0075, 0.0, 0.70 * length)), 3: ("proximal", (0.0075, 0.0, 0.22 * length))}
        for row in range(4):
            segment, offset = points[row]
            anchors[row * 5 + column] = (f"{side}_{finger}_{segment}_link", np.array(offset))
    # 作用：掌部 21–25 贴在掌面（底座 +x 面）上，从拇指侧排到小指侧，左手镜像。
    for index, y in enumerate((0.026, 0.013, 0.0, -0.013, -0.026)):
        anchors[20 + index] = (model.root, np.array((0.0215, thumb_sign * y, 0.058 if index % 2 else 0.068)))
    return anchors
