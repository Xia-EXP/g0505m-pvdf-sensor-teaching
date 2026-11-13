"""三维手：可在“拟人手”和强脑 Revo2 之间切换；25 点受力发光 + 受力驱动的示意弯曲，可拖动旋转。

只做屏幕显示，不连接真实机械手。动作是由受力推算的示意，界面上始终注明，
避免学生把它误解为传感器测到的手指关节角度。
"""

from __future__ import annotations

import time
from functools import lru_cache
from pathlib import Path

import numpy as np
from PySide6.QtCore import QSettings, Qt, QTimer
from PySide6.QtGui import QMatrix4x4, QSurfaceFormat
from PySide6.QtWidgets import QButtonGroup, QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

from . import human_hand_model as human
from .hand_view import fraction_color
from .models import CHANNEL_COUNT
from .revo2_model import FINGERS, load_hand, load_stl, pose_from_values, sensor_anchors

try:  # 作用：PyOpenGL 缺失或显卡驱动不支持时，调用方改用 2D 示意手，程序照常运行。
    import pyqtgraph.opengl as gl
    from pyqtgraph.opengl import shaders
    GL_AVAILABLE = True
except Exception:  # noqa: BLE001 - 任何导入失败都意味着不能用 3D
    gl = None
    GL_AVAILABLE = False

_VERTEX = """
    uniform mat4 u_mvp;
    uniform mat3 u_normal;
    attribute vec4 a_position;
    attribute vec3 a_normal;
    attribute vec4 a_color;
    varying vec4 v_color;
    varying vec3 v_normal;
    void main() {
        v_normal = normalize(u_normal * a_normal);
        v_color = a_color;
        gl_Position = u_mvp * a_position;
    }
"""


def _studio_fragment(rim_rgb: str, rim_strength: float, spec_strength: float, emissive: float) -> str:
    """影棚三点光：主光在左上前方、补光在右侧、轮廓光沿视线边缘；光随相机，任何角度都看得清。

    emissive>0 的材质（感应点）自身发光，暗处也保持受力颜色，不会被光照压暗。
    """
    return f"""
        #ifdef GL_ES
        precision mediump float;
        #endif
        varying vec4 v_color;
        varying vec3 v_normal;
        void main() {{
            vec3 n = normalize(v_normal);
            if (n.z < 0.0) n = -n;
            vec3 key = normalize(vec3(-0.45, 0.55, 0.70));
            vec3 fill = normalize(vec3(0.70, -0.25, 0.55));
            float wrap = max((dot(n, key) + 0.35) / 1.35, 0.0);
            float side = max(dot(n, fill), 0.0) * 0.30;
            float spec = pow(max(dot(n, normalize(key + vec3(0.0, 0.0, 1.0))), 0.0), 42.0) * {spec_strength:.3f};
            float fresnel = pow(1.0 - clamp(n.z, 0.0, 1.0), 2.6);
            vec3 base = v_color.rgb;
            vec3 lit = base * (0.26 + 0.70 * wrap + side);
            vec3 rgb = mix(lit, base * 1.15, {emissive:.3f}) + vec3(spec) + vec3({rim_rgb}) * fresnel * {rim_strength:.3f};
            gl_FragColor = vec4(min(rgb, vec3(1.0)), v_color.a);
        }}
    """


if GL_AVAILABLE:
    # 作用：pyqtgraph 自带着色器光从模型背后打来，掌面发黑；这里注册三种材质。
    for _name, _fragment in (
        ("hand_skin", _studio_fragment("0.24, 0.84, 0.82", 0.55, 0.18, 0.0)),   # 拟人手：瓷白 + 青色轮廓光
        ("hand_shell", _studio_fragment("0.75, 0.85, 1.0", 0.25, 0.45, 0.0)),   # Revo2：亮面外壳
        ("hand_glow", _studio_fragment("1.0, 1.0, 1.0", 0.35, 0.30, 0.55)),     # 感应点：自发光
    ):
        shaders.ShaderProgram(_name, [shaders.VertexShader(_VERTEX), shaders.FragmentShader(_fragment)])

SKIN_RGB = np.array((0.86, 0.89, 0.93))  # 拟人手表面：冷调瓷白，深色背景下轮廓干净。
PEARL_RGB = np.array((0.90, 0.91, 0.93))  # Revo2 白色外壳。
GRAPHITE_RGB = np.array((0.22, 0.25, 0.29))  # Revo2 机身与关节。
RUBBER_RGB = np.array((0.16, 0.18, 0.21))  # Revo2 指腹橡胶垫。
PAD_IDLE_RGB = np.array((0.36, 0.47, 0.56))  # 未受力感应点：淡淡可见，提示 25 点位置。
SMOOTHING = 0.25  # 每帧向目标姿态靠近的比例；让动作平滑但不迟钝（约 0.2 s 跟上）。
HALO_PX = (12.0, 34.0)  # 受力光晕的最小/最大像素直径；过大会糊成一片。
PAD_RADIUS = 0.0036  # 感应点小凸起半径（米）。
TINT_MAX = {"skin": 0.10, "shell": 0.22, "pad": 1.0}  # 受力零件混入热度色的上限；皮肤几乎保持本色，靠感应点发光表达受力。
HARD_EDGE_COS = np.cos(np.radians(42))  # Revo2 平滑法线：夹角超过 42° 的棱保持锐利。
# 作用：每种模型的取景 (中心 x, 拇指侧偏移, 中心 z, 需容纳的宽, 高) 与地面高度。
FRAMING = {"human": (0.010, 0.018, 0.068, 0.18, 0.30, -0.072), "revo2": (0.0, 0.034, 0.080, 0.19, 0.18, -0.004)}


def _qmatrix(matrix: np.ndarray) -> QMatrix4x4:
    """numpy 行主序 4×4 → Qt 矩阵；QMatrix4x4 构造参数同样按行给出。"""
    return QMatrix4x4(*[float(value) for value in matrix.flatten()])


def link_channels(side: str) -> dict[str, tuple[int, ...]]:
    """Revo2 每个连杆对应的感应点（0 起）：指尖/上段→远节，下段/根部→近节，掌部→底座。"""
    mapping: dict[str, tuple[int, ...]] = {f"{side}_base_link": tuple(range(20, 25))}
    for column, finger in enumerate(FINGERS):
        mapping[f"{side}_{finger}_distal_link"] = (column, column + 5)
        mapping[f"{side}_{finger}_touch_link"] = (column,)
        mapping[f"{side}_{finger}_proximal_link"] = (column + 10, column + 15)
    return mapping


def human_segment_channels() -> dict[str, tuple[int, ...]]:
    """拟人手每段对应的感应点（0 起），与 sensor_anchors 的摆放一致。"""
    mapping: dict[str, tuple[int, ...]] = {"palm": tuple(range(20, 25))}
    for column, finger in enumerate(FINGERS):
        segments = ("distal", "proximal", "metacarpal") if finger == "thumb" else ("distal", "middle", "proximal")
        mapping[f"{finger}_{segments[0]}"] = (column,)
        mapping[f"{finger}_{segments[1]}"] = (column + 5,)
        mapping[f"{finger}_{segments[2]}"] = (column + 10, column + 15)
    return mapping


@lru_cache(maxsize=64)
def smooth_stl(path: Path) -> tuple[np.ndarray, np.ndarray]:
    """读取 STL 并计算“带硬边”的平滑法线：曲面圆润，机械棱角仍然清楚。

    做法：同一位置的顶点合并后平均面法线；某个角与平均值夹角过大（是棱）时退回该面自身法线。
    """
    triangles, _ = load_stl(path)
    edges1, edges2 = triangles[:, 1] - triangles[:, 0], triangles[:, 2] - triangles[:, 0]
    face = np.cross(edges1, edges2)  # 长度∝面积，作为平均权重，小碎面不会把法线带偏。
    unit = face / np.maximum(np.linalg.norm(face, axis=1, keepdims=True), 1e-20)
    corners = triangles.reshape(-1, 3)
    _, ids = np.unique(np.round(corners / 1e-6).astype(np.int64), axis=0, return_inverse=True)
    ids = ids.reshape(-1)
    summed = np.zeros((ids.max() + 1, 3), dtype=np.float64)
    np.add.at(summed, ids, np.repeat(face, 3, axis=0))
    summed /= np.maximum(np.linalg.norm(summed, axis=1, keepdims=True), 1e-20)
    smooth = summed[ids].reshape(-1, 3, 3)
    hard = np.sum(smooth * unit[:, None, :], axis=2) < HARD_EDGE_COS
    normals = np.where(hard[..., None], unit[:, None, :], smooth)
    return triangles, normals.astype(np.float32)


def mesh_item(triangles: np.ndarray, normals: np.ndarray, shader: str, rgb: np.ndarray):
    """用预先算好的逐角法线建网格；pyqtgraph 自带的逐顶点法线是 Python 循环，大网格会卡住界面。"""
    count = len(triangles)
    data = gl.MeshData(vertexes=triangles.reshape(-1, 3), faces=np.arange(count * 3).reshape(-1, 3))
    data._vertexNormals = normals.reshape(-1, 3)  # noqa: SLF001 - pyqtgraph 未提供设置法线的公开接口
    return gl.GLMeshItem(meshdata=data, smooth=True, shader=shader, color=(*rgb, 1.0), glOptions="opaque")


@lru_cache(maxsize=1)
def pad_mesh() -> tuple[np.ndarray, np.ndarray]:
    """感应点小凸起：略压扁的球，贴在表面像手套上的感应片。"""
    vertices, normals = human.rounded_block((PAD_RADIUS * 0.55, PAD_RADIUS, PAD_RADIUS), (0.0, 0.0, 0.0),
                                            power=1.0, around=20, rings=12)
    return vertices, normals


def shadow_image() -> np.ndarray:
    """地面软阴影 + 一圈淡青色底座光环，让手“站”在台面上而不是悬空。"""
    size = 160
    axis = np.linspace(-1, 1, size)
    xx, yy = np.meshgrid(axis, axis)
    radius = np.sqrt(xx ** 2 + yy ** 2)
    image = np.zeros((size, size, 4), dtype=np.ubyte)
    shadow = np.clip(1 - radius / 0.75, 0, 1) ** 1.6
    ring = np.exp(-((radius - 0.80) / 0.03) ** 2) * 0.55
    image[..., 0] = np.clip(8 + ring * 60, 0, 255)
    image[..., 1] = np.clip(14 + ring * 214, 0, 255)
    image[..., 2] = np.clip(20 + ring * 208, 0, 255)
    image[..., 3] = np.clip(shadow * 170 + ring * 120, 0, 255)
    return image


class Hand3DView(QWidget):
    """3D 手卡片：上方模型，下方状态标签、模型切换和视角按钮。"""

    def __init__(self, parent=None) -> None:
        """只建框架；模型在第一次 set_data 时按左右手加载，避免启动变慢。"""
        super().__init__(parent)
        self.setObjectName("hand3dCard")
        self.setStyleSheet("QWidget#hand3dCard{background:#142130;border:1px solid #22344a;border-radius:12px;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self.settings = QSettings()
        self.kind = self.settings.value("hand3d/model", "human")
        self.kind = self.kind if self.kind in FRAMING else "human"
        self.side = ""
        self.built = ("", "")
        self.values: tuple[int, ...] | None = None
        self.maximum = 1000
        self.channel = 1
        self.items: list[tuple[object, str, np.ndarray, tuple[int, ...], str]] = []
        self.pads: list[tuple[object, str, np.ndarray]] = []
        self.target: dict[str, float] = {}
        self.current: dict[str, float] = {}
        self.view = gl.GLViewWidget()
        # 作用：4 倍多重采样抗锯齿，模型边缘不再有台阶；必须在控件首次显示前设置。
        surface = QSurfaceFormat(self.view.format())
        surface.setSamples(4)
        self.view.setFormat(surface)
        self.view.setBackgroundColor("#101a25")
        # 作用：操作方法和“示意”的含义放进悬停提示，画面上只留模型本身。
        self.view.setToolTip("拖动旋转，滚轮缩放。\n手指弯曲按各指受力推算，只作示意，不是测得的关节角度。")
        self.view.setMinimumSize(220, 220)
        self.azimuth, self.elevation = 18.0, 18.0
        layout.addWidget(self.view, 1)
        self.floor = gl.GLImageItem(shadow_image(), glOptions="translucent")
        self.halos = gl.GLScatterPlotItem(pxMode=True, glOptions="additive")
        self.selection = gl.GLScatterPlotItem(pxMode=True, glOptions="translucent")
        # 作用：状态只用一个短标签（“弯曲为示意”或数据中断原因），和按钮同排，不占整行说明。
        self.note = QLabel("弯曲为示意")
        self.note.setStyleSheet("color:#8aa0b2;background:#1b2b3c;border-radius:4px;padding:2px 8px;font-size:12px;")
        self.note.setToolTip("手指弯曲按各指受力推算，只作示意，不是测得的关节角度")
        self.note.setMinimumWidth(self.note.sizeHint().width())
        row = QHBoxLayout()
        row.setSpacing(6)
        row.addWidget(self.note)
        row.addStretch()
        # 作用：模型切换浮在三维画面右上角，不挤占底部按钮行，窄屏（1024 宽）也放得下。
        self.kind_bar = QWidget(self.view)
        bar = QHBoxLayout(self.kind_bar)
        bar.setContentsMargins(0, 0, 0, 0)
        bar.setSpacing(0)
        self.kind_group = QButtonGroup(self)
        for index, (key, text, tip) in enumerate((("human", "人手", "拟人手：感应点贴在指腹和掌面"),
                                                  ("revo2", "Revo2", "强脑 Revo2 灵巧手模型（Apache-2.0）"))):
            button = QPushButton(text)
            button.setCheckable(True)
            button.setChecked(key == self.kind)
            button.setToolTip(tip)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setFixedHeight(24)
            corner = "border-top-left-radius:6px;border-bottom-left-radius:6px;" if index == 0 else                 "border-top-right-radius:6px;border-bottom-right-radius:6px;"
            button.setStyleSheet("QPushButton{padding:2px 10px;font-size:12px;border-radius:0;" + corner +
                                 "background:rgba(27,43,60,200);border:1px solid #2c4258;color:#b8c8d6;}"
                                 "QPushButton:checked{background:#3dd6d0;color:#06232a;border-color:#3dd6d0;}")
            button.clicked.connect(lambda _=False, value=key: self.set_kind(value))
            self.kind_group.addButton(button, index)
            bar.addWidget(button)
        self.kind_bar.adjustSize()
        self.view.installEventFilter(self)
        for text, callback in (("掌面", self.reset_camera), ("侧面", lambda: self.camera(azimuth=90)),
                               ("手背", lambda: self.camera(azimuth=180))):
            button = QPushButton(text)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setFixedHeight(26)
            button.setMinimumWidth(40)
            button.setStyleSheet("padding:2px 6px;font-size:12px;")
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.reset_camera()
        self.timer = QTimer(self)
        self.timer.setInterval(33)  # 约 30 Hz 动画，远高于 20 帧/秒的上报率即可平滑。
        self.timer.timeout.connect(self._animate)
        self.timer.start()

    def set_kind(self, kind: str) -> None:
        """切换拟人手 / Revo2，并记住选择，下次打开沿用。"""
        if kind == self.kind:
            return
        self.kind = kind
        self.settings.setValue("hand3d/model", kind)
        # 作用：程序内切换（不是点按钮）时也同步按钮高亮，避免显示与选中不一致。
        self.kind_group.button(list(FRAMING).index(kind)).setChecked(True)
        if self.side:
            self._build(self.side)

    def camera(self, azimuth: float, elevation: float = 18) -> None:
        """设置观察方向；手指沿 +z 向上，azimuth=0 从 +x（掌面）方向看。仰角略抬，能看到地面阴影。"""
        self.azimuth, self.elevation = azimuth, elevation
        self.fit_camera()

    def fit_camera(self) -> None:
        """按画框宽高比算相机距离，让整只手填满画框。

        pyqtgraph 的 60° 视场角作用在水平方向：中心处可见宽度 ≈ 1.155×距离，可见高度再乘高/宽。
        """
        width, height = max(1, self.view.width()), max(1, self.view.height())
        centre_x, shift, centre_z, need_w, need_h, _ = FRAMING[self.kind]
        distance = max(need_w / 1.155, need_h / 1.155 * width / height)
        # 作用：拇指单侧伸出，几何中心偏向拇指一侧；右手在 +y，左手镜像到 -y。
        side_shift = -shift if self.side == "L" else shift
        self.view.opts["center"] = self.view.opts["center"].__class__(centre_x, side_shift, centre_z)
        self.view.setCameraPosition(distance=distance, elevation=self.elevation, azimuth=self.azimuth)

    def eventFilter(self, watched, event) -> bool:
        """三维画面尺寸变化时把模型切换按钮固定在右上角。"""
        if watched is self.view and event.type() == event.Type.Resize:
            self.kind_bar.move(self.view.width() - self.kind_bar.width() - 8, 8)
        return super().eventFilter(watched, event)

    def resizeEvent(self, event) -> None:
        """画框尺寸变化（窗口缩放、全屏）后重新取景，模型始终填满画框。"""
        super().resizeEvent(event)
        self.fit_camera()

    def reset_camera(self) -> None:
        """默认看掌面并略微偏侧，学生能同时看到感应点和手指弯曲。"""
        self.camera(azimuth=18)

    def _clear(self) -> None:
        """移除当前模型的全部图元，切换左右手或模型时重建。"""
        for item in [entry[0] for entry in self.items] + [entry[0] for entry in self.pads]:
            self.view.removeItem(item)
        for item in (self.floor, self.halos, self.selection):
            if item in self.view.items:
                self.view.removeItem(item)
        self.items, self.pads = [], []

    def _build(self, side: str) -> None:
        """按当前模型类型和左右手生成网格；网格计算有缓存，再次切换很快。"""
        self._clear()
        self.side = side
        # 作用：阴影图 160 像素对应约 0.17 m 宽的台面，放在手腕下方的地面上。
        _, shift, _, _, _, floor_z = FRAMING[self.kind]
        self.floor.resetTransform()
        self.floor.scale(0.17 / 160, 0.17 / 160, 1)
        self.floor.translate(-0.085 + 0.004, -0.085 + (-shift if side == "L" else shift) * 0.5, floor_z)
        self.view.addItem(self.floor)
        if self.kind == "human":
            self._build_human(side)
        else:
            self._build_revo2(side)
        pad_vertices, pad_normals = pad_mesh()
        for link, offset in self.anchors:
            item = mesh_item(pad_vertices, pad_normals, "hand_glow", PAD_IDLE_RGB)
            self.view.addItem(item)
            self.pads.append((item, link, offset))
        # 作用：光晕和选择环最后加入，叠在模型上方，不被手指遮住颜色。
        self.view.addItem(self.halos)
        self.view.addItem(self.selection)
        self.built = (self.kind, side)
        self.fit_camera()

    def _build_human(self, side: str) -> None:
        """拟人手：每段一个网格，统一瓷白材质。"""
        model = human.load_human_hand(side)
        self.model = model
        channels = human_segment_channels()
        for part in model.parts:
            item = mesh_item(part.vertices, part.normals, "hand_skin", SKIN_RGB)
            self.view.addItem(item)
            self.items.append((item, part.segment, SKIN_RGB, channels.get(part.segment, ()), "skin"))
        self.anchors = human.sensor_anchors(side)
        self.current = {key: 0.0 for key in (*FINGERS, "thumb_oppose")}

    def _build_revo2(self, side: str) -> None:
        """Revo2：白壳 + 石墨机身 + 深色指腹橡胶，平滑法线保留机械棱角。"""
        model = load_hand(side)
        self.model = model
        channels = link_channels(model.side)
        for visual in model.visuals:
            triangles, normals = smooth_stl(visual.mesh)
            name = visual.mesh.stem
            if "touch" in name:
                rgb, kind, shader = RUBBER_RGB, "pad", "hand_glow"
            elif name.endswith("base_link") or "metacarpal" in name:
                rgb, kind, shader = GRAPHITE_RGB, "shell", "hand_shell"
            else:
                rgb, kind, shader = PEARL_RGB, "shell", "hand_shell"
            item = mesh_item(triangles, normals, shader, rgb)
            item.visual_origin = visual.origin  # 零件相对连杆的固定偏移，每帧与连杆变换相乘。
            self.view.addItem(item)
            self.items.append((item, visual.link, rgb, channels.get(visual.link, ()), kind))
        self.anchors = sensor_anchors(model)
        self.current = {joint.name: 0.0 for joint in model.active_joints}

    def set_data(self, hand: str, values: tuple[int, ...] | None, channel: int, maximum: int) -> None:
        """接收当前手的 25 点数值；实际绘制在动画定时器里进行，避免与上报率耦合。

        values=None（断连/过期）时清零目标姿态，不刷「受力中」类活体话术。
        """
        if (self.kind, hand) != self.built:
            self._build(hand)
        live_values = values if values and len(values) == CHANNEL_COUNT else None
        self.values = live_values
        self.maximum = max(1, maximum)
        self.channel = channel
        self.target = (human.pose_from_values(live_values) if self.kind == "human"
                       else pose_from_values(self.model, live_values))
        self.note.setText("弯曲为示意" if live_values is not None else "无实时数据")

    def set_values(self, values) -> None:
        """ui.py 兼容桥：把 25 点喂给 set_data（沿用当前手/通道/量程）。"""
        hand = self.side or "R"
        self.set_data(hand, tuple(values) if values is not None else None, self.channel, self.maximum)

    def set_idle(self, reason: str = "无实时数据") -> None:
        """ui.py 兼容桥：断连/过期时清零姿态，不刷假受力话术。"""
        hand = self.side or "R"
        self.set_data(hand, None, self.channel, self.maximum)
        self.note.setText(reason)

    def set_channel_map(self, finger_map=None, palm_channels=None) -> None:
        """ui.py 兼容桥：3D 手用固定映射，忽略外部 channel_map，不改姿态公式。"""
        return

    def _transforms(self) -> dict[str, np.ndarray]:
        """当前（已平滑）姿态下每段/连杆的变换。"""
        if self.kind == "human":
            return self.model.segment_transforms(self.current)
        return self.model.link_transforms(self.current)

    def _animate(self) -> None:
        """平滑逼近目标姿态，更新零件位姿、受力着色、感应点发光和光晕。"""
        if not self.side or not self.isVisible():
            return
        for name, value in self.target.items():
            self.current[name] = self.current.get(name, 0.0) + (value - self.current.get(name, 0.0)) * SMOOTHING
        transforms = self._transforms()
        values = self.values or (0,) * CHANNEL_COUNT
        fractions = [max(0.0, min(1.0, value / self.maximum)) for value in values]
        for item, link, rgb, channels, kind in self.items:
            matrix = transforms[link]
            if hasattr(item, "visual_origin"):
                matrix = matrix @ item.visual_origin
            item.setTransform(_qmatrix(matrix))
            fraction = max((fractions[index] for index in channels), default=0.0)
            heat = np.array(fraction_color(fraction).getRgbF()[:3])
            # 作用：指腹橡胶垫直接显示热度色；外壳/皮肤只轻微染色，保持材质本色。
            mix = fraction * TINT_MAX[kind]
            color = rgb * (1 - mix) + heat * mix
            item.setColor((*color, 1.0))
        positions, halo_colors, halo_sizes = [], [], []
        for index, (item, link, offset) in enumerate(self.pads):
            matrix = transforms[link].copy()
            matrix[:3, 3] = (transforms[link] @ np.append(offset, 1.0))[:3]
            item.setTransform(_qmatrix(matrix))
            fraction = fractions[index]
            heat = np.array(fraction_color(fraction).getRgbF()[:3])
            glow = min(1.0, fraction * 4)  # 轻微受力即可从暗点亮起，避免低值时“看不见”。
            item.setColor((*(PAD_IDLE_RGB * (1 - glow) + heat * glow), 1.0))
            positions.append(matrix[:3, 3])
            halo_colors.append((*heat, 0.40 * glow))
            halo_sizes.append(HALO_PX[0] + (HALO_PX[1] - HALO_PX[0]) * fraction if fraction > 0.01 else 0.0)
        self.halos.setData(pos=np.array(positions), color=np.array(halo_colors), size=np.array(halo_sizes))
        pulse = .5 + .5 * np.sin(time.monotonic() * 6)
        # 作用：当前选中的测点外加一圈闪烁的白环，与 2D 手掌图的选择同步。
        self.selection.setData(pos=np.array([positions[self.channel - 1]]), color=(1, 1, 1, .30 + .35 * pulse),
                               size=24 + 6 * pulse)
