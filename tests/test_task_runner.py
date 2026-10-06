"""第二阶段：业务计划与不依赖 Qt 的执行流程。"""
from __future__ import annotations

import ast
import json
import os
import subprocess
import sys
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.audio.album_planner import build_album_plan, build_album_task, is_album_audio_file
from app.audio.cover_planner import build_cover_export_task
from app.audio.metadata_planner import MetadataError, apply_cover_edit, build_metadata_task, build_output_plan
from app.audio.models import TrackEdit
from app.audio.reader import collect_audio_files, read_audio_edits, read_audio_info
from app.converter import execute
from app.ffmpeg_environment import FfmpegCapabilities
from app.image.commands import build_image_command
from app.image.models import Batch, ConvertOptions
from app.image.planner import build_image_task
from app.image.planner import build_batches_for_inputs
from app.output_files import output_transaction
from app.task_models import BatchPlan, ProcessResult, ProcessStep, TaskPlan
from app.task_runner import TaskRunner
from app.video.models import VideoBatch, VideoCompressOptions
from app.video.planner import build_video_task


CAPS = FfmpegCapabilities("test", {"libx264", "libwebp", "libaom-av1", "gif"}, {"mp4", "avif", "gif"})


class TaskRunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_task_runner_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def sample(self, name, data=b"original"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    @staticmethod
    def process(command, **kwargs):
        Path(command[-1]).write_bytes(b"converted")
        return ProcessResult(0)

    def cover_edit(self):
        return TrackEdit(self.sample("source.mp3"), has_original_cover=True,
                         cover_codec="png", target_format_key="flac",
                         edited_values={"title": "歌曲🎵"})

    def plan(self, edit=None):
        return build_output_plan("ffmpeg", edit or self.cover_edit(), True, False, self.root / "export")

    def run_file(self, plan, process=None, logs=None):
        return TaskRunner(execute=process or self.process, on_log=(logs.append if logs is not None else None)).run(
            TaskPlan((BatchPlan("test", (plan,)),)))

    def assert_no_temporary(self):
        self.assertFalse(list(self.root.rglob("*.part.*")))

    def test_all_four_planners_return_read_only_complete_plans_and_run_without_widgets(self):
        picture = self.sample("image.png")
        video = self.sample("video.mkv")
        audio = self.sample("albums/album/disc/song.mp3")
        self.sample("albums/album/cover.jpg")
        self.sample("albums/album/album.txt", b"Album")
        self.sample("albums/album/artist.txt", b"Alice Bob")
        before = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
        destination = self.root / "export"
        plans = [
            build_image_task([Batch(self.root, destination / "images", [picture])], ConvertOptions("ffmpeg", ".webp"), CAPS),
            build_video_task([VideoBatch(self.root, destination / "videos", [video])], VideoCompressOptions("ffmpeg"), CAPS),
            build_album_task(build_album_plan(self.root / "albums"), "ffmpeg", True),
            build_metadata_task("ffmpeg", [TrackEdit(audio, edited_values={"title": "new"})], True, False, destination / "audio"),
        ]
        self.assertFalse(destination.exists())
        self.assertEqual(before, {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()})
        for plan in plans:
            file_plan = plan.batches[0].files[0]
            self.assertEqual(file_plan.command[-1], str(file_plan.temp_path))
            result = TaskRunner(execute=self.process).run(plan)
            self.assertEqual((result.ok, result.failed), (1, 0))
        self.assertTrue(all(path.read_bytes() == data for path, data in before.items()))
        self.assert_no_temporary()

    def test_public_app_modules_import_when_qt_and_ui_are_unavailable(self):
        script = """
import importlib.abc, sys
class DenyQt(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname.split('.')[0] in {'PyQt6', 'qfluentwidgets', 'ui'}:
            raise ImportError('Forbidden dependency: ' + fullname)
sys.meta_path.insert(0, DenyQt())
from app import converter, task_runner
from app.image import commands, planner
from app.video import commands, planner
from app.audio import reader, album_planner, metadata_planner, cover_planner
"""
        process = subprocess.run([sys.executable, "-c", script], capture_output=True,
                                 timeout=10, cwd=Path(__file__).resolve().parents[1],
                                 creationflags=subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)
        self.assertEqual(process.returncode, 0, process.stderr.decode("utf-8", errors="replace"))

    def test_converter_and_runner_import_only_common_app_mechanisms(self):
        root = Path(__file__).resolve().parents[1] / "app"
        for name in ("converter.py", "task_runner.py"):
            tree = ast.parse((root / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.ImportFrom):
                    self.assertFalse(any(domain in (node.module or "").split(".") for domain in ("image", "video", "audio", "ui")))

    def test_tif_alias_and_encoder_specific_commands_include_explicit_output(self):
        source = self.root / "source.tif"
        for extension, encoder in ((None, "tiff"), (".tiff", "tiff"), (".avif", "libaom-av1"), (".gif", "gif")):
            output = self.root / f"result{extension or '.tif'}"
            command = build_image_command(ConvertOptions("ffmpeg", extension), CAPS, source, output)
            self.assertEqual(command[-1], str(output))
            self.assertEqual(command[command.index("-c:v") + 1], encoder)
            if extension == ".avif":
                self.assertIn("-cpu-used", command)
                self.assertIn("-row-mt", command)

    def test_image_task_materializes_preview_time_name_without_mutating_preview(self):
        sources = [self.sample("one.png"), self.sample("two.png")]
        batches = build_batches_for_inputs(sources)
        self.assertEqual(batches[0].output_dir.name, "<任务开始时间>")
        with patch("app.image.planner.make_task_output_name", return_value="2026-10-06_12-30-45"):
            plan = build_image_task(batches, ConvertOptions("ffmpeg"), CAPS)
        self.assertEqual(plan.batches[0].files[0].output_path.parent.name, "2026-10-06_12-30-45")
        self.assertEqual(batches[0].output_dir.name, "<任务开始时间>")
        self.assertFalse(plan.batches[0].files[0].output_path.parent.exists())

    def test_existing_image_output_skips_before_unsupported_encoder_failure(self):
        source = self.sample("song.png")
        existing = self.sample("export/song.webp", b"keep")
        plan = build_image_task([Batch(self.root, existing.parent, [source])],
                                ConvertOptions("ffmpeg", ".webp", overwrite=False),
                                FfmpegCapabilities("test", set(), set()))
        result = TaskRunner(execute=lambda *_args, **_kwargs: self.fail("Skipped plan ran")).run(plan)
        self.assertEqual((result.ok, result.failed, result.skipped), (0, 0, 1))
        self.assertEqual(result.output_dirs, [str(existing.parent)])
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_album_finish_request_completes_all_directories_in_current_album(self):
        for album in ("A", "B"):
            for disc in ("Disc1", "Disc2"):
                self.sample(f"albums/{album}/{disc}/song.mp3")
            self.sample(f"albums/{album}/cover.jpg")
            self.sample(f"albums/{album}/album.txt", b"Album")
            self.sample(f"albums/{album}/artist.txt", b"Artist")
        plan = build_album_task(build_album_plan(self.root / "albums"), "ffmpeg", True)
        calls, progress, statistics = [], [], []
        runner = TaskRunner(on_progress=progress.append, on_statistics=statistics.append)
        def process(command, **kwargs):
            calls.append(command)
            runner.request_finish_after_current_batch()
            return self.process(command)
        runner._execute = process
        result = runner.run(plan)
        self.assertEqual(len(calls), 2)
        self.assertEqual((result.total, result.ok, result.failed), (4, 2, 0))
        self.assertTrue(result.early_stopped)
        self.assertFalse(result.cancelled)
        self.assertEqual([item.done for item in progress], [1, 2])
        self.assertEqual(statistics[-1].done, 2)
        self.assertTrue(all(not file.output_path.parent.exists() for file in plan.batches[1].files))
        self.assert_no_temporary()

    def test_cancel_before_start_takes_priority_over_finish_and_creates_nothing(self):
        plan = self.plan()
        runner = TaskRunner(execute=lambda *_args, **_kwargs: self.fail("Cancelled task executed"))
        runner.request_finish_after_current_batch()
        runner.request_cancel()
        result = runner.run(TaskPlan((BatchPlan("test", (plan,)),)))
        self.assertTrue(result.cancelled)
        self.assertFalse(result.early_stopped)
        self.assertEqual((result.ok, result.failed, result.skipped), (0, 0, 0))
        self.assertFalse(plan.output_path.parent.exists())

    def test_preserving_cover_is_a_read_only_multi_step_plan_without_changing_draft(self):
        edit = self.cover_edit()
        plan = self.plan(edit)
        self.assertEqual(len(plan.preparation_steps), 1)
        self.assertEqual(plan.preparation_steps[0].command[-1], str(plan.auxiliary_paths[0]))
        self.assertIn(str(plan.auxiliary_paths[0]), plan.command)
        self.assertIsNone(edit.cover_action)
        self.assertIsNone(edit.cover_source)
        self.assertFalse(plan.output_path.parent.exists())
        self.assertEqual(edit.path.read_bytes(), b"original")
        result = self.run_file(plan)
        self.assertEqual(result.ok, 1)
        self.assertIsNone(edit.cover_source)
        self.assert_no_temporary()

    def test_cover_extraction_failure_warns_then_uses_fallback_without_cover(self):
        plan = self.plan()
        calls, logs = [], []
        def process(command, **kwargs):
            calls.append(command)
            if len(calls) == 1:
                Path(command[-1]).write_bytes(b"incomplete cover")
                return ProcessResult(1, "broken cover")
            self.assertNotIn(str(plan.auxiliary_paths[0]), command)
            return self.process(command)
        result = self.run_file(plan, process, logs)
        self.assertEqual((result.ok, result.failed), (1, 0))
        self.assertEqual(calls[-1], plan.preparation_steps[0].fallback_command)
        self.assertTrue(any("broken cover" in event.message for event in logs))
        self.assert_no_temporary()

    def test_missing_preparation_output_uses_fallback_even_with_zero_return_code(self):
        plan = self.plan()
        count = 0
        def process(command, **kwargs):
            nonlocal count
            count += 1
            return ProcessResult(0) if count == 1 else self.process(command)
        logs = []
        result = self.run_file(plan, process, logs)
        self.assertEqual(result.ok, 1)
        self.assertTrue(any("未生成准备步骤" in event.message for event in logs))
        self.assert_no_temporary()

    def test_cancel_between_cover_extraction_and_write_cleans_all_temporary_files(self):
        plan = self.plan()
        calls = []
        runner = TaskRunner()
        def process(command, **kwargs):
            calls.append(command)
            self.process(command)
            runner.request_cancel()
            return ProcessResult(0)
        runner._execute = process
        result = runner.run(TaskPlan((BatchPlan("test", (plan,)),)))
        self.assertTrue(result.cancelled)
        self.assertEqual(len(calls), 1)
        self.assertEqual((result.ok, result.failed), (0, 0))
        self.assertFalse(plan.output_path.exists())
        self.assertEqual(plan.source_path.read_bytes(), b"original")
        self.assert_no_temporary()

    def test_failed_required_step_prevents_final_command(self):
        plan = self.plan()
        plan = replace(plan, preparation_steps=(replace(plan.preparation_steps[0], failure_warning=None),))
        calls = []
        def process(command, **kwargs):
            calls.append(command)
            Path(command[-1]).write_bytes(b"partial")
            return ProcessResult(1, "required step failed")
        result = self.run_file(plan, process)
        self.assertEqual((result.ok, result.failed), (0, 1))
        self.assertEqual(len(calls), 1)
        self.assert_no_temporary()

    def test_existing_output_skips_cover_extraction_and_retains_existing_data(self):
        plan = replace(self.plan(), overwrite=False)
        plan.output_path.parent.mkdir()
        plan.output_path.write_bytes(b"existing")
        result = self.run_file(plan, lambda *_args, **_kwargs: self.fail("Skipped plan executed"))
        self.assertEqual(result.skipped, 1)
        self.assertEqual(plan.output_path.read_bytes(), b"existing")
        self.assert_no_temporary()

    def test_occupied_auxiliary_path_is_retained_and_prevents_execution(self):
        plan = self.plan()
        plan.output_path.parent.mkdir()
        plan.auxiliary_paths[0].write_bytes(b"another task")
        result = self.run_file(plan, lambda *_args, **_kwargs: self.fail("Occupied plan executed"))
        self.assertEqual(result.failed, 1)
        self.assertEqual(plan.auxiliary_paths[0].read_bytes(), b"another task")
        self.assertEqual(plan.source_path.read_bytes(), b"original")

    def test_auxiliary_paths_cannot_include_source_or_undeclared_output(self):
        plan = self.plan()
        for invalid in (
            replace(plan, auxiliary_paths=(plan.source_path,)),
            replace(plan, preparation_steps=(ProcessStep(("ffmpeg",), output_path=self.root / "other"),)),
        ):
            with self.assertRaises(ValueError), output_transaction(invalid):
                self.fail("Invalid plan ran")
        self.assertEqual(plan.source_path.read_bytes(), b"original")
        self.assertFalse(plan.output_path.parent.exists())

    def test_wav_cover_rule_updates_only_memory_and_respects_existing_format_choice(self):
        source = self.sample("song.wav")
        cover = self.sample("cover.jpg")
        original = TrackEdit(source, original_ext=".wav")
        edited = apply_cover_edit(original, "set", cover, auto_convert_wav=True, wav_target_key="mp3")
        self.assertEqual(edited.target_format_key, "mp3")
        self.assertEqual(edited.bitrate, "192k")
        self.assertIsNone(original.cover_action)
        self.assertIsNone(original.target_format_key)
        selected = replace(original, target_format_key="flac")
        self.assertEqual(apply_cover_edit(selected, "set", cover, auto_convert_wav=True, wav_target_key="mp3").target_format_key, "flac")
        self.assertIsNone(apply_cover_edit(original, "set", cover).target_format_key)
        with self.assertRaises(MetadataError):
            apply_cover_edit(original, "set", cover, auto_convert_wav=True, wav_target_key="ogg")
        self.assertEqual(source.read_bytes(), b"original")

    def test_invalid_metadata_plan_counts_failure_and_continues_other_files(self):
        source = self.sample("song.mp3")
        edits = [TrackEdit(self.root / "missing.mp3"), TrackEdit(source, target_format_key="invalid"), TrackEdit(source, edited_values={"title": "good"})]
        result = TaskRunner(execute=self.process).run(build_metadata_task("ffmpeg", edits, True, False, self.root / "export"))
        self.assertEqual((result.total, result.ok, result.failed), (3, 1, 2))
        self.assertEqual(source.read_bytes(), b"original")
        self.assert_no_temporary()

    def test_cover_export_reserves_same_batch_names_and_preserves_existing_covers(self):
        sources = [self.sample("A/song.mp3"), self.sample("B/song.mp3"), self.sample("C/none.mp3")]
        existing = self.sample("covers/song.jpg", b"keep")
        plan = build_cover_export_task("ffmpeg", [(sources[0], "mjpeg"), (sources[1], "mjpeg"), (sources[2], None)], existing.parent)
        self.assertEqual([file.output_path.name for file in plan.batches[0].files[:2]], ["song (1).jpg", "song (2).jpg"])
        result = TaskRunner(execute=self.process).run(plan)
        self.assertEqual((result.ok, result.skipped), (2, 1))
        self.assertEqual(existing.read_bytes(), b"keep")
        self.assertFalse((existing.parent / "none.jpg").exists())
        self.assert_no_temporary()

    def test_album_and_metadata_preserve_distinct_hidden_input_rules(self):
        hidden = self.sample("album/.hidden.mp3")
        partial = self.sample("album/song.part.mp3")
        regular = self.sample("album/song.mp3")
        self.assertTrue(is_album_audio_file(hidden))
        self.assertTrue(is_album_audio_file(partial))
        self.assertEqual(collect_audio_files(hidden.parent), [regular])

    def test_audio_reader_reports_timeout_cancel_and_malformed_json(self):
        source = self.sample("音乐🎵.mp3")
        for process, expected in (
            (ProcessResult(-1, "timeout", timed_out=True), "超时"),
            (ProcessResult(0, stdout=b"[1]"), "解析失败"),
            (ProcessResult(0, stdout=b"{"), "解析失败"),
            (ProcessResult(1, "bad input"), "bad input"),
        ):
            with patch("app.converter.execute", return_value=process):
                self.assertIn(expected, read_audio_info("ffprobe", source).error)
        with patch("app.converter.execute", return_value=ProcessResult(-1, cancelled=True)):
            info = read_audio_info("ffprobe", source)
            self.assertTrue(info.cancelled)
            self.assertIsNone(info.error)
        with patch("app.converter.execute", return_value=ProcessResult(0, stdout=json.dumps({"format": {"tags": {"title": None}}}).encode())):
            self.assertNotIn("title", read_audio_info("ffprobe", source).values)

    def test_cancelled_read_does_not_emit_a_row_or_progress(self):
        source = self.sample("song.mp3")
        rows, progress = [], []
        with patch("app.converter.execute", return_value=ProcessResult(-1, cancelled=True)):
            result = read_audio_edits("ffprobe", "ffmpeg", [source], on_row=rows.append, on_progress=progress.append)
        self.assertTrue(result.cancelled)
        self.assertEqual((result.ok, result.failed), (0, 0))
        self.assertFalse(rows)
        self.assertFalse(progress)

    def test_execute_keeps_windows_children_without_console(self):
        with patch("app.converter.subprocess.Popen") as popen:
            popen.return_value.poll.return_value = 0
            popen.return_value.returncode = 0
            result = execute(["ffmpeg"])
        self.assertTrue(result.succeeded)
        self.assertEqual(popen.call_args.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)


if __name__ == "__main__":
    unittest.main()
