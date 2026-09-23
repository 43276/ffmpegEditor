"""音频处理模块主页：与图片处理主页保持一致的卡片式布局。

当前主页暂无一般操作内容，仅提供已集成的“专辑批处理”功能入口；
后续新增音频工具时，在“功能入口”卡片中继续添加入口即可。
"""
from __future__ import annotations

from PyQt6.QtWidgets import QHBoxLayout, QStackedWidget, QVBoxLayout, QWidget

from qfluentwidgets import (
    CaptionLabel,
    CardWidget,
    FluentIcon,
    IconWidget,
    ScrollArea,
    StrongBodyLabel,
    TitleLabel,
)

from ui.AlbumPage import AlbumPage


class AudioPage(QWidget):
    """音频处理模块主页。

    内部使用 QStackedWidget 在“主页”和“专辑批处理”之间切换；
    专辑批处理页面的返回按钮会回到本主页。
    """

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

        self.album_page.backRequested.connect(self._ShowHomePage)
        self._stack.setCurrentWidget(self._home_page)

    def _BuildHomePage(self) -> ScrollArea:
        page = ScrollArea(self)
        page.setWidgetResizable(True)
        content = QWidget(page)
        content.setObjectName("audioHomeContent")
        page.setWidget(content)

        content_layout = QVBoxLayout(content)
        content_layout.setContentsMargins(30, 22, 30, 26)
        content_layout.setSpacing(14)

        # 与图片处理主页一致的页头
        content_layout.addWidget(TitleLabel("音频处理", content))
        content_layout.addWidget(
            CaptionLabel("音频类批量处理工具（基于 FFmpeg）· 更多功能后续补充", content)
        )

        # 功能入口：只保留“专辑批处理”入口卡片
        entry_card = CardWidget(content)
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

        content_layout.addWidget(entry_card)
        content_layout.addStretch(1)

        entry_card.clicked.connect(lambda: self._stack.setCurrentWidget(self.album_page))
        return page

    def _ShowHomePage(self) -> None:
        self._stack.setCurrentWidget(self._home_page)

    def SetFfmpegPath(self, path: str | None) -> None:
        self.album_page.SetFfmpegPath(path)

    def Shutdown(self) -> None:
        self.album_page.Shutdown()
