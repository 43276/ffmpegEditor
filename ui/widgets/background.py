"""独立背景层；图片与模糊只在配置变化时生成，重绘时复用缓存。"""
from __future__ import annotations

from PyQt6.QtCore import QRectF, QSize, Qt
from PyQt6.QtGui import QColor, QImageReader, QPainter, QPixmap
from PyQt6.QtWidgets import QGraphicsBlurEffect, QGraphicsPixmapItem, QGraphicsScene, QWidget
from qfluentwidgets import isDarkTheme, qconfig


class BackgroundLayer(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self._source = QPixmap()
        self._pixmap = QPixmap()
        self._display = QPixmap()
        self._path = ""
        self._blur = -1
        self._opacity = 0.75
        self.lower()
        self.hide()
        qconfig.themeChangedFinished.connect(self.update)

    def Configure(self, path: str, transparency: int, blur: int) -> str:
        if path != self._path:
            if path:
                reader = QImageReader(path)
                reader.setAutoTransform(True)
                size = reader.size()
                if size.isValid() and (size.width() > 2560 or size.height() > 1600):
                    reader.setScaledSize(size.scaled(QSize(2560, 1600), Qt.AspectRatioMode.KeepAspectRatio))
                source = QPixmap.fromImage(reader.read())
                if source.isNull():
                    self.hide()
                    return f"无法读取背景图片：{reader.errorString()}"
                self._source = source
            else:
                self._source = QPixmap()
            self._path = path
            self._blur = -1
        if blur != self._blur:
            self._blur = blur
            self._pixmap = self._BlurredPixmap(blur)
            self._display = QPixmap()
        self._opacity = (100 - transparency) / 100
        self.setVisible(bool(path))
        self.update()
        return ""

    def _BlurredPixmap(self, radius: int) -> QPixmap:
        if self._source.isNull() or radius == 0:
            return self._source
        scene = QGraphicsScene()
        item = QGraphicsPixmapItem(self._source)
        effect = QGraphicsBlurEffect()
        effect.setBlurRadius(radius)
        item.setGraphicsEffect(effect)
        scene.addItem(item)
        bounds = QRectF(self._source.rect())
        scene.setSceneRect(bounds)
        result = QPixmap(self._source.size())
        result.fill(Qt.GlobalColor.transparent)
        painter = QPainter(result)
        scene.render(painter, bounds, bounds)
        painter.end()
        return result

    def paintEvent(self, event) -> None:  # noqa: N802
        if self._pixmap.isNull():
            return
        dpr = self.devicePixelRatioF()
        if self._display.isNull() or self._display.devicePixelRatioF() != dpr:
            self._display = self._pixmap.scaled(
                QSize(round(self.width() * dpr), round(self.height() * dpr)),
                Qt.AspectRatioMode.KeepAspectRatioByExpanding,
                Qt.TransformationMode.SmoothTransformation,
            )
            self._display.setDevicePixelRatio(dpr)
        painter = QPainter(self)
        painter.setOpacity(self._opacity)
        # 按比例铺满窗口，超出部分居中裁剪。
        width, height = self._display.width() / dpr, self._display.height() / dpr
        painter.drawPixmap(round((self.width() - width) / 2), round((self.height() - height) / 2), self._display)
        # 深色主题下压暗亮图片，避免白色文字直接落在亮背景上。
        painter.setOpacity(1.0)
        if isDarkTheme():
            painter.fillRect(self.rect(), QColor(0, 0, 0, round(180 * self._opacity)))

    def resizeEvent(self, event) -> None:  # noqa: N802
        self._display = QPixmap()
        super().resizeEvent(event)
