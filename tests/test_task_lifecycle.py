"""第三阶段：真实 Qt 线程结束、过期事件、取消重启与异步退出。"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings, QTimer, Qt
from PyQt6 import sip
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication, QDialog

from app.audio.models import AudioInfo, TrackEdit
from app.ffmpeg_environment import FfmpegCapabilities, probe_ffmpeg
from app.task_models import LogEvent, LogLevel, ProcessResult, TaskProgress, TaskResult, TaskStatistics
from ui.AlbumPage import AlbumPage
from ui.ImagePage import ImagePage
from ui.MainWindow import FallbackMainWindow, MainWindow
from ui.MetadataPage import MetadataPage, _CoverDialog
from ui.tasks.controller import TaskController, TaskState
from ui.tasks.jobs import cover_export_job
from ui.tasks.worker import TaskWorker


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


def delayed_worker(release):
    class DelayedWorker(TaskWorker):
        def run(self):
            super().run()
            # 模拟已经发送业务结果、QThread 仍在做最后清理的时间窗。
            release.wait(4)
    return DelayedWorker


class HeldProcess:
    def __init__(self):
        self.entered = threading.Event()
        self.cancel_seen = threading.Event()
        self.release = threading.Event()

    def __call__(self, command, cancel_check, **kwargs):
        Path(command[-1]).write_bytes(b"partial")
        self.entered.set()
        while not cancel_check() and not self.release.wait(0.005):
            pass
        if cancel_check():
            self.cancel_seen.set()
            self.release.wait(4)
        return ProcessResult(-1 if cancel_check() else 0, cancelled=cancel_check())


class TaskLifecycleTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_lifecycle_")
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "settings.ini"), QSettings.Format.IniFormat)

    def tearDown(self):
        APP.processEvents()
        self.temp.cleanup()

    def sample(self, name, data=b"original"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def stop_controller(self, controller, release):
        release.set()
        controller.shutdown()
        wait_until(lambda: not controller.active)

    def test_result_retains_worker_and_disallows_restart_until_thread_finished(self):
        release = threading.Event()
        controller = TaskController()
        completed, results = [], []
        controller.completed.connect(lambda kind, result: completed.append(result))
        controller.resultReady.connect(results.append)
        try:
            with patch("ui.tasks.controller.TaskWorker", delayed_worker(release)):
                self.assertTrue(controller.start(lambda context: TaskResult(total=1, ok=1)))
            worker = controller.worker
            wait_until(lambda: bool(results))
            self.assertEqual(controller.state, TaskState.FINALIZING)
            self.assertIs(controller.worker, worker)
            self.assertTrue(worker.isRunning())
            self.assertFalse(controller.can_cancel)
            self.assertFalse(controller.start(lambda context: TaskResult()))
            self.assertFalse(completed)
            release.set()
            wait_until(lambda: not controller.active)
            self.assertEqual(completed[0].ok, 1)
            old_id = controller.task_id
            self.assertTrue(controller.start(lambda context: TaskResult()))
            self.assertGreater(controller.task_id, old_id)
            wait_until(lambda: not controller.active)
        finally:
            self.stop_controller(controller, release)

    def test_all_old_task_events_are_ignored_after_restart(self):
        release = threading.Event()
        controller = TaskController()
        logs, progress, statistics, rows, results = [], [], [], [], []
        controller.logMessage.connect(logs.append)
        controller.progressChanged.connect(progress.append)
        controller.statisticsChanged.connect(statistics.append)
        controller.rowLoaded.connect(rows.append)
        controller.resultReady.connect(results.append)
        try:
            controller.start(lambda context: TaskResult())
            wait_until(lambda: not controller.active)
            old_id = controller.task_id
            results.clear()

            def hold(context):
                release.wait(4)
                return TaskResult()

            controller.start(hold)
            worker = controller.worker
            worker.logMessage.emit(old_id, LogEvent(LogLevel.ERROR, "old"))
            worker.progressChanged.emit(old_id, TaskProgress(99, 99, "old"))
            worker.statisticsChanged.emit(old_id, TaskStatistics(99, 0, 99, 0, 99))
            worker.rowLoaded.emit(old_id, "old")
            worker.taskFinished.emit(old_id, TaskResult(failed=99))
            worker.logMessage.emit(controller.task_id, LogEvent(LogLevel.INFO, "current"))
            wait_until(lambda: bool(logs))
            self.assertEqual([event.message for event in logs], ["current"])
            self.assertFalse(progress or statistics or rows or results)
            self.assertEqual(controller.state, TaskState.RUNNING)
        finally:
            self.stop_controller(controller, release)

    def test_cancel_during_planning_reaches_runner_before_any_output(self):
        release, entered = threading.Event(), threading.Event()
        source = self.sample("track.mp3")
        controller = TaskController()
        operation = cover_export_job("ffmpeg", [(source, "mjpeg")], str(self.root / "covers"))
        results = []
        controller.completed.connect(lambda kind, result: results.append(result))

        def planning(context):
            entered.set()
            release.wait(4)
            return operation(context)

        try:
            with patch("app.converter.execute") as execute:
                controller.start(planning, total=1, supports_finish=True)
                wait_until(entered.is_set)
                self.assertTrue(controller.request_finish_after_current_batch())
                self.assertTrue(controller.can_cancel)
                self.assertTrue(controller.request_cancel())
                self.assertFalse(controller.request_finish_after_current_batch())
                release.set()
                wait_until(lambda: not controller.active)
                execute.assert_not_called()
            self.assertTrue(results[0].cancelled)
            self.assertFalse(results[0].early_stopped)
            self.assertFalse((self.root / "covers").exists())
        finally:
            self.stop_controller(controller, release)

    def test_unexpected_read_failure_reports_result_and_releases_worker(self):
        controller = TaskController()
        results = []
        controller.completed.connect(lambda kind, result: results.append(result))

        def fail(context):
            raise RuntimeError("read failed")

        controller.start(fail, total=2, kind="read")
        self.assertFalse(controller.can_finish)
        wait_until(lambda: not controller.active)
        self.assertEqual((results[0].total, results[0].failed), (2, 1))
        self.assertIn("read failed", results[0].error_logs[0])
        self.assertTrue(controller.start(lambda context: TaskResult()))
        wait_until(lambda: not controller.active)

    def test_chained_read_does_not_emit_idle_while_new_task_is_running(self):
        release, entered = threading.Event(), threading.Event()
        controller = TaskController()
        idle = []
        controller.idle.connect(lambda: idle.append(True))

        def read(context):
            entered.set()
            release.wait(4)
            return TaskResult()

        def completed(kind, result):
            if kind == "write":
                controller.start(read, kind="read")

        controller.completed.connect(completed)
        try:
            controller.start(lambda context: TaskResult(), kind="write")
            wait_until(entered.is_set)
            self.assertEqual(controller.kind, "read")
            self.assertFalse(idle)
            release.set()
            wait_until(lambda: not controller.active)
            self.assertEqual(idle, [True])
        finally:
            self.stop_controller(controller, release)

    def test_image_and_album_restore_buttons_and_shutdown_only_after_thread_exit(self):
        for page_type in (ImagePage, AlbumPage):
            with self.subTest(page=page_type.__name__):
                release = threading.Event()
                with patch("ui.ImagePage.QSettings", return_value=self.settings):
                    page = page_type()
                if page_type is ImagePage:
                    page.SetFfmpegPath("ffmpeg", CAPS, "test")
                    page._SetInputPaths([self.sample("image.png")])
                    page.auto_shutdown_switch.setChecked(True)
                else:
                    self.sample("albums/Album/cover.jpg")
                    self.sample("albums/Album/album.txt", b"Album")
                    self.sample("albums/Album/artist.txt", b"Artist")
                    self.sample("albums/Album/track.mp3")
                    page.SetFfmpegPath("ffmpeg")
                    page._SetRootPath(self.root / "albums")
                try:
                    def process(command, **kwargs):
                        Path(command[-1]).write_bytes(b"converted")
                        return ProcessResult(0)

                    with patch("ui.tasks.controller.TaskWorker", delayed_worker(release)), \
                            patch("app.converter.execute", side_effect=process), \
                            patch.object(page, "_ShowInfoBar"), \
                            patch.object(ImagePage, "_ScheduleShutdown") as shutdown:
                        page._OnStartClicked()
                        wait_until(lambda: page._task_controller.state == TaskState.FINALIZING)
                        self.assertFalse(page.start_button.isEnabled())
                        self.assertFalse(page.cancel_button.isEnabled())
                        self.assertFalse(page.end_button.isEnabled())
                        shutdown.assert_not_called()
                        old_id = page._task_controller.task_id
                        page._OnStartClicked()
                        self.assertEqual(page._task_controller.task_id, old_id)
                        release.set()
                        wait_until(lambda: not page._task_controller.active)
                        self.assertTrue(page.start_button.isEnabled())
                        self.assertTrue(page.open_folder_button.isEnabled())
                        if page_type is ImagePage:
                            shutdown.assert_called_once()
                finally:
                    self.stop_controller(page._task_controller, release)
                    page.Shutdown()
                    page.deleteLater()

    def test_metadata_read_keeps_import_and_write_disabled_until_thread_exit(self):
        source = self.sample("track.mp3")
        page = MetadataPage()
        page._ffmpeg_path, page._ffprobe_path = "ffmpeg", "ffprobe"
        release = threading.Event()
        try:
            with patch("ui.tasks.controller.TaskWorker", delayed_worker(release)), \
                    patch("app.audio.reader.read_audio_info", return_value=AudioInfo(source, {}, False, None)), \
                    patch.object(page, "_ShowInfoBar"):
                page._StartImport([source])
                wait_until(lambda: page._task_controller.state == TaskState.FINALIZING)
                page._rows[0].edited_values["title"] = "new"
                page._UpdateControls()
                self.assertFalse(page.import_files_button.isEnabled())
                self.assertFalse(page.confirm_button.isEnabled())
                self.assertFalse(page.table.isEnabled())
                old_id = page._task_controller.task_id
                page._StartImport([source])
                self.assertEqual(page._task_controller.task_id, old_id)
                release.set()
                wait_until(lambda: not page._task_controller.active)
                self.assertTrue(page.import_files_button.isEnabled())
                self.assertTrue(page.confirm_button.isEnabled())
                self.assertTrue(page.table.isEnabled())
        finally:
            self.stop_controller(page._task_controller, release)
            page.Shutdown()
            page.deleteLater()

    def test_metadata_read_can_cancel_and_restart_without_old_rows(self):
        page = MetadataPage()
        page._ffmpeg_path, page._ffprobe_path = "ffmpeg", "ffprobe"
        source = self.sample("track.mp3")
        entered = threading.Event()
        try:
            def process(command, cancel_check, **kwargs):
                entered.set()
                while not cancel_check():
                    time.sleep(0.005)
                return ProcessResult(-1, cancelled=True)

            with patch("app.converter.execute", side_effect=process), patch.object(page, "_ShowInfoBar"):
                page._StartImport([source])
                wait_until(entered.is_set)
                self.assertTrue(page.cancel_button.isEnabled())
                page._OnCancelClicked()
                page._UpdateControls()
                self.assertFalse(page.cancel_button.isEnabled())
                wait_until(lambda: not page._task_controller.active)
                self.assertFalse(page._rows)
                self.assertIn("已取消", page.status_label.text())
            with patch("app.audio.reader.read_audio_info", return_value=AudioInfo(source, {"title": "新曲😀"}, False, None)), \
                    patch.object(page, "_ShowInfoBar"):
                page._StartImport([source])
                wait_until(lambda: not page._task_controller.active)
                self.assertEqual(len(page._rows), 1)
                self.assertEqual(page._rows[0].original_values["title"], "新曲😀")
        finally:
            page.Shutdown()
            wait_until(lambda: not page._task_controller.active)
            page.deleteLater()

    def test_cover_dialog_all_close_paths_wait_for_cancellation_and_real_exit(self):
        for action in ("reject", "close", "accept", "escape"):
            with self.subTest(action=action):
                source = self.sample(f"{action}.mp3")
                dialog = _CoverDialog(None, "all", "ffmpeg", [(source, "mjpeg")], self.root)
                process = HeldProcess()
                finished = []
                dialog.finished.connect(finished.append)
                dialog.show()
                try:
                    with patch("ui.MetadataPage.QFileDialog.getExistingDirectory", return_value=str(self.root / action)), \
                            patch("app.converter.execute", side_effect=process):
                        dialog._OnExportAll()
                        wait_until(process.entered.is_set)
                        started = time.monotonic()
                        if action == "escape":
                            QTest.keyClick(dialog, Qt.Key.Key_Escape)
                        else:
                            getattr(dialog, action)()
                        self.assertLess(time.monotonic() - started, 1)
                        wait_until(process.cancel_seen.is_set)
                        self.assertTrue(dialog.active)
                        self.assertTrue(dialog.isVisible())
                        self.assertFalse(finished)
                        process.release.set()
                        wait_until(lambda: not dialog.active and bool(finished))
                    expected = QDialog.DialogCode.Accepted if action == "accept" else QDialog.DialogCode.Rejected
                    self.assertEqual(finished, [expected])
                    self.assertFalse(dialog.isVisible())
                    self.assertFalse(list((self.root / action).glob("*.part.*")))
                    self.assertEqual(source.read_bytes(), b"original")
                finally:
                    self.stop_controller(dialog._export_controller, process.release)
                    dialog.reject()
                    dialog.deleteLater()

    def test_cover_export_result_retains_thread_and_allows_another_export_after_exit(self):
        source = self.sample("track.mp3")
        dialog = _CoverDialog(None, "all", "ffmpeg", [(source, "mjpeg")], self.root)
        release = threading.Event()
        destination = self.root / "covers"
        try:
            def process(command, **kwargs):
                Path(command[-1]).write_bytes(b"cover")
                return ProcessResult(0)

            with patch("ui.MetadataPage.QFileDialog.getExistingDirectory", return_value=str(destination)), \
                    patch("ui.tasks.controller.TaskWorker", delayed_worker(release)), \
                    patch("app.converter.execute", side_effect=process):
                dialog._OnExportAll()
                wait_until(lambda: dialog._export_controller.state == TaskState.FINALIZING)
                self.assertTrue(dialog.active)
                self.assertTrue(all(not button.isEnabled() for button in dialog._buttons))
                old_id = dialog._export_controller.task_id
                dialog._OnExportAll()
                self.assertEqual(dialog._export_controller.task_id, old_id)
                release.set()
                wait_until(lambda: not dialog.active)
                self.assertTrue(all(button.isEnabled() for button in dialog._buttons))
                dialog._OnExportAll()
                wait_until(lambda: not dialog.active)
                self.assertGreater(dialog._export_controller.task_id, old_id)
            self.assertEqual(len(list(destination.glob("*.jpg"))), 2)
        finally:
            self.stop_controller(dialog._export_controller, release)
            dialog.reject()
            dialog.deleteLater()

    def test_metadata_write_shutdown_preserves_temp_cover_until_thread_exit(self):
        page = MetadataPage()
        page._ffmpeg_path = "ffmpeg"
        source = self.sample("track.mp3")
        cover = page._cover_temp_path / "pasted.png"
        cover.write_bytes(b"cover")
        page._rows = [TrackEdit(source, edited_values={"title": "new"}, cover_action="set", cover_source=cover)]
        process = HeldProcess()
        try:
            with patch("app.converter.execute", side_effect=process), patch.object(page, "_ShowInfoBar"):
                page._OnConfirmClicked()
                wait_until(process.entered.is_set)
                page.Shutdown()
                wait_until(process.cancel_seen.is_set)
                self.assertTrue(page._task_controller.active)
                self.assertTrue(cover.exists())
                process.release.set()
                wait_until(lambda: not page._task_controller.active)
            self.assertFalse(page._cover_temp_path.exists())
            self.assertEqual(source.read_bytes(), b"original")
            self.assertFalse(list(self.root.rglob("*.part.*")))
        finally:
            self.stop_controller(page._task_controller, process.release)
            page.Shutdown()
            page.deleteLater()

    def test_modal_cover_dialog_defers_deletion_after_result_until_thread_exit(self):
        source = self.sample("track.mp3")
        page = MetadataPage()
        dialog = _CoverDialog(page, "all", "ffmpeg", [(source, "mjpeg")], page._cover_temp_path)
        dialog.setAttribute(Qt.WidgetAttribute.WA_DeleteOnClose)
        controller = dialog._export_controller
        release = threading.Event()
        snapshots = []

        def process(command, **kwargs):
            Path(command[-1]).write_bytes(b"cover")
            return ProcessResult(0)

        def close_after_result():
            if controller.state != TaskState.FINALIZING:
                QTimer.singleShot(5, close_after_result)
                return
            dialog.close()
            snapshots.append((controller.active, controller.worker.isRunning(), sip.isdeleted(dialog)))
            QTimer.singleShot(20, release.set)

        try:
            with patch("ui.MetadataPage.QFileDialog.getExistingDirectory", return_value=str(self.root / "covers")), \
                    patch("ui.tasks.controller.TaskWorker", delayed_worker(release)), \
                    patch("app.converter.execute", side_effect=process):
                QTimer.singleShot(0, dialog._OnExportAll)
                QTimer.singleShot(5, close_after_result)
                self.assertEqual(dialog.exec(), QDialog.DialogCode.Rejected)
            self.assertEqual(snapshots, [(True, True, False)])
            self.assertFalse(controller.active)
            wait_until(lambda: sip.isdeleted(dialog))
        finally:
            release.set()
            wait_until(lambda: not controller.active)
            page.Shutdown()
            page.deleteLater()

    def test_metadata_shutdown_preserves_temp_directory_until_export_dialog_stops(self):
        page = MetadataPage()
        source = self.sample("track.mp3")
        cover = page._cover_temp_path / "pasted.png"
        cover.write_bytes(b"cover")
        dialog = _CoverDialog(page, "all", "ffmpeg", [(source, "mjpeg")], page._cover_temp_path)
        dialog.show()
        process = HeldProcess()
        try:
            with patch("ui.MetadataPage.QFileDialog.getExistingDirectory", return_value=str(self.root / "covers")), \
                    patch("app.converter.execute", side_effect=process):
                dialog._OnExportAll()
                wait_until(process.entered.is_set)
                page.Shutdown()
                wait_until(process.cancel_seen.is_set)
                self.assertTrue(cover.exists())
                self.assertTrue(dialog.active)
                process.release.set()
                wait_until(lambda: not dialog.active)
            self.assertFalse(page._cover_temp_path.exists())
            self.assertFalse(dialog.isVisible())
        finally:
            self.stop_controller(dialog._export_controller, process.release)
            page.Shutdown()
            page.deleteLater()

    def test_both_main_windows_request_every_task_and_probe_then_close_without_blocking(self):
        for window_type in (FallbackMainWindow, MainWindow):
            with self.subTest(window=window_type.__name__):
                releases, entered, cancelled = [], [], []

                def operation():
                    release, entry, cancel = threading.Event(), threading.Event(), threading.Event()
                    releases.append(release)
                    entered.append(entry)
                    cancelled.append(cancel)

                    def run(context):
                        entry.set()
                        while not context.cancel_check() and not release.wait(0.005):
                            pass
                        if context.cancel_check():
                            cancel.set()
                            release.wait(4)
                        return TaskResult(cancelled=context.cancel_check())
                    return run

                with patch("ui.MainWindow.QSettings", return_value=self.settings), \
                        patch("ui.ImagePage.QSettings", return_value=self.settings), \
                        patch("ui.VideoPage.QSettings", return_value=self.settings), \
                        patch("ui.FfmpegService.FfmpegService.Start"):
                    window = window_type()
                    window.show()
                    APP.processEvents()
                    page = window.audio_page.metadata_page
                    dialog = _CoverDialog(page, "all", "ffmpeg", [], page._cover_temp_path)
                    dialog.show()
                    controllers = window.findChildren(TaskController)
                    for controller in controllers:
                        controller.start(operation())
                    probe_operation = operation()

                    def probe(path, cancel_check):
                        class Context:
                            pass
                        context = Context()
                        context.cancel_check = cancel_check
                        probe_operation(context)
                        return CAPS

                    try:
                        with patch("ui.FfmpegService.locate_ffmpeg", return_value="ffmpeg"), \
                                patch("ui.FfmpegService.probe_ffmpeg", side_effect=probe):
                            window._ffmpeg_service.SetPath("ffmpeg")
                            wait_until(lambda: all(event.is_set() for event in entered))
                            tick = []
                            QTimer.singleShot(0, lambda: tick.append(True))
                            started = time.monotonic()
                            window.close()
                            self.assertLess(time.monotonic() - started, 1)
                            wait_until(lambda: all(event.is_set() for event in cancelled))
                            self.assertTrue(tick)
                            self.assertTrue(window.isVisible())
                            self.assertTrue(page._cover_temp_path.exists())
                            # 其他任务先结束，窗口继续等待仍在清理的探测线程。
                            for release in releases[:-1]:
                                release.set()
                            wait_until(lambda: all(not controller.active for controller in controllers))
                            self.assertTrue(window.isVisible())
                            self.assertTrue(window._ffmpeg_service.active)
                            releases[-1].set()
                            wait_until(lambda: not window.isVisible())
                        self.assertFalse(window._ffmpeg_service.active)
                        self.assertFalse(page._cover_temp_path.exists())
                    finally:
                        for release in releases:
                            release.set()
                        window.close()
                        wait_until(lambda: not window._ffmpeg_service.active
                                   and all(not controller.active for controller in controllers))
                        window.deleteLater()

    def test_cancellable_probe_stops_before_following_capability_commands(self):
        cancelled = threading.Event()
        calls = []

        def process(command, cancel_check, **kwargs):
            calls.append(command)
            cancelled.set()
            return ProcessResult(-1, cancelled=True)

        with patch("app.converter.execute", side_effect=process):
            with self.assertRaisesRegex(Exception, "已取消"):
                probe_ffmpeg("ffmpeg", cancel_check=cancelled.is_set)
        self.assertEqual(len(calls), 1)


if __name__ == "__main__":
    unittest.main()
