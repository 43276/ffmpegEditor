"""通用控件封装：对 qfluentwidgets 的少量收敛。

qfluentwidgets 的 SwitchButton 只把构造时传入的文本当作“关闭”状态文本，
勾选后会显示默认的 "On"，说明文字（如“覆盖已存在的输出文件”）会被替换掉。
这里统一成开/关都显示同一段指定文本。
"""
from __future__ import annotations

from PyQt6.QtWidgets import QWidget

from qfluentwidgets import IndicatorPosition, SwitchButton


def MakeSwitchButton(
    text: str,
    parent: QWidget = None,
    indicator_pos: IndicatorPosition = IndicatorPosition.LEFT,
) -> SwitchButton:
    """创建一个开/关都显示 ``text`` 的开关按钮。

    Parameters
    ----------
    text: str
        开关旁的说明文本，勾选前后都保持不变
    parent: QWidget
        父控件
    indicator_pos: IndicatorPosition
        指示器位置（左/右）
    """
    switch = SwitchButton(text, parent, indicator_pos)
    # SwitchButton 内部只在 _updateText() 时用 onText/offText 刷新标签，
    # 两者设为同一文本即可保证勾选状态变化时文本不被改成 "On"
    switch.setOnText(text)
    switch.setOffText(text)
    return switch
