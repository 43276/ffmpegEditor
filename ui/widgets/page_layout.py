"""共享滚动内容及卡片布局，业务页面负责自己的参数控件。"""
from PyQt6.QtWidgets import QVBoxLayout, QWidget
from qfluentwidgets import CaptionLabel, HeaderCardWidget, TitleLabel


def make_card(parent, title: str):
    card = HeaderCardWidget(parent)
    card.setTitle(title)
    body = QWidget(card)
    layout = QVBoxLayout(body)
    layout.setContentsMargins(0, 0, 0, 0)
    layout.setSpacing(10)
    card.viewLayout.addWidget(body)
    return card, layout


def build_page_content(scroll, title: str, caption: str, *,
                       object_name: str = "pageContent", margins=(30, 22, 30, 26)):
    scroll.setWidgetResizable(True)
    content = QWidget(scroll)
    content.setObjectName(object_name)
    scroll.setWidget(content)
    layout = QVBoxLayout(content)
    layout.setContentsMargins(*margins)
    layout.setSpacing(14)
    layout.addWidget(TitleLabel(title, content))
    if caption:
        layout.addWidget(CaptionLabel(caption, content))
    return content, layout
