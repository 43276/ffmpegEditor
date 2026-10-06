"""可选真实 FFmpeg 集成验证：python -m tests.verify_ffmpeg -v。

样本、输出和备份均放在临时目录；不修改用户配置或媒体。
"""
from __future__ import annotations

import hashlib
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from PyQt6.QtGui import QImageReader
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication

from app.audio.models import TrackEdit
from app.audio.reader import read_audio_info, extract_cover_thumbnail
from app.converter import execute
from app.ffmpeg_environment import locate_ffmpeg, locate_ffprobe, probe_ffmpeg
from app.image.models import ConvertOptions
from app.video.models import VideoCompressOptions
from ui.tasks.controller import TaskController
from ui.tasks.jobs import (album_inputs_job, cover_export_job, image_inputs_job,
                           metadata_read_job, metadata_write_job, video_inputs_job)

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def digest(path):
    return hashlib.sha256(path.read_bytes()).hexdigest()


class RealFfmpegTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.ffmpeg = locate_ffmpeg()
        cls.ffprobe = locate_ffprobe(cls.ffmpeg)
        cls.caps = probe_ffmpeg(cls.ffmpeg)

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_integration_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def tearDown(self):
        self.assertFalse(list(self.root.rglob("*.part.*")), "临时输出未清理")

    def generate(self, relative, arguments):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        result = execute([self.ffmpeg, "-hide_banner", "-loglevel", "error", "-y",
                          *arguments, str(path)], timeout_seconds=30)
        self.assertTrue(result.succeeded, result.stderr)
        return path

    def image(self, name="cover.png"):
        return self.generate(name, ["-f", "lavfi", "-i", "testsrc=size=48x48:rate=3",
                                    "-frames:v", "1"])

    def audio(self, name="song.mp3"):
        return self.generate(name, ["-f", "lavfi", "-i", "sine=frequency=440:duration=0.4"])

    def probe(self, path):
        result = execute([self.ffprobe, "-v", "error", "-show_streams", "-show_format",
                          "-of", "json", str(path)], capture_stdout=True, timeout_seconds=30)
        self.assertTrue(result.succeeded, result.stderr)
        return json.loads(result.stdout)

    def run_job(self, operation, *, cancel_after_progress=False):
        controller = TaskController()
        results = []
        controller.completed.connect(lambda _kind, result: results.append(result))
        if cancel_after_progress:
            controller.progressChanged.connect(lambda progress: controller.request_cancel())
        controller.start(operation, supports_finish=True)
        deadline = time.monotonic() + 60
        while controller.active and time.monotonic() < deadline:
            QTest.qWait(5)
        if controller.active:
            controller.shutdown()
            while controller.active:
                QTest.qWait(5)
            self.fail("真实 FFmpeg 任务超时")
        self.assertEqual(len(results), 1)
        result = results[0]
        self.assertEqual(result.failed, 0, result.error_logs)
        return result

    def test_images_animation_and_existing_output_protection(self):
        source = self.image("images/source.png")
        original = digest(source)
        for extension in (".jpg", ".tif", ".avif"):
            with self.subTest(extension=extension):
                result = self.run_job(image_inputs_job(
                    [source], ConvertOptions(self.ffmpeg, extension), self.caps))
                self.assertEqual(result.ok, 1)
                self.assertTrue((source.parent / "source_output" / f"source{extension}").is_file())
        output = source.parent / "source_output/source.jpg"
        output.write_bytes(b"existing output")
        result = self.run_job(image_inputs_job(
            [source], ConvertOptions(self.ffmpeg, ".jpg", overwrite=False), self.caps))
        self.assertEqual(result.skipped, 1)
        self.assertEqual(output.read_bytes(), b"existing output")
        animated = self.generate("images/moving.gif", ["-f", "lavfi", "-i",
            "testsrc=size=32x32:rate=3", "-frames:v", "3", "-loop", "0"])
        animated_hash = digest(animated)
        result = self.run_job(image_inputs_job(
            [animated], ConvertOptions(self.ffmpeg, ".webp"), self.caps))
        self.assertEqual(result.ok, 1)
        webp = animated.parent / "moving_output/moving.webp"
        self.assertEqual(QImageReader(str(webp)).imageCount(), 3)
        result = self.run_job(image_inputs_job(
            [webp], ConvertOptions(self.ffmpeg, ".gif"), self.caps))
        self.assertEqual(result.ok, 1)
        self.assertEqual(QImageReader(str(webp.parent / "moving_output/moving.gif")).imageCount(), 3)
        self.run_job(image_inputs_job([animated], ConvertOptions(self.ffmpeg, ".png"), self.caps))
        self.assertEqual(QImageReader(str(animated.parent / "moving_output/moving.png")).imageCount(), 1)
        self.assertEqual(digest(source), original)
        self.assertEqual(digest(animated), animated_hash)

    def test_video_h264_h265_and_optional_audio(self):
        source = self.generate("videos/movie.mkv", ["-f", "lavfi", "-i",
            "testsrc=size=64x64:rate=8:duration=0.5", "-f", "lavfi", "-i",
            "sine=frequency=440:duration=0.5", "-c:v", "libx264", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-shortest"])
        original = digest(source)
        for encoder, expected in (("libx264", "h264"), ("libx265", "hevc")):
            with self.subTest(encoder=encoder):
                result = self.run_job(video_inputs_job([source],
                    VideoCompressOptions(self.ffmpeg, encoder=encoder, preset="ultrafast"), self.caps))
                self.assertEqual(result.ok, 1)
                streams = self.probe(source.parent / "movie_output/movie.mp4")["streams"]
                self.assertEqual([stream["codec_name"] for stream in streams], [expected, "aac"])
        silent = self.generate("videos/silent.mkv", ["-f", "lavfi", "-i",
            "testsrc=size=64x64:rate=8:duration=0.25", "-c:v", "libx264", "-pix_fmt", "yuv420p"])
        result = self.run_job(video_inputs_job([silent], VideoCompressOptions(self.ffmpeg), self.caps))
        self.assertEqual(result.ok, 1)
        self.assertEqual(digest(source), original)

    def test_album_unicode_tags_and_generated_directory_filter(self):
        cover = self.image("albums/专辑/images/cover.png")
        (cover.parent / "album.txt").write_text("唱片集🎵", encoding="utf-8-sig")
        (cover.parent / "artist.txt").write_text("Alice Bob", encoding="utf-8")
        source = self.audio("albums/专辑/disc/歌曲.wav")
        original = digest(source)
        result = self.run_job(album_inputs_job(self.root / "albums", True, self.ffmpeg))
        self.assertEqual(result.ok, 1)
        output = source.parent / "专辑/歌曲.mp3"
        info = read_audio_info(self.ffprobe, output)
        self.assertEqual(info.values["album"], "唱片集🎵")
        self.assertEqual(info.values["artist"], "Alice;Bob")
        self.assertTrue(info.has_cover)
        self.assertEqual(digest(source), original)
        self.assertEqual(self.run_job(album_inputs_job(self.root / "albums", True, self.ffmpeg)).total, 1)

    def test_metadata_backup_cover_preservation_thumbnail_and_exports(self):
        source, cover = self.audio("音乐🎵.mp3"), self.image()
        original = digest(source)
        result = self.run_job(metadata_write_job(self.ffmpeg, [TrackEdit(
            source, edited_values={"title": "新标题😀", "artist": "作者;作曲者"},
            cover_action="set", cover_source=cover, backup=True)], True, True, None))
        self.assertEqual(result.ok, 1)
        self.assertEqual(digest(source.with_name(source.name + ".bak")), original)
        info = read_audio_info(self.ffprobe, source)
        self.assertEqual(info.values["title"], "新标题😀")
        self.assertTrue(info.has_cover)
        self.assertTrue(extract_cover_thumbnail(self.ffmpeg, source))
        edited_hash = digest(source)
        converted = TrackEdit.from_audio_info(info)
        converted.target_format_key = "flac"
        converted.edited_values["title"] = "转换😀"
        self.assertEqual(self.run_job(metadata_write_job(
            self.ffmpeg, [converted], True, False, str(self.root / "converted"))).ok, 1)
        self.assertTrue(read_audio_info(self.ffprobe, self.root / "converted/音乐🎵.flac").has_cover)
        result = self.run_job(cover_export_job(self.ffmpeg,
            [(source, info.cover_codec), (source, info.cover_codec)], str(self.root / "exports")))
        self.assertEqual(result.ok, 2)
        self.assertEqual(len(list((self.root / "exports").iterdir())), 2)
        self.assertEqual(digest(source), edited_hash)
        self.assertEqual(self.run_job(metadata_read_job(self.ffprobe, self.ffmpeg, [self.root])).failed, 0)

    def test_real_export_cancel_and_restart(self):
        source, cover = self.audio(), self.image()
        self.run_job(metadata_write_job(self.ffmpeg, [TrackEdit(source, cover_action="set",
                                                               cover_source=cover)], True, True, None))
        original = digest(source)
        output = self.root / "cancelled_exports"
        result = self.run_job(cover_export_job(self.ffmpeg, [(source, "mjpeg")] * 60,
                                               str(output)), cancel_after_progress=True)
        self.assertTrue(result.cancelled)
        self.assertLess(result.ok, 60)
        self.assertEqual(self.run_job(cover_export_job(self.ffmpeg, [(source, "mjpeg")], str(output))).ok, 1)
        self.assertEqual(digest(source), original)


if __name__ == "__main__":
    unittest.main()
