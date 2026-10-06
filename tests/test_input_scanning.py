"""后台扫描边界、任务输入快照与启动时重新校验。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtWidgets import QApplication

from app.audio.album_planner import build_album_plan
from app.audio.reader import collect_audio_inputs
from app.ffmpeg_environment import FfmpegCapabilities
from app.image.models import ConvertOptions
from app.image.planner import build_batches_for_inputs, build_image_task
from app.input_paths import ScanCancelled, directory_entries
from app.task_models import ProcessResult
from app.task_runner import TaskRunner
from app.video.planner import build_video_batches_for_inputs
from ui.tasks.jobs import image_inputs_job
from ui.tasks.worker import TaskWorker

APP = QApplication.instance() or QApplication([])
CAPS = FfmpegCapabilities("test", {"png", "mjpeg"}, set())


class InputScanningTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_scanning_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def sample(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"original")
        return path

    def test_image_and_video_duplicate_inputs_are_only_processed_once(self):
        for name, planner in (("image.png", build_batches_for_inputs),
                              ("video.mp4", build_video_batches_for_inputs)):
            path = self.sample(name)
            batches = planner([path, path.parent / "." / path.name])
            self.assertEqual(batches[0].files, [path])
            self.assertEqual(batches[0].output_dir.name, f"{path.stem}_output")

    def test_recursive_scan_checks_cancellation_between_entries(self):
        for index in range(60):
            self.sample(f"images/{index}.png")
        calls = []
        def cancelled():
            calls.append(True)
            return len(calls) >= 12
        with self.assertRaises(ScanCancelled):
            build_batches_for_inputs([self.root / "images"], cancel_check=cancelled)
        self.assertEqual(len(calls), 12)
        self.assertFalse(list(self.root.rglob("*_output")))

    def test_album_and_metadata_scans_accept_immediate_cancellation(self):
        self.sample("albums/one/song.mp3")
        for operation in (lambda: build_album_plan(self.root / "albums", cancel_check=lambda: True),
                          lambda: collect_audio_inputs([self.root], cancel_check=lambda: True)):
            with self.assertRaises(ScanCancelled):
                operation()

    def test_metadata_mixed_inputs_are_filtered_and_deduplicated(self):
        source = self.sample("audio/song.mp3")
        self.sample("audio/.hidden.mp3")
        self.sample("audio/x.part.mp3")
        self.sample("audio/cover.png")
        self.assertEqual(collect_audio_inputs([source, source.parent, source]), [source])

    def test_unreadable_subdirectory_keeps_readable_audio_inputs(self):
        source = self.sample("audio/song.mp3")
        blocked = self.sample("audio/blocked/secret.mp3").parent
        def entries(folder, **kwargs):
            if folder == blocked:
                raise PermissionError("access denied")
            return directory_entries(folder, **kwargs)
        with patch("app.input_paths.directory_entries", side_effect=entries):
            self.assertEqual(collect_audio_inputs([source.parent]), [source])

    def test_start_rescans_directory_and_freezes_input_and_options(self):
        first = self.sample("images/a.png")
        self.assertEqual(len(build_batches_for_inputs([first.parent])[0].files), 1)
        paths = [first.parent]
        options = ConvertOptions("original-ffmpeg", ".jpg")
        worker = TaskWorker(1, image_inputs_job(paths, options, CAPS))
        second = self.sample("images/b.png")
        paths.clear()
        options.ffmpeg_path = "changed-ffmpeg"
        commands = []
        def execute(command, **kwargs):
            commands.append(command)
            Path(command[-1]).write_bytes(b"converted")
            return ProcessResult(0)
        with patch("app.converter.execute", side_effect=execute):
            worker.run()
        self.assertEqual(worker.result.ok, 2)
        self.assertTrue(all(command[0] == "original-ffmpeg" for command in commands))
        self.assertEqual({Path(command[command.index("-i") + 1]) for command in commands},
                         {first, second})
        self.assertEqual(first.read_bytes(), b"original")

    def test_start_rejects_deleted_selected_file_before_output_creation(self):
        source = self.sample("image.png")
        worker = TaskWorker(1, image_inputs_job([source], ConvertOptions("ffmpeg"), CAPS))
        source.unlink()
        with patch("app.converter.execute") as execute:
            worker.run()
            execute.assert_not_called()
        self.assertEqual(worker.result.failed, 1)
        self.assertFalse((self.root / "image_output").exists())

    def test_runner_revalidates_source_after_plan_was_built(self):
        source = self.sample("image.png")
        plan = build_image_task(build_batches_for_inputs([source]), ConvertOptions("ffmpeg"), CAPS)
        source.unlink()
        with patch("app.converter.execute") as execute:
            result = TaskRunner().run(plan)
            execute.assert_not_called()
        self.assertEqual(result.failed, 1)
        self.assertFalse(plan.batches[0].files[0].output_path.parent.exists())

    def test_scan_cancellation_reports_cancelled_without_failure(self):
        worker = TaskWorker(1, lambda context: (_ for _ in ()).throw(ScanCancelled()))
        worker.run()
        self.assertTrue(worker.result.cancelled)
        self.assertEqual(worker.result.failed, 0)
        self.assertFalse(worker.result.error_logs)

    def test_finish_in_last_batch_preserves_stop_intent(self):
        source = self.sample("image.png")
        plan = build_image_task(build_batches_for_inputs([source]), ConvertOptions("ffmpeg"), CAPS)
        runner = TaskRunner()
        def execute(command, **kwargs):
            Path(command[-1]).write_bytes(b"converted")
            runner.request_finish_after_current_batch()
            return ProcessResult(0)
        with patch("app.converter.execute", side_effect=execute):
            result = runner.run(plan)
        self.assertEqual(result.ok, 1)
        self.assertTrue(result.early_stopped)

    def test_cancel_in_final_progress_prevents_completion_shutdown(self):
        source = self.sample("image.png")
        plan = build_image_task(build_batches_for_inputs([source]), ConvertOptions("ffmpeg"), CAPS)
        runner = TaskRunner(on_progress=lambda _progress: runner.request_cancel())
        def execute(command, **kwargs):
            Path(command[-1]).write_bytes(b"converted")
            return ProcessResult(0)
        with patch("app.converter.execute", side_effect=execute):
            result = runner.run(plan)
        self.assertEqual(result.ok, 1)
        self.assertTrue(result.cancelled)


if __name__ == "__main__":
    unittest.main()
