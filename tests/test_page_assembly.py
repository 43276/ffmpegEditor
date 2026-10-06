"""主窗口服务注入、旧配置恢复与共享页面交互检查。"""
from __future__ import annotations

import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

from PyQt6.QtCore import QPoint, QPointF, QSettings, Qt
from PyQt6.QtGui import QColor, QImage, QPalette, QWheelEvent
from PyQt6.QtTest import QTest
from PyQt6.QtWidgets import QApplication
from qfluentwidgets import Theme, setTheme

from app.audio.models import TrackEdit
from app.ffmpeg_environment import FfmpegCapabilities
from app.task_models import LogLevel
from ui.main_window import FallbackMainWindow, MainWindow
from ui.services.ffmpeg_service import EnvironmentSnapshot
from ui.widgets.log_panel import LogPanel


APP = QApplication.instance() or QApplication([])
CAPS = FfmpegCapabilities("test-environment", {"libx264", "libx265", "libwebp"}, {"mp4", "webp"})


def wait_until(predicate, timeout=4):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        APP.processEvents()
        if predicate():
            return
        QTest.qWait(5)
    raise AssertionError("页面交互或后台退出未完成")


def wheel(viewport):
    position = viewport.rect().center()
    event = QWheelEvent(QPointF(position), QPointF(viewport.mapToGlobal(position)), QPoint(), QPoint(0, -120),
                        Qt.MouseButton.NoButton, Qt.KeyboardModifier.NoModifier,
                        Qt.ScrollPhase.NoScrollPhase, False)
    APP.sendEvent(viewport, event)


class PageAssemblyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.settings = QSettings(str(self.root / "legacy.ini"), QSettings.Format.IniFormat)
        self.addCleanup(self.temp.cleanup)
        self.addCleanup(lambda: setTheme(Theme.LIGHT))
        shutdown = patch("ui.services.desktop_actions.DesktopActions.schedule_shutdown", return_value=False)
        shutdown.start()
        self.addCleanup(shutdown.stop)

    def make_window(self, window_type=FallbackMainWindow):
        with patch("ui.main_window.QSettings", return_value=self.settings), \
                patch("ui.services.ffmpeg_service.FfmpegService.Start"):
            window = window_type()
            APP.processEvents()
        window._SyncEnvironment(EnvironmentSnapshot("ffmpeg", "ffprobe", CAPS, "ready", message="✓ test"))
        self.addCleanup(window.deleteLater)
        self.addCleanup(window.close)
        return window

    def test_both_windows_restore_legacy_settings_and_inject_one_environment(self):
        values = {"convert/quality": 67, "convert/max_dimension": 1280,
                  "output/overwrite": False, "task/auto_shutdown": True,
                  "video/crf": 29, "video/preset": "slow", "video/audio_bitrate": "128k",
                  "video/encoder": "libx265", "background/transparency": 37, "background/blur": 0}
        for key, value in values.items():
            self.settings.setValue(key, value)
        background = self.root / "背景.png"
        image = QImage(240, 160, QImage.Format.Format_RGB32)
        image.fill(QColor("#6b91bf"))
        self.assertTrue(image.save(str(background)))
        self.settings.setValue("background/path", str(background))
        self.settings.sync()
        for window_type in (FallbackMainWindow, MainWindow):
            with self.subTest(window=window_type.__name__):
                window = self.make_window(window_type)
                self.assertEqual(window.image_page.quality_slider.value(), 67)
                self.assertEqual(window.image_page.dimension_spin.value(), 1280)
                self.assertFalse(window.image_page.overwrite_switch.isChecked())
                self.assertTrue(window.image_page.auto_shutdown_switch.isChecked())
                self.assertEqual(window.video_page.crf_slider.value(), 29)
                self.assertEqual(window.video_page.preset_combo.currentData(), "slow")
                self.assertEqual(window.video_page.audio_combo.currentData(), "128k")
                self.assertEqual(window.video_page.encoder_combo.currentData(), "libx265")
                self.assertIs(window.image_page._settings, window.video_page._settings)
                self.assertIs(window.audio_page.album_page._desktop_actions, window.image_page._desktop_actions)
                self.assertEqual(window.audio_page.metadata_page._ffprobe_path, "ffprobe")
                self.assertEqual(window.settings_page.ffmpeg_status.text(), "✓ test")
                self.assertEqual(window.settings_page.transparency_slider.value(), 37)
                self.assertFalse(window._background_layer._source.isNull())
                self.assertEqual(window.settings_page.background_status.text(), "")
                window.close()

    def test_audio_bottom_navigation_and_independent_table_and_page_scrolling(self):
        window = self.make_window()
        window.show()
        window.switchTo(window.audio_page)
        page = window.audio_page.metadata_page
        page._rows = [TrackEdit(self.root / f"歌曲 {index:02d}.mp3", original_values={"title": f"标题 {index}"})
                      for index in range(30)]
        QTest.qWait(250)
        page.scroll_area.ensureWidgetVisible(page.table)
        QTest.qWait(50)
        self.assertGreater(page.table.verticalScrollBar().maximum(), 0)
        self.assertGreater(page.scroll_area.verticalScrollBar().maximum(), 0)
        outer_value = page.scroll_area.verticalScrollBar().value()
        wheel(page.table.viewport())
        wait_until(lambda: page.table.verticalScrollBar().value() > 0)
        QTest.qWait(250)
        self.assertEqual(page.scroll_area.verticalScrollBar().value(), outer_value)
        page.scroll_area.verticalScrollBar().setValue(0)
        inner_value = page.table.verticalScrollBar().value()
        wheel(page.scroll_area.viewport())
        wait_until(lambda: page.scroll_area.verticalScrollBar().value() > 0)
        QTest.qWait(250)
        self.assertEqual(page.table.verticalScrollBar().value(), inner_value)
        entry = page.content_layout.itemAt(page.content_layout.count() - 2).widget()
        page.scroll_area.ensureWidgetVisible(entry)
        QTest.qWait(50)
        QTest.mouseClick(entry, Qt.MouseButton.LeftButton)
        self.assertIs(window.audio_page._stack.currentWidget(), window.audio_page.album_page)
        QTest.mouseClick(window.audio_page.album_page.back_button, Qt.MouseButton.LeftButton)
        self.assertIs(window.audio_page._stack.currentWidget(), window.audio_page._home_page)

    def test_theme_log_colors_escape_and_utf8_export(self):
        panel = LogPanel()
        self.addCleanup(panel.deleteLater)
        panel.append(LogLevel.ERROR, "错误 <script>alert('中文😀')</script> & 标签")
        self.assertIn("<script>alert('中文😀')</script> & 标签", panel.browser.toPlainText())
        for theme, color in ((Theme.LIGHT, "#c42b1c"), (Theme.DARK, "#ff9aa2")):
            setTheme(theme)
            APP.processEvents()
            self.assertIn(color, panel.browser.toHtml())
            self.assertIn("&lt;script&gt;", panel.browser.toHtml())
        path = self.root / "错误日志.txt"
        notices = []
        with patch("ui.widgets.log_panel.QFileDialog.getSaveFileName", return_value=(str(path), "")):
            LogPanel.export_entries(panel, "导出", "errors.txt", ["中文😀 <错误>"],
                                    lambda *args, **kwargs: notices.append((args, kwargs)))
        self.assertEqual(path.read_text(encoding="utf-8"), "中文😀 <错误>\n")
        self.assertEqual(notices[-1][0][0], "导出成功")

    def test_main_window_waits_for_preview_cancellation_without_blocking_gui(self):
        window = self.make_window()
        window.show()
        controller = window.image_page._preview_controller
        entered, cancelled, release = threading.Event(), threading.Event(), threading.Event()

        def scan(cancel_check):
            entered.set()
            while not cancel_check():
                time.sleep(0.005)
            cancelled.set()
            release.wait(3)
            return []

        try:
            controller.request(scan)
            wait_until(entered.is_set)
            started = time.monotonic()
            window.close()
            self.assertLess(time.monotonic() - started, 1)
            wait_until(cancelled.is_set)
            self.assertTrue(window.isVisible())
            self.assertTrue(controller.active)
            release.set()
            wait_until(lambda: not controller.active and not window.isVisible())
        finally:
            release.set()
            wait_until(lambda: not controller.active)

    def test_metadata_existing_rows_follow_theme_without_losing_edits(self):
        window = self.make_window()
        page = window.audio_page.metadata_page
        row = TrackEdit(self.root / "中文.mp3", original_ext=".mp3", edited_values={"title": "保留草稿"})
        page._rows = [row]
        for theme, expected in ((Theme.DARK, "#ffffff"), (Theme.LIGHT, "#000000")):
            setTheme(theme)
            APP.processEvents()
            for column in (0, 1, 2):
                label = page.table.cellWidget(0, column)
                label.ensurePolished()
                self.assertEqual(label.palette().color(QPalette.ColorRole.WindowText).name(), expected)
            self.assertEqual(page._rows[0].edited_values["title"], "保留草稿")


if __name__ == "__main__":
    unittest.main()
