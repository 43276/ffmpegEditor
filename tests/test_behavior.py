"""文件规则与编码行为基线；所有样本均位于独立临时目录。"""
from __future__ import annotations

import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import patch

from app.audio.album_planner import build_album_plan as BuildAlbumPlan, output_path_for as OutputPathFor
from app.audio.album_commands import build_album_command as BuildFfmpegCommand
from app.audio.models import TrackEdit, TrackMetadata
from app.audio.metadata_planner import build_output_plan as BuildOutputPlan
from app.audio.reader import collect_audio_files as CollectAudioFiles, read_audio_info as ReadAudioInfo
from app.errors import FfmpegError as ConverterError
from app.ffmpeg_environment import FfmpegCapabilities
from app.image.commands import build_image_command as BuildCommand
from app.image.models import Batch, ConvertOptions
from app.image.planner import (
    build_batches as BuildBatches, build_batches_for_inputs as BuildBatchesForInputs,
    make_task_output_name as MakeTaskOutputName, relocate_batch_outputs as RelocateBatchOutputs, InputError,
)
from app.video.commands import build_video_compress_command as BuildVideoCompressCommand
from app.video.models import VideoCompressOptions
from app.video.planner import build_video_batches_for_inputs as BuildVideoBatchesForInputs, VideoInputError
from app.task_models import ProcessResult


CAPABILITIES = FfmpegCapabilities(
    "test", {"libwebp", "libwebp_anim", "gif", "libaom-av1", "libx264", "libx265"},
    {"avif", "gif", "mp4"},
)


def values_for(command, flag):
    return [command[index + 1] for index, token in enumerate(command[:-1]) if token == flag]


class BehaviorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="ffmpeg_behavior_")
        self.root = Path(self.temp.name)
        self.addCleanup(self.temp.cleanup)

    def sample(self, relative, data=b"source"):
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(data)
        return path

    def test_single_image_entry_uses_documented_output_folder(self):
        source = self.sample("photo.png")
        expected = self.root / "photo_output"
        self.assertEqual(BuildBatches(source)[0].output_dir, expected)
        self.assertEqual(BuildBatchesForInputs([source])[0].output_dir, expected)
        relocated = BuildBatchesForInputs([source], output_root=self.root / "export")[0]
        self.assertEqual(relocated.output_dir, self.root / "export" / "photo_output")
        self.assertFalse(relocated.output_dir.exists())

    def test_multiple_images_share_explicit_time_folder_and_sort(self):
        first = self.sample("photos/Z.jpg")
        second = self.sample("photos/a.png")
        name = MakeTaskOutputName(datetime(2026, 10, 6, 12, 30, 45))
        batch = BuildBatchesForInputs([first, second], multi_file_output_name=name)[0]
        self.assertEqual(batch.output_dir, first.parent / "2026-10-06_12-30-45")
        self.assertEqual(batch.files, [second, first])
        preview = BuildBatchesForInputs([first, second])[0]
        self.assertEqual(preview.output_dir.name, "<任务开始时间>")
        self.assertFalse(batch.output_dir.exists())

    def test_leaf_directory_rule_excludes_intermediate_and_generated_files(self):
        intermediate = self.sample("photos/root.png")
        leaf = self.sample("photos/leaf/real.png")
        generated = self.sample("photos/leaf/leaf_output/previous.png")
        batches = BuildBatches(intermediate.parent)
        self.assertEqual([batch.files for batch in batches], [[leaf]])
        self.assertEqual(batches[0].output_dir, leaf.parent / "leaf_output")
        repeated = BuildBatches(intermediate.parent, include_output_dirs=True)
        self.assertEqual([batch.files for batch in repeated], [[generated]])

    def test_selection_validation_is_shared_by_image_and_video_rules(self):
        for builder, error, suffix in (
            (BuildBatchesForInputs, InputError, ".png"),
            (BuildVideoBatchesForInputs, VideoInputError, ".mp4"),
        ):
            with self.subTest(builder=builder.__name__):
                first = self.sample(f"a/first{suffix}")
                second = self.sample(f"b/second{suffix}")
                with self.assertRaises(error):
                    builder([first, second])
                with self.assertRaises(error):
                    builder([first, first.parent])
                with self.assertRaises(error):
                    builder([])
                with self.assertRaises(error):
                    builder([self.root / "missing"])

    def test_relocated_batches_disambiguate_equal_folder_names(self):
        first = self.sample("a/disc/one.png")
        second = self.sample("b/disc/two.png")
        batches = [Batch(path.parent, path.parent / "disc_output", [path], "disc")
                   for path in (first, second)]
        destination = self.root / "export"
        moved = RelocateBatchOutputs(batches, destination)
        self.assertEqual([batch.output_dir.name for batch in moved],
                         ["disc_output", "disc_output (1)"])
        self.assertFalse(destination.exists())

    def test_video_multiple_files_keep_time_suffix_rule(self):
        first = self.sample("a.mp4")
        second = self.sample("b.mkv")
        with patch("app.video.planner.make_task_output_name", return_value="2026-10-06_12-30-45"):
            batch = BuildVideoBatchesForInputs([first, second])[0]
        self.assertEqual(batch.output_dir, self.root / "2026-10-06_12-30-45_output")
        self.assertEqual(BuildVideoBatchesForInputs([first])[0].output_dir,
                         self.root / "a_output")

    def test_image_animation_and_static_frame_rules(self):
        source = self.root / "animated.gif"
        webp = BuildCommand(ConvertOptions("ffmpeg", ".webp"), CAPABILITIES, source, self.root / "out.webp")
        self.assertEqual(values_for(webp, "-c:v"), ["libwebp_anim"])
        self.assertNotIn("-frames:v", webp)
        self.assertIn("16383", values_for(webp, "-vf")[0])
        gif = BuildCommand(ConvertOptions("ffmpeg", ".gif"), CAPABILITIES, source, self.root / "out.gif")
        self.assertNotIn("-frames:v", gif)
        self.assertIn("palettegen", values_for(gif, "-vf")[0])
        self.assertIn("paletteuse", values_for(gif, "-vf")[0])
        self.assertEqual(values_for(gif, "-loop"), ["0"])
        static = BuildCommand(ConvertOptions("ffmpeg", ".jpg"), CAPABILITIES, source, self.root / "out.jpg")
        self.assertEqual(values_for(static, "-frames:v"), ["1"])

    def test_video_command_keeps_only_video_and_optional_audio(self):
        options = VideoCompressOptions("ffmpeg", encoder="libx265", overwrite=False)
        command = BuildVideoCompressCommand(options, CAPABILITIES, self.root / "video.mkv", self.root / "out.mp4")
        self.assertEqual(values_for(command, "-map"), ["0:v:0", "0:a?"])
        self.assertEqual(values_for(command, "-tag:v"), ["hvc1"])
        self.assertEqual(values_for(command, "-c:a"), ["aac"])
        self.assertEqual(values_for(command, "-movflags"), ["+faststart"])
        self.assertIn("-n", command)
        with self.assertRaises(ConverterError):
            BuildVideoCompressCommand(options, FfmpegCapabilities("test", set(), set()),
                                      self.root / "video.mkv", self.root / "out.mp4")

    def album(self, name, images=1, metadata=True):
        audio = self.sample(f"albums/{name}/disc/song.wav")
        for index in range(images):
            cover = self.sample(f"albums/{name}/images/cover{index}.jpg")
        if images and metadata:
            self.sample(f"albums/{name}/images/album.txt", "唱片集".encode("utf-8-sig"))
            self.sample(f"albums/{name}/images/artist.txt", b"Alice Bob")
        return audio

    def test_album_scanning_metadata_and_generated_directory_rules(self):
        audio = self.album("good")
        generated = self.sample("albums/good/disc/good/old.mp3")
        plan = BuildAlbumPlan(self.root / "albums")
        self.assertEqual(plan.file_count, 1)
        task = plan.tasks[0]
        self.assertEqual(task.metadata.album, "唱片集")
        self.assertEqual(task.metadata.artist, "Alice;Bob")
        self.assertEqual(task.groups[0].files, [audio])
        self.assertEqual(task.groups[0].output_dir, generated.parent)
        self.assertEqual(OutputPathFor(audio.parent, generated.parent, audio),
                         generated.parent / "song.mp3")

    def test_album_invalid_cover_or_metadata_skips_album(self):
        self.album("none", images=0)
        self.album("many", images=2)
        self.album("missing", metadata=False)
        plan = BuildAlbumPlan(self.root / "albums")
        self.assertFalse(plan.tasks)
        self.assertEqual(len(plan.skipped_dirs), 3)

    def test_album_mp3_cover_and_wav_conversion(self):
        for suffix, expected_codec in ((".mp3", "copy"), (".wav", "libmp3lame")):
            with self.subTest(suffix=suffix):
                command = BuildFfmpegCommand(
                    "ffmpeg", self.root / f"song{suffix}", self.root / "cover.png",
                    self.root / "out.mp3", True, TrackMetadata("album", "artist"),
                )
                self.assertEqual(values_for(command, "-c:a"), [expected_codec])
                self.assertEqual(values_for(command, "-disposition:v:0"), ["attached_pic"])
                self.assertEqual(values_for(command, "-id3v2_version"), ["3"])
                self.assertIn("title=", values_for(command, "-metadata"))
                self.assertIn("#=", values_for(command, "-metadata"))

    def test_metadata_planning_is_read_only_and_keeps_copy_mode(self):
        source = self.sample("song.mp3")
        edit = TrackEdit(source, edited_values={"title": "新标题", "genre": ""}, backup=True)
        plan = BuildOutputPlan("ffmpeg", edit, False, True, None)
        self.assertEqual(plan.output_path, source)
        self.assertTrue(plan.replaces_source)
        self.assertFalse(plan.converted)
        self.assertEqual(values_for(plan.command, "-c"), ["copy"])
        self.assertIn("genre=", values_for(plan.command, "-metadata"))
        self.assertEqual(source.read_bytes(), b"source")
        self.assertFalse(plan.temp_path.exists())
        self.assertFalse(source.with_name("song.mp3.bak").exists())

    def test_metadata_extension_change_protects_existing_files(self):
        source = self.sample("song.mp3")
        existing = self.sample("song.flac", b"keep")
        edit = TrackEdit(source, target_format_key="flac")
        plan = BuildOutputPlan("ffmpeg", edit, True, True, None)
        self.assertEqual(plan.output_path, self.root / "song (1).flac")
        self.assertFalse(plan.replaces_source)
        self.assertEqual(values_for(plan.command, "-c:a"), ["flac"])
        self.assertEqual(existing.read_bytes(), b"keep")

    def test_metadata_same_batch_outputs_are_disambiguated(self):
        sources = [self.sample("a/song.mp3"), self.sample("b/song.mp3")]
        taken = set()
        destination = self.root / "export"
        plans = [BuildOutputPlan("ffmpeg", TrackEdit(source), True, False, destination, taken)
                 for source in sources]
        self.assertEqual([plan.output_path.name for plan in plans], ["song.mp3", "song (1).mp3"])
        self.assertFalse(destination.exists())

    def test_metadata_cover_actions_and_unsupported_container_warnings(self):
        source = self.sample("song.mp3")
        cover = self.sample("cover.png")
        setting = TrackEdit(source, cover_action="set", cover_source=cover)
        plan = BuildOutputPlan("ffmpeg", setting, True, True, None)
        self.assertEqual(values_for(plan.command, "-map"), ["0:a?", "1:v:0"])
        self.assertEqual(values_for(plan.command, "-disposition:v:0"), ["attached_pic"])
        removing = BuildOutputPlan("ffmpeg", TrackEdit(source, cover_action="remove"), True, True, None)
        self.assertEqual(values_for(removing.command, "-map"), ["0:a?"])
        setting.target_format_key = "aac"
        setting.edited_values = {"title": "ignored"}
        unsupported = BuildOutputPlan("ffmpeg", setting, True, False, self.root / "export")
        self.assertEqual(len(unsupported.warnings), 2)
        self.assertEqual(values_for(unsupported.command, "-i"), [str(source)])
        self.assertNotIn("-metadata", unsupported.command)

    def test_audio_collection_excludes_hidden_and_partial_files(self):
        regular = self.sample("audio/song.mp3")
        self.sample("audio/.hidden.mp3")
        self.sample("audio/song.part.mp3")
        self.sample("audio/.song.id.part.mp3")
        self.assertEqual(CollectAudioFiles(regular.parent), [regular])

    def test_audio_info_reads_unicode_tags_and_cover(self):
        source = self.sample("音乐🎵.mp3")
        data = {"format": {"tags": {"title": "歌曲🎵", "artist": "作者"}},
                "streams": [{"codec_type": "video", "codec_name": "mjpeg",
                             "disposition": {"attached_pic": 1}}]}
        process = ProcessResult(0, stdout=json.dumps(data, ensure_ascii=False).encode("utf-8"))
        with patch("app.converter.execute", return_value=process) as run:
            info = ReadAudioInfo("ffprobe", source)
        self.assertEqual(info.values["title"], "歌曲🎵")
        self.assertTrue(info.has_cover)
        self.assertEqual(info.cover_codec, "mjpeg")
        self.assertTrue(run.call_args.kwargs["capture_stdout"])
        self.assertEqual(run.call_args.kwargs["timeout_seconds"], 60)


if __name__ == "__main__":
    unittest.main()
