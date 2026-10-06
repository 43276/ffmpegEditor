"""第四阶段：稳定行标识、编辑范围、草稿保留及后台导入。"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6 import sip
from PyQt6.QtCore import QCoreApplication, QEvent, QTimer
from PyQt6.QtGui import QImage, QPalette
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import PushButton, Theme, qconfig, setTheme

from app.audio.models import AudioInfo, TrackEdit
from app.input_paths import ScanCancelled
from app.task_models import TaskResult
from ui.audio.metadata.controller import CoverResources, MetadataController
from ui.audio.metadata.dialogs import CoverDialog, FormatDialog
from ui.audio.metadata.page import MetadataPage
from ui.audio.metadata.table import MetadataTable
from ui.services.ffmpeg_service import EnvironmentSnapshot

APP = QApplication.instance() or QApplication([])
APP.setQuitOnLastWindowClosed(False)


def wait_until(predicate, seconds=4):
    deadline = time.monotonic() + seconds
    while not predicate():
        if time.monotonic() >= deadline:
            raise AssertionError("等待 Qt 事件超时")
        QTest.qWait(5)
    APP.processEvents()


class MetadataComponentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="metadata_components_")
        self.root = Path(self.temp.name)
        self.controller = MetadataController()
        self.controller.set_environment(EnvironmentSnapshot("ffmpeg", "ffprobe", status="ready"))

    def tearDown(self):
        self.controller.shutdown()
        wait_until(lambda: not self.controller.tasks.active)
        APP.processEvents()
        self.controller.deleteLater()
        self.temp.cleanup()

    def sample(self, name):
        path = self.root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"source")
        return path

    def rows(self):
        rows = [TrackEdit(self.sample("one.mp3"), original_values={"title": "original"}),
                TrackEdit(self.sample("two.wav"), original_ext=".wav"),
                TrackEdit(self.sample("bad.mp3"), error="unreadable")]
        self.controller.replace_rows(rows)
        return rows

    def test_row_ids_survive_refresh_and_edits_follow_reordered_rows(self):
        one, two, _bad = self.rows()
        original_ids = [row_id for row_id, _row in self.controller.entries]
        fresh = [TrackEdit(row.path, original_ext=row.original_ext, error=row.error) for row in self.controller.rows]
        self.controller.replace_rows(fresh)
        self.assertEqual([row_id for row_id, _row in self.controller.entries], original_ids)
        table = MetadataTable()
        table.set_entries(list(reversed(self.controller.entries)))
        table.textEdited.connect(self.controller.edit_text)
        try:
            index = table.row_index(original_ids[1])
            field = table._ColumnIndexOfField("title")
            editor = table.cellWidget(index, field).line_edit
            editor.setText("second only")
            editor.textEdited.emit("second only")
            self.assertEqual(fresh[1].edited_values, {"title": "second only"})
            self.assertFalse(fresh[0].edited_values)
            self.assertFalse(one.edited_values or two.edited_values)
        finally:
            table.deleteLater()

    def test_batch_text_format_and_cover_operations_keep_single_row_scope(self):
        one, two, bad = self.rows()
        first_id = self.controller.row_id(one)
        self.controller.apply_text("title", "all")
        self.controller.apply_text("title", "single", first_id)
        self.assertEqual([one.edited_values["title"], two.edited_values["title"]], ["single", "all"])
        self.assertFalse(bad.edited_values)
        self.controller.apply_format("flac", None, first_id)
        self.assertEqual(one.target_format_key, "flac")
        self.assertIsNone(two.target_format_key)
        self.controller.apply_cover("remove", None, first_id)
        self.assertEqual(one.cover_action, "remove")
        self.assertIsNone(two.cover_action)
        self.controller.apply_format("mp3", "256k")
        self.assertEqual([one.bitrate, two.bitrate], ["256k", "256k"])
        self.assertIsNone(bad.target_format_key)

    def test_hiding_fields_preserves_drafts_and_masks_only_the_write_snapshot(self):
        one, _two, _bad = self.rows()
        row_id = self.controller.row_id(one)
        self.controller.apply_text("title", "中文 😀", row_id)
        self.controller.apply_cover("remove", None, row_id)
        self.controller.set_field_visible("title", False)
        self.controller.set_field_visible("cover", False)
        self.assertEqual(self.controller.write_snapshot(), [])
        self.assertEqual(one.edited_values["title"], "中文 😀")
        self.assertEqual(one.cover_action, "remove")
        self.controller.set_field_visible("title", True)
        self.controller.set_field_visible("cover", True)
        snapshot = self.controller.write_snapshot()[0]
        self.assertEqual(snapshot.edited_values["title"], "中文 😀")
        self.assertEqual(snapshot.cover_action, "remove")
        snapshot.edited_values["title"] = "isolated"
        self.assertEqual(one.edited_values["title"], "中文 😀")

    def test_zoom_field_visibility_and_reset_preserve_expected_table_values(self):
        page = MetadataPage()
        page.SetEnvironment(EnvironmentSnapshot("ffmpeg", "ffprobe", status="ready"))
        one = TrackEdit(self.sample("track.wav"), original_ext=".wav", original_values={"title": "source"})
        page._rows = [one]
        row_id = page._controller.row_id(one)
        try:
            page._controller.apply_text("title", "draft", row_id)
            page._controller.apply_cover("remove", None, row_id)
            page._controller.apply_format("mp3", "256k", row_id)
            page._OnFieldToggled("title", False)
            page._OnCoverZoomChanged("large")
            page._OnFieldToggled("title", True)
            editor = page.table.cellWidget(0, page.table._ColumnIndexOfField("title")).line_edit
            self.assertEqual(editor.text(), "draft")
            self.assertEqual(page.table.rowHeight(0), 106)
            self.assertIn("256k", page.table.cellWidget(0, 0).text())
            self.assertEqual(page.table.cellWidget(0, page.table._CoverColumn()).text(), "将移除")
            page._OnResetClicked()
            self.assertFalse(one.modified)
            editor = page.table.cellWidget(0, page.table._ColumnIndexOfField("title")).line_edit
            self.assertEqual(editor.text(), "")
            self.assertEqual(editor.placeholderText(), "source")
        finally:
            page.Shutdown()
            page.deleteLater()

    def test_existing_format_filename_and_cover_cells_follow_theme_changes(self):
        table = MetadataTable()
        table.add_row("1", TrackEdit(self.sample("track.mp3"), original_ext=".mp3"))
        previous = qconfig.theme
        table.show()
        try:
            setTheme(Theme.DARK)
            QTest.qWait(20)
            for index in range(3):
                color = table.cellWidget(0, index).palette().color(QPalette.ColorRole.WindowText)
                self.assertGreater(color.lightness(), 200)
            setTheme(Theme.LIGHT)
            QTest.qWait(20)
            for index in range(3):
                color = table.cellWidget(0, index).palette().color(QPalette.ColorRole.WindowText)
                self.assertLess(color.lightness(), 60)
        finally:
            setTheme(previous)
            table.close()
            table.deleteLater()

    def test_format_dialog_keeps_existing_bitrate_and_lossless_choices(self):
        dialog = FormatDialog(None, "single", "mp3", "256k")
        try:
            self.assertEqual(dialog.bitrate_combo.itemData(dialog.bitrate_combo.currentIndex()), "256k")
            dialog.format_combo.setCurrentIndex(dialog.format_combo.findData("flac"))
            self.assertFalse(dialog.bitrate_combo.isEnabled())
            dialog._OnOk()
            self.assertEqual(dialog.format_key, "flac")
            self.assertIsNone(dialog.bitrate)
        finally:
            dialog.deleteLater()

    def test_single_cover_dialog_has_no_batch_export_and_pngs_have_unique_owned_paths(self):
        resources = CoverResources()
        dialog = CoverDialog(None, "single", "ffmpeg", None, resources)
        image = QImage(12, 8, QImage.Format.Format_RGB32)
        image.fill(0xFF0066AA)
        try:
            self.assertFalse(any("提取全部" in button.text() for button in dialog.findChildren(PushButton)))
            dialog._SetCoverImage(image)
            first = dialog.cover_source
            dialog._SetCoverImage(image)
            second = dialog.cover_source
            self.assertNotEqual(first, second)
            self.assertEqual(first.parent, resources.path)
            self.assertFalse(QImage(str(first)).isNull())
            self.assertFalse(QImage(str(second)).isNull())
            resources.request_cleanup()
            self.assertTrue(resources.path.exists())
            dialog.reject()
            self.assertFalse(resources.path.exists())
            dialog._SetCoverImage(image)
            self.assertFalse(resources.path.exists())
            self.assertIn("正在关闭", dialog.status_label.text())
        finally:
            dialog.reject()
            dialog.deleteLater()
            resources.request_cleanup()

    def test_invalid_cover_selection_preserves_the_previous_valid_preview_and_result(self):
        resources = CoverResources()
        image = QImage(12, 8, QImage.Format.Format_RGB32)
        image.fill(0xFF0066AA)
        valid = self.root / "valid.png"
        image.save(str(valid), "PNG")
        invalid = self.sample("broken.jpg")
        dialog = CoverDialog(None, "single", "ffmpeg", None, resources)
        try:
            dialog._SetCoverSource(valid)
            dialog._SetCoverSource(invalid)
            self.assertEqual(dialog.cover_source, valid)
            self.assertIn("无法读取", dialog.status_label.text())
            dialog._OnOk()
            self.assertEqual(dialog.result_action, "set")
            self.assertEqual(dialog.cover_source, valid)
        finally:
            dialog.reject()
            dialog.deleteLater()
            resources.request_cleanup()

    def test_large_import_emits_rows_without_recounting_edits_for_every_row(self):
        loaded = [TrackEdit(self.root / f"batch/{index}.mp3") for index in range(3000)]
        counts = []
        self.controller.modifiedChanged.connect(counts.append)

        def read_job(_ffprobe, _ffmpeg, _paths):
            def operation(context):
                for row in loaded:
                    context.on_row(row)
                return TaskResult(total=len(loaded), ok=len(loaded))
            return operation

        with patch("ui.audio.metadata.controller.metadata_read_job", side_effect=read_job):
            self.assertTrue(self.controller.start_import([self.root]))
            wait_until(lambda: not self.controller.tasks.active)
        self.assertEqual(len(self.controller.rows), 3000)
        self.assertLessEqual(len(counts), 3)
        self.controller.apply_text("title", "batch")
        self.assertTrue(all(row.edited_values["title"] == "batch" for row in loaded))
        self.assertEqual(self.controller.modified_count, 3000)

    def test_confirm_write_captures_drafts_and_environment_before_later_changes(self):
        one, _two, _bad = self.rows()
        self.controller.apply_text("title", "at start", self.controller.row_id(one))
        entered, release = threading.Event(), threading.Event()
        captured = []

        def write_job(ffmpeg, edits, overwrite, in_place, output_root):
            captured.append((ffmpeg, edits, overwrite, in_place, output_root))
            def operation(context):
                entered.set()
                release.wait(4)
                return TaskResult(total=1, ok=1)
            return operation

        try:
            with patch("ui.audio.metadata.controller.metadata_write_job", side_effect=write_job):
                self.assertTrue(self.controller.confirm_write(overwrite=False, in_place=True, output_root=None, backup=True))
            wait_until(entered.is_set)
            one.edited_values["title"] = "after start"
            self.controller.set_environment(EnvironmentSnapshot(status="checking"))
            self.assertEqual(captured[0][0], "ffmpeg")
            self.assertEqual(captured[0][1][0].edited_values["title"], "at start")
            self.assertTrue(captured[0][1][0].backup)
            self.assertFalse(one.backup)
            self.controller.shutdown()
            self.assertTrue(self.controller.resources.path.exists())
            release.set()
            wait_until(lambda: not self.controller.tasks.active)
            self.assertFalse(self.controller.resources.path.exists())
        finally:
            release.set()

    def test_folder_collection_runs_off_gui_thread_and_can_cancel_before_any_read(self):
        source = self.sample("folder/track.mp3")
        main_thread = threading.get_ident()
        entered, release = threading.Event(), threading.Event()
        worker_threads, completed, ticks = [], [], []
        self.controller.completed.connect(lambda kind, result: completed.append(result))

        def scan(paths, *, cancel_check):
            worker_threads.append(threading.get_ident())
            entered.set()
            while not release.wait(0.005):
                if cancel_check():
                    raise ScanCancelled()
            return [source]

        try:
            with patch("ui.tasks.jobs.collect_audio_inputs", side_effect=scan), \
                    patch("app.audio.reader.read_audio_info") as read:
                self.assertTrue(self.controller.start_import([source.parent]))
                wait_until(entered.is_set)
                QTimer.singleShot(0, lambda: ticks.append(True))
                wait_until(lambda: bool(ticks))
                self.assertNotEqual(worker_threads, [main_thread])
                read.assert_not_called()
                self.assertTrue(self.controller.tasks.request_cancel())
                wait_until(lambda: not self.controller.tasks.active)
                self.assertTrue(completed[-1].cancelled)
                self.assertFalse(self.controller.rows)
            with patch("app.audio.reader.read_audio_info", return_value=AudioInfo(source, {"title": "read"}, False, None)):
                self.assertTrue(self.controller.start_import([source.parent, source]))
                wait_until(lambda: not self.controller.tasks.active)
            self.assertEqual(len(self.controller.rows), 1)
            self.assertEqual(self.controller.rows[0].original_values["title"], "read")
        finally:
            release.set()

    def test_environment_checking_disables_confirmation_without_discarding_draft(self):
        page = MetadataPage()
        row = TrackEdit(self.sample("track.mp3"), edited_values={"title": "draft"})
        page._rows = [row]
        try:
            page.SetEnvironment(EnvironmentSnapshot("ffmpeg", "ffprobe", status="ready"))
            self.assertTrue(page.confirm_button.isEnabled())
            page.SetEnvironment(EnvironmentSnapshot(status="checking"))
            self.assertFalse(page.confirm_button.isEnabled())
            self.assertEqual(row.edited_values["title"], "draft")
        finally:
            page.Shutdown()
            page.deleteLater()

    def test_page_and_dialog_deletion_release_resources_without_deleted_qobject_signals(self):
        page = MetadataPage()
        owner = page._controller.resources
        dialog = CoverDialog(page, "single", "ffmpeg", None, owner)
        page.Shutdown()
        self.assertFalse(owner.path.exists())
        page.deleteLater()
        QCoreApplication.sendPostedEvents(None, QEvent.Type.DeferredDelete)
        APP.processEvents()
        self.assertTrue(sip.isdeleted(page))
        self.assertTrue(sip.isdeleted(dialog))
        self.assertFalse(owner.path.exists())


if __name__ == "__main__":
    unittest.main()
