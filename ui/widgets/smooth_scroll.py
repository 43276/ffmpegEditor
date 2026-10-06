"""通过窗口更新请求驱动滚动，避免第三方滚动引擎的固定 60 FPS。"""
from __future__ import annotations

from PyQt6 import sip
from PyQt6.QtCore import QElapsedTimer, QEvent, QObject, QPoint, Qt
from PyQt6.QtGui import QPainter, QPixmap, QRegion
from PyQt6.QtWidgets import QAbstractScrollArea, QWidget
from qfluentwidgets import ScrollArea


class _ScrollSnapshot(QWidget):
    """滚动期间复用控件图像，结束时立即恢复实时控件绘制。"""

    def __init__(self, area: ScrollArea):
        super().__init__(area.viewport())
        self.area = area
        self.content = None
        self._pixmap = QPixmap()
        self.setAttribute(Qt.WidgetAttribute.WA_TransparentForMouseEvents)
        self.hide()

    def Begin(self) -> None:
        if self.content is not None:
            return
        content = self.area.widget()
        if content is None or not content.updatesEnabled():
            return
        dpr = content.devicePixelRatioF()
        # 超长内容保留实时绘制，避免一次性分配过大的图片缓存。
        if content.width() * content.height() * dpr * dpr > 8_000_000:
            return
        pixmap = QPixmap(round(content.width() * dpr), round(content.height() * dpr))
        pixmap.setDevicePixelRatio(dpr)
        pixmap.fill(Qt.GlobalColor.transparent)
        painter = QPainter(pixmap)
        content.render(painter, QPoint(), QRegion(), QWidget.RenderFlag.DrawChildren)
        painter.end()
        self._pixmap = pixmap
        self.content = content
        content.setUpdatesEnabled(False)
        self.setGeometry(self.area.viewport().rect())
        self.show()
        self.raise_()

    def End(self) -> None:
        if self.content is not None and not sip.isdeleted(self.content):
            self.content.setUpdatesEnabled(True)
        self.content = None
        self.hide()
        self._pixmap = QPixmap()

    def paintEvent(self, event) -> None:  # noqa: N802
        painter = QPainter(self)
        painter.drawPixmap(-self.area.horizontalScrollBar().value(), -self.area.verticalScrollBar().value(), self._pixmap)


class _ScrollAnimator(QObject):
    def __init__(self, area: QAbstractScrollArea):
        super().__init__(area)
        self.area = area
        self._viewport = area.viewport()
        self._window = None
        self._bar = None
        self._clock = QElapsedTimer()
        self._start = self._target = 0
        self._duration = 220.0
        self._active = False
        self._snapshot = _ScrollSnapshot(area) if isinstance(area, SmoothScrollArea) else None
        self._viewport.installEventFilter(self)

    def eventFilter(self, obj, event):  # noqa: N802
        if obj is self._viewport:
            if event.type() in (QEvent.Type.Hide, QEvent.Type.Resize):
                self._Stop()
            elif event.type() == QEvent.Type.Wheel:
                return self._Wheel(event)
        elif obj is self._window and event.type() == QEvent.Type.UpdateRequest:
            self._Advance()
        return False

    def _Wheel(self, event) -> bool:
        if event.modifiers() != Qt.KeyboardModifier.NoModifier:
            return False
        horizontal = event.angleDelta().y() == 0 and event.pixelDelta().y() == 0
        bar = self.area.horizontalScrollBar() if horizontal else self.area.verticalScrollBar()
        pixels = event.pixelDelta().x() if horizontal else event.pixelDelta().y()
        delta = event.angleDelta().x() if horizontal else event.angleDelta().y()
        direction = pixels or delta
        if not direction or (direction > 0 and bar.value() == bar.minimum()) or (
            direction < 0 and bar.value() == bar.maximum()
        ):
            self._Stop()
            return False
        if pixels:
            self._Stop()
            bar.setValue(bar.value() - pixels)
        else:
            handle = self.area.window().windowHandle()
            if handle is None:
                return False
            if self._window is not handle:
                if self._window is not None:
                    self._window.removeEventFilter(self)
                self._window = handle
                handle.installEventFilter(self)
            continuing = self._active and self._bar is bar
            if continuing and (self._target - bar.value()) * delta > 0:
                continuing = False
            target = self._target if continuing else bar.value()
            self._bar = bar
            self._start = bar.value()
            self._target = max(bar.minimum(), min(bar.maximum(), target - delta * 1.5))
            self._clock.start()
            self._active = self._target != self._start
            if self._active:
                if self._snapshot:
                    self._snapshot.Begin()
                handle.requestUpdate()
            else:
                self._Stop()
        event.accept()
        return True

    def _Advance(self) -> None:
        if not self._active:
            return
        if not self.area.isVisible():
            self._Stop()
            return
        elapsed = self._clock.nsecsElapsed() / 1_000_000
        fraction = min(1.0, elapsed / self._duration)
        eased = 1 - (1 - fraction) ** 3
        self._bar.setValue(round(self._start + (self._target - self._start) * eased))
        if self._snapshot:
            self._snapshot.update()
        self._active = fraction < 1.0
        if self._active:
            self._window.requestUpdate()
        else:
            self._Stop()

    def _Stop(self) -> None:
        self._active = False
        if self._snapshot:
            self._snapshot.End()


class SmoothScrollArea(ScrollArea):
    def __init__(self, parent=None):
        super().__init__(parent)
        EnableSmoothScrolling(self)

    def setWidget(self, widget) -> None:  # noqa: N802
        super().setWidget(widget)
        # 只清除滚动容器底色，不覆盖卡片、编辑框等子控件的主题样式。
        self.setStyleSheet("QScrollArea { border: none; background: transparent; }")
        self.viewport().setAutoFillBackground(False)
        widget.setAutoFillBackground(False)


def EnableSmoothScrolling(area: QAbstractScrollArea) -> None:
    if not hasattr(area, "_animator"):
        area._animator = _ScrollAnimator(area)
