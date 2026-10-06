"""第四阶段：后台预览、启动快照及共享日志/任务展示回归。"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import Theme, qconfig

from app.ffmpeg_environment import FfmpegCapabilities
from app.image.planner import build_batches_for_inputs
from app.task_models import LogLevel, ProcessResult, TaskProgress, TaskResult
from ui.audio.album_page import AlbumPage
from ui.image.page import ImagePage
from ui.video.page import VideoPage
from ui.tasks.controller import TaskController
from ui.tasks.worker import TaskWorker
from ui.tasks.preview import PreviewController, PreviewState
from ui.widgets.log_panel import LogPanel
from ui.widgets.task_panel import TaskPanel


APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)
CAPS = FfmpegCapabilities("test", {"png", "mjpeg", "libx264"}, {"mp4"})


def wait_until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待 Qt 事件超时")
        QTest.qWait(5)
    APP.processEvents()


class PreviewWidgetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_preview_")
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)
        self.objects = []
        self.releases = []

    def tearDown(self):
        for release in self.releases:
            release.set()
        for obj in self.objects:
            if hasattr(obj, "Shutdown"):
                obj.Shutdown()
                wait_until(lambda: not obj._task_controller.active and not obj._preview_controller.active)
            elif hasattr(obj, "shutdown"):
                obj.shutdown()
                wait_until(lambda: not obj.active)
            if hasattr(obj, "close"):
                obj.close()
        APP.processEvents()
        self.temp.cleanup()

    def sample(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
        return path

    def page(self, cls, **kwargs):
        obj = cls(settings=self.settings, **kwargs)
        self.objects.append(obj)
        if hasattr(obj, "_ShowInfoBar"):
            obj._ShowInfoBar = Mock()
        if hasattr(obj, "_ShowInfo"):
            obj._ShowInfo = Mock()
        return obj

    def test_only_latest_preview_runs_and_gui_responds_while_pending(self):
        controller = PreviewController()
        self.objects.append(controller)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        results, calls = [], []
        controller.resultReady.connect(results.append)

        def first(cancel_check):
            entered.set()
            release.wait(4)
            return "obsolete"

        controller.request(first)
        wait_until(entered.is_set)
        controller.request(lambda check: calls.append("middle") or "middle")
        controller.request(lambda check: calls.append("latest") or "latest")
        self.assertEqual(controller.state, PreviewState.PENDING)
        # Processing a UI event cannot require the held scan to complete.
        APP.processEvents()
        self.assertTrue(controller.active)
        self.assertFalse(results)
        release.set()
        wait_until(lambda: not controller.active)
        self.assertEqual(calls, ["latest"])
        self.assertEqual(results, ["latest"])
        self.assertEqual(controller.state, PreviewState.VALID)

    def test_obsolete_error_and_shutdown_result_are_not_published(self):
        controller = PreviewController()
        self.objects.append(controller)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        errors, results = [], []
        controller.errorOccurred.connect(errors.append)
        controller.resultReady.connect(results.append)

        def fail(check):
            entered.set()
            release.wait(4)
            raise ValueError("old error")

        controller.request(fail)
        wait_until(entered.is_set)
        worker = controller._worker
        controller.shutdown()
        self.assertTrue(controller.active)
        self.assertIs(controller._worker, worker)
        release.set()
        wait_until(lambda: not controller.active)
        self.assertEqual(controller.state, PreviewState.EMPTY)
        self.assertFalse(errors or results)

    def test_consumer_invalidating_ready_state_does_not_receive_old_result(self):
        controller = PreviewController()
        self.objects.append(controller)
        results = []
        controller.resultReady.connect(results.append)
        controller.stateChanged.connect(
            lambda state: controller.invalidate() if state == PreviewState.VALID else None)
        controller.request(lambda check: "obsolete")
        wait_until(lambda: not controller.active)
        self.assertEqual(controller.state, PreviewState.EMPTY)
        self.assertFalse(results)

    def _late_stop(self, *, cancel):
        emitted, release = threading.Event(), threading.Event()
        self.releases.append(release)
        class HeldResultWorker(TaskWorker):
            def run(self):
                super().run()
                emitted.set()
                release.wait(4)
        controller = TaskController()
        self.objects.append(controller)
        results = []
        controller.completed.connect(lambda kind, result: results.append(result))
        with patch("ui.tasks.controller.TaskWorker", HeldResultWorker):
            controller.start(lambda context: TaskResult(total=1, ok=1), total=1, supports_finish=True)
        # The business result has been emitted, but its queued GUI callback has not run.
        self.assertTrue(emitted.wait(2))
        requested = controller.request_cancel() if cancel else controller.request_finish_after_current_batch()
        self.assertTrue(requested)
        release.set()
        wait_until(lambda: not controller.active)
        return results[0]

    def test_cancel_before_queued_result_preserves_stop_intent(self):
        result = self._late_stop(cancel=True)
        self.assertTrue(result.cancelled)
        self.assertFalse(result.early_stopped)

    def test_finish_before_queued_result_preserves_stop_intent(self):
        result = self._late_stop(cancel=False)
        self.assertTrue(result.early_stopped)
        self.assertFalse(result.cancelled)

    def test_pages_reject_pending_and_invalid_previews(self):
        image, video, album = (self.page(cls) for cls in (ImagePage, VideoPage, AlbumPage))
        image.SetFfmpegPath("ffmpeg", CAPS, "ready")
        video.SetFfmpegPath("ffmpeg", CAPS)
        album.SetFfmpegPath("ffmpeg")
        source = self.sample("picture.png")
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def delayed(paths, **kwargs):
            entered.set()
            release.wait(4)
            return build_batches_for_inputs(paths, **kwargs)
        with patch("ui.image.page.BuildBatchesForInputs", delayed):
            image._SetInputPaths([source])
            wait_until(entered.is_set)
            self.assertFalse(image.start_button.isEnabled())
            image._OnStartClicked()
            self.assertFalse(image._task_controller.active)
            release.set()
            wait_until(lambda: image._preview_controller.state == PreviewState.VALID)
            self.assertTrue(image.start_button.isEnabled())
        image._SetInputPaths([self.root / "missing.png"])
        video._SetInputs([self.root / "missing.mp4"])
        album._SetRootPath(self.root / "missing_album")
        wait_until(lambda: all(page._preview_controller.state == PreviewState.ERROR for page in (image, video, album)))
        for page, start in ((image, image._OnStartClicked), (video, video._Start), (album, album._OnStartClicked)):
            self.assertFalse(page.start_button.isEnabled())
            start()
            self.assertFalse(page._task_controller.active)

    def test_input_text_change_invalidates_ready_preview_immediately(self):
        page = self.page(ImagePage)
        page.SetFfmpegPath("ffmpeg", CAPS, "ready")
        page._SetInputPaths([self.sample("picture.png")])
        wait_until(lambda: page._preview_controller.state == PreviewState.VALID)
        page._OnPathTextEdited(str(self.root / "missing.png"))
        self.assertNotEqual(page._preview_controller.state, PreviewState.VALID)
        self.assertFalse(page.start_button.isEnabled())
        wait_until(lambda: not page._preview_controller.active)

    def test_standalone_page_close_retains_preview_thread(self):
        page = self.page(ImagePage)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        def hold(paths, **kwargs):
            entered.set()
            release.wait(4)
            return []
        with patch("ui.image.page.BuildBatchesForInputs", hold):
            page._SetInputPaths([self.sample("picture.png")])
            wait_until(entered.is_set)
            page.show()
            APP.processEvents()
            page.close()
            self.assertTrue(page.isVisible())
            self.assertTrue(page._preview_controller.active)
            release.set()
            wait_until(lambda: not page._preview_controller.active and not page.isVisible())

    def test_start_rescans_snapshot_and_rejects_deleted_input(self):
        page = self.page(ImagePage)
        source = self.sample("picture.png")
        page.SetFfmpegPath("ffmpeg", CAPS, "ready")
        page._SetInputPaths([source])
        wait_until(lambda: page._preview_controller.state == PreviewState.VALID)
        source.unlink()
        results = []
        page._task_controller.completed.connect(lambda kind, result: results.append(result))
        with patch("app.converter.execute") as execute:
            page._OnStartClicked()
            wait_until(lambda: not page._task_controller.active)
        execute.assert_not_called()
        self.assertEqual(results[0].failed, 1)
        self.assertIn("不存在", results[0].error_logs[0])
        self.assertFalse((self.root / "picture_output").exists())

    def test_running_task_uses_environment_and_shutdown_intent_snapshot(self):
        desktop = Mock()
        desktop.schedule_shutdown.return_value = False
        page = self.page(ImagePage, desktop_actions=desktop)
        source = self.sample("picture.png")
        page.SetFfmpegPath("old_ffmpeg", CAPS, "ready")
        page._SetInputPaths([source])
        page.auto_shutdown_switch.setChecked(True)
        wait_until(lambda: page._preview_controller.state == PreviewState.VALID)
        entered, release = threading.Event(), threading.Event()
        self.releases.append(release)
        commands = []
        def execute(command, **kwargs):
            commands.append(command)
            entered.set()
            release.wait(4)
            Path(command[-1]).write_bytes(b"converted")
            return ProcessResult(0)
        with patch("app.converter.execute", execute):
            page._OnStartClicked()
            wait_until(entered.is_set)
            page.SetFfmpegPath("new_ffmpeg", CAPS, "changed")
            page.auto_shutdown_switch.setChecked(False)
            release.set()
            wait_until(lambda: not page._task_controller.active)
        self.assertEqual(commands[0][0], "old_ffmpeg")
        desktop.schedule_shutdown.assert_called_once()
        self.assertTrue(desktop.schedule_shutdown.call_args.args[1])
        self.assertEqual(source.read_bytes(), b"source")

    def test_log_escape_theme_timestamps_clear_and_utf8_export(self):
        panel = LogPanel()
        self.objects.append(panel)
        with patch("ui.widgets.log_panel.isDarkTheme", return_value=False):
            panel.append(LogLevel.ERROR, '<unsafe> 中文 🐱\n第二行')
        self.assertIn("<unsafe> 中文 🐱", panel.browser.toPlainText())
        before = panel.browser.toPlainText()
        self.assertIn("#c42b1c", panel.browser.toHtml())
        with patch("ui.widgets.log_panel.isDarkTheme", return_value=True):
            qconfig.themeChanged.emit(Theme.DARK)
        self.assertEqual(panel.browser.toPlainText(), before)
        self.assertIn("#ff9aa2", panel.browser.toHtml())
        path = self.root / "日志.txt"
        notice = Mock()
        with patch("ui.widgets.log_panel.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            panel.export_entries(panel, "导出", "log.txt", ["中文 🐱", "second"], notice)
        self.assertEqual(path.read_text(encoding="utf-8"), "中文 🐱\nsecond\n")
        notice.assert_called_once()
        panel.browser.clear()
        qconfig.themeChanged.emit(Theme.LIGHT)
        self.assertEqual(panel.browser.toPlainText(), "")

    def test_task_capabilities_and_progress_total_follow_structured_events(self):
        controller = TaskController()
        self.objects.append(controller)
        panel = TaskPanel(supports_finish=False, show_exports=False)
        self.objects.append(panel)
        panel.update_state(controller, ready=True)
        self.assertTrue(panel.start_button.isEnabled())
        self.assertTrue(panel.end_button.isHidden())
        self.assertTrue(panel.export_error_button.isHidden())
        panel.set_progress(TaskProgress(2, 8, "clip.mp4"), show_file=True)
        self.assertEqual(panel.progress_bar.maximum(), 8)
        self.assertEqual(panel.progress_bar.value(), 2)
        self.assertIn("clip.mp4", panel.status_label.text())
        panel.set_result(TaskResult(total=8, ok=2, cancelled=True))
        self.assertEqual(panel.status_label.text(), "已取消")


if __name__ == "__main__":
    unittest.main()
