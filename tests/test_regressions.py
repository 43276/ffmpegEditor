"""共享配置、视频停止语义与滚动的回归检查（不修改用户设置）。"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QLabel, QVBoxLayout, QWidget

from app.ffmpeg_environment import FfmpegCapabilities
from app.task_models import ProcessResult
from app.video.models import VideoBatch, VideoCompressOptions
from ui.services.ffmpeg_service import FfmpegService
from ui.widgets.smooth_scroll import SmoothScrollArea
from ui.video.page import VideoPage
from ui.tasks.worker import TaskWorker
from ui.tasks.jobs import video_job


CAPS = FfmpegCapabilities("test FFmpeg", {"libx264"}, {"mp4"})
APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def WaitUntil(predicate, seconds=3):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() > deadline:
            raise AssertionError("等待 Qt 事件超时")
        QTest.qWait(10)
    APP.processEvents()


class RegressionTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_regression_")
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)

    def tearDown(self):
        APP.processEvents()
        self.temp.cleanup()

    def test_ffmpeg_path_change_during_probe_discards_old_capabilities(self):
        entered, release = threading.Event(), threading.Event()
        states = []

        def probe(path, **kwargs):
            if path == "old.exe":
                entered.set()
                release.wait(2)
            return FfmpegCapabilities(path, {"libx264"}, {"mp4"})

        service = FfmpegService(self.settings)
        service.stateChanged.connect(lambda *state: states.append(state))
        try:
            with patch("ui.services.ffmpeg_service.locate_ffmpeg", side_effect=lambda path: path), patch("ui.services.ffmpeg_service.probe_ffmpeg", side_effect=probe):
                service.SetPath("old.exe")
                WaitUntil(entered.is_set)
                service.SetPath("new.exe")
                release.set()
                WaitUntil(lambda: service._worker is None)
            self.assertEqual(states[-1][0], "new.exe")
            self.assertEqual(states[-1][1].version, "new.exe")
            self.assertFalse(any(state[0] == "old.exe" for state in states))
            self.assertEqual(self.settings.value("ffmpeg/path"), "new.exe")
        finally:
            release.set()
            service.Shutdown()
            WaitUntil(lambda: not service.active)

    def test_invalid_explicit_ffmpeg_path_stays_unavailable(self):
        states = []
        service = FfmpegService(self.settings)
        service.stateChanged.connect(lambda *state: states.append(state))
        try:
            service.SetPath(str(self.root / "missing.exe"))
            WaitUntil(lambda: service._worker is None)
            self.assertIsNone(states[-1][0])
            self.assertIsNone(states[-1][1])
            self.assertIn("路径不存在", states[-1][2])
        finally:
            service.Shutdown()
            WaitUntil(lambda: not service.active)

    def test_video_cancel_cleans_partial_output_and_can_restart(self):
        source = self.root / "source.mp4"
        source.write_bytes(b"original video")
        entered = threading.Event()
        page = VideoPage(settings=self.settings)
        page.SetFfmpegPath("ffmpeg.exe", CAPS)
        page._SetInputs([source])
        WaitUntil(lambda: not page._preview_controller.active)
        summaries = []

        def process(command, cancel_check, **kwargs):
            partial = Path(command[-1])
            partial.write_bytes(b"partial video")
            entered.set()
            deadline = time.monotonic() + 2
            while not cancel_check() and time.monotonic() < deadline:
                time.sleep(0.005)
            return ProcessResult(-1, cancelled=cancel_check())

        try:
            with patch("app.converter.execute", side_effect=process), patch.object(page, "_ShowInfo"):
                page._Start()
                page._task_controller.completed.connect(lambda _kind, result: summaries.append(result))
                WaitUntil(entered.is_set)
                page._Cancel()
                self.assertFalse(page.cancel_button.isEnabled())
                WaitUntil(lambda: not page._task_controller.active)
                self.assertEqual(page.status.text(), "已取消")
                self.assertTrue(summaries[-1].cancelled)
                self.assertFalse(list(self.root.rglob("*.part.mp4")))
                self.assertEqual(source.read_bytes(), b"original video")

            def success(command, cancel_check, **kwargs):
                Path(command[-1]).write_bytes(b"compressed video")
                return ProcessResult(0)

            with patch("app.converter.execute", side_effect=success), patch.object(page, "_ShowInfo"):
                page._Start()
                self.assertIn("正在校验输入", page.status.text())
                WaitUntil(lambda: not page._task_controller.active)
                self.assertIn("压缩完成", page.status.text())
                self.assertTrue(page.start_button.isEnabled())
                self.assertEqual((self.root / "source_output" / "source.mp4").read_bytes(), b"compressed video")
                self.assertEqual(source.read_bytes(), b"original video")
        finally:
            page.Shutdown()
            WaitUntil(lambda: not page._task_controller.active)
            page.deleteLater()

    def test_video_end_finishes_every_file_in_current_batch_only(self):
        sources = [self.root / name for name in ("first.mp4", "second.mp4", "later.mp4")]
        for source in sources:
            source.write_bytes(b"original")
        batches = [
            VideoBatch(self.root, self.root / "first_output", sources[:2]),
            VideoBatch(self.root, self.root / "later_output", sources[2:]),
        ]
        worker = TaskWorker(1, video_job(batches, VideoCompressOptions("ffmpeg.exe"), CAPS))
        calls, summaries = [], []
        worker.taskFinished.connect(lambda _id, result: summaries.append(result))

        def process(command, cancel_check, **kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b"compressed")
            worker.request_finish_after_current_batch()
            return ProcessResult(0)

        try:
            with patch("app.converter.execute", side_effect=process):
                worker.start()
                WaitUntil(lambda: bool(summaries))
                worker.wait()
            self.assertEqual(len(calls), 2)
            self.assertTrue(summaries[-1].early_stopped)
            self.assertEqual(summaries[-1].ok, 2)
            self.assertFalse((self.root / "later_output").exists())
            self.assertTrue(all(source.read_bytes() == b"original" for source in sources))
        finally:
            worker.request_cancel()
            worker.wait()

    def test_window_driven_scroll_completes_and_stops(self):
        area = SmoothScrollArea()
        content = QWidget()
        content.setMinimumSize(200, 1600)
        area.setWidget(content)
        area.resize(400, 300)
        area.show()
        try:
            QTest.qWait(30)
            event = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
            APP.sendEvent(area.viewport(), event)
            WaitUntil(lambda: not area._animator._active)
            self.assertEqual(area.verticalScrollBar().value(), 180)
            self.assertTrue(content.updatesEnabled())
            QTest.qWait(50)
            self.assertEqual(area.verticalScrollBar().value(), 180)
        finally:
            area.close()
            area.deleteLater()

    def test_scroll_snapshot_restores_live_content_on_hide_and_resize(self):
        area = SmoothScrollArea()
        content = QWidget()
        content.setMinimumSize(200, 1600)
        layout = QVBoxLayout(content)
        label = QLabel("old", content)
        layout.addWidget(label)
        area.setWidget(content)
        area.resize(400, 300)
        area.show()
        try:
            QTest.qWait(30)
            def wheel():
                event = QWheelEvent(QPointF(100, 100), QPointF(100, 100), QPoint(), QPoint(0, -120), Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier, Qt.ScrollPhase.NoScrollPhase, False)
                APP.sendEvent(area.viewport(), event)

            wheel()
            self.assertFalse(content.updatesEnabled())
            label.setText("new")
            area.resize(500, 350)
            APP.processEvents()
            self.assertFalse(area._animator._active)
            self.assertTrue(content.updatesEnabled())
            self.assertEqual(label.text(), "new")
            wheel()
            self.assertFalse(content.updatesEnabled())
            area.hide()
            self.assertTrue(content.updatesEnabled())
            self.assertFalse(area._animator._active)
        finally:
            area.close()
            area.deleteLater()


if __name__ == "__main__":
    unittest.main()
