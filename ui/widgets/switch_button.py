"""保持开关两种状态下的说明文本一致。"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget
from qfluentwidgets import IndicatorPosition, SwitchButton


def MakeSwitchButton(
    text: str,
    parent: QWidget = None,
    indicator_pos: IndicatorPosition = IndicatorPosition.LEFT,
) -> SwitchButton:
    switch = SwitchButton(text, parent, indicator_pos)
    switch.setOnText(text)
    switch.setOffText(text)
    return switch
