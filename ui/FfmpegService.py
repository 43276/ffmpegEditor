"""共享 FFmpeg 配置与异步能力检测。"""
from __future__ import annotations

from threading import Event

from PyQt6.QtCore import QObject, QSettings, QThread, Qt, pyqtSignal

from app.ffmpeg_environment import locate_ffmpeg, probe_ffmpeg


class _ProbeThread(QThread):
    def __init__(self, explicit: str, parent=None):
        super().__init__(parent)
        self.explicit = explicit
        self.path = None
        self.capabilities = None
        self.error = ""
        self._cancel = Event()

    def RequestCancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            self.path = locate_ffmpeg(self.explicit or None)
            self.capabilities = probe_ffmpeg(self.path, cancel_check=self._cancel.is_set)
        except Exception as exc:  # noqa: BLE001 -- 在线程结束后统一报告
            self.error = str(exc)


class FfmpegService(QObject):
    stateChanged = pyqtSignal(object, object, str)
    idle = pyqtSignal()

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._explicit = settings.value("ffmpeg/path", "", type=str)
        self._worker: _ProbeThread | None = None
        self._pending = False
        self._closing = False

    @property
    def active(self) -> bool:
        return self._worker is not None

    def Start(self) -> None:
        if not self._closing:
            self.SetPath(self._explicit)

    def SetPath(self, explicit: str) -> None:
        if self._closing:
            return
        self._explicit = explicit.strip()
        self._settings.setValue("ffmpeg/path", self._explicit)
        self._settings.sync()
        self.stateChanged.emit(None, None, "正在检测 FFmpeg…")
        if self._worker is not None:
            self._pending = True
            return
        self._worker = _ProbeThread(self._explicit, self)
        self._worker.finished.connect(self._CompleteProbe, Qt.ConnectionType.QueuedConnection)
        self._worker.start()

    def _CompleteProbe(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.deleteLater()
        if self._closing:
            self.idle.emit()
            return
        if self._pending:
            self._pending = False
            self.Start()
        elif worker.error:
            self.stateChanged.emit(None, None, f"✗ {worker.error}")
        else:
            self.stateChanged.emit(worker.path, worker.capabilities, f"✓ {worker.capabilities.version}")
        if not self.active:
            self.idle.emit()

    def Shutdown(self) -> None:
        self._closing = True
        self._pending = False
        if self._worker is not None:
            self._worker.RequestCancel()
        else:
            self.idle.emit()
