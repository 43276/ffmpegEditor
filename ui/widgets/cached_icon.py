"""主题感知的 SVG 图标栅格缓存。"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, Qt
from PyQt6.QtGui import QPainter, QPixmap, QPixmapCache
from qfluentwidgets import Theme
from qfluentwidgets.common.icon import FluentIconBase


class CachedIcon(FluentIconBase):
    """缓存侧栏 SVG 的栅格结果，保留主题与选中颜色。"""

    def __init__(self, icon: FluentIconBase):
        self._icon = icon

    def path(self, theme=Theme.AUTO):
        return self._icon.path(theme)

    def render(self, painter, rect, theme=Theme.AUTO, indexes=None, **attributes):
        rect = QRectF(rect)
        dpr = painter.device().devicePixelRatioF()
        key = repr(("ffmpegEditor/icon", self.path(theme), rect.width(), rect.height(), dpr, indexes, sorted(attributes.items())))
        pixmap = QPixmapCache.find(key)
        if pixmap is None:
            pixmap = QPixmap(round(rect.width() * dpr), round(rect.height() * dpr))
            pixmap.setDevicePixelRatio(dpr)
            pixmap.fill(Qt.GlobalColor.transparent)
            cached_painter = QPainter(pixmap)
            self._icon.render(cached_painter, QRectF(0, 0, rect.width(), rect.height()), theme, indexes, **attributes)
            cached_painter.end()
            QPixmapCache.insert(key, pixmap)
        painter.drawPixmap(rect, pixmap, QRectF(pixmap.rect()))
