"""共享 FFmpeg 配置与异步能力检测。"""
from __future__ import annotations

from PyQt6.QtCore import QObject, QSettings, QThread, pyqtSignal

from app.Converter import LocateFfmpeg, ProbeFfmpeg


class _ProbeThread(QThread):
    def __init__(self, explicit: str, parent=None):
        super().__init__(parent)
        self.explicit = explicit
        self.path = None
        self.capabilities = None
        self.error = ""

    def run(self) -> None:
        try:
            self.path = LocateFfmpeg(self.explicit or None)
            self.capabilities = ProbeFfmpeg(self.path)
        except Exception as exc:  # noqa: BLE001 -- 在线程结束后统一报告
            self.error = str(exc)


class FfmpegService(QObject):
    stateChanged = pyqtSignal(object, object, str)

    def __init__(self, settings: QSettings, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._explicit = settings.value("ffmpeg/path", "", type=str)
        self._worker: _ProbeThread | None = None
        self._pending = False
        self._closing = False

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
        self._worker.finished.connect(self._CompleteProbe)
        self._worker.start()

    def _CompleteProbe(self) -> None:
        worker = self._worker
        self._worker = None
        if worker is None:
            return
        worker.deleteLater()
        if self._closing:
            return
        if self._pending:
            self._pending = False
            self.Start()
        elif worker.error:
            self.stateChanged.emit(None, None, f"✗ {worker.error}")
        else:
            self.stateChanged.emit(worker.path, worker.capabilities, f"✓ {worker.capabilities.version}")

    def Shutdown(self) -> None:
        self._closing = True
        if self._worker is not None:
            self._worker.wait()
