"""音频处理模块主页。
"""
from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon,
    IconWidget,
    StrongBodyLabel,
)

from ui.AlbumPage import AlbumPage
from ui.MetadataPage import MetadataPage


class AudioPage(QWidget):
    """音频处理模块主页：元数据编辑 + 专辑批处理入口。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("audioPage")

        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        self._stack = QStackedWidget(self)
        layout.addWidget(self._stack)

        self._home_page = self._BuildHomePage()
        self.album_page = AlbumPage(self)
        self._stack.addWidget(self._home_page)
        self._stack.addWidget(self.album_page)

        self.album_page.backRequested.connect(
            lambda: self._stack.setCurrentWidget(self._home_page)
        )
        self._stack.setCurrentWidget(self._home_page)

    def _BuildHomePage(self) -> QWidget:
        page = QWidget(self)
        page.setObjectName("audioHomeContent")

        layout = QVBoxLayout(page)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(0)

        # 功能入口卡片：只保留“专辑批处理”
        entry_wrap = QWidget(page)
        entry_wrap_layout = QVBoxLayout(entry_wrap)
        entry_wrap_layout.setContentsMargins(30, 22, 30, 0)
        entry_wrap_layout.setSpacing(0)
        entry_wrap_layout.addWidget(self._BuildAlbumEntryCard(entry_wrap))
        layout.addWidget(entry_wrap)

        # 元数据编辑直接作为主页内容显示
        self.metadata_page = MetadataPage(page)
        layout.addWidget(self.metadata_page, 1)
        return page

    def _BuildAlbumEntryCard(self, parent: QWidget) -> CardWidget:
        entry_card = CardWidget(parent)
        entry_card.setClickEnabled(True)
        entry_card.setMinimumHeight(72)

        entry_layout = QHBoxLayout(entry_card)
        entry_layout.setContentsMargins(16, 12, 16, 12)
        entry_layout.setSpacing(12)

        icon_widget = IconWidget(entry_card)
        icon_widget.setIcon(FluentIcon.ALBUM)
        icon_widget.setFixedSize(40, 40)
        entry_layout.addWidget(icon_widget)

        text_layout = QVBoxLayout()
        text_layout.setSpacing(2)
        title_label = StrongBodyLabel("专辑批处理", entry_card)
        desc_label = CaptionLabel(
            "给每个专辑文件夹里的音频批量写入封面、唱片集与艺术家元数据",
            entry_card,
        )
        text_layout.addWidget(title_label)
        text_layout.addWidget(desc_label)
        entry_layout.addLayout(text_layout, 1)

        arrow_label = CaptionLabel("进入 >", entry_card)
        entry_layout.addWidget(arrow_label)

        entry_card.clicked.connect(lambda: self._stack.setCurrentWidget(self.album_page))
        return entry_card

    def SetFfmpegPath(self, path: str | None) -> None:
        self.album_page.SetFfmpegPath(path)
        self.metadata_page.SetFfmpegPath(path)

    def Shutdown(self) -> None:
        self.album_page.Shutdown()
        self.metadata_page.Shutdown()
