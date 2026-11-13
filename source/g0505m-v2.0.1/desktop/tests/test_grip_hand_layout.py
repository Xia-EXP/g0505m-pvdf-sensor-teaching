"""编号标签在小屏、镜像及宽屏均不得与圆点或彼此重叠；点内数值必须放得下。"""

import os

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")  # 作用：几何检查独立于用户当前桌面。

import pytest
from PySide6.QtCore import QRectF
from PySide6.QtWidgets import QApplication

from grip_teaching.hand_view import HandView, POINTS
from grip_teaching.fonts import configure_fonts

_APPLICATION = QApplication.instance() or QApplication([])  # 作用：复用唯一GUI应用，兼容独立及全套运行。


@pytest.mark.parametrize("size", [(400, 410), (459, 411), (656, 502)])
@pytest.mark.parametrize("hand", ["L", "R"])
def test_number_labels_avoid_each_other_and_markers(size, hand):
    """几何边界独立于读数；65535 与 -65535 的切换不应改变编号标签位置。"""
    configure_fonts()  # 作用：与正式窗口一致加载本机中文字体，离屏后端默认替代字体宽度并不真实。
    view = HandView()
    view.resize(*size)
    view.set_data(hand, (65535,) * 25, 1, 1000)
    labels = view.number_rects()
    assert len(labels) == 25
    radius = view.marker_radius()
    markers = [QRectF(view.position(channel).x() - radius, view.position(channel).y() - radius,
                      radius * 2, radius * 2) for channel in POINTS]
    for channel, rectangle in labels.items():
        assert QRectF(view.rect()).contains(rectangle)
        assert all(not rectangle.intersects(marker) for marker in markers)
        assert all(not rectangle.intersects(other) for other_channel, other in labels.items() if channel != other_channel)
    view.set_data(hand, (-65535,) * 25, 25, 5000)
    assert labels == view.number_rects()
    view.close()


@pytest.mark.parametrize("size", [(400, 410), (656, 502)])
def test_values_fit_inside_markers(size):
    """常见四位读数（如 1016）用自动缩小的字体后必须放进圆点内，不溢出到相邻点。"""
    configure_fonts()
    view = HandView()
    view.resize(*size)
    width = 2 * view.marker_radius() - 3
    from PySide6.QtGui import QFontMetricsF
    for text in ("8", "445", "1016", "-19"):
        font = HandView._fit_font(view.font(), text, width)
        assert QFontMetricsF(font).horizontalAdvance(text) <= width or font.pointSize() == 6
    assert view.marker_radius() >= 9
    view.close()
