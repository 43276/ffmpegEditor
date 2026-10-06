"""验证各业务入口确实使用公共结果与输出保护。"""
from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtWidgets import QApplication

from app.AddCover import BuildAlbumPlan
from app.audio.models import AudioInfo, TrackEdit
from app.ffmpeg_environment import FfmpegCapabilities
from app.image.models import Batch, ConvertOptions
from app.task_models import ProcessResult, TaskResult
from app.video.models import VideoBatch, VideoCompressOptions
from ui.AlbumWorker import AlbumWorker
from ui.MetadataWorker import CoverExportWorker, MetadataReadWorker, MetadataWriteWorker
from ui.VideoWorker import VideoCompressWorker
from ui.Worker import ConvertWorker


APP = QApplication.instance() or QApplication([])
CAPS = FfmpegCapabilities("test", {"libx264", "libwebp"}, {"mp4"})


class WorkerOutputTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_worker_outputs_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def sample(self, name, data=b"original"):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    @staticmethod
    def convert(command, **kwargs):
        source = Path(command[command.index("-i") + 1])
        Path(command[-1]).write_bytes(source.read_bytes() + b" converted")
        return ProcessResult(0)

    def run_worker(self, worker, target, process=None):
        summaries = []
        worker.taskFinished.connect(lambda *args: summaries.append(args[-1]))
        with patch(target, side_effect=process or self.convert):
            worker.run()
        self.assertEqual(len(summaries), 1)
        self.assertIsInstance(summaries[0], TaskResult)
        self.assertFalse(list(self.root.rglob("*.part.*")))
        return summaries[0]

    def image_worker(self):
        sources = [self.sample("images/song.jpg", b"jpg"), self.sample("images/song.png", b"png")]
        output = self.root / "images_output"
        return ConvertWorker(1, [Batch(sources[0].parent, output, sources)],
                             ConvertOptions("ffmpeg", target_extension=".webp"), CAPS), output

    def video_worker(self, overwrite=True):
        sources = [self.sample("videos/song.mkv", b"mkv"), self.sample("videos/song.mov", b"mov")]
        output = self.root / "videos_output"
        return VideoCompressWorker([VideoBatch(sources[0].parent, output, sources)],
                                   VideoCompressOptions("ffmpeg", overwrite=overwrite), CAPS), output

    def album_worker(self):
        root = self.root / "albums"
        self.sample("albums/album/cover.jpg")
        self.sample("albums/album/album.txt", b"Album")
        self.sample("albums/album/artist.txt", b"Alice Bob")
        self.sample("albums/album/song.mp3", b"mp3")
        self.sample("albums/album/song.wav", b"wav")
        return AlbumWorker(1, BuildAlbumPlan(root), True, "ffmpeg"), root / "album" / "album"

    def metadata_worker(self):
        sources = [self.sample("audio/A/song.mp3", b"A"), self.sample("audio/B/song.mp3", b"B")]
        edits = [TrackEdit(source, edited_values={"title": "new"}) for source in sources]
        output = self.root / "metadata_output"
        return MetadataWriteWorker(1, "ffmpeg", edits, True, False, str(output)), output

    def test_same_batch_image_video_album_and_metadata_outputs_do_not_overwrite_each_other(self):
        cases = [(self.image_worker, "ui.Worker.RunFileProcess", ".webp", [b"jpg", b"png"]),
                 (self.video_worker, "ui.VideoWorker.RunFileProcess", ".mp4", [b"mkv", b"mov"]),
                 (self.album_worker, "ui.AlbumWorker.RunFileProcess", ".mp3", [b"mp3", b"wav"]),
                 (self.metadata_worker, "ui.MetadataWorker.RunFileProcess", ".mp3", [b"A", b"B"])]
        for factory, target, suffix, originals in cases:
            with self.subTest(factory=factory.__name__):
                worker, output = factory()
                originals_on_disk = {path: path.read_bytes() for path in self.root.rglob("*") if path.is_file()}
                result = self.run_worker(worker, target)
                self.assertEqual((result.ok, result.failed, result.skipped), (2, 0, 0))
                files = sorted(output.glob(f"*{suffix}"))
                self.assertEqual(len(files), 2)
                self.assertEqual({path.name for path in files}, {f"song{suffix}", f"song (1){suffix}"})
                self.assertEqual({path.read_bytes() for path in files}, {original + b" converted" for original in originals})
                self.assertTrue(all(path.read_bytes() == data for path, data in originals_on_disk.items()))

    def test_unexpected_execution_failure_cleans_each_worker_and_reports_failure(self):
        cases = [(self.image_worker, "ui.Worker.RunFileProcess"),
                 (self.video_worker, "ui.VideoWorker.RunFileProcess"),
                 (self.album_worker, "ui.AlbumWorker.RunFileProcess"),
                 (self.metadata_worker, "ui.MetadataWorker.RunFileProcess")]
        for factory, target in cases:
            with self.subTest(factory=factory.__name__):
                worker, output = factory()

                def fail(command, **kwargs):
                    Path(command[-1]).write_bytes(b"partial")
                    raise RuntimeError("execution failed")

                result = self.run_worker(worker, target, fail)
                self.assertEqual(result.ok, 0)
                self.assertGreater(result.failed, 0)
                self.assertFalse(list(output.glob("*")))

    def test_cancelling_after_process_returns_cleans_each_worker_without_saving(self):
        cases = [(self.image_worker, "ui.Worker.RunFileProcess"),
                 (self.video_worker, "ui.VideoWorker.RunFileProcess"),
                 (self.album_worker, "ui.AlbumWorker.RunFileProcess"),
                 (self.metadata_worker, "ui.MetadataWorker.RunFileProcess")]
        for factory, target in cases:
            with self.subTest(factory=factory.__name__):
                worker, output = factory()

                def cancel(command, **kwargs):
                    Path(command[-1]).write_bytes(b"partial")
                    worker.RequestCancel()
                    return ProcessResult(0)

                result = self.run_worker(worker, target, cancel)
                self.assertTrue(result.cancelled)
                self.assertEqual((result.ok, result.failed), (0, 0))
                self.assertFalse(list(output.glob("*")))

    def test_video_does_not_overwrite_output_created_while_processing(self):
        worker, output = self.video_worker(overwrite=False)

        def conflict(command, **kwargs):
            self.convert(command)
            # 最后一段临时文件名由公共工具生成；业务正式路径来自已知计划。
            destination = output / ("song (1).mp4" if (output / "song.mp4").exists() else "song.mp4")
            destination.write_bytes(b"created by another task")
            return ProcessResult(0)

        result = self.run_worker(worker, "ui.VideoWorker.RunFileProcess", conflict)
        self.assertEqual((result.ok, result.skipped, result.failed), (0, 2, 0))
        self.assertTrue(all(path.read_bytes() == b"created by another task" for path in output.iterdir()))

    def test_metadata_backup_failure_reports_failure_and_preserves_source(self):
        source = self.sample("track.mp3")
        worker = MetadataWriteWorker(1, "ffmpeg", [TrackEdit(source, edited_values={"title": "new"}, backup=True)], True, True, None)
        with patch("app.output_files.shutil.copy2", side_effect=OSError("backup failed")):
            result = self.run_worker(worker, "ui.MetadataWorker.RunFileProcess")
        self.assertEqual((result.ok, result.failed), (0, 1))
        self.assertTrue(any("backup failed" in message for message in result.error_logs))
        self.assertEqual(source.read_bytes(), b"original")

    def test_audio_read_and_cover_export_use_typed_task_results(self):
        source = self.sample("track.mp3")
        rows, read_results = [], []
        worker = MetadataReadWorker(1, "ffprobe", "ffmpeg", [source])
        worker.rowLoaded.connect(lambda _id, edit: rows.append(edit))
        worker.taskFinished.connect(lambda _id, result: read_results.append(result))
        with patch("ui.MetadataWorker.ReadAudioInfo", return_value=AudioInfo(source, {"title": "曲目😀"}, False, None)):
            worker.run()
        self.assertIsInstance(read_results[0], TaskResult)
        self.assertEqual(rows[0].original_values["title"], "曲目😀")
        export = CoverExportWorker(1, "ffmpeg", [(source, "mjpeg"), (source, None)], str(self.root / "covers"))
        result = self.run_worker(export, "app.MetadataEdit.RunFileProcess")
        self.assertEqual((result.ok, result.skipped), (1, 1))
        self.assertEqual(result.output_dir, str(self.root / "covers"))


if __name__ == "__main__":
    unittest.main()
