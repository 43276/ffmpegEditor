"""共享配置、环境快照与 Windows 桌面动作的行为检查。"""
from __future__ import annotations

import os
import subprocess
import tempfile
import threading
import time
import unittest
from dataclasses import FrozenInstanceError
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QSettings
from PyQt6.QtWidgets import QApplication

from app.ffmpeg_environment import FfmpegCapabilities
from app.task_models import TaskResult
from ui.services.desktop_actions import DesktopActions
from ui.services.ffmpeg_service import FfmpegService
from ui.services.settings_service import SettingsService


APP = QApplication.instance() or QApplication([])
CAPS = FfmpegCapabilities("test-version", frozenset({"mjpeg"}), frozenset({"image2"}))


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        time.sleep(0.005)
    raise AssertionError("后台服务未按预期完成")


class ServicesStructureTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings_path = str(self.root / "settings.ini")
        self.settings = SettingsService(QSettings(self.settings_path, QSettings.Format.IniFormat))

    def tearDown(self):
        self.temp.cleanup()

    def test_configuration_preserves_organization_and_existing_keys(self):
        self.assertEqual((SettingsService.ORGANIZATION, SettingsService.APPLICATION),
                         ("CompressImages", "ImageConverter"))
        for key, value in (("ffmpeg/path", "中文工具/ffmpeg.exe"),
                           ("task/auto_shutdown", True), ("background/blur", 18),
                           ("video/quality", 24)):
            self.settings.setValue(key, value)
        self.settings.sync()
        restored = SettingsService(QSettings(self.settings_path, QSettings.Format.IniFormat))
        self.assertEqual(restored.value("ffmpeg/path", "", type=str), "中文工具/ffmpeg.exe")
        self.assertTrue(restored.value("task/auto_shutdown", False, type=bool))
        self.assertEqual(restored.value("background/blur", 0, type=int), 18)
        self.assertEqual(restored.value("video/quality", 0, type=int), 24)

    def test_latest_environment_replaces_pending_detection_and_old_snapshot_is_immutable(self):
        service = FfmpegService(self.settings)
        release = threading.Event()
        entered = threading.Event()
        changes = []
        legacy = []
        service.environmentChanged.connect(changes.append)
        service.stateChanged.connect(lambda path, caps, message: legacy.append((path, caps, message)))

        def probe(path, cancel_check):
            if path == "first":
                entered.set()
                release.wait(3)
            return CAPS

        try:
            with patch("ui.services.ffmpeg_service.locate_ffmpeg", side_effect=lambda path: path), \
                    patch("ui.services.ffmpeg_service.locate_ffprobe", side_effect=lambda path: path + "-probe"), \
                    patch("ui.services.ffmpeg_service.probe_ffmpeg", side_effect=probe):
                service.SetPath("first")
                wait_until(entered.is_set)
                service.SetPath("second")
                second_check = service.snapshot
                service.SetPath("third")
                release.set()
                wait_until(lambda: not service.active)
            ready = [snapshot for snapshot in changes if snapshot.status == "ready"]
            self.assertEqual([snapshot.ffmpeg_path for snapshot in ready], ["third"])
            self.assertEqual(service.snapshot.ffprobe_path, "third-probe")
            self.assertEqual(service.snapshot.capabilities, CAPS)
            self.assertEqual(service.snapshot.request_id, 3)
            self.assertEqual(second_check.request_id, 2)
            self.assertEqual(second_check.status, "checking")
            with self.assertRaises(FrozenInstanceError):
                second_check.ffmpeg_path = "changed"
            self.assertEqual(legacy[-1][:2], ("third", CAPS))
            self.assertEqual(self.settings.value("ffmpeg/path", "", type=str), "third")
        finally:
            release.set()
            service.Shutdown()
            wait_until(lambda: not service.active)

    def test_missing_ffprobe_disables_audio_reading_without_losing_ffmpeg_capabilities(self):
        service = FfmpegService(self.settings)
        try:
            with patch("ui.services.ffmpeg_service.locate_ffmpeg", return_value="ffmpeg"), \
                    patch("ui.services.ffmpeg_service.probe_ffmpeg", return_value=CAPS), \
                    patch("ui.services.ffmpeg_service.locate_ffprobe", side_effect=RuntimeError("missing ffprobe")):
                service.Start()
                wait_until(lambda: not service.active)
            self.assertEqual(service.snapshot.status, "ready")
            self.assertEqual(service.snapshot.ffmpeg_path, "ffmpeg")
            self.assertIsNone(service.snapshot.ffprobe_path)
            self.assertEqual(service.snapshot.error, "missing ffprobe")
            self.assertIn("音频读取不可用", service.snapshot.message)
        finally:
            service.Shutdown()

    def test_failed_explicit_path_clears_paths_and_reports_detection_error(self):
        service = FfmpegService(self.settings)
        with patch("ui.services.ffmpeg_service.locate_ffmpeg", side_effect=RuntimeError("invalid path")), \
                patch("ui.services.ffmpeg_service.locate_ffprobe") as ffprobe:
            service.SetPath("invalid")
            wait_until(lambda: not service.active)
        self.assertEqual(service.snapshot.status, "error")
        self.assertEqual(service.snapshot.error, "invalid path")
        self.assertIsNone(service.snapshot.ffmpeg_path)
        self.assertIsNone(service.snapshot.ffprobe_path)
        ffprobe.assert_not_called()
        service.Shutdown()

    def test_shutdown_obeys_user_intent_and_task_stop_conditions(self):
        actions = DesktopActions()
        with patch("ui.services.desktop_actions.sys.platform", "win32"), \
                patch("ui.services.desktop_actions.subprocess.run", return_value=SimpleNamespace(returncode=0)) as run:
            self.assertFalse(actions.schedule_shutdown(TaskResult(), False))
            self.assertFalse(actions.schedule_shutdown(TaskResult(cancelled=True), True))
            self.assertFalse(actions.schedule_shutdown(TaskResult(early_stopped=True), True))
            run.assert_not_called()
            self.assertTrue(actions.schedule_shutdown(TaskResult(ok=1), True))
            self.assertEqual(run.call_args.args[0], ["shutdown", "/s", "/t", "60"])
            self.assertFalse(run.call_args.kwargs["check"])
            self.assertEqual(run.call_args.kwargs["timeout"], 10)
            if os.name == "nt":
                self.assertNotEqual(run.call_args.kwargs["creationflags"], 0)

    def test_output_directory_requires_existing_directory_and_windows(self):
        actions = DesktopActions()
        with patch("ui.services.desktop_actions.sys.platform", "win32"), \
                patch("ui.services.desktop_actions.os.startfile", create=True) as startfile:
            self.assertFalse(actions.open_output_directory(self.root / "missing"))
            startfile.assert_not_called()
            self.assertTrue(actions.open_output_directory(self.root))
            startfile.assert_called_once_with(str(self.root))
        with patch("ui.services.desktop_actions.sys.platform", "linux"):
            self.assertFalse(actions.open_output_directory(self.root))

    def test_shutdown_timeout_and_nonzero_exit_become_page_reportable_errors(self):
        actions = DesktopActions()
        with patch("ui.services.desktop_actions.sys.platform", "win32"), \
                patch("ui.services.desktop_actions.subprocess.run",
                      side_effect=subprocess.TimeoutExpired("shutdown", 10)):
            with self.assertRaisesRegex(OSError, "超时"):
                actions.schedule_shutdown(TaskResult(), True)
        with patch("ui.services.desktop_actions.sys.platform", "win32"), \
                patch("ui.services.desktop_actions.subprocess.run", return_value=SimpleNamespace(returncode=5)):
            with self.assertRaisesRegex(OSError, "5"):
                actions.schedule_shutdown(TaskResult(), True)


if __name__ == "__main__":
    unittest.main()
