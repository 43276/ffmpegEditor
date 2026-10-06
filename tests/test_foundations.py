"""公共环境、执行结果与输出生命周期的失败路径验证。"""
from __future__ import annotations

import os
import subprocess
import sys
import tempfile
import time
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch

from app.converter import execute as RunFileProcess
from app.errors import FfmpegError
from app.ffmpeg_environment import locate_ffmpeg, locate_ffprobe, probe_ffmpeg
from app.audio.metadata_planner import build_output_plan as BuildOutputPlan
from app.audio.cover_planner import build_cover_export_plan
from app.task_runner import TaskRunner
from app.audio.models import TrackEdit
from app.output_files import (
    commit_output, output_transaction, path_key, reserve_output_path, temporary_path_for,
)
from app.task_models import BatchPlan, FilePlan, ProcessResult, TaskPlan


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_foundations_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)
        self.source = self.root / "source.mp3"
        self.source.write_bytes(b"original")

    def plan(self, *, overwrite=False, in_place=False, backup=False):
        output = self.source if in_place else self.root / "export" / "song.mp3"
        return FilePlan(self.source, output, temporary_path_for(output), ("ffmpeg",),
                        overwrite=overwrite, replaces_source=in_place, backup=backup)

    def test_explicit_invalid_tools_never_fall_back_to_path(self):
        with patch("app.ffmpeg_environment.shutil.which", return_value="other.exe") as which:
            for locate in (locate_ffmpeg, lambda path: locate_ffprobe(explicit=path), locate_ffprobe):
                with self.subTest(locate=locate), self.assertRaises(FfmpegError):
                    locate(str(self.root / "missing.exe"))
            which.assert_not_called()

    def test_ffprobe_prefers_sibling_and_allows_path_when_sibling_is_missing(self):
        ffmpeg = self.root / "ffmpeg.exe"
        ffmpeg.touch()
        ffprobe = self.root / ("ffprobe.exe" if os.name == "nt" else "ffprobe")
        ffprobe.touch()
        with patch("app.ffmpeg_environment.shutil.which", return_value="path-probe") as which:
            self.assertEqual(locate_ffprobe(str(ffmpeg)), str(ffprobe.resolve()))
            which.assert_not_called()
            ffprobe.unlink()
            self.assertEqual(locate_ffprobe(str(ffmpeg)), "path-probe")

    def test_probe_parses_only_table_rows_and_preserves_subprocess_settings(self):
        results = [
            subprocess.CompletedProcess([], 0, "ffmpeg version test\n", ""),
            subprocess.CompletedProcess([], 0, "Encoders:\n V..... = Video\n V....D libx264 H.264\n A....D aac AAC\n", ""),
            subprocess.CompletedProcess([], 0, "File formats:\n .E = Muxing\n  E mp4 MP4\n  E matroska,webm Matroska / WebM\n", ""),
        ]
        with patch("app.ffmpeg_environment.subprocess.run", side_effect=results) as run:
            caps = probe_ffmpeg("ffmpeg")
        self.assertEqual(caps.version, "ffmpeg version test")
        self.assertEqual(caps.encoders, {"libx264", "aac"})
        self.assertEqual(caps.muxers, {"mp4", "matroska", "webm"})
        self.assertFalse(hasattr(caps, "VideoEncoderName"))
        for call in run.call_args_list:
            self.assertEqual(call.kwargs["encoding"], "utf-8")
            self.assertEqual(call.kwargs["errors"], "replace")
            self.assertEqual(call.kwargs["timeout"], 30)
            self.assertEqual(call.kwargs["creationflags"], subprocess.CREATE_NO_WINDOW if os.name == "nt" else 0)

    def test_probe_reports_timeout_and_failed_capability_command(self):
        with patch("app.ffmpeg_environment.subprocess.run", side_effect=subprocess.TimeoutExpired("ffmpeg", 30)):
            with self.assertRaisesRegex(FfmpegError, "检测超时"):
                probe_ffmpeg("ffmpeg")
        results = [subprocess.CompletedProcess([], 0, "version", ""),
                   subprocess.CompletedProcess([], 1, "", "broken encoders")]
        with patch("app.ffmpeg_environment.subprocess.run", side_effect=results):
            with self.assertRaisesRegex(FfmpegError, "broken encoders"):
                probe_ffmpeg("ffmpeg")

    def test_process_returns_binary_stdout_without_pipe_deadlock(self):
        command = [sys.executable, "-c", "import sys; sys.stdout.buffer.write(b'\\x00\\xff' * 100000); sys.stderr.buffer.write(b'error' * 100000 + b'\\xff')"]
        result = RunFileProcess(command, timeout_seconds=5, capture_stdout=True)
        self.assertTrue(result.succeeded, result.stderr)
        self.assertEqual(result.stdout, b"\x00\xff" * 100000)
        self.assertLessEqual(len(result.stderr), 1500)
        self.assertTrue(result.stderr.endswith("�"))

    def test_process_start_failure_cancel_and_timeout_are_distinct(self):
        failed = RunFileProcess([str(self.root / "missing.exe")])
        self.assertFalse(failed.succeeded)
        self.assertIn("无法启动", failed.stderr)
        with patch("app.converter.subprocess.Popen") as popen:
            cancelled = RunFileProcess(["unused"], cancel_check=lambda: True)
            popen.assert_not_called()
        self.assertTrue(cancelled.cancelled)
        self.assertFalse(cancelled.timed_out)
        timed_out = RunFileProcess([sys.executable, "-c", "import time; time.sleep(20)"], timeout_seconds=0.1)
        self.assertTrue(timed_out.timed_out)
        self.assertFalse(timed_out.cancelled)
        self.assertFalse(timed_out.succeeded)

    def test_process_running_cancel_stops_child(self):
        marker = self.root / "started"
        command = [sys.executable, "-c", "from pathlib import Path; import time; Path(__import__('sys').argv[1]).touch(); time.sleep(20)", str(marker)]
        started_at = time.monotonic()
        result = RunFileProcess(command, cancel_check=lambda: marker.exists(), timeout_seconds=5)
        self.assertTrue(result.cancelled)
        self.assertLess(time.monotonic() - started_at, 5)
        self.assertFalse(result.succeeded)

    def test_planning_reserves_distinct_names_without_creating_files(self):
        desired = self.root / "new" / "song.mp3"
        taken = {path_key(self.source)}
        first = reserve_output_path(desired, taken)
        second = reserve_output_path(desired, taken)
        self.assertEqual(first, desired)
        self.assertEqual(second.name, "song (1).mp3")
        self.assertFalse(desired.parent.exists())
        first_temp, second_temp = temporary_path_for(first), temporary_path_for(first)
        self.assertNotEqual(first_temp, second_temp)
        self.assertEqual(first_temp.suffix, ".mp3")

    def test_no_overwrite_rechecks_destination_created_during_conversion(self):
        plan = self.plan()
        with output_transaction(plan):
            plan.temp_path.write_bytes(b"converted")
            plan.output_path.write_bytes(b"appeared during conversion")
            self.assertFalse(commit_output(plan))
        self.assertEqual(plan.output_path.read_bytes(), b"appeared during conversion")
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertFalse(plan.temp_path.exists())

    def test_overwrite_replaces_only_output_and_cleanup_removes_only_own_temp(self):
        plan = self.plan(overwrite=True)
        other = replace(plan, temp_path=temporary_path_for(plan.output_path))
        with output_transaction(plan):
            other.temp_path.write_bytes(b"another task")
            plan.output_path.write_bytes(b"old output")
            plan.temp_path.write_bytes(b"converted")
            self.assertTrue(commit_output(plan))
        self.assertEqual(plan.output_path.read_bytes(), b"converted")
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertEqual(other.temp_path.read_bytes(), b"another task")

    def test_failure_cleans_partial_and_does_not_remove_occupied_temp(self):
        plan = self.plan()
        with self.assertRaisesRegex(RuntimeError, "failure"):
            with output_transaction(plan):
                plan.temp_path.write_bytes(b"partial")
                raise RuntimeError("failure")
        self.assertFalse(plan.temp_path.exists())
        plan.temp_path.write_bytes(b"occupied")
        with self.assertRaises(FileExistsError), output_transaction(plan):
            self.fail("occupied temporary must not be executed")
        self.assertEqual(plan.temp_path.read_bytes(), b"occupied")
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_in_place_backup_succeeds_before_source_replacement(self):
        plan = self.plan(in_place=True, backup=True)
        with output_transaction(plan):
            plan.temp_path.write_bytes(b"modified")
            self.assertTrue(commit_output(plan))
        self.assertEqual(self.source.read_bytes(), b"modified")
        self.assertEqual(self.source.with_name("source.mp3.bak").read_bytes(), b"original")

    def test_backup_failure_preserves_source_existing_backup_and_cleans_partial(self):
        plan = self.plan(in_place=True, backup=True)
        backup = self.source.with_name("source.mp3.bak")
        backup.write_bytes(b"older backup")

        def failing_copy(source, dest):
            Path(dest).write_bytes(b"incomplete backup")
            raise OSError("backup failed")

        with patch("app.output_files.shutil.copy2", side_effect=failing_copy):
            with self.assertRaisesRegex(OSError, "backup failed"):
                with output_transaction(plan):
                    plan.temp_path.write_bytes(b"modified")
                    commit_output(plan)
        self.assertEqual(self.source.read_bytes(), b"original")
        self.assertEqual(backup.read_bytes(), b"older backup")
        self.assertFalse(list(self.root.rglob("*.part.*")))

    def test_invalid_plan_cannot_clean_or_replace_source(self):
        plan = replace(self.plan(), temp_path=self.source)
        with self.assertRaises(ValueError), output_transaction(plan):
            self.fail("invalid plan must not run")
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_metadata_export_to_source_folder_keeps_source_and_extension_change_policy(self):
        edit = TrackEdit(self.source, edited_values={"title": "new"})
        plan = BuildOutputPlan("ffmpeg", edit, True, False, self.root)
        self.assertEqual(plan.output_path.name, "source (1).mp3")
        self.assertFalse(plan.replaces_source)
        converted = BuildOutputPlan("ffmpeg", replace(edit, target_format_key="flac"), True, True, None)
        self.assertFalse(converted.overwrite)
        with output_transaction(converted):
            converted.temp_path.write_bytes(b"converted")
            converted.output_path.write_bytes(b"newly occupied")
            self.assertFalse(commit_output(converted))
        self.assertEqual(converted.output_path.read_bytes(), b"newly occupied")
        self.assertEqual(self.source.read_bytes(), b"original")

    def test_cover_failure_does_not_leave_partial_or_overwrite_existing_cover(self):
        cover = self.root / "source.jpg"
        cover.write_bytes(b"existing cover")

        def fail(command, **kwargs):
            Path(command[-1]).write_bytes(b"partial cover")
            return ProcessResult(1, "bad cover")

        plan = build_cover_export_plan("ffmpeg", self.source, self.root, "mjpeg")
        result = TaskRunner(execute=fail).run(TaskPlan((BatchPlan("cover", (plan,)),)))
        self.assertEqual(result.failed, 1)
        self.assertIn("bad cover", result.error_logs[0])
        self.assertEqual(cover.read_bytes(), b"existing cover")
        self.assertFalse((self.root / "source (1).jpg").exists())
        self.assertFalse(list(self.root.rglob("*.part.*")))


if __name__ == "__main__":
    unittest.main()
