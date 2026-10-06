"""后台预览：最多一个扫描线程，连续请求只执行最新快照。"""
from __future__ import annotations

from enum import Enum
from threading import Event

from PyQt6.QtCore import QObject, QThread, Qt, pyqtSignal, pyqtSlot


class PreviewState(Enum):
    EMPTY = "empty"
    PENDING = "pending"
    VALID = "valid"
    ERROR = "error"


class _PreviewWorker(QThread):
    def __init__(self, request_id, operation, parent):
        super().__init__(parent)
        self.request_id = request_id
        self.operation = operation
        self.cancel = Event()
        self.result = None
        self.error = None

    def run(self):
        try:
            if not self.cancel.is_set():
                self.result = self.operation(self.cancel.is_set)
        except Exception as exc:
            if not self.cancel.is_set():
                self.error = str(exc)


class PreviewController(QObject):
    resultReady = pyqtSignal(object)
    errorOccurred = pyqtSignal(str)
    stateChanged = pyqtSignal(object)
    idle = pyqtSignal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self._request_id = 0
        self._worker = None
        self._pending = None
        self._closing = False
        self._state = PreviewState.EMPTY

    @property
    def request_id(self):
        return self._request_id

    @property
    def active(self):
        return self._worker is not None

    @property
    def state(self):
        return self._state

    def _set_state(self, state):
        if state != self._state:
            self._state = state
            self.stateChanged.emit(state)

    def invalidate(self):
        self._request_id += 1
        self._pending = None
        if self._worker:
            self._worker.cancel.set()
        self._set_state(PreviewState.EMPTY)

    def request(self, operation):
        self.invalidate()
        if self._closing:
            return self._request_id
        self._pending = (self._request_id, operation)
        self._set_state(PreviewState.PENDING)
        self._start_pending()
        return self._request_id

    def _start_pending(self):
        if self.active or self._closing or not self._pending:
            return
        request_id, operation = self._pending
        self._pending = None
        worker = _PreviewWorker(request_id, operation, self)
        self._worker = worker
        worker.finished.connect(self._on_finished, Qt.ConnectionType.QueuedConnection)
        worker.start()

    @pyqtSlot()
    def _on_finished(self):
        worker = self.sender()
        if worker is not self._worker:
            return
        self._worker = None
        if not self._closing and worker.request_id == self._request_id and not worker.cancel.is_set():
            if worker.error is None:
                self._set_state(PreviewState.VALID)
                if worker.request_id == self._request_id and not self._closing:
                    self.resultReady.emit(worker.result)
            else:
                self._set_state(PreviewState.ERROR)
                if worker.request_id == self._request_id and not self._closing:
                    self.errorOccurred.emit(worker.error)
        worker.deleteLater()
        self._start_pending()
        if not self.active:
            self.idle.emit()

    def shutdown(self):
        self._closing = True
        self.invalidate()
        if not self.active:
            self.idle.emit()
