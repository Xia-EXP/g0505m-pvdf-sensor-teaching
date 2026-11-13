"""强脑 Revo2 三维手：25 点受力着色 + 受力驱动的示意弯曲，可拖动旋转观察。

只做屏幕显示，不连接真实机械手。动作是由受力推算的示意，界面上始终注明，
避免学生把它误解为传感器测到的手指关节角度。
"""

from __future__ import annotations

import time

import numpy as np
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QMatrix4x4
from PySide6.QtWidgets import QHBoxLayout, QLabel, QPushButton, QVBoxLayout, QWidget

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

if GL_AVAILABLE:
    # 作用：pyqtgraph 自带 shaded 着色器的光从模型背后打来，掌面几乎全黑；
    # 这里注册“头灯”着色器：光随相机方向，双面受光，加环境光和少量高光，任何角度都看得清。
    shaders.ShaderProgram("revo2_headlight", [
        shaders.VertexShader("""
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
        """),
        shaders.FragmentShader("""
            #ifdef GL_ES
            precision mediump float;
            #endif
            varying vec4 v_color;
            varying vec3 v_normal;
            void main() {
                vec3 n = normalize(v_normal);
                vec3 light = normalize(vec3(0.35, 0.45, 1.0));
                float diffuse = abs(dot(n, light));
                float rim = pow(1.0 - abs(n.z), 2.0) * 0.18;
                float spec = pow(max(dot(n, normalize(light + vec3(0.0, 0.0, 1.0))), 0.0), 24.0) * 0.25;
                vec3 rgb = v_color.rgb * (0.38 + 0.62 * diffuse) + vec3(spec + rim);
                gl_FragColor = vec4(min(rgb, vec3(1.0)), v_color.a);
            }
        """),
    ])

SHELL_RGB = np.array((0.80, 0.84, 0.88))  # 未受力零件的浅灰金属色，暗背景下轮廓清楚。
SMOOTHING = 0.25  # 每帧向目标姿态靠近的比例；让动作平滑但不迟钝（约 0.2 s 跟上）。
MARKER_PX = (9.0, 26.0)  # 受力标记的最小/最大像素直径。


def _qmatrix(matrix: np.ndarray) -> QMatrix4x4:
    """numpy 行主序 4×4 → Qt 矩阵；QMatrix4x4 构造参数同样按行给出。"""
    return QMatrix4x4(*[float(value) for value in matrix.flatten()])


def link_channels(side: str) -> dict[str, tuple[int, ...]]:
    """每个连杆对应的感应点（0 起）：指尖/上段→远节，下段/根部→近节，掌部→底座。"""
    mapping: dict[str, tuple[int, ...]] = {f"{side}_base_link": tuple(range(20, 25))}
    for column, finger in enumerate(FINGERS):
        mapping[f"{side}_{finger}_distal_link"] = (column, column + 5)
        mapping[f"{side}_{finger}_touch_link"] = (column,)
        mapping[f"{side}_{finger}_proximal_link"] = (column + 10, column + 15)
    return mapping


class Hand3DView(QWidget):
    """3D 手卡片：上方模型，下方中文说明和视角按钮。"""

    def __init__(self, parent=None) -> None:
        """只建框架；模型在第一次 set_data 时按左右手加载，避免启动变慢。"""
        super().__init__(parent)
        self.setObjectName("hand3dCard")
        self.setStyleSheet("QWidget#hand3dCard{background:#142130;border:1px solid #22344a;border-radius:12px;}")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(8, 8, 8, 8)
        layout.setSpacing(4)
        self.side = ""
        self.values: tuple[int, ...] | None = None
        self.maximum = 1000
        self.channel = 1
        self.items: list[tuple[object, str]] = []
        self.anchors: list[tuple[str, np.ndarray]] = []
        self.target: dict[str, float] = {}
        self.current: dict[str, float] = {}
        self.view = gl.GLViewWidget()
        self.view.setBackgroundColor("#0e1620")
        self.view.setMinimumSize(220, 220)
        self.reset_camera()
        layout.addWidget(self.view, 1)
        self.markers = gl.GLScatterPlotItem(pxMode=True, glOptions="translucent")
        self.selection = gl.GLScatterPlotItem(pxMode=True, glOptions="translucent")
        self.caption = QLabel("强脑 Revo2 仿真 · 不控真臂")
        self.caption.setStyleSheet("color:#6ae4df;font-weight:bold;background:transparent;")
        self.caption.setAlignment(Qt.AlignmentFlag.AlignCenter)
        layout.addWidget(self.caption)
        row = QHBoxLayout()
        self.note = QLabel("手指弯曲由受力推算，仅示意 · 拖动旋转 · 滚轮缩放")
        self.note.setStyleSheet("color:#7f96a9;background:transparent;")
        self.note.setWordWrap(True)
        row.addWidget(self.note, 1)
        for text, callback in (("掌面", self.reset_camera), ("侧面", lambda: self.camera(azimuth=90)),
                               ("手背", lambda: self.camera(azimuth=180))):
            button = QPushButton(text)
            button.setFocusPolicy(Qt.FocusPolicy.NoFocus)
            button.setFixedHeight(26)
            button.clicked.connect(callback)
            row.addWidget(button)
        layout.addLayout(row)
        self.timer = QTimer(self)
        self.timer.setInterval(33)  # 约 30 Hz 动画，远高于 20 帧/秒的上报率即可平滑。
        self.timer.timeout.connect(self._animate)
        self.timer.start()

    def camera(self, azimuth: float, elevation: float = 12) -> None:
        """设置观察方向；手指沿 +z 向上，azimuth=0 从 +x（掌面）方向看。"""
        self.view.setCameraPosition(distance=0.21, elevation=elevation, azimuth=azimuth)

    def reset_camera(self) -> None:
        """默认看掌面并略微偏侧，学生能同时看到感应点和手指弯曲。"""
        self.view.opts["center"] = self.view.opts["center"].__class__(0.0, 0.0, 0.078)
        self.camera(azimuth=18)

    def _build(self, side: str) -> None:
        """切换左右手时重建网格；STL 解析有缓存，再次切换很快。"""
        for item, _ in self.items:
            self.view.removeItem(item)
        for item in (self.markers, self.selection):
            if item in self.view.items:
                self.view.removeItem(item)
        model = load_hand(side)
        self.model = model
        self.items = []
        for visual in model.visuals:
            triangles, _ = load_stl(visual.mesh)
            mesh = gl.MeshData(vertexes=triangles)
            item = gl.GLMeshItem(meshdata=mesh, smooth=False, shader="revo2_headlight", color=(*SHELL_RGB, 1.0),
                                 glOptions="opaque")
            item.visual_origin = visual.origin  # 零件相对连杆的固定偏移，每帧与连杆变换相乘。
            self.view.addItem(item)
            self.items.append((item, visual.link))
        self.anchors = sensor_anchors(model)
        # 作用：先画选择光环再画标记，使光环衬在标记后面而不是盖住数值颜色。
        self.view.addItem(self.selection)
        self.view.addItem(self.markers)
        self.links = link_channels(model.side)
        self.current = {joint.name: 0.0 for joint in model.active_joints}
        self.side = side
        self.caption.setText("强脑 Revo2 仿真 · 不控真臂")

    def set_data(self, hand: str, values: tuple[int, ...] | None, channel: int, maximum: int) -> None:
        """接收当前手的 25 点数值；实际绘制在动画定时器里进行，避免与上报率耦合。

        values=None（断连/过期）时清零目标姿态，不刷「受力中」类活体话术。
        """
        if hand != self.side:
            self._build(hand)
        live_values = values if values and len(values) == CHANNEL_COUNT else None
        self.values = live_values
        self.maximum = max(1, maximum)
        self.channel = channel
        self.target = pose_from_values(self.model, live_values)
        if live_values is None:
            self.note.setText("等待实时数据 · 过期/未连接不驱动弯曲")
        else:
            self.note.setText("手指弯曲由受力推算，仅示意 · 拖动旋转 · 滚轮缩放")


    def set_values(self, values) -> None:
        """ui.py 兼容桥：把 25 点喂给 set_data（沿用当前手/通道/量程）。"""
        hand = self.side or "R"
        self.set_data(hand, tuple(values) if values is not None else None, self.channel, self.maximum)

    def set_idle(self, reason: str = "等待实时数据 · 过期/未连接不驱动弯曲") -> None:
        """ui.py 兼容桥：断连/过期时清零姿态，不刷假受力话术。"""
        hand = self.side or "R"
        self.set_data(hand, None, self.channel, self.maximum)
        self.note.setText(reason)

    def _animate(self) -> None:
        """平滑逼近目标姿态，更新零件位姿、零件着色和受力标记。"""
        if not self.side or not self.isVisible():
            return
        for name, value in self.target.items():
            self.current[name] += (value - self.current.get(name, 0.0)) * SMOOTHING
        transforms = self.model.link_transforms(self.current)
        values = self.values or (0,) * CHANNEL_COUNT
        for item, link in self.items:
            item.setTransform(_qmatrix(transforms[link] @ item.visual_origin))
            channels = self.links.get(link, ())
            load = max((values[index] for index in channels), default=0)
            fraction = max(0.0, min(1.0, load / self.maximum))
            heat = fraction_color(fraction)
            # 作用：按受力把零件从金属灰渐变到热度色；只染有对应感应点的零件，其余保持原色。
            rgb = SHELL_RGB * (1 - fraction * .85) + np.array(heat.getRgbF()[:3]) * fraction * .85
            item.setColor((*rgb, 1.0))
        positions, colors, sizes = [], [], []
        pulse = .5 + .5 * np.sin(time.monotonic() * 6)
        for index, (link, offset) in enumerate(self.anchors):
            point = transforms[link] @ np.append(offset, 1.0)
            positions.append(point[:3])
            fraction = max(0.0, min(1.0, values[index] / self.maximum))
            color = fraction_color(fraction)
            colors.append((*color.getRgbF()[:3], .55 + .45 * fraction))
            sizes.append(MARKER_PX[0] + (MARKER_PX[1] - MARKER_PX[0]) * fraction)
        self.markers.setData(pos=np.array(positions), color=np.array(colors), size=np.array(sizes))
        selected = positions[self.channel - 1]
        # 作用：当前选中的测点外加一圈闪烁的白环，与 2D 手掌图的选择同步。
        self.selection.setData(pos=np.array([selected]), color=(1, 1, 1, .35 + .4 * pulse),
                               size=sizes[self.channel - 1] + 10)
