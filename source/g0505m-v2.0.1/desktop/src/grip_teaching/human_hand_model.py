"""程序生成的拟人手：圆润手掌 + 三节手指，受力驱动示意弯曲，25 个感应点贴在对应指腹和掌面。

本模块不依赖 Qt/OpenGL，便于单元测试。坐标系与 Revo2 模型一致，两种模型可共用取景：
单位米，手指沿 +z，掌心朝 +x，右手拇指在 +y；左手是右手关于 y 的镜像。

物理边界：G0505M 测不到关节角度，弯曲由各指受力推算，只作示意；感应点位置是示意性对应，
不是传感器的物理坐标。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from functools import lru_cache

import numpy as np

from .models import CHANNEL_COUNT
from .revo2_model import FINGERS, curl_fraction, finger_loads

MIRROR = np.diag([1.0, -1.0, 1.0, 1.0])  # 右手 → 左手的镜像变换（y 取反）。

# 作用：四指掌指关节位置 (y, z)、外展角（弧度，正值偏向拇指侧）、三节长度 (近/中/远) 与指根半径。
# 尺寸按成人手约 0.9 倍取，整只手与 Revo2 大小相近，学生一眼能认出是“手”。
FINGER_SPECS = {
    "index": ((0.0262, 0.083), 0.06, (0.042, 0.025, 0.020), 0.0098),
    "middle": ((0.0080, 0.086), 0.0, (0.046, 0.028, 0.021), 0.0101),
    "ring": ((-0.0100, 0.084), -0.06, (0.043, 0.026, 0.020), 0.0095),
    "pinky": ((-0.0270, 0.078), -0.13, (0.034, 0.020, 0.018), 0.0083),
}
THUMB_BASE = (0.006, 0.027, 0.028)  # 拇指腕掌关节位置（右手）。
THUMB_LENGTHS = (0.036, 0.030, 0.025)  # 掌骨、近节、远节。
THUMB_RADIUS = 0.0118
# 作用：关节在放松状态下也略弯，手看起来自然，不像僵直的手套模型。
REST = {"mcp": 0.14, "pip": 0.20, "dip": 0.12}
CURL = {"mcp": 1.05, "pip": 1.30, "dip": 0.80}  # 握紧时在放松角基础上再增加的角度。
THUMB_OPPOSE_START = 3.0  # 拇指受力达到弯曲比例的 1/3 即转到对掌位，与 Revo2 映射一致。


@dataclass(frozen=True)
class Part:
    """一个网格零件：所属段名、局部坐标系下的三角形顶点 (N,3,3) 与逐角法线 (N,3,3)。"""

    segment: str
    vertices: np.ndarray
    normals: np.ndarray


def axis_rotation(axis: str, angle: float) -> np.ndarray:
    """绕 x/y/z 轴的齐次旋转矩阵。"""
    c, s = math.cos(angle), math.sin(angle)
    matrix = np.eye(4)
    i, j = {"x": (1, 2), "y": (2, 0), "z": (0, 1)}[axis]
    matrix[i, i], matrix[i, j], matrix[j, i], matrix[j, j] = c, -s, s, c
    return matrix


def translation(x: float, y: float, z: float) -> np.ndarray:
    """平移矩阵。"""
    matrix = np.eye(4)
    matrix[:3, 3] = (x, y, z)
    return matrix


def frame_from(direction: np.ndarray, pad_hint: np.ndarray) -> np.ndarray:
    """由“段的延伸方向”和“指腹朝向”构造旋转：局部 z=延伸方向，局部 x=指腹方向。"""
    z = direction / np.linalg.norm(direction)
    x = pad_hint - np.dot(pad_hint, z) * z
    x /= np.linalg.norm(x)
    y = np.cross(z, x)
    matrix = np.eye(4)
    matrix[:3, 0], matrix[:3, 1], matrix[:3, 2] = x, y, z
    return matrix


def grid_mesh(points: np.ndarray, centre: np.ndarray, closed_u: bool = True) -> tuple[np.ndarray, np.ndarray]:
    """把参数网格 (nv, nu, 3) 三角化，并用网格梯度求顶点法线，朝外方向以 centre 判断。

    法线来自变形后的网格本身，所以手掌收窄、指节压扁之后光照仍然正确。
    """
    if closed_u:
        points = np.concatenate([points, points[:, :1]], axis=1)
    du = np.gradient(points, axis=1)
    dv = np.gradient(points, axis=0)
    normals = np.cross(du, dv)
    length = np.linalg.norm(normals, axis=2, keepdims=True)
    # 作用：极点处梯度退化，用“指向外侧”的方向代替，避免出现黑点。
    outward = points - centre
    fallback = outward / np.maximum(np.linalg.norm(outward, axis=2, keepdims=True), 1e-12)
    normals = np.where(length > 1e-12, normals / np.maximum(length, 1e-12), fallback)
    flip = np.sum(normals * outward, axis=2, keepdims=True) < 0
    normals = np.where(flip, -normals, normals)
    a, b = points[:-1, :-1], points[:-1, 1:]
    c, d = points[1:, :-1], points[1:, 1:]
    na, nb = normals[:-1, :-1], normals[:-1, 1:]
    nc, nd = normals[1:, :-1], normals[1:, 1:]
    triangles = np.concatenate([np.stack([a, c, b], 2).reshape(-1, 3, 3), np.stack([b, c, d], 2).reshape(-1, 3, 3)])
    corner_normals = np.concatenate([np.stack([na, nc, nb], 2).reshape(-1, 3, 3),
                                     np.stack([nb, nc, nd], 2).reshape(-1, 3, 3)])
    return triangles.astype(np.float32), corner_normals.astype(np.float32)


def capsule(length: float, r0: float, r1: float, flat: float = 0.86, around: int = 32,
            cap_rings: int = 9, body_rings: int = 8) -> tuple[np.ndarray, np.ndarray]:
    """沿 +z 的锥形胶囊（两端半球），x 方向按 flat 压扁，模拟指节“宽于厚”的截面。"""
    profile = []
    for i in range(cap_rings + 1):  # 底部半球：从 -90° 到 0°。
        angle = -math.pi / 2 + math.pi / 2 * i / cap_rings
        profile.append((r0 * math.cos(angle), r0 * math.sin(angle)))
    for i in range(1, body_rings):
        t = i / body_rings
        profile.append((r0 + (r1 - r0) * t, length * t))
    for i in range(cap_rings + 1):  # 顶部半球：从 0° 到 90°。
        angle = math.pi / 2 * i / cap_rings
        profile.append((r1 * math.cos(angle), length + r1 * math.sin(angle)))
    rho = np.array([p[0] for p in profile])[:, None]
    z = np.array([p[1] for p in profile])[:, None]
    phi = np.linspace(0, 2 * math.pi, around, endpoint=False)[None, :]
    points = np.stack([rho * np.cos(phi) * flat, rho * np.sin(phi), np.broadcast_to(z, (len(profile), around))],
                      axis=2)
    return grid_mesh(points, np.array((0.0, 0.0, length / 2)))


def rounded_block(half: tuple[float, float, float], centre: tuple[float, float, float], power: float = 0.32,
                  taper: float = 0.0, around: int = 48, rings: int = 28) -> tuple[np.ndarray, np.ndarray]:
    """超椭球“圆角块”；power 越小越接近方块。taper>0 时底部收窄，形成手掌下窄上宽的轮廓。"""
    def signed_power(value, exponent):
        return np.sign(value) * np.abs(value) ** exponent

    v = np.linspace(-math.pi / 2, math.pi / 2, rings)[:, None]
    u = np.linspace(0, 2 * math.pi, around, endpoint=False)[None, :]
    hx, hy, hz = half
    x = hx * signed_power(np.cos(v), power) * signed_power(np.cos(u), power)
    y = hy * signed_power(np.cos(v), power) * signed_power(np.sin(u), power)
    z = hz * signed_power(np.sin(v), power) + 0 * u
    y = y * (1 - taper * (1 - (z / hz + 1) / 2))
    points = np.stack([x + centre[0], y + centre[1], z + centre[2]], axis=2)
    return grid_mesh(points, np.array(centre))


def ellipsoid(radii: tuple[float, float, float], centre: tuple[float, float, float]) -> tuple[np.ndarray, np.ndarray]:
    """椭球，用于大鱼际、小鱼际等肌肉隆起。"""
    return rounded_block(radii, centre, power=1.0, around=36, rings=20)


def _wrist() -> tuple[np.ndarray, np.ndarray]:
    """手腕：扁圆胶囊，从掌根向下延伸，宽度略小于手掌，看起来是“手连着前臂”而不是底座。"""
    vertices, normals = capsule(0.040, 0.0225, 0.0240, flat=0.62, around=40)
    return vertices - np.array((0.0, 0.0, 0.048), dtype=np.float32), normals


def thumb_frame(oppose: float) -> np.ndarray:
    """拇指根部朝向：放松时斜向外上方，对掌时转到掌心前方、指腹朝向食指。"""
    rest_dir, rest_pad = np.array((0.42, 0.42, 0.80)), np.array((0.80, -0.58, 0.0))
    opp_dir, opp_pad = np.array((0.80, 0.18, 0.58)), np.array((0.20, -0.95, 0.10))
    direction = rest_dir + (opp_dir - rest_dir) * oppose
    pad = rest_pad + (opp_pad - rest_pad) * oppose
    return translation(*THUMB_BASE) @ frame_from(direction, pad)


@dataclass(frozen=True)
class HumanHand:
    """一只拟人手：零件网格、关节骨架和 25 点锚点；side 为 "L" 或 "R"。"""

    side: str
    parts: tuple[Part, ...]

    @property
    def mirror(self) -> bool:
        """左手由右手镜像得到。"""
        return self.side == "L"

    def segment_transforms(self, pose: dict[str, float]) -> dict[str, np.ndarray]:
        """给定各指弯曲比例（0..1）与拇指对掌比例，返回每段相对手腕的 4×4 变换。"""
        transforms = {"palm": np.eye(4)}
        for finger, ((y, z), splay, lengths, _) in FINGER_SPECS.items():
            curl = pose.get(finger, 0.0)
            # 作用：握拳时手指自然并拢，外展角随弯曲减小。
            base = translation(0.0, y, z) @ axis_rotation("x", -splay * (1 - 0.6 * curl))
            angles = [REST[key] + CURL[key] * curl for key in ("mcp", "pip", "dip")]
            current = base
            for index, segment in enumerate(("proximal", "middle", "distal")):
                if index:
                    current = current @ translation(0.0, 0.0, lengths[index - 1])
                current = current @ axis_rotation("y", angles[index])
                transforms[f"{finger}_{segment}"] = current
        curl = pose.get("thumb", 0.0)
        current = thumb_frame(pose.get("thumb_oppose", 0.0))
        for index, (segment, bend) in enumerate((("metacarpal", 0.10 + 0.30 * curl),
                                                 ("proximal", 0.15 + 0.55 * curl),
                                                 ("distal", 0.10 + 0.80 * curl))):
            if index:
                current = current @ translation(0.0, 0.0, THUMB_LENGTHS[index - 1])
            current = current @ axis_rotation("y", bend)
            transforms[f"thumb_{segment}"] = current
        if self.mirror:
            transforms = {name: MIRROR @ matrix @ MIRROR for name, matrix in transforms.items()}
        return transforms


def segment_geometry() -> list[tuple[str, float, float, float]]:
    """每段的 (段名, 长度, 起点半径, 终点半径)，网格和锚点共用同一份尺寸。"""
    segments = []
    for finger, (_, _, lengths, radius) in FINGER_SPECS.items():
        radii = (radius, radius * 0.93, radius * 0.86, radius * 0.80)
        for index, segment in enumerate(("proximal", "middle", "distal")):
            segments.append((f"{finger}_{segment}", lengths[index], radii[index], radii[index + 1]))
    radii = (THUMB_RADIUS * 1.15, THUMB_RADIUS, THUMB_RADIUS * 0.92, THUMB_RADIUS * 0.84)
    for index, segment in enumerate(("metacarpal", "proximal", "distal")):
        segments.append((f"thumb_{segment}", THUMB_LENGTHS[index], radii[index], radii[index + 1]))
    return segments


def _mirror_mesh(vertices: np.ndarray, normals: np.ndarray) -> tuple[np.ndarray, np.ndarray]:
    """y 取反后三角形绕向会反，交换两个角使正面仍朝外。"""
    vertices = vertices * np.array((1, -1, 1), dtype=np.float32)
    normals = normals * np.array((1, -1, 1), dtype=np.float32)
    return vertices[:, ::-1].copy(), normals[:, ::-1].copy()


@lru_cache(maxsize=2)
def load_human_hand(side: str) -> HumanHand:
    """生成左/右拟人手；结果缓存，切换左右手不重复计算网格。"""
    meshes: list[tuple[str, tuple[np.ndarray, np.ndarray]]] = [
        # 作用：手掌主体略窄于掌指关节连线，底部收窄接手腕；鱼际隆起让掌面有立体感。
        ("palm", rounded_block((0.0145, 0.0385, 0.047), (0.0, 0.0, 0.047), power=0.42, taper=0.16)),
        ("palm", ellipsoid((0.0120, 0.0175, 0.027), (0.0060, 0.0205, 0.030))),
        ("palm", ellipsoid((0.0105, 0.0125, 0.032), (0.0040, -0.0255, 0.036))),
        ("palm", _wrist()),
    ]
    for name, length, r0, r1 in segment_geometry():
        meshes.append((name, capsule(length, r0, r1)))
    parts = []
    for name, (vertices, normals) in meshes:
        if side == "L":
            vertices, normals = _mirror_mesh(vertices, normals)
        parts.append(Part(name, vertices, normals))
    return HumanHand(side, tuple(parts))


def pose_from_values(values: tuple[int, ...] | None) -> dict[str, float]:
    """25 点受力 → 各指弯曲比例和拇指对掌比例（示意，不是实测关节角）。"""
    loads = finger_loads(values)
    pose = {finger: curl_fraction(loads[finger]) for finger in FINGERS}
    pose["thumb_oppose"] = min(1.0, pose["thumb"] * THUMB_OPPOSE_START)
    return pose


def sensor_anchors(side: str) -> list[tuple[str, np.ndarray]]:
    """25 个感应点：(所属段名, 段局部坐标点)。点落在指腹/掌面表面，局部 +x 为掌侧。

    每列顺序与厂家编号一致：指尖=远节、上段=中节、下段=近节中部、根部=近节靠掌处；
    拇指依次为远节、近节、掌骨上部、掌骨下部。掌部 21–25 按 2D 手掌图的相对位置摆放。
    """
    sizes = {name: (length, r0, r1) for name, length, r0, r1 in segment_geometry()}
    sign = -1.0 if side == "L" else 1.0
    anchors: list[tuple[str, np.ndarray]] = [("", np.zeros(3))] * CHANNEL_COUNT
    rows = {
        "thumb": (("distal", 0.55), ("proximal", 0.50), ("metacarpal", 0.78), ("metacarpal", 0.35)),
    }
    for column, finger in enumerate(FINGERS):
        spots = rows.get(finger, (("distal", 0.55), ("middle", 0.50), ("proximal", 0.62), ("proximal", 0.16)))
        for row, (segment, t) in enumerate(spots):
            name = f"{finger}_{segment}"
            length, r0, r1 = sizes[name]
            radius = r0 + (r1 - r0) * t
            anchors[row * 5 + column] = (name, np.array((0.86 * radius * 0.98, 0.0, length * t)))
    palm = ((0.002, 0.040), (0.009, 0.067), (-0.011, 0.021), (-0.022, 0.027), (-0.026, 0.056))
    for index, (y, z) in enumerate(palm):
        anchors[20 + index] = ("palm", np.array((0.0128, sign * y, z)))
    return anchors
