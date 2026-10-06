"""共享 FFmpeg / ffprobe 配置、环境快照与异步能力检测。"""
from __future__ import annotations

from dataclasses import dataclass
from threading import Event

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal

from app.ffmpeg_environment import FfmpegCapabilities, locate_ffmpeg, locate_ffprobe, probe_ffmpeg
from ui.services.settings_service import SettingsService


@dataclass(frozen=True)
class EnvironmentSnapshot:
    ffmpeg_path: str | None = None
    ffprobe_path: str | None = None
    capabilities: FfmpegCapabilities | None = None
    status: str = "unavailable"
    error: str = ""
    message: str = "尚未检测 FFmpeg"
    request_id: int = 0


class _ProbeThread(QThread):
    def __init__(self, explicit: str, request_id: int, parent=None):
        super().__init__(parent)
        self.explicit = explicit
        self.request_id = request_id
        self.path = None
        self.ffprobe_path = None
        self.capabilities = None
        self.error = ""
        self.ffprobe_error = ""
        self._cancel = Event()

    def RequestCancel(self) -> None:
        self._cancel.set()

    def run(self) -> None:
        try:
            self.path = locate_ffmpeg(self.explicit or None)
            self.capabilities = probe_ffmpeg(self.path, cancel_check=self._cancel.is_set)
            if self._cancel.is_set():
                return
            try:
                self.ffprobe_path = locate_ffprobe(self.path)
            except Exception as exc:  # noqa: BLE001 -- partial environment remains usable
                self.ffprobe_error = str(exc)
        except Exception as exc:  # noqa: BLE001 -- report after QThread finishes
            self.error = str(exc)


class FfmpegService(QObject):
    stateChanged = pyqtSignal(object, object, str)
    environmentChanged = pyqtSignal(object)
    idle = pyqtSignal()

    def __init__(self, settings: SettingsService, parent=None):
        super().__init__(parent)
        self._settings = settings
        self._explicit = settings.value("ffmpeg/path", "", type=str)
        self._worker: _ProbeThread | None = None
        self._pending = False
        self._closing = False
        self._request_id = 0
        self._snapshot = EnvironmentSnapshot()

    @property
    def active(self) -> bool:
        return self._worker is not None

    @property
    def snapshot(self) -> EnvironmentSnapshot:
        return self._snapshot

    def _Publish(self, snapshot: EnvironmentSnapshot) -> None:
        self._snapshot = snapshot
        self.environmentChanged.emit(snapshot)
        self.stateChanged.emit(snapshot.ffmpeg_path, snapshot.capabilities, snapshot.message)

    def Start(self) -> None:
        if not self._closing:
            self.SetPath(self._explicit)

    def SetPath(self, explicit: str) -> None:
        if self._closing:
            return
        self._explicit = explicit.strip()
        self._request_id += 1
        self._settings.setValue("ffmpeg/path", self._explicit)
        self._settings.sync()
        self._Publish(EnvironmentSnapshot(status="checking", message="正在检测 FFmpeg…", request_id=self._request_id))
        if self._worker is not None:
            self._pending = True
            self._worker.RequestCancel()
            return
        self._BeginProbe()

    def _BeginProbe(self) -> None:
        self._worker = _ProbeThread(self._explicit, self._request_id, self)
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
            self._BeginProbe()
        elif worker.request_id == self._request_id:
            if worker.error:
                self._Publish(EnvironmentSnapshot(status="error", error=worker.error,
                                                  message=f"✗ {worker.error}", request_id=worker.request_id))
            else:
                message = f"✓ {worker.capabilities.version}"
                if worker.ffprobe_error:
                    message += f"；音频读取不可用：{worker.ffprobe_error}"
                self._Publish(EnvironmentSnapshot(worker.path, worker.ffprobe_path, worker.capabilities,
                                                  "ready", worker.ffprobe_error, message, worker.request_id))
        if not self.active:
            self.idle.emit()

    def Shutdown(self) -> None:
        self._closing = True
        self._pending = False
        if self._worker is not None:
            self._worker.RequestCancel()
        else:
            self.idle.emit()
